#!/usr/bin/env python3
"""Local Codex → Vercel proxy that repairs strict tool schemas for Muse Spark."""

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
from rewrite import rewrite_request_bytes, summarize_models, summarize_tools

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18787
DEFAULT_UPSTREAM = "https://ai-gateway.vercel.sh"
HERE = os.path.dirname(os.path.abspath(__file__))
LAST_TOOLS_PATH = os.path.join(HERE, "last-muse-tools.json")
LAST_TOOLS_FULL_PATH = os.path.join(HERE, "last-muse-tools-full.json")
LAST_ERROR_PATH = os.path.join(HERE, "last-upstream-error.json")
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
        path = self.path.split("?", 1)[0]
        if self.command == "POST" and path.rstrip("/").endswith("responses") and body:
            try:
                parsed = json.loads(body)
                model = summarize_models(parsed)
            except ValueError:
                model = ""
            body, patched = rewrite_request_bytes(body)
            if patched:
                log.info("rewrote %s schema object(s) for model %s", patched, model or "?")
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
                except Exception:
                    log.exception("failed to write %s", LAST_TOOLS_PATH)

        self._forward(body)

    def _forward(self, body: bytes) -> None:
        scheme, host, port, tls = split_upstream(self.server.upstream_url)
        if tls:
            conn = HTTPSConnection(host, port, context=ssl.create_default_context(), timeout=600)
        else:
            conn = HTTPConnection(host, port, timeout=600)

        headers = {}
        for key, value in self.headers.items():
            if key.lower() in HOP_BY_HOP:
                continue
            headers[key] = value
        headers["Host"] = host if port in (80, 443) else "%s:%s" % (host, port)
        headers["Accept-Encoding"] = "identity"
        if body:
            headers["Content-Length"] = str(len(body))
        elif "Content-Length" in headers:
            headers["Content-Length"] = "0"

        try:
            conn.request(self.command, self.path, body=body or None, headers=headers)
            upstream = conn.getresponse()
            self._write_upstream(upstream)
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

    def _write_upstream(self, upstream) -> None:
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
        self.send_response(upstream.status, upstream.reason)
        for key, value in upstream.getheaders():
            if key.lower() in HOP_BY_HOP or key.lower() == "content-length":
                continue
            self.send_header(key, value)
        if length_header and self.command != "HEAD":
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
        while True:
            chunk = upstream.read(65536)
            if not chunk:
                break
            self.wfile.write(("%X\r\n" % len(chunk)).encode("ascii") + chunk + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host: str, port: int, upstream_url: str):
        self.upstream_url = upstream_url.rstrip("/")
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
