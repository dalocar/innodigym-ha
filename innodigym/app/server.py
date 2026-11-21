#!/usr/bin/env python3
"""
Local proxy + static server for the INNODIGYM Power program editor.

Why a proxy: power.renhesports.com doesn't send CORS headers, so the browser
can't call it directly from a page served on localhost. This process forwards
anything under /api/* to the real API, unmodified, and serves the static
frontend for everything else. It stores no credentials itself — the token
lives only in the browser (localStorage) and is forwarded per-request via the
Authorization header the frontend sets.
"""
import http.server
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request

import accounts
import db
import hevy
import local_api
import mqtt
import sync

UPSTREAM = "https://power.renhesports.com"
PORT = int(os.environ.get("INNODIGYM_PORT", "8787"))

# Persistence is off unless INNODIGYM_DATA_DIR is set, so `python server.py` and
# docker-compose behave exactly as before. The Home Assistant add-on sets it to /data.
DATA_DIR = os.environ.get("INNODIGYM_DATA_DIR")
DB = None          # sqlite3 connection, opened in main()
WORKER = None      # background sync thread, started in main()
PUBLISHER = mqtt.Publisher(None)  # replaced in main() when a broker is available
OPTIONS = {"sync_interval_minutes": 10, "discovery_prefix": "homeassistant", "users": [], "timezone": ""}


def load_options(data_dir):
    """Add-on options written by the Supervisor to <data_dir>/options.json (absent outside the add-on)."""
    try:
        with open(os.path.join(data_dir, "options.json"), encoding="utf-8") as f:
            return {**OPTIONS, **json.load(f)}
    except FileNotFoundError:
        return dict(OPTIONS)


_tls = threading.local()


def conn():
    """One SQLite connection per server thread."""
    if getattr(_tls, "conn", None) is None:
        _tls.conn = db.connect(os.path.join(DATA_DIR, "innodigym.db"))
    return _tls.conn


def publish_async(user_id):
    """Refresh a user's Home Assistant sensors after a change, without making the browser wait for the broker."""
    if not PUBLISHER.enabled:
        return

    def run():
        c = db.connect(os.path.join(DATA_DIR, "innodigym.db"))
        try:
            u = db.get_user(c, user_id)
            if u:
                PUBLISHER.publish_state(c, u)
        finally:
            c.close()
    threading.Thread(target=run, daemon=True, name="mqtt-publish").start()


