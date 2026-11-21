import os
import sys
import tempfile
import unittest
from datetime import timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "innodigym", "app"))
import accounts  # noqa: E402
import db  # noqa: E402
import hevy  # noqa: E402
import sync  # noqa: E402


class FakeHevy:
    def __init__(self, templates=None, fail=None):
        self.tpl = dict(templates or {})
        self.created_templates, self.workouts, self.fail = [], [], fail

    def templates(self):
        return dict(self.tpl)

    def create_template(self, title, kind):
        self.created_templates.append((title, kind))
        return "T%d" % len(self.created_templates)

    def create_workout(self, w):
        if self.fail:
            raise self.fail
        self.workouts.append(w)
        return "H%d" % len(self.workouts)


class FakeVendor:
    """No records; only logins and (optionally) token rejection."""
    def __init__(self, valid_token="fresh"):
        self.token, self.valid, self.logins = None, valid_token, []

    def get(self, path):
        if self.token != self.valid:
            raise sync.VendorError("HTTP 401", auth=True)
        return {"data": []}

    def login(self, email, password):
        self.logins.append((email, password))
        return self.valid


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.dir.name, "t.db"))

    def tearDown(self):
        self.conn.close()
        self.dir.cleanup()

    def user(self, **opts):
        accounts.apply_options(self.conn, [dict({"name": "Alex"}, **opts)])
        return db.list_users(self.conn)[0]

    def workout(self, uid, when="2026-11-14 10:00:00", sets=None, wid=-1):
        sets = sets or [{"exercise": "Row", "weight_kg": 20.0, "reps": 10},
                        {"exercise": "Row", "weight_kg": 22.5, "reps": 8},
                        {"exercise": "Press", "weight_kg": 15.0, "reps": 12}]
        return db.add_manual_workout(self.conn, uid, when, "Rutina A", sets, duration_s=1800)


class AccountsTest(Base):
    def test_creates_and_updates_users_from_options(self):
        u = self.user(innogym_email="a@x.com", innogym_password="pw", hevy_api_key="k", ha_user="alex")
        self.assertEqual((u["managed"], u["vendor_email"], u["vendor_password"], u["hevy_api_key"]), (1, "a@x.com", "pw", "k"))
        self.assertTrue(u["hevy_since"])
        accounts.apply_options(self.conn, [{"name": "alex", "innogym_email": "a@x.com", "innogym_password": "new", "hevy_since": "2026-01-01"}])
        users = db.list_users(self.conn)
        self.assertEqual(len(users), 1)
        self.assertEqual((users[0]["vendor_password"], users[0]["hevy_api_key"], users[0]["hevy_since"]), ("new", None, "2026-01-01"))

    def test_hevy_since_defaults_once_and_is_kept(self):
        u = self.user(hevy_api_key="k")
        self.conn.execute("UPDATE users SET hevy_since='2026-02-02' WHERE id=?", (u["id"],))
        self.assertEqual(self.user(hevy_api_key="k")["hevy_since"], "2026-02-02")

    def test_ha_login_is_linked_to_options_user(self):
        self.user(ha_user="alex")
        u = db.get_or_create_ha_user(self.conn, "abc123", "Alex Display", "Alex")
        self.assertEqual(len(db.list_users(self.conn)), 1)
        self.assertEqual(u["ha_user_id"], "abc123")
        self.assertEqual(db.get_or_create_ha_user(self.conn, "abc123", "x")["id"], u["id"])

    def test_options_user_adopts_an_existing_panel_user(self):
        db.get_or_create_ha_user(self.conn, "abc123", "Whoever", "alex")
        accounts.apply_options(self.conn, [{"name": "Alex", "ha_user": "alex", "hevy_api_key": "k"}])
        users = db.list_users(self.conn)
        self.assertEqual(len(users), 1)
        self.assertEqual((users[0]["ha_user_id"], users[0]["hevy_api_key"]), ("abc123", "k"))


