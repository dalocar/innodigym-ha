import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "innodigym", "app")
sys.path.insert(0, ROOT)
import db  # noqa: E402
import server  # noqa: E402
import sync  # noqa: E402

REC = {"sportRecordId": 100, "startTime": "2026-10-28 08:49:29", "name": "Rutina B",
       "coreData": 1000.0, "consume": 394, "duration": 961}
EXT = [{"actionId": 1, "actionName": "Seated Row", "sort": 1, "weight": 200, "count": 10, "duration": 30}]


class Triggered:
    def __init__(self):
        self.count = 0


class LocalApiHttpTest(unittest.TestCase):
    """Runs the real handler on an ephemeral port, as Home Assistant ingress would call it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        server.DATA_DIR = cls.tmp.name
        server.WORKER = None
        cls.logins = []
        server.Ctx.login = staticmethod(cls._login)
        os.chdir(ROOT)
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    @classmethod
    def _login(cls, email, password):
        cls.logins.append(email)
        if password == "bad":
            raise sync.VendorError("wrong password", auth=True)
        return "secret-token-1"

    def call(self, method, path, body=None, user=("ha-1", "Alex")):
        headers = {"Content-Type": "application/json"}
        if user:
            headers["X-Remote-User-Id"], headers["X-Remote-User-Display-Name"] = user
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def seed(self, ha, name):
        c = db.connect(os.path.join(self.tmp.name, "innodigym.db"))
        u = db.get_or_create_ha_user(c, ha, name)
        db.upsert_vendor_record(c, u["id"], REC, EXT)
        c.close()
        return u["id"]

    def test_first_visit_creates_the_user_from_headers(self):
        s, me = self.call("GET", "/local/me", user=("ha-new", "Newcomer"))
        self.assertEqual((s, me["name"], me["vendorLinked"]), (200, "Newcomer", False))
        self.assertNotIn("token", json.dumps(me))

    def test_standalone_without_headers_is_the_local_user(self):
        s, me = self.call("GET", "/local/me", user=None)
        self.assertEqual((s, me["name"]), (200, "Local"))

    def test_users_only_see_their_own_workouts(self):
        self.seed("ha-a", "A")
        wid = 100
        self.assertEqual(self.call("GET", "/local/workouts", user=("ha-a", "A"))[1][0]["id"], wid)
        self.assertEqual(self.call("GET", "/local/workouts", user=("ha-b", "B"))[1], [])
        self.assertEqual(self.call("GET", "/local/workouts/100", user=("ha-b", "B"))[0], 404)
        self.assertEqual(self.call("DELETE", "/local/workouts/100", user=("ha-b", "B"))[0], 404)
        self.assertEqual(self.call("GET", "/local/workouts/100", user=("ha-a", "A"))[0], 200)

    def test_manual_workout_note_and_delete(self):
        s, w = self.call("POST", "/local/workouts", {"date": "2026-11-01", "name": "Free weights",
                         "sets": [{"exercise": "Bench", "weightKg": 40, "reps": 10}]}, user=("ha-m", "M"))
        self.assertEqual((s, w["volume_kg"], w["source"]), (201, 400.0, "manual"))
        s, w2 = self.call("PUT", "/local/workouts/%d/note" % w["id"], {"text": "easy", "rpe": 6}, user=("ha-m", "M"))
        self.assertEqual((s, w2["note"], w2["rpe"]), (200, "easy", 6))
        self.assertEqual(self.call("DELETE", "/local/workouts/%d" % w["id"], user=("ha-m", "M"))[0], 200)
        self.assertEqual(self.call("GET", "/local/workouts", user=("ha-m", "M"))[1], [])

    def test_validation(self):
        u = ("ha-v", "V")
        bad = [{"date": "2026-13-45", "sets": [{"exercise": "x", "weightKg": 1, "reps": 1}]},
               {"date": "2026-11-01", "sets": []},
               {"date": "2026-11-01", "sets": [{"exercise": "x", "weightKg": -5, "reps": 1}]},
               {"date": "2026-11-01", "sets": [{"exercise": "x", "weightKg": 5, "reps": 0}]},
               {"date": "2026-11-01", "sets": [{"exercise": " ", "weightKg": 5, "reps": 1}]}]
        for b in bad:
            self.assertEqual(self.call("POST", "/local/workouts", b, user=u)[0], 400, b)
        self.seed("ha-v", "V")
        self.assertEqual(self.call("PUT", "/local/workouts/100/note", {"text": "x", "rpe": 11}, user=u)[0], 400)
        self.assertEqual(self.call("POST", "/local/body-weight", {"date": "2026-11-01", "kg": 5}, user=u)[0], 400)
        self.assertEqual(self.call("PUT", "/local/me", {"goalPerWeek": 0}, user=u)[0], 400)

    def test_exercises_and_history(self):
        u = ("ha-e", "E")
        self.seed("ha-e", "E")
        self.assertEqual(self.call("GET", "/local/exercises", user=u)[1], [{"exercise": "Seated Row", "sets": 1, "sessions": 1}])
        s, h = self.call("GET", "/local/exercises/Seated%20Row", user=u)
        self.assertEqual((s, h[0]["weight_kg"], h[0]["reps"]), (200, 20.0, 10))

    def test_all_sets_in_one_call(self):
        u = ("ha-s", "S")
        self.seed("ha-s", "S")
        s, rows = self.call("GET", "/local/sets", user=u)
        self.assertEqual((s, len(rows), rows[0]["exercise"], rows[0]["workout_id"]), (200, 1, "Seated Row", 100))
        self.assertEqual(self.call("GET", "/local/sets?since=2026-11-01", user=u)[1], [])

    def test_import_backup_files_is_idempotent_and_skips_junk(self):
        u = ("ha-i", "I")
        rec = {"sportRecordId": 500, "startTime": "2026-10-28 08:49:29", "name": "Rutina B", "coreData": 65720.0, "consume": 394, "duration": 961}
        detail = dict(rec, name=None, extData=[{"actionId": 1, "actionName": "Row", "sort": 1, "weight": 150, "count": 12, "duration": 40}])
        body = {"files": [{"code": 200, "data": [rec]}, {"data": detail}, {"data": {"nope": 1}}, "junk"]}
        s, r = self.call("POST", "/local/import", body, user=u)
        self.assertEqual((s, r), (200, {"new": 1, "updated": 0, "withSets": 1, "skipped": 2}))
        s, r = self.call("POST", "/local/import", body, user=u)
        self.assertEqual((r["new"], r["updated"]), (0, 1))
        w = self.call("GET", "/local/workouts/500", user=u)[1]
        self.assertEqual((w["name"], w["volume_kg"], len(w["sets"])), ("Rutina B", 6572.0, 1))
        self.assertEqual(self.call("GET", "/local/workouts", user=("ha-j", "J"))[1], [])
        self.assertEqual(self.call("POST", "/local/import", {"files": []}, user=u)[0], 400)

    def test_body_weight_and_goal(self):
        u = ("ha-bw", "BW")
        self.call("POST", "/local/body-weight", {"date": "2026-11-09", "kg": 76.8}, user=u)
        self.assertEqual(self.call("GET", "/local/body-weight", user=u)[1], [{"date": "2026-11-09", "kg": 76.8}])
        self.assertEqual(self.call("PUT", "/local/me", {"goalPerWeek": 4}, user=u)[1]["goalPerWeek"], 4)

    def test_vendor_login_stores_token_but_never_returns_it(self):
        u = ("ha-l", "L")
        s, body = self.call("POST", "/local/me/vendor-login", {"email": "a@b.c", "password": "bad"}, user=u)
        self.assertEqual(s, 401)
        s, me = self.call("POST", "/local/me/vendor-login", {"email": "a@b.c", "password": "ok"}, user=u)
        self.assertEqual((s, me["vendorLinked"], me["vendorEmail"]), (200, True, "a@b.c"))
        self.assertNotIn("secret-token-1", json.dumps(me))
        c = db.connect(os.path.join(self.tmp.name, "innodigym.db"))
        self.assertEqual(db.get_user_by_ha_id(c, "ha-l")["token"], "secret-token-1")
        c.close()

    def test_proxy_uses_the_stored_vendor_token_when_the_browser_sends_none(self):
        seen = []

        class Upstream(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success": true}')

            def log_message(self, *a):
                pass

        up = http.server.HTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=up.serve_forever, daemon=True).start()
        old = server.UPSTREAM
        server.UPSTREAM = "http://127.0.0.1:%d" % up.server_address[1]
        try:
            u = ("ha-p", "P")
            self.call("POST", "/local/me/vendor-token", {"token": "tok-p"}, user=u)
            self.call("GET", "/api/app/getInfo", user=u)
            self.call("GET", "/api/app/getInfo", user=("ha-q", "Q"))      # no token stored
            req = urllib.request.Request("http://127.0.0.1:%d/api/x" % self.port,
                                         headers={"Authorization": "Bearer mine", "X-Remote-User-Id": "ha-p"})
            urllib.request.urlopen(req).read()                            # explicit header wins
        finally:
            server.UPSTREAM = old
            up.shutdown()
        self.assertEqual(seen, ["Bearer tok-p", None, "Bearer mine"])

    def test_unknown_route_bad_json_and_sync_trigger(self):
        u = ("ha-x", "X")
        self.assertEqual(self.call("GET", "/local/nope", user=u)[0], 404)
        req = urllib.request.Request("http://127.0.0.1:%d/local/me" % self.port, method="PUT", data=b"{oops",
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)
        self.assertEqual(self.call("POST", "/local/sync", user=u)[0], 202)


if __name__ == "__main__":
    unittest.main()