class Ctx:
    """What local_api needs from the running server."""

    @staticmethod
    def trigger():
        if WORKER:
            WORKER.trigger()

    @staticmethod
    def login(email, password):
        return sync.VendorClient().login(email, password)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory="static", **kwargs)

    def log_message(self, fmt, *args):
        # Keep container logs readable; comment out to see every request.
        print("%s - %s" % (self.address_string(), fmt % args))

    def end_headers(self):
        # Static files are tiny and change on every deploy; without this the
        # browser heuristically caches an old app.js against a new index.html.
        if self.path.startswith("/local/"):
            self.send_header("Cache-Control", "no-store")
        elif not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def current_user(self):
        """Home Assistant ingress names the logged-in person in X-Remote-User-* headers. Only
        ingress can reach the add-on's port, so they are trusted. Without them (running the
        app standalone) everyone is the single user 'Local'."""
        hid = self.headers.get("X-Remote-User-Id")
        name = self.headers.get("X-Remote-User-Display-Name") or self.headers.get("X-Remote-User-Name")
        return db.get_or_create_ha_user(conn(), hid or "local", name or "Local",
                                        self.headers.get("X-Remote-User-Name"))

    def local(self, method):
        if not DATA_DIR:
            return self._json(404, {"error": "persistence is not enabled (INNODIGYM_DATA_DIR is not set)"})
        url = urllib.parse.urlsplit(self.path)
        parts = [p for p in url.path[len("/local/"):].split("/") if p]
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = None
        if length:
            if length > (8 * 1024 * 1024 if parts == ["import"] else 65536):
                return self._json(413, {"error": "request too large"})
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                return self._json(400, {"error": "body is not valid JSON"})
        try:
            user = self.current_user()
            status, data = local_api.handle(conn(), user, method, parts, query, body, Ctx)
            if method != "GET" and status < 300 and parts[:1] != ["sync"]:
                publish_async(user["id"])
        except local_api.ApiError as e:
            status, data = e.status, {"error": e.message}
        except Exception as e:  # noqa: BLE001
            print("local api error: %s: %s" % (type(e).__name__, e), flush=True)
            status, data = 500, {"error": "internal error"}
        self._json(status, data)

    def _json(self, status, data):
        self._relay(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        if self.path.startswith("/local/"):
            self.local("GET")
        elif self.path.startswith("/api/"):
            self.proxy("GET")
        elif self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.startswith("/local/"):
            self.local("POST")
        elif self.path.startswith("/api/"):
            self.proxy("POST")
        else:
            self.send_error(404)

    def do_PUT(self):
        if self.path.startswith("/local/"):
            self.local("PUT")
        elif self.path.startswith("/api/"):
            self.proxy("PUT")
        else:
            self.send_error(404)

    def do_DELETE(self):
        if self.path.startswith("/local/"):
            self.local("DELETE")
        elif self.path.startswith("/api/"):
            self.proxy("DELETE")
        else:
            self.send_error(404)

    def proxy(self, method):
        url = UPSTREAM + self.path
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None

        req = urllib.request.Request(url, data=body, method=method)
        auth = self.headers.get("Authorization")
        if not auth and DATA_DIR:
            # add-on mode: the browser holds no vendor token, the server uses the user's stored one
            token = self.current_user()["token"]
            auth = "Bearer " + token if token else None
        if auth:
            req.add_header("Authorization", auth)
        ctype = self.headers.get("Content-Type")
        req.add_header("Content-Type", ctype or "application/json")

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                self._relay(resp.status, resp.read(), resp.headers.get("Content-Type"))
        except urllib.error.HTTPError as e:
            self._relay(e.code, e.read(), e.headers.get("Content-Type") if e.headers else None)
        except Exception as e:  # noqa: BLE001 - surface any proxy failure to the UI
            payload = json.dumps({"error": True, "proxyError": str(e)}).encode()
            self._relay(502, payload, "application/json")

    def _relay(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type or "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    global DB, OPTIONS, WORKER, PUBLISHER
    if DATA_DIR:
        os.makedirs(DATA_DIR, exist_ok=True)
        OPTIONS = load_options(DATA_DIR)
        DB = db.connect(os.path.join(DATA_DIR, "innodigym.db"))
        accounts.apply_options(DB, OPTIONS.get("users") or [])
        print(f"Database ready in {DATA_DIR} ({len(db.list_users(DB))} users)")
        PUBLISHER = mqtt.Publisher(mqtt.broker_from_environment(), OPTIONS["discovery_prefix"])
        print("MQTT: %s" % ("publishing to Home Assistant" if PUBLISHER.enabled else "off (no broker settings)"), flush=True)
        def initial():
            c = db.connect(os.path.join(DATA_DIR, "innodigym.db"))
            try:
                PUBLISHER.publish_all(c)
            finally:
                c.close()
        threading.Thread(target=initial, daemon=True, name="mqtt-initial").start()
        WORKER = sync.SyncWorker(os.path.join(DATA_DIR, "innodigym.db"), OPTIONS["sync_interval_minutes"],
                                 on_user=mqtt.sync_hook(PUBLISHER),
                                 tz=hevy.resolve_timezone(OPTIONS.get("timezone")))
        WORKER.start()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Serving on http://0.0.0.0:{PORT}  (proxying /api/* to {UPSTREAM})")
    server.serve_forever()


if __name__ == "__main__":
    main()
