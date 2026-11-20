"""
The add-on's own JSON API (`local/*`), separate from the vendor proxy (`api/*`).
Pure functions over a database connection so they can be tested without HTTP.

Every call acts on one user, resolved by server.py from the Home Assistant ingress
headers. A user can only ever read or change their own rows. Vendor tokens are never
returned.
"""
from __future__ import annotations

import re
from datetime import datetime

import backup
import db
import hevy
import sync

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def _bad(msg):
    return ApiError(400, msg)


def _date(v, field="date"):
    if not isinstance(v, str) or not DATE_RE.match(v):
        raise _bad("%s must be YYYY-MM-DD" % field)
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        raise _bad("%s is not a real date" % field)
    return v


def _num(v, field, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= v <= hi):
        raise _bad("%s must be a number between %s and %s" % (field, lo, hi))
    return v


def _int(v, field, lo, hi):
    if isinstance(v, bool) or not isinstance(v, int) or not (lo <= v <= hi):
        raise _bad("%s must be a whole number between %d and %d" % (field, lo, hi))
    return v


def _text(v, field, max_len, required=False):
    if v is None:
        v = ""
    if not isinstance(v, str) or len(v) > max_len or (required and not v.strip()):
        raise _bad("%s must be text of 1 to %d characters" % (field, max_len) if required
                   else "%s must be text of at most %d characters" % (field, max_len))
    return v.strip() if required else v


def _me(conn, user):
    last = db.last_sync(conn, user["id"])
    return {"id": user["id"], "name": user["name"], "goalPerWeek": user["goal_per_week"],
            "language": user["language"], "vendorLinked": bool(user["token"]),
            "vendorEmail": user["vendor_email"], "lastSync": last,
            "managed": bool(user["managed"]),
            "hevy": {"linked": bool(user["hevy_api_key"]), "since": user["hevy_since"],
                     "lastRun": user["hevy_last_run"], "lastError": user["hevy_last_error"],
                     **hevy.counts(conn, user)}}


def _workout_id(parts, i):
    try:
        return int(parts[i])
    except (IndexError, ValueError):
        raise ApiError(404, "not found")


def handle(conn, user, method, parts, query, body, ctx):
    """parts: path after 'local/', split on '/'. query: dict of strings. body: parsed JSON or None.
    ctx: object with .trigger() (run a sync soon) and .login(email, password) -> token.
    Returns (status, jsonable)."""
    uid = user["id"]
    key = (method, parts[0] if parts else "")

    if key == ("GET", "me") and len(parts) == 1:
        return 200, _me(conn, user)

    if key == ("PUT", "me") and len(parts) == 1:
        goal = _int((body or {}).get("goalPerWeek"), "goalPerWeek", 1, 14)
        db.set_goal(conn, uid, goal)
        return 200, _me(conn, db.get_user(conn, uid))

    if key == ("POST", "me") and parts[1:] == ["vendor-login"]:
        b = body or {}
        email, password = _text(b.get("email"), "email", 200, True), _text(b.get("password"), "password", 200, True)
        try:
            token = ctx.login(email, password)
        except sync.VendorError as e:
            raise ApiError(401 if e.auth else 502, "vendor login failed: %s" % e)
        db.set_token(conn, uid, token, email)
        ctx.trigger()
        return 200, _me(conn, db.get_user(conn, uid))

    if key == ("POST", "me") and parts[1:] == ["vendor-token"]:
        # for accounts that can no longer log in but still hold a working token
        token = _text((body or {}).get("token"), "token", 4000, True)
        db.set_token(conn, uid, token)
        ctx.trigger()
        return 200, _me(conn, db.get_user(conn, uid))

    if key == ("GET", "sync") and len(parts) == 1:
        return 200, {"last": db.last_sync(conn, uid), "vendorLinked": bool(user["token"])}

    if key == ("POST", "sync") and len(parts) == 1:
        ctx.trigger()
        return 202, {"queued": True}

    if key == ("GET", "workouts") and len(parts) == 1:
        since = _date(query["since"], "since") if query.get("since") else None
        limit = _int(int(query["limit"]), "limit", 1, 2000) if query.get("limit", "").isdigit() else None
        return 200, db.list_workouts(conn, uid, since, limit)

    if key == ("POST", "workouts") and len(parts) == 1:
        b = body or {}
        date = _date(b.get("date"))
        clock = b.get("time") or "12:00:00"
        if not isinstance(clock, str) or not TIME_RE.match(clock):
            raise _bad("time must be HH:MM or HH:MM:SS")
        raw_sets = b.get("sets")
        if not isinstance(raw_sets, list) or not raw_sets or len(raw_sets) > 200:
            raise _bad("sets must be a list of 1 to 200 sets")
        sets = []
        for i, s in enumerate(raw_sets):
            if not isinstance(s, dict):
                raise _bad("set %d is not an object" % (i + 1))
            sets.append({"exercise": _text(s.get("exercise"), "exercise", 80, True),
                         "weight_kg": _num(s.get("weightKg"), "weightKg", 0, 1000),
                         "reps": _int(s.get("reps"), "reps", 1, 1000),
                         "duration_s": _int(s.get("durationS", 0), "durationS", 0, 3600)})
        name = _text(b.get("name") or "Manual workout", "name", 80, True)
        wid = db.add_manual_workout(conn, uid, date + " " + (clock if len(clock) > 5 else clock + ":00"), name, sets,
                                    duration_s=sum(s["duration_s"] for s in sets))
        return 201, db.get_workout(conn, uid, wid)

    if parts and parts[0] == "workouts" and len(parts) >= 2:
        wid = _workout_id(parts, 1)
        w = db.get_workout(conn, uid, wid)
        if w is None:
            raise ApiError(404, "workout not found")
        if len(parts) == 2 and method == "GET":
            return 200, w
        if len(parts) == 2 and method == "DELETE":
            db.delete_workout(conn, uid, wid)
            return 200, {"deleted": wid}
        if parts[2:] == ["note"] and method == "PUT":
            b = body or {}
            rpe = b.get("rpe")
            if rpe is not None:
                _int(rpe, "rpe", 1, 10)
            db.set_note(conn, uid, wid, _text(b.get("text"), "text", 2000), rpe)
            return 200, db.get_workout(conn, uid, wid)

    if key == ("POST", "import") and len(parts) == 1:
        files = (body or {}).get("files")
        if not isinstance(files, list) or not files or len(files) > 100:
            raise _bad("files must be a list of 1 to 100 parsed JSON documents")
        return 200, backup.import_docs(conn, uid, files)

    if key == ("GET", "sets") and len(parts) == 1:
        since = _date(query["since"], "since") if query.get("since") else None
        return 200, db.all_sets(conn, uid, since)

    if key == ("GET", "exercises") and len(parts) == 1:
        return 200, db.list_exercises(conn, uid)

    if parts[:1] == ["exercises"] and len(parts) == 2 and method == "GET":
        from urllib.parse import unquote
        return 200, db.exercise_history(conn, uid, unquote(parts[1]))

    if key == ("GET", "body-weight") and len(parts) == 1:
        return 200, db.list_body_weight(conn, uid)

    if key == ("POST", "body-weight") and len(parts) == 1:
        b = body or {}
        db.set_body_weight(conn, uid, _date(b.get("date")), _num(b.get("kg"), "kg", 20, 400))
        return 201, db.list_body_weight(conn, uid)

    raise ApiError(404, "not found")
