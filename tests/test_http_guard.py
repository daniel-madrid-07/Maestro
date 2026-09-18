"""The API has no authentication, so its only guard is that a web page on
another origin cannot reach it. These run a real server on a free port."""

import json
import socket
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from maestro import server


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HttpGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.port), server.Handler)
        cls.httpd.daemon_threads = True
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, path, method="GET", headers=None, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=body, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_no_cors_header_is_ever_sent(self):
        for path in ("/health", "/sessions", "/usage"):
            _, headers, _ = self.call(path)
            self.assertNotIn("Access-Control-Allow-Origin", headers, path)

    def test_a_local_client_is_served(self):
        status, _, body = self.call("/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_a_page_on_another_origin_is_refused(self):
        # What a browser sends when evil.example fetches our API.
        status, _, _ = self.call("/health", headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_our_own_panel_is_allowed(self):
        origin = f"http://127.0.0.1:{self.port}"
        status, _, _ = self.call("/health", headers={"Origin": origin, "Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)

    def test_a_form_post_cannot_launch_a_session(self):
        # text/plain and friends need no preflight, so the content type is
        # checked as well as the origin.
        status, _, _ = self.call(
            "/sessions", method="POST",
            headers={"Content-Type": "text/plain"},
            body=json.dumps({"agent_profile": "worker", "working_directory": "/tmp"}).encode(),
        )
        self.assertEqual(status, 415)

    def test_options_grants_nothing(self):
        status, headers, _ = self.call("/sessions", method="OPTIONS")
        self.assertEqual(status, 405)
        self.assertNotIn("Access-Control-Allow-Origin", headers)


if __name__ == "__main__":
    unittest.main()
