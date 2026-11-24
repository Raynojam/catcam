"""Read-only Cat Lounge with a friendly invitation + PIN screen. Loopback only."""
import argparse
import json
from pathlib import Path
import sqlite3
import threading
from urllib.parse import urlsplit

from lounge import ROOT, ASSETS, STREAM_PATH, Camera, Handler, Server
from pin_access import Access, LoginError

PUBLIC = {"/gate.css": ("gate.css", "text/css; charset=utf-8"),
          "/gate.js": ("gate.js", "application/javascript; charset=utf-8"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml")}
PROTECTED = set(ASSETS) | {"/frame.jpg", "/status.json", STREAM_PATH}


class PINHandler(Handler):
    def end_headers(self):
        if getattr(self, "session_cookie", None):
            self.send_header("Set-Cookie", self.session_cookie)
        if getattr(self, "retry_after", None):
            self.send_header("Retry-After", str(self.retry_after))
        super().end_headers()

    def still_allowed(self):
        try:
            return bool(self.server.access.authenticated(self.headers.get("Cookie")))
        except (OSError, ValueError, TypeError, sqlite3.Error):
            return False

    def json_reply(self, status, data):
        self.reply(status, json.dumps(data).encode(), "application/json")

    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            if path in PUBLIC:
                name, mime = PUBLIC[path]
                return self.reply(200, (ROOT/name).read_bytes(), mime)
            if path not in PROTECTED:
                return self.reply(404, b"Not found", "text/plain")
            signed_in = self.server.access.authenticated(self.headers.get("Cookie"))
            if path == "/" and not signed_in:
                return self.reply(200, (ROOT/"gate.html").read_bytes(), "text/html; charset=utf-8")
            if not signed_in:
                return self.json_reply(401, {"state":"locked"})
            return super().do_GET()
        except (OSError, ValueError, TypeError, sqlite3.Error):
            self.json_reply(503, {"state":"unavailable"})

    do_HEAD = do_GET

    def do_POST(self):
        self.close_connection = True
        path = urlsplit(self.path).path
        if path not in ("/api/login", "/api/logout"):
            return self.reply(404, b"Not found", "text/plain")
        try:
            config = self.server.access.settings()
            # The configured origin is fixed at install; never trust forwarded headers.
            if self.headers.get("Origin") != config["origin"] or self.headers.get("Sec-Fetch-Site") == "cross-site":
                return self.json_reply(403, {"error":"Open the lounge directly to sign in."})
            if (self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json"
                    or self.headers.get("Transfer-Encoding")):
                return self.json_reply(400, {"error":"Invalid request"})
            length = self.headers.get("Content-Length", "")
            if not length.isascii() or not length.isdecimal() or not 0 < int(length) <= 2048:
                return self.json_reply(400, {"error":"Invalid request"})
            try:
                body = json.loads(self.rfile.read(int(length)))
            except (ValueError, UnicodeError):
                return self.json_reply(400, {"error":"Invalid JSON"})
            if not isinstance(body, dict):
                return self.json_reply(400, {"error":"Invalid request"})
            if path == "/api/logout":
                self.server.access.logout(self.headers.get("Cookie"))
                self.session_cookie = self.server.access.clear_cookie()
                return self.json_reply(200, {"ok":True})
            if not {"pin"} <= set(body) <= {"invite", "pin"}:
                return self.json_reply(400, {"error":"Invalid request"})
            token = self.server.access.login(body.get("invite"), body["pin"])
            self.session_cookie = self.server.access.cookie(token)
            self.json_reply(200, {"ok":True})
        except LoginError as error:
            self.retry_after = error.retry
            result = {"error": str(error), "retry_after": error.retry}
            if error.code:
                result["code"] = error.code
            self.json_reply(error.status, result)
        except (OSError, ValueError, TypeError, sqlite3.Error):
            self.json_reply(503, {"error":"The lounge is temporarily unavailable. Please try again."})


def make_server(settings, auth, database, port=8088, source="http://127.0.0.1:8085"):
    access = Access(auth, database)
    server = Server(("127.0.0.1", port), PINHandler)
    server.camera, server.access = Camera(settings, source), access
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path.home()/".config/catcam-lounge/settings.json")
    parser.add_argument("--auth", type=Path, default=Path.home()/".config/catcam-lounge/pin-access.json")
    parser.add_argument("--database", type=Path, default=Path.home()/".local/share/catcam-lounge/access.sqlite3")
    parser.add_argument("--port", type=int, default=8088)
    args = parser.parse_args()
    server = make_server(args.config, args.auth, args.database, args.port)
    threading.Thread(target=server.camera.run, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.camera.stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
