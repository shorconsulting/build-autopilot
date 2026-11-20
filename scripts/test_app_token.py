import base64
import http.server
import json
import os
import subprocess
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class FakeGitHubApp(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def reply(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_GET(self):
        FakeGitHubApp.seen.append(("GET", self.path, self.headers.get("Authorization")))
        if self.path == "/orgs/acme/installation":
            return self.reply(200, {"id": 42})
        self.reply(404, {})

    def do_POST(self):
        FakeGitHubApp.seen.append(("POST", self.path, self.headers.get("Authorization")))
        if self.path == "/app/installations/42/access_tokens":
            return self.reply(201, {"token": "ghs_test"})
        self.reply(404, {})


class AppToken(unittest.TestCase):
    def test_mints_an_installation_token_with_a_signed_jwt(self):
        d = tempfile.mkdtemp()
        key, pub = os.path.join(d, "k.pem"), os.path.join(d, "p.pem")
        subprocess.run(["openssl", "genrsa", "-out", key, "2048"], check=True, capture_output=True)
        subprocess.run(["openssl", "rsa", "-in", key, "-pubout", "-out", pub], check=True, capture_output=True)
        srv = http.server.HTTPServer(("127.0.0.1", 0), FakeGitHubApp)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        env = dict(os.environ, GITHUB_API_URL="http://127.0.0.1:%d" % srv.server_port)
        env.pop("HTTPS_PROXY", None), env.pop("HTTP_PROXY", None), env.pop("https_proxy", None), env.pop("http_proxy", None)
        env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1"
        out = subprocess.run([os.path.join(HERE, "app-token.sh"), "123", key, "acme"], env=env,
                             capture_output=True, text=True)
        srv.shutdown()
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "ghs_test"), out.stderr)
        jwt = FakeGitHubApp.seen[0][2].split(" ", 1)[1]
        h, p, s = jwt.split(".")
        self.assertEqual(json.loads(unb64(p))["iss"], "123")
        sig = os.path.join(d, "sig")
        with open(sig, "wb") as fh:
            fh.write(unb64(s))
        ok = subprocess.run(["openssl", "dgst", "-sha256", "-verify", pub, "-signature", sig],
                            input=("%s.%s" % (h, p)).encode(), capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stdout)


if __name__ == "__main__":
    unittest.main()
