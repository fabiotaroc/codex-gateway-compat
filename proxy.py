#!/usr/bin/env python3
"""Local Codex → Vercel proxy that makes non-OpenAI models work in Codex Desktop.

Request side (rewrite.py): strict-schema fixes for Muse Spark, namespace
flattening for every non-OpenAI model. Response side (stream.py): restores
namespaced tool calls and coerces integral floats in tool arguments.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import ssl
import sys
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rewrite import RequestContext, rewrite_request_bytes, summarize_models, summarize_tools
from stream import SSERewriter, rewrite_json_response

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18787
DEFAULT_UPSTREAM = "https://ai-gateway.vercel.sh"
HERE = os.path.dirname(os.path.abspath(__file__))
LAST_TOOLS_PATH = os.path.join(HERE, "last-muse-tools.json")
LAST_TOOLS_FULL_PATH = os.path.join(HERE, "last-muse-tools-full.json")
LAST_ERROR_PATH = os.path.join(HERE, "last-upstream-error.json")
LAST_REQUEST_PATH = os.path.join(HERE, "last-muse-request.json")
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
}
OVERRIDDEN_HEADERS = {"content-length", "accept-encoding"}

log = logging.getLogger("muse-schema-proxy")


def split_upstream(url: str) -> Tuple[str, str, int, bool]:
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("upstream must be an http(s) URL with a host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname, port, parsed.scheme == "https"


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MuseSchemaProxy/1.0"

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()

    def do_OPTIONS(self) -> None:
        self._handle()

    def _read_body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            size = int(length)
        except ValueError:
            return b""
        if size <= 0:
            return b""
        return self.rfile.read(size)

    def _health(self) -> None:
        payload = json.dumps(
            {
                "ok": True,
                "service": "muse-schema-proxy",
                "upstream": self.server.upstream_url,
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _handle(self) -> None:
        if self.path.rstrip("/") in {"/health", "/codex/v1/health"}:
            self._health()
            return

        body = self._read_body()
        patched = 0
        ctx: Optional[RequestContext] = None
        path = self.path.split("?", 1)[0]
        if self.command == "POST" and path.rstrip("/").endswith("responses") and body:
            try:
                parsed = json.loads(body)
                model = summarize_models(parsed)
            except ValueError:
                model = ""
            body, patched, ctx = rewrite_request_bytes(body)
            if patched:
                log.info(
                    "rewrote %s object(s) for model %s (flatten=%s, %s namespaced tools)",
                    patched,
                    model or "?",
                    ctx.flatten,
                    len(ctx.mapping),
                )
            if patched and self.server.capture_debug:
                try:
                    parsed_after = json.loads(body)
                    summary = summarize_tools(parsed_after)
                    with open(LAST_TOOLS_PATH, "w", encoding="utf-8") as handle:
                        json.dump(
                            {
                                "model": model,
                                "patched": patched,
                                "bytes": len(body),
                                "tools": summary,
                            },
                            handle,
                            indent=2,
                        )
                    with open(LAST_TOOLS_FULL_PATH, "w", encoding="utf-8") as handle:
                        json.dump(parsed_after.get("tools"), handle)
                    with open(LAST_REQUEST_PATH, "w", encoding="utf-8") as handle:
                        json.dump(
                            {
                                "path": self.path,
                                "headers": {
                                    k: ("<redacted>" if k.lower() == "authorization" else v)
                                    for k, v in self.headers.items()
                                },
                                "body": parsed_after,
                            },
                            handle,
                        )
                except Exception:
                    log.exception("failed to write %s", LAST_TOOLS_PATH)

        self._forward(body, ctx)

    def _forward(self, body: bytes, ctx: Optional[RequestContext] = None) -> None:
        scheme, host, port, tls = split_upstream(self.server.upstream_url)
        if tls:
            conn = HTTPSConnection(host, port, context=ssl.create_default_context(), timeout=600)
        else:
            conn = HTTPConnection(host, port, timeout=600)

        # Header names from Codex arrive lowercase; drop every header we
        # re-emit ourselves so the upstream never sees conflicting duplicates.
        had_content_length = False
        headers = {}
        for key, value in self.headers.items():
            lowered = key.lower()
            if lowered in HOP_BY_HOP or lowered in OVERRIDDEN_HEADERS:
                if lowered == "content-length":
                    had_content_length = True
                continue
            headers[key] = value
        headers["Host"] = host if port in (80, 443) else "%s:%s" % (host, port)
        headers["Accept-Encoding"] = "identity"
        if body:
            headers["Content-Length"] = str(len(body))
        elif had_content_length:
            headers["Content-Length"] = "0"

        try:
            conn.request(self.command, self.path, body=body or None, headers=headers)
            upstream = conn.getresponse()
            self._write_upstream(upstream, ctx)
        except BrokenPipeError:
            log.info("client closed the connection")
        except Exception as exc:
            log.exception("upstream request failed: %s", exc)
            if self.wfile.closed:
                return
            try:
                message = json.dumps({"error": "proxy_upstream_error", "message": str(exc)}).encode("utf-8")
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(message)))
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(message)
            except BrokenPipeError:
                log.info("client closed the connection")
        finally:
            conn.close()

    def _log_upstream_error(self, status: int, payload: bytes, headers) -> None:
        preview = payload[:2000].decode("utf-8", errors="replace")
        log.warning("upstream %s: %s", status, preview.replace("\n", " "))
        try:
            parsed = None
            try:
                parsed = json.loads(payload)
            except ValueError:
                parsed = preview
            with open(LAST_ERROR_PATH, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "status": status,
                        "headers": dict(headers),
                        "body": parsed,
                    },
                    handle,
                    indent=2,
                )
        except Exception:
            log.exception("failed to write %s", LAST_ERROR_PATH)

    def _write_upstream(self, upstream, ctx: Optional[RequestContext] = None) -> None:
        rewrite = ctx is not None and ctx.rewrites_responses
        if upstream.status >= 400:
            payload = upstream.read()
            self._log_upstream_error(upstream.status, payload, upstream.getheaders())
            self.send_response(upstream.status, upstream.reason)
            for key, value in upstream.getheaders():
                if key.lower() in HOP_BY_HOP or key.lower() == "content-length":
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            self.wfile.flush()
            return

        length_header = upstream.getheader("Content-Length")
        content_type = (upstream.getheader("Content-Type") or "").lower()
        is_sse = "text/event-stream" in content_type
        is_json = "application/json" in content_type
        self.send_response(upstream.status, upstream.reason)
        for key, value in upstream.getheaders():
            if key.lower() in HOP_BY_HOP or key.lower() == "content-length":
                continue
            self.send_header(key, value)

        if rewrite and is_json and not is_sse and self.command != "HEAD":
            # Non-streaming JSON: buffer, rewrite, re-measure.
            payload = rewrite_json_response(upstream.read(), ctx)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            return

        if length_header and self.command != "HEAD" and not (rewrite and is_sse):
            self.send_header("Content-Length", length_header)
            self.send_header("Connection", "close")
            self.end_headers()
            remaining = int(length_header)
            while remaining > 0:
                chunk = upstream.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                self.wfile.write(chunk)
            self.wfile.flush()
            return

        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command == "HEAD":
            return

        rewriter = SSERewriter(ctx) if (rewrite and is_sse) else None
        # read1() returns as soon as any bytes are available; read() would
        # block until 64 KB accumulate and destroy SSE streaming.
        while True:
            chunk = upstream.read1(65536)
            if not chunk:
                break
            if rewriter is not None:
                chunk = rewriter.feed(chunk)
                if not chunk:
                    continue
            self._write_chunk(chunk)
        if rewriter is not None:
            tail = rewriter.flush()
            if tail:
                self._write_chunk(tail)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _write_chunk(self, chunk: bytes) -> None:
        self.wfile.write(("%X\r\n" % len(chunk)).encode("ascii") + chunk + b"\r\n")
        self.wfile.flush()


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host: str, port: int, upstream_url: str, capture_debug: bool = True):
        self.upstream_url = upstream_url.rstrip("/")
        # Writes last-muse-*.json next to this file for troubleshooting.
        self.capture_debug = capture_debug
        super().__init__((host, port), ProxyHandler)


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("MUSE_SCHEMA_PROXY_HOST", DEFAULT_HOST))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MUSE_SCHEMA_PROXY_PORT", DEFAULT_PORT)),
    )
    parser.add_argument(
        "--upstream",
        default=os.environ.get("MUSE_SCHEMA_PROXY_UPSTREAM", DEFAULT_UPSTREAM),
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    split_upstream(args.upstream)
    server = ProxyServer(args.host, args.port, args.upstream)
    log.info("listening on http://%s:%s → %s", args.host, args.port, server.upstream_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
