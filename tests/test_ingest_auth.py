"""Tests the sign-in and token renewal in ingest_fynd.py against a small local stand-in for Fynd."""
import base64
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ingest_fynd as ing

CLIENT_ID, SECRET = "client-1", "secret-1"


class FakeFynd(BaseHTTPRequestHandler):
    tokens_issued = 0
    creates = 0

    def log_message(self, *args):
        pass

    def reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path.endswith("/oauth/token"):
            expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
            if self.headers.get("Authorization") != expected or body.get("grant_type") != "client_credentials":
                return self.reply(401, {"message": "bad credentials"})
            FakeFynd.tokens_issued += 1
            return self.reply(200, {"access_token": f"tok{FakeFynd.tokens_issued}", "expires_in": 3599})
        FakeFynd.creates += 1
        # the first token "expires" once the second product is sent
        if self.headers.get("Authorization") == "Bearer tok1" and FakeFynd.creates >= 2:
            return self.reply(401, {"message": "token expired"})
        self.reply(201, {"ok": True})


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeFynd)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.host = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeFynd.tokens_issued = 0
        FakeFynd.creates = 0
        self.cfg = {"host": self.host, "company_id": "1234", "client_id": CLIENT_ID,
                    "client_secret": SECRET, "token_scope": [], "token": ""}

    def test_fetch_token(self):
        token, expires = ing.fetch_token(self.cfg)
        self.assertEqual(token, "tok1")
        self.assertEqual(expires, 3599)

    def test_wrong_secret_stops_with_a_clear_message(self):
        self.cfg["client_secret"] = "wrong"
        with self.assertRaises(SystemExit) as ctx:
            ing.fetch_token(self.cfg)
        self.assertIn("Could not get a token", str(ctx.exception))

    def test_expired_token_is_renewed_and_the_request_retried(self):
        url = f"{self.host}/service/platform/catalog/v3.0/company/1234/products/"
        refresh = lambda: ing.fetch_token(self.cfg)
        auth = {"token": refresh()[0]}
        self.assertEqual(ing.post_json(url, auth, {"a": 1}, refresh)[0], 201)   # product 1, token still good
        self.assertEqual(ing.post_json(url, auth, {"a": 2}, refresh)[0], 201)   # product 2, 401 then renewed
        self.assertEqual(auth["token"], "tok2")
        self.assertEqual(FakeFynd.tokens_issued, 2)

    def test_without_credentials_a_401_is_returned_not_retried_forever(self):
        url = f"{self.host}/service/platform/catalog/v3.0/company/1234/products/"
        FakeFynd.creates = 5
        status, _ = ing.post_json(url, {"token": "tok1"}, {"a": 1}, refresh=None)
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
