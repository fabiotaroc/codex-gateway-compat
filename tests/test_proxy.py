#!/usr/bin/env python3
import json
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from proxy.server import ProxyServer


class UpstreamHandler(BaseHTTPRequestHandler):
    last_body = b""
    last_path = ""
    last_content_lengths = []

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        UpstreamHandler.last_content_lengths = self.headers.get_all("Content-Length") or []
        length = int(self.headers.get("Content-Length") or 0)
        UpstreamHandler.last_body = self.rfile.read(length)
        UpstreamHandler.last_path = self.path
        body = json.loads(UpstreamHandler.last_body or b"{}")

        if body.get("stream") is False:
            # Non-streaming: answer with a flat function call as JSON.
            payload = json.dumps(
                {"id": "r", "output": [{"type": "function_call", "name": "mcp__codex_app--list_projects", "arguments": '{"limit":5.0}', "call_id": "c"}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        # Two SSE events separated by a delay: the proxy must relay the first
        # one before the second is produced.
        item = {"type": "function_call", "name": "mcp__codex_app--list_projects", "arguments": '{"limit":5.0}', "call_id": "c"}
        events = (
            b'event: response.created\ndata: {"type":"response.created","first":true}\n\n',
            ("event: response.output_item.done\ndata: %s\n\n" % json.dumps({"type": "response.output_item.done", "item": item})).encode(),
            b'event: response.completed\ndata: {"type":"response.completed","ok":true}\n\n',
        )
        for event in events:
            self.wfile.write(("%X\r\n" % len(event)).encode() + event + b"\r\n")
            self.wfile.flush()
            time.sleep(0.3)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class ProxyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        cls.upstream_thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.upstream_thread.start()
        upstream_port = cls.upstream.server_address[1]
        cls.proxy = ProxyServer("127.0.0.1", 0, "http://127.0.0.1:%s" % upstream_port, capture_debug=False)
        cls.proxy_thread = threading.Thread(target=cls.proxy.serve_forever, daemon=True)
        cls.proxy_thread.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.upstream.shutdown()
        cls.proxy.server_close()
        cls.upstream.server_close()

    def _post(self, body):
        conn = HTTPConnection("127.0.0.1", self.proxy.server_address[1], timeout=5)
        raw = json.dumps(body).encode("utf-8")
        # Codex (reqwest) emits lowercase header names; mimic that so the
        # proxy must dedupe case-insensitively.
        conn.request(
            "POST",
            "/codex/v1/responses",
            body=raw,
            headers={
                "content-type": "application/json",
                "content-length": str(len(raw)),
                "accept-encoding": "gzip, br",
            },
        )
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data

    def test_health(self):
        conn = HTTPConnection("127.0.0.1", self.proxy.server_address[1], timeout=5)
        conn.request("GET", "/health")
        response = conn.getresponse()
        payload = json.loads(response.read())
        conn.close()
        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])

    def test_rewrites_muse_and_forwards_stream(self):
        status, data = self._post(
            {
                "model": "meta/muse-spark-1.3-contributor",
                "tools": [
                    {
                        "type": "function",
                        "name": "list_threads",
                        "parameters": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {"limit": {"type": "integer"}},
                        },
                    }
                ],
            }
        )
        self.assertEqual(status, 200)
        self.assertIn(b"data: ", data)
        forwarded = json.loads(UpstreamHandler.last_body)
        self.assertEqual(forwarded["tools"][0]["parameters"]["required"], ["limit"])
        self.assertEqual(UpstreamHandler.last_path, "/codex/v1/responses")
        # Rewritten body changes length; exactly one, correct Content-Length must reach upstream.
        self.assertEqual(len(UpstreamHandler.last_content_lengths), 1)
        self.assertEqual(int(UpstreamHandler.last_content_lengths[0]), len(UpstreamHandler.last_body))

    def _stream_first_event_latency(self, model):
        import socket

        sock = socket.create_connection(("127.0.0.1", self.proxy.server_address[1]), timeout=5)
        body = json.dumps({"model": model, "input": "hi", "tools": []}).encode()
        sock.sendall(
            b"POST /codex/v1/responses HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            + ("Content-Length: %d\r\n\r\n" % len(body)).encode()
            + body
        )
        started = time.time()
        buf = b""
        while b'"first"' not in buf:
            buf += sock.recv(4096)
        first_at = time.time() - started
        while b'"ok"' not in buf:
            buf += sock.recv(4096)
        sock.close()
        return first_at, buf

    def test_streams_incrementally(self):
        first_at, _ = self._stream_first_event_latency("openai/gpt-5.6-luna")
        # First event must arrive well before the upstream finishes (~0.9s total).
        self.assertLess(first_at, 0.5, "first SSE event was buffered: %.2fs" % first_at)

    def test_rewriting_stream_still_streams_and_unflattens_calls(self):
        # Establish the namespace mapping through the request first.
        namespaced = {
            "model": "meta/muse-spark-1.3-contributor",
            "tools": [{"type": "namespace", "name": "mcp__codex_app", "tools": [{"name": "list_projects", "parameters": {"type": "object", "properties": {}}}]}],
        }
        status, data = self._post(namespaced)
        self.assertEqual(status, 200)
        forwarded = json.loads(UpstreamHandler.last_body)
        self.assertEqual(forwarded["tools"][0]["type"], "function")
        self.assertEqual(forwarded["tools"][0]["name"], "mcp__codex_app--list_projects")
        text = data.decode()
        self.assertIn('"namespace":"mcp__codex_app"', text)
        self.assertIn('"name":"list_projects"', text)
        self.assertIn('{\\"limit\\":5}', text)
        self.assertNotIn("5.0", text)
        self.assertIn("event: response.output_item.done\ndata: ", text)

        first_at, _ = self._stream_first_event_latency("meta/muse-spark-1.3-contributor")
        self.assertLess(first_at, 0.5, "rewriting proxy buffered the stream: %.2fs" % first_at)

    def test_non_streaming_json_is_rewritten_with_correct_length(self):
        status, data = self._post(
            {
                "model": "meta/muse-spark-1.3-contributor",
                "stream": False,
                "tools": [{"type": "namespace", "name": "mcp__codex_app", "tools": [{"name": "list_projects", "parameters": {"type": "object", "properties": {}}}]}],
            }
        )
        self.assertEqual(status, 200)
        body = json.loads(data)
        self.assertEqual(body["output"][0]["namespace"], "mcp__codex_app")
        self.assertEqual(body["output"][0]["name"], "list_projects")
        self.assertEqual(json.loads(body["output"][0]["arguments"]), {"limit": 5})

    def test_does_not_rewrite_other_models(self):
        self._post(
            {
                "model": "openai/gpt-5.5",
                "tools": [
                    {
                        "type": "function",
                        "name": "list_threads",
                        "parameters": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {"limit": {"type": "integer"}},
                        },
                    }
                ],
            }
        )
        forwarded = json.loads(UpstreamHandler.last_body)
        self.assertNotIn("required", forwarded["tools"][0]["parameters"])


if __name__ == "__main__":
    unittest.main()
