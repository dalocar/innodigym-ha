"""
Training statistics for the Home Assistant sensors. Mirrors static/training-core.js (weeks start on
Monday; a personal record is a strictly heavier top weight for an exercise than in any earlier
workout, and a first-ever session is not one). Pure functions over rows from db.py.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta


def _day(w):
    return w["started_at"][:10]


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_stats(workouts, monday: date) -> dict:
    a, b = monday.isoformat(), (monday + timedelta(days=6)).isoformat()
    ws = [w for w in workouts if a <= _day(w) <= b]
    return {"n": len(ws), "volume": round(sum(w["volume_kg"] for w in ws), 1),
            "kcal": sum(w["kcal"] for w in ws), "seconds": sum(w["duration_s"] for w in ws)}


def goal_streak(workouts, goal: int, today: date) -> int:
    """Consecutive finished weeks (before the current one) with at least `goal` workouts."""
    m, n = monday_of(today) - timedelta(days=7), 0
    while n < 520 and week_stats(workouts, m)["n"] >= goal:
        n += 1
        m -= timedelta(days=7)
    return n


def new_prs(sets, workout_id):
    """Exercises where `workout_id` set a personal record: [{'exercise', 'weight_kg', 'previous_kg'}]."""
    order = sorted(sets, key=lambda s: (s["started_at"], s["workout_id"], s["idx"]))
    tops = {}                                   # (workout, exercise) -> heaviest set in it
    for s in order:
        k = (s["workout_id"], s["exercise"])
        tops[k] = max(tops.get(k, 0.0), s["weight_kg"])
    best, out = {}, []                          # exercise -> heaviest weight in earlier workouts
    for (wid, ex), top in tops.items():         # dicts keep the chronological insertion order
        prev = best.get(ex)
        if wid == workout_id and prev is not None and top > prev:
            out.append({"exercise": ex, "weight_kg": top, "previous_kg": prev})
        best[ex] = max(prev or 0.0, top)
    return out


def snapshot(workouts, goal: int, today: date) -> dict:
    """Everything the per-user sensors show. `workouts` is newest first (db.list_workouts)."""
    cur = week_stats(workouts, monday_of(today))
    last = workouts[0] if workouts else None
    return {
        "workouts_this_week": cur["n"],
        "volume_this_week_kg": cur["volume"],
        "energy_this_week_kcal": cur["kcal"],
        "goal_per_week": goal,
        "goal_streak_weeks": goal_streak(workouts, goal, today),
        "last_workout_name": last["name"] if last else None,
        "last_workout_date": _day(last) if last else None,
        "last_workout_kcal": last["kcal"] if last else None,
        "last_workout_volume_kg": last["volume_kg"] if last else None,
        "total_volume_kg": round(sum(w["volume_kg"] for w in workouts), 1),
        "trained_today": "ON" if any(_day(w) == today.isoformat() for w in workouts) else "OFF",
    }
