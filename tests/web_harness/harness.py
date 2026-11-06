"""Runs the real web app and API locally with fake sign-in and an in-memory database, so signed-in
pages can be tried in a browser: .venv/bin/python tests/web_harness/harness.py "$PWD" tests/web_harness
then open http://127.0.0.1:8799/ . Local only: it skips sign-in checks, so never deploy it."""
import os, sys, re, json, tempfile, importlib.util as u, mimetypes
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
os.environ["STOCK_HUB_DATA"] = tempfile.mkdtemp()
root, scratch = sys.argv[1], sys.argv[2]
spec = u.spec_from_file_location("web_api", root + "/web/api/index.py"); api = u.module_from_spec(spec); spec.loader.exec_module(api)
api.verify_user = lambda a: "u1"
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def out(self, code, body, ctype="application/json"):
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)); route = urlparse(self.path).path.rsplit("/", 1)[-1]
        c, b = api.respond_post(route, "x", raw); self.out(c, json.dumps(b).encode())
    def do_GET(self):
        url = urlparse(self.path)
        if url.path.startswith("/api/"):
            c, b, _ = api.respond(url.path, url.query); return self.out(c, json.dumps(b).encode())
        if url.path == "/fake.js": return self.out(200, open(scratch + "/fake-firebase.js", "rb").read(), "text/javascript")
        p = root + "/web/public" + ("/index.html" if url.path == "/" else url.path)
        if not os.path.isfile(p): return self.out(404, b"{}")
        data = open(p, "rb").read()
        if p.endswith("app.js"): data = re.sub(rb"https://www\.gstatic\.com/firebasejs/[^\"]+", b"/fake.js", data)
        self.out(200, data, mimetypes.guess_type(p)[0] or "text/plain")
ThreadingHTTPServer(("127.0.0.1", 8799), H).serve_forever()
