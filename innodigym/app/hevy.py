"""
Pushes finished workouts to Hevy (https://api.hevyapp.com/v1, personal API key, Hevy Pro).

One direction only: InnoGym -> Hevy. A workout is pushed once; its Hevy id is stored on the row so
a later run never duplicates it. Only workouts that started on or after the user's `hevy_since`
date are pushed, so switching Hevy on does not upload the whole history.

Exercises: the InnoGym exercise name is looked up in `hevy_exercises` (user overrides can be put
there), then among the Hevy templates by exact title (case-insensitive), and otherwise a custom
template is created. Weights stay as the machine reports them (per cable for combined exercises).
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import db

BASE = "https://api.hevyapp.com"
MAX_PER_RUN = 20       # workouts pushed per sync run (Hevy allows 1,000 a day)
MAX_ATTEMPTS = 5       # a workout Hevy keeps rejecting is left alone after this many tries
FMT = "%Y-%m-%d %H:%M:%S"


class HevyError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status
        self.auth = status in (401, 403) and "exceeds-custom" not in message
        self.rate = status == 429


class HevyClient:
    def __init__(self, api_key, base=BASE, timeout=20):
        self.key, self.base, self.timeout = api_key, base, timeout

    def _call(self, method, path, body=None):
        req = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"api-key": self.key, "Content-Type": "application/json", "Accept": "application/json"})
        what = "%s %s" % (method, path.split("?")[0])
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw, status, ctype = r.read(), r.status, r.headers.get("Content-Type", "?")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                j = json.loads(e.read() or b"{}")
                detail = j.get("message") or j.get("error") or ""
            except ValueError:
                pass
            raise HevyError("%s: HTTP %d %s" % (what, e.code, detail), status=e.code)
        except (urllib.error.URLError, OSError) as e:
            raise HevyError("%s: network: %s" % (what, getattr(e, "reason", e)))
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            snippet = " ".join(raw[:150].decode("utf-8", "replace").split())
            raise HevyError("%s: HTTP %s, reply was not JSON (%s): %s" % (what, status, ctype, snippet))

    def get(self, path):
        return self._call("GET", path)

    def post(self, path, body):
        return self._call("POST", path, body)

    def templates(self):
        """Every exercise template on the account as {title.lower(): id}."""
        out, page = {}, 1
        while True:
            d = self.get("/v1/exercise_templates?page=%d&pageSize=100" % page)
            for t in d.get("exercise_templates") or []:
                out.setdefault((t.get("title") or "").strip().lower(), str(t["id"]))
            if page >= (d.get("page_count") or 1):
                return out
            page += 1

    def create_template(self, title, exercise_type):
        d = self.post("/v1/exercise_templates", {"exercise": {
            "title": title, "exercise_type": exercise_type,
            "equipment_category": "machine", "muscle_group": "other"}})
        if not isinstance(d, dict) or d.get("id") in (None, ""):
            raise HevyError("POST /v1/exercise_templates: reply has no id: %.150s" % json.dumps(d))
        return str(d["id"])

    def create_workout(self, workout):
        d = self.post("/v1/workouts", {"workout": workout})
        w = d.get("workout")
        w = w[0] if isinstance(w, list) and w else w
        return str((w or {}).get("id") or "")


_TEMPLATE_ID = re.compile(r"^[0-9A-Fa-f]{8}$")


def resolve_template(name, templates, override=None):
    """Hevy template id for an InnoGym exercise name, or None when a custom one has to be created.
    templates: {title.lower(): id}. Order: the user's override (a Hevy title or an 8-character id; an override
    that matches nothing raises HevyError rather than silently creating something else), exact title, then a
    title that differs only by a bracketed qualifier ("Plank" -> "Plank (Weighted)") when exactly one does."""
    if override:
        if _TEMPLATE_ID.match(override) and override not in templates:
            return override
        tid = templates.get(override.strip().lower())
        if not tid:
            raise HevyError('options map "%s" to "%s", which is not a Hevy exercise' % (name, override))
        return tid
    key = name.strip().lower()
    if key in templates:
        return templates[key]
    same = {tid for title, tid in templates.items() if re.sub(r"\s*\([^)]*\)\s*$", "", title) == key}
    return same.pop() if len(same) == 1 else None


def supervisor_timezone(timeout=5):
    """IANA time zone configured in Home Assistant (Supervisor /info), or None outside the add-on."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        req = urllib.request.Request("http://supervisor/info", headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (json.loads(r.read()).get("data") or {}).get("timezone")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def resolve_timezone(configured=None):
    """tzinfo for InnoGym's local timestamps: the `timezone` option, else Home Assistant's, else TZ, else the
    container's own zone (None). Unknown names are ignored with a log line."""
    from zoneinfo import ZoneInfo
    for name in (configured, supervisor_timezone(), os.environ.get("TZ")):
        if name:
            try:
                return ZoneInfo(name.lstrip(":"))
            except Exception:  # noqa: BLE001 - bad name or no tz database
                print("hevy: time zone %r not usable" % name, flush=True)
    return None


def exercise_type(sets):
    """Hevy custom exercise type that fits the sets of one exercise."""
    weighted = any(s["weight_kg"] > 0 for s in sets)
    reps = any(s["reps"] > 0 for s in sets)
    timed = any(s["duration_s"] > 0 for s in sets)
    if weighted and reps:
        return "weight_reps"
    if reps:
        return "reps_only"
    if weighted and timed:
        return "weight_duration"
    return "duration"


