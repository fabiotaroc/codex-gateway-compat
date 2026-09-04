#!/usr/bin/env python3
import json
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from proxy import ProxyServer


class UpstreamHandler(BaseHTTPRequestHandler):
    last_body = b""
    last_path = ""

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        UpstreamHandler.last_body = self.rfile.read(length)
        UpstreamHandler.last_path = self.path
        payload = b"data: {\"type\":\"ok\"}\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(payload)


class ProxyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        cls.upstream_thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.upstream_thread.start()
        upstream_port = cls.upstream.server_address[1]
        cls.proxy = ProxyServer("127.0.0.1", 0, "http://127.0.0.1:%s" % upstream_port)
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
        conn.request("POST", "/codex/v1/responses", body=raw, headers={"Content-Type": "application/json"})
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
