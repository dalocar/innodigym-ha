import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "innodigym", "app"))
import stats  # noqa: E402

TODAY = date(2026, 11, 13)  # Friday


def W(started_at, **o):
    return {"id": 1, "started_at": started_at, "name": "A", "duration_s": 600, "kcal": 100, "volume_kg": 1000.0, **o}


def S(wid, started_at, ex, kg, reps=10, idx=0):
    return {"workout_id": wid, "started_at": started_at, "exercise": ex, "weight_kg": kg, "reps": reps, "idx": idx}


class StatsTest(unittest.TestCase):
    def test_week_starts_monday(self):
        self.assertEqual(stats.monday_of(TODAY), date(2026, 11, 9))
        self.assertEqual(stats.monday_of(date(2026, 11, 15)), date(2026, 11, 9))

    def test_week_stats_window(self):
        ws = [W("2026-11-08 20:00:00"), W("2026-11-09 08:00:00"), W("2026-11-15 23:00:00"), W("2026-11-16 00:10:00")]
        self.assertEqual(stats.week_stats(ws, date(2026, 11, 9)),
                         {"n": 2, "volume": 2000.0, "kcal": 200, "seconds": 1200})

    def test_goal_streak(self):
        ws = []
        for mon, n in [("2026-11-02", 3), ("2026-10-26", 3), ("2026-10-19", 2), ("2026-10-12", 3)]:
            y, m, d = map(int, mon.split("-"))
            ws += [W("%04d-%02d-%02d 08:00:00" % (y, m, d + i)) for i in range(n)]
        self.assertEqual(stats.goal_streak(ws, 3, TODAY), 2)
        self.assertEqual(stats.goal_streak(ws, 2, TODAY), 4)
        self.assertEqual(stats.goal_streak([], 3, TODAY), 0)

    def test_new_prs_matches_the_ui_rule(self):
        sets = [S(1, "2026-10-01 08:00:00", "Row", 20), S(1, "2026-10-01 08:00:00", "Row", 20, idx=1),
                S(2, "2026-10-03 08:00:00", "Row", 20),                 # same top: no PR
                S(3, "2026-10-05 08:00:00", "Row", 22.5),               # PR
                S(3, "2026-10-05 08:00:00", "Curl", 10, idx=1),         # first Curl session: no PR
                S(4, "2026-10-07 08:00:00", "Row", 15),
                S(5, "2026-10-09 08:00:00", "Row", 22.5),               # equal: no PR
                S(6, "2026-10-11 08:00:00", "Curl", 12.5)]              # PR
        self.assertEqual(stats.new_prs(sets, 3), [{"exercise": "Row", "weight_kg": 22.5, "previous_kg": 20.0}])
        self.assertEqual(stats.new_prs(sets, 6), [{"exercise": "Curl", "weight_kg": 12.5, "previous_kg": 10.0}])
        for wid in (1, 2, 4, 5):
            self.assertEqual(stats.new_prs(sets, wid), [], wid)
        self.assertEqual(stats.new_prs(sets, 999), [])

    def test_heavier_set_later_in_the_same_workout(self):
        sets = [S(1, "2026-10-01 08:00:00", "Row", 20), S(2, "2026-10-03 08:00:00", "Row", 20),
                S(2, "2026-10-03 08:00:00", "Row", 25, idx=1)]
        self.assertEqual(stats.new_prs(sets, 2)[0]["weight_kg"], 25)

    def test_snapshot(self):
        ws = [W("2026-11-13 07:00:00", name="Rutina A", kcal=400, volume_kg=4500.0), W("2026-11-11 07:00:00")]
        s = stats.snapshot(ws, 3, TODAY)
        self.assertEqual((s["workouts_this_week"], s["volume_this_week_kg"], s["trained_today"], s["last_workout_name"],
                          s["last_workout_date"], s["total_volume_kg"]), (2, 5500.0, "ON", "Rutina A", "2026-11-13", 5500.0))

    def test_snapshot_without_workouts(self):
        s = stats.snapshot([], 3, TODAY)
        self.assertEqual((s["workouts_this_week"], s["trained_today"], s["last_workout_name"], s["last_workout_date"]),
                         (0, "OFF", None, None))


if __name__ == "__main__":
    unittest.main()