def _set(s, kind):
    out = {"type": "normal"}
    if kind in ("weight_reps", "weight_duration"):
        out["weight_kg"] = s["weight_kg"]
    if kind in ("weight_reps", "reps_only"):
        out["reps"] = s["reps"]
    if kind in ("duration", "weight_duration"):
        out["duration_seconds"] = s["duration_s"]
    return out


def _groups(sets):
    """Consecutive sets of the same exercise form one Hevy exercise."""
    groups = []
    for s in sets:
        if groups and groups[-1][0] == s["exercise"]:
            groups[-1][1].append(s)
        else:
            groups.append((s["exercise"], [s]))
    return groups


def _iso(local_text, tz=None):
    dt = datetime.strptime(local_text, FMT)
    dt = dt.astimezone() if tz is None else dt.replace(tzinfo=tz)   # naive local time -> the container's zone
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_workout(w, sets, template_for, tz=None):
    """w: workout row dict, sets: set row dicts in order, template_for(name, sets) -> Hevy template id."""
    start = datetime.strptime(w["started_at"], FMT)
    end = start + timedelta(seconds=max(int(w["duration_s"] or 0), 60))
    exercises = []
    for name, group in _groups(sets):
        tid = template_for(name, group)
        kind = exercise_type(group)
        exercises.append({"exercise_template_id": tid, "sets": [_set(s, kind) for s in group]})
    return {"title": w["name"] or "Workout", "description": "Logged by InnoGym", "is_private": False,
            "start_time": _iso(w["started_at"], tz), "end_time": _iso(end.strftime(FMT), tz),
            "exercises": exercises}


def counts(conn, user):
    """{'sent': workouts already in Hevy, 'waiting': workouts the next run will try}."""
    uid = user["id"]
    sent = conn.execute("SELECT COUNT(*) FROM workouts WHERE user_id=? AND hevy_id IS NOT NULL", (uid,)).fetchone()[0]
    waiting = conn.execute(
        "SELECT COUNT(*) FROM workouts WHERE user_id=? AND hevy_id IS NULL AND deleted_at IS NULL AND started_at >= ?"
        " AND hevy_attempts < ? AND EXISTS (SELECT 1 FROM sets s WHERE s.user_id=workouts.user_id AND s.workout_id=workouts.id)",
        (uid, user["hevy_since"] or "9999-12-31", MAX_ATTEMPTS)).fetchone()[0]
    return {"sent": sent, "waiting": waiting}


def push_user(conn, user, client, tz=None, now=None):
    """Push this user's pending workouts. Never raises. Returns {'pushed', 'error'}."""
    uid = user["id"]
    res = {"pushed": 0, "error": None}
    since = user["hevy_since"] or "9999-12-31"
    pending = conn.execute(
        "SELECT id, started_at, name, duration_s FROM workouts WHERE user_id=? AND hevy_id IS NULL"
        " AND deleted_at IS NULL AND started_at >= ? AND hevy_attempts < ?"
        " AND EXISTS (SELECT 1 FROM sets s WHERE s.user_id=workouts.user_id AND s.workout_id=workouts.id)"
        " ORDER BY started_at LIMIT ?", (uid, since, MAX_ATTEMPTS, MAX_PER_RUN)).fetchall()
    errors = []
    try:
        overrides = json.loads(user["hevy_map"] or "{}")
    except ValueError:
        overrides = {}
    if pending:
        cache = {}

        def template_for(name, group):
            if name in cache:
                return cache[name]
            override = overrides.get(name.strip().lower())
            row = None if override else conn.execute(
                "SELECT template_id FROM hevy_exercises WHERE user_id=? AND exercise=?", (uid, name)).fetchone()
            tid = row[0] if row else None
            if not tid:
                if "templates" not in cache:
                    cache["templates"] = client.templates()
                tid = resolve_template(name, cache["templates"], override) or client.create_template(name, exercise_type(group))
                with conn:
                    conn.execute("INSERT OR REPLACE INTO hevy_exercises (user_id, exercise, template_id) VALUES (?,?,?)",
                                 (uid, name, tid))
            cache[name] = tid
            return tid

        try:
            for w in pending:
                sets = [dict(r) for r in conn.execute(
                    "SELECT exercise, weight_kg, reps, duration_s FROM sets WHERE user_id=? AND workout_id=? ORDER BY idx",
                    (uid, w["id"]))]
                try:
                    hid = client.create_workout(build_workout(dict(w), sets, template_for, tz))
                except HevyError as e:
                    if e.auth or e.rate:
                        raise
                    errors.append("%s: %s" % (w["started_at"][:10], e))
                    with conn:
                        conn.execute("UPDATE workouts SET hevy_attempts=hevy_attempts+1, hevy_error=? WHERE user_id=? AND id=?",
                                     (str(e), uid, w["id"]))
                    continue
                except Exception as e:  # noqa: BLE001 - one odd workout must not stop the others or the worker
                    errors.append("%s: unexpected %s: %s" % (w["started_at"][:10], type(e).__name__, e))
                    with conn:
                        conn.execute("UPDATE workouts SET hevy_attempts=hevy_attempts+1, hevy_error=? WHERE user_id=? AND id=?",
                                     (errors[-1], uid, w["id"]))
                    continue
                with conn:
                    conn.execute("UPDATE workouts SET hevy_id=?, hevy_error=NULL WHERE user_id=? AND id=?",
                                 (hid or "unknown", uid, w["id"]))
                res["pushed"] += 1
        except HevyError as e:
            errors.append(("api key rejected: " if e.auth else "rate limited: " if e.rate else "") + str(e))
    res["error"] = "; ".join(errors[:3]) or None
    with conn:
        conn.execute("UPDATE users SET hevy_last_run=?, hevy_last_error=? WHERE id=?", (db._now(), res["error"], uid))
    return res
