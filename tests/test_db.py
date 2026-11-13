import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "innodigym", "app"))
import db  # noqa: E402

REC = {"sportRecordId": 100, "startTime": "2026-10-28 08:49:29", "name": "Rutina B",
       "coreData": 65720.0, "consume": 394, "duration": 961, "coins": 39, "extData": None}
EXT = [
    {"actionId": 512, "actionName": "Seated Chest Fly", "sort": 2, "weight": 150, "count": 8, "duration": 29},
    {"actionId": 512, "actionName": "Seated Chest Fly", "sort": 1, "weight": 150, "count": 12, "duration": 42},
]


class DbTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.dir.name, "t.db"))
        self.alex = db.create_user(self.conn, "Alex", ha_user_id="ha-1")
        self.sam = db.create_user(self.conn, "Sam", ha_user_id="ha-2")

    def tearDown(self):
        self.conn.close()
        self.dir.cleanup()

    def test_migrate_is_idempotent_and_versioned(self):
        db.migrate(self.conn)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)

    def test_newer_database_is_refused(self):
        self.conn.execute("PRAGMA user_version = 99")
        with self.assertRaises(RuntimeError):
            db.migrate(self.conn)

    def test_upsert_converts_units_and_orders_sets(self):
        self.assertTrue(db.upsert_vendor_record(self.conn, self.alex, REC, EXT))
        w = db.get_workout(self.conn, self.alex, 100)
        self.assertEqual(w["volume_kg"], 6572.0)          # coreData is tenths of kg
        self.assertEqual([s["reps"] for s in w["sets"]], [12, 8])  # ordered by vendor `sort`
        self.assertEqual(w["sets"][0]["weight_kg"], 15.0)  # 150 tenths -> 15 kg
        self.assertEqual(db.sets_volume_kg(w), 300.0)

    def test_resync_does_not_duplicate_or_wipe(self):
        db.upsert_vendor_record(self.conn, self.alex, REC, EXT)
        db.set_note(self.conn, self.alex, 100, "shoulder tight", 7)
        self.assertFalse(db.upsert_vendor_record(self.conn, self.alex, dict(REC, consume=400)))  # list only
        rows = db.list_workouts(self.conn, self.alex)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kcal"], 400)
        self.assertEqual(rows[0]["set_count"], 2)          # sets survive a list-only refresh
        self.assertEqual((rows[0]["note"], rows[0]["rpe"]), ("shoulder tight", 7))

    def test_deleted_workout_stays_deleted_after_sync(self):
        db.upsert_vendor_record(self.conn, self.alex, REC, EXT)
        db.delete_workout(self.conn, self.alex, 100)
        db.upsert_vendor_record(self.conn, self.alex, REC, EXT)
        self.assertEqual(db.list_workouts(self.conn, self.alex), [])
        self.assertIsNone(db.get_workout(self.conn, self.alex, 100))

    def test_users_are_isolated_even_with_same_record_id(self):
        db.upsert_vendor_record(self.conn, self.alex, REC, EXT)
        db.upsert_vendor_record(self.conn, self.sam, dict(REC, name="Other"), EXT)
        self.assertEqual(db.list_workouts(self.conn, self.alex)[0]["name"], "Rutina B")
        self.assertEqual(db.list_workouts(self.conn, self.sam)[0]["name"], "Other")
        db.delete_workout(self.conn, self.alex, 100)
        self.assertEqual(len(db.list_workouts(self.conn, self.sam)), 1)

    def test_manual_workouts_get_distinct_negative_ids(self):
        db.upsert_vendor_record(self.conn, self.alex, REC)
        a = db.add_manual_workout(self.conn, self.alex, "2026-11-01 18:00:00", "Free weights",
                                  [{"exercise": "Bench Press", "weight_kg": 40, "reps": 10}])
        b = db.add_manual_workout(self.conn, self.alex, "2026-11-02 18:00:00", "Free weights", [])
        self.assertEqual((a, b), (-1, -2))
        self.assertEqual(db.get_workout(self.conn, self.alex, a)["volume_kg"], 400.0)
        self.assertEqual(db.known_record_ids(self.conn, self.alex), {100})   # manual ids are not vendor ids

    def test_ids_without_sets(self):
        db.upsert_vendor_record(self.conn, self.alex, REC)
        db.upsert_vendor_record(self.conn, self.alex, dict(REC, sportRecordId=101), EXT)
        self.assertEqual(db.ids_without_sets(self.conn, self.alex), [100])

    def test_rpe_range_enforced(self):
        db.upsert_vendor_record(self.conn, self.alex, REC)
        with self.assertRaises(sqlite3.IntegrityError):
            db.set_note(self.conn, self.alex, 100, "x", 11)

    def test_body_weight_upserts_per_day(self):
        db.set_body_weight(self.conn, self.alex, "2026-11-09", 76.8)
        db.set_body_weight(self.conn, self.alex, "2026-11-09", 76.5)
        db.set_body_weight(self.conn, self.alex, "2026-10-26", 77.2)
        self.assertEqual(db.list_body_weight(self.conn, self.alex),
                         [{"date": "2026-10-26", "kg": 77.2}, {"date": "2026-11-09", "kg": 76.5}])

    def test_since_filter_and_limit(self):
        for i, d in enumerate(["2026-10-01", "2026-10-15", "2026-11-01"]):
            db.upsert_vendor_record(self.conn, self.alex, dict(REC, sportRecordId=i + 1, startTime=d + " 08:00:00"))
        self.assertEqual([w["id"] for w in db.list_workouts(self.conn, self.alex, since="2026-10-10")], [3, 2])
        self.assertEqual(len(db.list_workouts(self.conn, self.alex, limit=1)), 1)

    def test_sync_log(self):
        self.assertIsNone(db.last_sync(self.conn, self.alex))
        db.log_sync(self.conn, self.alex, fetched=5, inserted=1)
        db.log_sync(self.conn, self.alex, error="HTTP 401")
        self.assertEqual(db.last_sync(self.conn, self.alex)["error"], "HTTP 401")


if __name__ == "__main__":
    unittest.main()