class LoginTest(Base):
    def test_logs_in_when_there_is_no_token_and_again_when_rejected(self):
        self.user(innogym_email="a@x.com", innogym_password="pw")
        v = FakeVendor()
        make = lambda token: (setattr(v, "token", token), v)[1]  # noqa: E731
        sync.sync_all(self.conn, make, make_hevy=None)
        self.assertEqual(db.list_users(self.conn)[0]["token"], "fresh")
        self.assertIsNone(db.last_sync(self.conn, 1)["error"])
        self.conn.execute("UPDATE users SET token='stale'")
        sync.sync_all(self.conn, make, make_hevy=None)
        self.assertEqual(len(v.logins), 2)
        self.assertEqual(db.list_users(self.conn)[0]["token"], "fresh")
        self.assertIsNone(db.last_sync(self.conn, 1)["error"])

    def test_wrong_password_is_reported(self):
        self.user(innogym_email="a@x.com", innogym_password="pw")

        class Bad(FakeVendor):
            def login(self, e, p):
                raise sync.VendorError("wrong password", auth=True)
        sync.sync_all(self.conn, lambda t: Bad(), make_hevy=None)
        self.assertIn("login", db.last_sync(self.conn, 1)["error"])


class PushTest(Base):
    def test_pushes_once_with_templates_and_grouped_sets(self):
        u = self.user(hevy_api_key="k", hevy_since="2026-11-01")
        wid = self.workout(u["id"])
        client = FakeHevy({"row": "R1"})
        r = hevy.push_user(self.conn, u, client, tz=timezone.utc)
        self.assertEqual((r["pushed"], r["error"]), (1, None))
        w = client.workouts[0]
        self.assertEqual([e["exercise_template_id"] for e in w["exercises"]], ["R1", "T1"])
        self.assertEqual(client.created_templates, [("Press", "weight_reps")])
        self.assertEqual(w["exercises"][0]["sets"], [
            {"type": "normal", "weight_kg": 20.0, "reps": 10}, {"type": "normal", "weight_kg": 22.5, "reps": 8}])
        self.assertEqual((w["start_time"], w["end_time"]), ("2026-11-14T10:00:00Z", "2026-11-14T10:30:00Z"))
        self.assertEqual(self.conn.execute("SELECT hevy_id FROM workouts WHERE id=?", (wid,)).fetchone()[0], "H1")
        self.assertEqual(hevy.push_user(self.conn, u, client, tz=timezone.utc)["pushed"], 0)
        self.assertEqual(len(client.workouts), 1)

    def test_skips_history_before_since_and_deleted(self):
        u = self.user(hevy_api_key="k", hevy_since="2026-11-10")
        self.workout(u["id"], when="2026-11-01 10:00:00")
        gone = self.workout(u["id"], when="2026-11-12 10:00:00")
        db.delete_workout(self.conn, u["id"], gone)
        client = FakeHevy()
        self.assertEqual(hevy.push_user(self.conn, u, client)["pushed"], 0)

    def test_rejected_workout_is_retried_a_few_times_then_left(self):
        u = self.user(hevy_api_key="k", hevy_since="2026-11-01")
        self.workout(u["id"])
        client = FakeHevy({"row": "R", "press": "P"}, fail=hevy.HevyError("HTTP 400 bad", status=400))
        for _ in range(hevy.MAX_ATTEMPTS + 2):
            r = hevy.push_user(self.conn, u, client)
        self.assertEqual(self.conn.execute("SELECT hevy_attempts FROM workouts").fetchone()[0], hevy.MAX_ATTEMPTS)
        self.assertIsNone(r["error"])  # the last runs had nothing to try

    def test_bad_key_stops_the_run_and_is_reported(self):
        u = self.user(hevy_api_key="k", hevy_since="2026-11-01")
        self.workout(u["id"])
        r = hevy.push_user(self.conn, u, FakeHevy({"row": "R", "press": "P"}, fail=hevy.HevyError("HTTP 401", status=401)))
        self.assertIn("api key rejected", r["error"])
        self.assertEqual(self.conn.execute("SELECT hevy_attempts FROM workouts").fetchone()[0], 0)

    def test_counts_for_the_status_card(self):
        u = self.user(hevy_api_key="k", hevy_since="2026-11-01")
        self.workout(u["id"])
        self.workout(u["id"], when="2026-11-15 10:00:00")
        self.assertEqual(hevy.counts(self.conn, u), {"sent": 0, "waiting": 2})
        hevy.push_user(self.conn, u, FakeHevy({"row": "R", "press": "P"}))
        self.assertEqual(hevy.counts(self.conn, u), {"sent": 2, "waiting": 0})

    def test_template_matching_rules(self):
        t = {"squat (barbell)": "A", "squat (dumbbell)": "B", "plank": "P", "row (cable)": "R", "bench press": "BP"}
        self.assertEqual(hevy.resolve_template("Plank", t), "P")
        self.assertEqual(hevy.resolve_template("Row", t), "R")              # one bracketed variant
        self.assertIsNone(hevy.resolve_template("Squat", t))                # ambiguous -> custom
        self.assertIsNone(hevy.resolve_template("Chest Press", t))
        self.assertEqual(hevy.resolve_template("Squat", t, "Squat (Dumbbell)"), "B")
        self.assertEqual(hevy.resolve_template("Squat", t, "DEADBEEF"), "DEADBEEF")
        with self.assertRaises(hevy.HevyError):
            hevy.resolve_template("Squat", t, "Nonexistent")

    def test_option_mapping_beats_everything_and_bad_one_is_reported(self):
        u = self.user(hevy_api_key="k", hevy_since="2026-11-01", hevy_actions="Row = Row (Cable); press=Overhead Press")
        self.workout(u["id"])
        client = FakeHevy({"row (cable)": "R", "overhead press": "O"})
        self.assertEqual(hevy.push_user(self.conn, u, client)["pushed"], 1)
        self.assertEqual([e["exercise_template_id"] for e in client.workouts[0]["exercises"]], ["R", "O"])
        self.assertEqual(client.created_templates, [])
        u2 = self.user(hevy_api_key="k", hevy_since="2026-11-01", hevy_actions="Row=Nope")
        self.workout(u2["id"], when="2026-11-16 10:00:00")
        r = hevy.push_user(self.conn, u2, FakeHevy({"row": "x"}))
        self.assertEqual(r["pushed"], 0)
        self.assertIn("not a Hevy exercise", r["error"])

    def test_timezone_is_applied(self):
        from zoneinfo import ZoneInfo
        u = self.user(hevy_api_key="k", hevy_since="2026-01-01")
        self.workout(u["id"], when="2026-07-14 10:00:00")
        c = FakeHevy({"row": "R", "press": "P"})
        hevy.push_user(self.conn, u, c, tz=ZoneInfo("Europe/Madrid"))
        self.assertEqual(c.workouts[0]["start_time"], "2026-07-14T08:00:00Z")   # CEST, UTC+2

    def test_user_without_vendor_login_still_gets_manual_workouts_pushed(self):
        u = self.user(hevy_api_key="k", hevy_since="2026-11-01")
        self.workout(u["id"])
        h = FakeHevy({"row": "R", "press": "P"})
        out = sync.sync_all(self.conn, lambda t: self.fail("no vendor call expected"), make_hevy=lambda key: h)
        self.assertEqual(out["Alex"]["hevy"]["pushed"], 1)

    def test_non_json_reply_says_what_came_back(self):
        import http.server, threading
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
                self.wfile.write(b"<html>  Just a moment...  </html>")
            log_message = lambda *a: None  # noqa: E731
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(hevy.HevyError) as cm:
                hevy.HevyClient("k", base="http://127.0.0.1:%d" % srv.server_address[1]).get("/v1/exercise_templates?page=1")
        finally:
            srv.shutdown()
        msg = str(cm.exception)
        for part in ("GET /v1/exercise_templates", "HTTP 200", "text/html", "Just a moment"):
            self.assertIn(part, msg)

    def test_create_template_accepts_plain_text_json_and_quoted_ids(self):
        import http.server, threading
        replies = [(b"2926a432-975e-4848-94a0-7425cd735b90", "text/html; charset=utf-8"),
                   (b'{"id": 123}', "application/json"), (b'"abcd1234"', "application/json")]
        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body, ctype = replies.pop(0)
                self.send_response(200); self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            log_message = lambda *a: None  # noqa: E731
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = hevy.HevyClient("k", base="http://127.0.0.1:%d" % srv.server_address[1])
            got = [c.create_template("X", "weight_reps") for _ in range(3)]
        finally:
            srv.shutdown()
        self.assertEqual(got, ["2926a432-975e-4848-94a0-7425cd735b90", "123", "abcd1234"])

    def test_created_template_is_remembered_so_retries_do_not_create_it_again(self):
        u = self.user(hevy_api_key="k", hevy_since="2026-11-01")
        self.workout(u["id"], sets=[{"exercise": "Seated Chest Fly", "weight_kg": 10.0, "reps": 10}])
        c = FakeHevy(fail=hevy.HevyError("HTTP 400 nope", status=400))
        hevy.push_user(self.conn, u, c)
        self.assertEqual(len(c.created_templates), 1)
        hevy.push_user(self.conn, u, c)       # same workout again: the template is in hevy_exercises now
        self.assertEqual(len(c.created_templates), 1)

    def test_sets_without_an_action_name_are_not_sent(self):
        u = self.user(hevy_api_key="k", hevy_since="2026-11-01")
        self.workout(u["id"], sets=[{"exercise": "Unknown", "weight_kg": 10.0, "reps": 10}])
        self.workout(u["id"], when="2026-11-16 10:00:00", sets=[
            {"exercise": "Unknown", "weight_kg": 10.0, "reps": 10}, {"exercise": "Row", "weight_kg": 20.0, "reps": 8}])
        c = FakeHevy({"row": "R"})
        self.assertEqual(hevy.push_user(self.conn, u, c)["pushed"], 1)
        self.assertEqual(len(c.workouts[0]["exercises"]), 1)
        self.assertEqual(hevy.counts(self.conn, u), {"sent": 1, "waiting": 0})

    def test_unexpected_error_in_one_workout_does_not_escape(self):
        u = self.user(hevy_api_key="k", hevy_since="2026-11-01")
        self.workout(u["id"])
        class Boom(FakeHevy):
            def create_workout(self, w):
                raise KeyError("id")
        r = hevy.push_user(self.conn, u, Boom({"row": "R", "press": "P"}))
        self.assertIn("unexpected KeyError", r["error"])

    def test_restart_gives_rejected_workouts_new_attempts(self):
        u = self.user(hevy_api_key="k", hevy_since="2026-11-01")
        self.workout(u["id"])
        self.conn.execute("UPDATE workouts SET hevy_attempts=5")
        accounts.apply_options(self.conn, [{"name": "Alex", "hevy_api_key": "k"}])
        self.assertEqual(self.conn.execute("SELECT hevy_attempts FROM workouts").fetchone()[0], 0)

    def test_alternating_left_right_actions_become_one_block(self):
        u = self.user(hevy_api_key="k", hevy_since="2026-11-01", hevy_actions="Row (L)=Cable Row; Row (R)=Cable Row")
        sets = [{"exercise": n, "weight_kg": 15.0, "reps": 12} for n in ("Row (L)", "Row (R)", "Row (L)", "Row (R)")]
        self.workout(u["id"], sets=sets + [{"exercise": "Press", "weight_kg": 20.0, "reps": 8}])
        c = FakeHevy({"cable row": "CR", "press": "P"})
        hevy.push_user(self.conn, u, c)
        ex = c.workouts[0]["exercises"]
        self.assertEqual([(e["exercise_template_id"], len(e["sets"])) for e in ex], [("CR", 4), ("P", 1)])

    def test_exercise_types(self):
        s = lambda w, r, d=0: {"weight_kg": w, "reps": r, "duration_s": d}  # noqa: E731
        self.assertEqual(hevy.exercise_type([s(10, 5)]), "weight_reps")
        self.assertEqual(hevy.exercise_type([s(0, 5)]), "reps_only")
        self.assertEqual(hevy.exercise_type([s(0, 0, 30)]), "duration")
        self.assertEqual(hevy.exercise_type([s(10, 0, 30)]), "weight_duration")

    def test_sync_all_pushes_after_the_vendor_sync(self):
        u = self.user(innogym_email="a@x.com", innogym_password="pw", hevy_api_key="k", hevy_since="2026-11-01")
        self.workout(u["id"])
        v, h = FakeVendor(), FakeHevy({"row": "R", "press": "P"})
        out = sync.sync_all(self.conn, lambda t: (setattr(v, "token", t), v)[1], make_hevy=lambda key: h)
        self.assertEqual(out["Alex"]["hevy"]["pushed"], 1)
        self.assertIsNone(db.list_users(self.conn)[0]["hevy_last_error"])


if __name__ == "__main__":
    unittest.main()
