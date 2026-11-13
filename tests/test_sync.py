import os
import sys
import tempfile
import unittest
import urllib.parse
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "innodigym", "app"))
import db  # noqa: E402
import sync  # noqa: E402

NOW = datetime(2026, 11, 13, 9, 0, 0)


def rec(i, start, name="Rutina A"):
    return {"sportRecordId": i, "startTime": start, "name": name, "coreData": 1000.0,
            "consume": 100, "duration": 600, "extData": None}


def ext(n=2):
    return [{"actionId": 1, "actionName": "Row", "sort": k, "weight": 200, "count": 10, "duration": 30} for k in range(n)]


class FakeVendor:
    """Answers the two read endpoints from in-memory data and records every call."""

    def __init__(self, records, fail_detail=(), auth_fail=False):
        self.records, self.calls = records, []
        self.fail_detail, self.auth_fail = set(fail_detail), auth_fail

    def get(self, path):
        self.calls.append(path)
        if self.auth_fail:
            raise sync.VendorError("HTTP 401", auth=True)
        url = urllib.parse.urlparse(path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path == "/sport/record/list":
            rows = [r for r in self.records if q["startTime"] <= r["startTime"] <= q["endTime"]]
            rows.sort(key=lambda r: r["startTime"], reverse=True)
            return {"data": rows[:int(q["size"])]}
        if url.path == "/sport/record/detail":
            rid = int(q["recordId"])
            if rid in self.fail_detail:
                raise sync.VendorError("system error")
            r = next(r for r in self.records if r["sportRecordId"] == rid)
            return {"data": dict(r, name=None, extData=ext())}
        raise AssertionError(path)

    def detail_calls(self):
        return [c for c in self.calls if c.startswith("/sport/record/detail")]


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "t.db")
        self.conn = db.connect(self.path)
        self.uid = db.create_user(self.conn, "Alex", token="tok")
        self.user = db.list_users(self.conn)[0]

    def tearDown(self):
        self.conn.close()
        self.dir.cleanup()

    def test_first_run_imports_everything_with_sets(self):
        v = FakeVendor([rec(1, "2026-09-01 08:00:00"), rec(2, "2026-10-01 08:00:00"), rec(3, "2026-11-12 08:00:00")])
        r = sync.sync_user(self.conn, self.user, v, NOW)
        self.assertEqual((r["fetched"], r["inserted"], r["details"], r["error"]), (3, 3, 3, None))
        w = db.get_workout(self.conn, self.uid, 1)
        self.assertEqual((len(w["sets"]), w["sets"][0]["weight_kg"], w["name"]), (2, 20.0, "Rutina A"))  # list name kept
        self.assertEqual(db.last_sync(self.conn, self.uid)["inserted"], 3)

    def test_second_run_adds_nothing_and_only_refreshes_recent_details(self):
        v = FakeVendor([rec(1, "2026-09-01 08:00:00"), rec(3, "2026-11-12 08:00:00")])
        sync.sync_user(self.conn, self.user, v, NOW)
        v.calls.clear()
        r = sync.sync_user(self.conn, self.user, v, NOW)
        self.assertEqual((r["fetched"], r["inserted"]), (1, 0))   # window starts 7 days before the newest
        self.assertEqual(len(v.detail_calls()), 1)                # only the one inside the look-back window
        self.assertEqual(len(db.list_workouts(self.conn, self.uid)), 2)

    def test_new_workout_arrives_on_next_run(self):
        v = FakeVendor([rec(1, "2026-11-10 08:00:00")])
        sync.sync_user(self.conn, self.user, v, NOW)
        v.records.append(rec(2, "2026-11-13 07:30:00"))
        r = sync.sync_user(self.conn, self.user, v, NOW)
        self.assertEqual(r["inserted"], 1)
        self.assertEqual(db.get_workout(self.conn, self.uid, 2)["sets"][0]["exercise"], "Row")

    def test_local_delete_survives_sync(self):
        v = FakeVendor([rec(1, "2026-11-12 08:00:00")])
        sync.sync_user(self.conn, self.user, v, NOW)
        db.delete_workout(self.conn, self.uid, 1)
        sync.sync_user(self.conn, self.user, v, NOW)
        self.assertEqual(db.list_workouts(self.conn, self.uid), [])

    def test_vendor_dropping_a_record_keeps_the_local_copy(self):
        v = FakeVendor([rec(1, "2026-11-12 08:00:00")])
        sync.sync_user(self.conn, self.user, v, NOW)
        v.records.clear()
        sync.sync_user(self.conn, self.user, v, NOW)
        self.assertEqual(len(db.list_workouts(self.conn, self.uid)), 1)

    def test_detail_failure_is_logged_and_retried_next_run(self):
        v = FakeVendor([rec(1, "2026-09-01 08:00:00"), rec(2, "2026-09-02 08:00:00")], fail_detail=[2])
        r = sync.sync_user(self.conn, self.user, v, NOW)
        self.assertEqual((r["inserted"], r["details"]), (2, 1))
        self.assertIn("detail 2", r["error"])
        self.assertEqual(db.ids_without_sets(self.conn, self.uid), [2])
        v.fail_detail.clear()
        r = sync.sync_user(self.conn, self.user, v, NOW)       # old record, outside the window, still gets filled
        self.assertEqual((r["details"], r["error"]), (1, None))
        self.assertEqual(db.ids_without_sets(self.conn, self.uid), [])

    def test_auth_failure_stops_and_is_flagged(self):
        r = sync.sync_user(self.conn, self.user, FakeVendor([], auth_fail=True), NOW)
        self.assertTrue(r["error"].startswith("auth:"))
        self.assertEqual(db.last_sync(self.conn, self.uid)["error"], r["error"])

    def test_full_pages_are_followed_backwards(self):
        records = [rec(i, "2026-10-%02d %02d:00:00" % (1 + i // 24, i % 24)) for i in range(1, 8)]
        old = sync.PAGE_SIZE
        sync.PAGE_SIZE = 3
        try:
            got = sync.fetch_records(FakeVendor(records), sync.FAR_PAST, "2026-12-01 00:00:00")
        finally:
            sync.PAGE_SIZE = old
        self.assertEqual(sorted(r["sportRecordId"] for r in got), list(range(1, 8)))

    def test_sync_all_skips_users_without_token_and_isolates_users(self):
        sam = db.create_user(self.conn, "Sam")                  # no token
        made = []
        out = sync.sync_all(self.conn, lambda tok: made.append(tok) or FakeVendor([rec(9, "2026-11-12 08:00:00")]))
        self.assertEqual(list(out), ["Alex"])
        self.assertEqual(made, ["tok"])
        self.assertEqual(db.list_workouts(self.conn, sam), [])

    def test_worker_runs_and_stops(self):
        w = sync.SyncWorker(self.path, 60, make_client=lambda tok: FakeVendor([rec(5, "2026-11-12 08:00:00")]))
        w.start()
        for _ in range(100):
            if db.last_sync(self.conn, self.uid):
                break
            w.join(0.05)
        w.stop()
        w.join(2)
        self.assertFalse(w.is_alive())
        self.assertEqual(db.last_sync(self.conn, self.uid)["inserted"], 1)


if __name__ == "__main__":
    unittest.main()
