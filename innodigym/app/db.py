"""
Local persistence for the Home Assistant add-on: one SQLite file, standard
library only. Every table carries user_id so several people can share it.

Units: weights are stored in kg (the vendor sends tenths of a kg), durations in
seconds, timestamps as the vendor's local "YYYY-MM-DD HH:MM:SS" text.

Vendor rows are keyed by the vendor's own record id, so re-importing the same
record updates it instead of duplicating it. Manual workouts get negative ids.
A workout deleted in the UI is soft-deleted (deleted_at) so a later sync does
not bring it back.
"""
from __future__ import annotations

import json
import sqlite3
import time

SCHEMA_VERSION = 2

_MIGRATIONS = {
    1: """
    CREATE TABLE users (
        id            INTEGER PRIMARY KEY,
        name          TEXT NOT NULL,
        ha_user_id    TEXT UNIQUE,          -- Home Assistant user, from ingress headers
        vendor_email  TEXT,
        token         TEXT,                 -- vendor bearer token, never sent to the browser
        goal_per_week INTEGER NOT NULL DEFAULT 3,
        language      TEXT NOT NULL DEFAULT 'en',
        created_at    TEXT NOT NULL
    );
    CREATE TABLE workouts (
        user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        id         INTEGER NOT NULL,        -- vendor sportRecordId, negative for manual
        started_at TEXT NOT NULL,
        name       TEXT NOT NULL,
        duration_s INTEGER NOT NULL DEFAULT 0,
        kcal       INTEGER NOT NULL DEFAULT 0,
        volume_kg  REAL NOT NULL DEFAULT 0, -- vendor total (coreData / 10); see sets_volume_kg()
        source     TEXT NOT NULL DEFAULT 'vendor',
        raw_json   TEXT,                    -- untouched vendor record, so no field is ever lost
        deleted_at TEXT,
        PRIMARY KEY (user_id, id)
    );
    CREATE INDEX workouts_by_date ON workouts (user_id, started_at);
    CREATE TABLE sets (
        user_id     INTEGER NOT NULL,
        workout_id  INTEGER NOT NULL,
        idx         INTEGER NOT NULL,
        action_id   INTEGER,
        exercise    TEXT NOT NULL,
        weight_kg   REAL NOT NULL DEFAULT 0, -- as the machine reports it (per cable if combined)
        reps        INTEGER NOT NULL DEFAULT 0,
        duration_s  INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, workout_id, idx),
        FOREIGN KEY (user_id, workout_id) REFERENCES workouts(user_id, id) ON DELETE CASCADE
    );
    CREATE INDEX sets_by_exercise ON sets (user_id, exercise);
    CREATE TABLE notes (
        user_id    INTEGER NOT NULL,
        workout_id INTEGER NOT NULL,
        rpe        INTEGER CHECK (rpe IS NULL OR rpe BETWEEN 1 AND 10),
        text       TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (user_id, workout_id),
        FOREIGN KEY (user_id, workout_id) REFERENCES workouts(user_id, id) ON DELETE CASCADE
    );
    CREATE TABLE body_weight (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        date    TEXT NOT NULL,
        kg      REAL NOT NULL,
        PRIMARY KEY (user_id, date)
    );
    CREATE TABLE sync_log (
        id       INTEGER PRIMARY KEY,
        user_id  INTEGER REFERENCES users(id) ON DELETE CASCADE,
        ts       TEXT NOT NULL,
        fetched  INTEGER NOT NULL DEFAULT 0,
        inserted INTEGER NOT NULL DEFAULT 0,
        error    TEXT
    );
    """,
    2: """
    ALTER TABLE users ADD COLUMN ha_username TEXT;       -- HA login name, to link an options user to the panel visitor
    ALTER TABLE users ADD COLUMN vendor_password TEXT;   -- only when set in the add-on options; lets the worker re-login
    ALTER TABLE users ADD COLUMN hevy_api_key TEXT;
    ALTER TABLE users ADD COLUMN hevy_since TEXT;        -- only workouts from this date on are pushed to Hevy
    ALTER TABLE users ADD COLUMN hevy_last_run TEXT;
    ALTER TABLE users ADD COLUMN hevy_last_error TEXT;
    ALTER TABLE users ADD COLUMN managed INTEGER NOT NULL DEFAULT 0;  -- 1 when defined in the add-on options
    ALTER TABLE workouts ADD COLUMN hevy_id TEXT;
    ALTER TABLE workouts ADD COLUMN hevy_attempts INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE workouts ADD COLUMN hevy_error TEXT;
    CREATE TABLE hevy_exercises (
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        exercise    TEXT NOT NULL,           -- InnoGym exercise name
        template_id TEXT NOT NULL,           -- Hevy exercise template id
        PRIMARY KEY (user_id, exercise)
    );
    """,
}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str) -> sqlite3.Connection:
    """Open (creating if needed) the database and bring it to SCHEMA_VERSION."""
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            "database is version %d, this build understands up to %d" % (version, SCHEMA_VERSION)
        )
    for v in range(version + 1, SCHEMA_VERSION + 1):
        # executescript commits first, so run the DDL and the version bump together
        conn.executescript("BEGIN;" + _MIGRATIONS[v] + "PRAGMA user_version = %d; COMMIT;" % v)


# ---- users -----------------------------------------------------------------

def create_user(conn, name, ha_user_id=None, vendor_email=None, token=None,
                goal_per_week=3, language="en") -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO users (name, ha_user_id, vendor_email, token, goal_per_week, language, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (name, ha_user_id, vendor_email, token, goal_per_week, language, _now()),
        )
    return cur.lastrowid


def get_user_by_ha_id(conn, ha_user_id):
    return conn.execute("SELECT * FROM users WHERE ha_user_id = ?", (ha_user_id,)).fetchone()


def get_user(conn, user_id):
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_or_create_ha_user(conn, ha_user_id, name, username=None):
    """The person behind a Home Assistant login. Created on first visit, unless an add-on options user
    names this HA username, in which case that user is linked to the login."""
    u = get_user_by_ha_id(conn, ha_user_id)
    if u:
        return u
    if username:
        u = conn.execute("SELECT * FROM users WHERE lower(ha_username) = lower(?) AND ha_user_id IS NULL",
                         (username,)).fetchone()
        if u:
            with conn:
                conn.execute("UPDATE users SET ha_user_id = ? WHERE id = ?", (ha_user_id, u["id"]))
            return get_user(conn, u["id"])
    uid = create_user(conn, name or "User", ha_user_id=ha_user_id)
    if username:
        with conn:
            conn.execute("UPDATE users SET ha_username = ? WHERE id = ?", (username, uid))
    return get_user(conn, uid)


def set_goal(conn, user_id, goal_per_week):
    with conn:
        conn.execute("UPDATE users SET goal_per_week = ? WHERE id = ?", (int(goal_per_week), user_id))


def list_users(conn):
    return conn.execute("SELECT * FROM users ORDER BY id").fetchall()


def set_token(conn, user_id, token, vendor_email=None):
    with conn:
        conn.execute("UPDATE users SET token = ?, vendor_email = COALESCE(?, vendor_email) WHERE id = ?",
                     (token, vendor_email, user_id))


# ---- vendor records --------------------------------------------------------

def _kg(tenths) -> float:
    return round((tenths or 0) / 10.0, 1)


def upsert_vendor_record(conn, user_id, record, ext_data=None) -> bool:
    """Store one record from sport/record/list (and its sets from
    sport/record/detail `extData`, when known). Returns True if it was new.

    Existing notes and a soft-delete mark are kept. Sets are only replaced when
    ext_data is given, so a list-only refresh never wipes them."""
    rid = int(record["sportRecordId"])
    with conn:
        is_new = conn.execute("SELECT 1 FROM workouts WHERE user_id=? AND id=?", (user_id, rid)).fetchone() is None
        conn.execute(
            """INSERT INTO workouts (user_id, id, started_at, name, duration_s, kcal, volume_kg, source, raw_json)
               VALUES (?,?,?,?,?,?,?, 'vendor', ?)
               ON CONFLICT (user_id, id) DO UPDATE SET
                 started_at=excluded.started_at, name=excluded.name, duration_s=excluded.duration_s,
                 kcal=excluded.kcal, volume_kg=excluded.volume_kg, raw_json=excluded.raw_json""",
            (user_id, rid, record["startTime"], record.get("name") or "Workout",
             int(record.get("duration") or 0), int(record.get("consume") or 0),
             _kg(record.get("coreData")), json.dumps(record, ensure_ascii=False)),
        )
        if ext_data:
            conn.execute("DELETE FROM sets WHERE user_id=? AND workout_id=?", (user_id, rid))
            for i, e in enumerate(sorted(ext_data, key=lambda e: e.get("sort") or 0)):
                conn.execute(
                    "INSERT INTO sets (user_id, workout_id, idx, action_id, exercise, weight_kg, reps, duration_s)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (user_id, rid, i, e.get("actionId"), e.get("actionName") or "Unknown",
                     _kg(e.get("weight")), int(e.get("count") or 0), int(e.get("duration") or 0)),
                )
    return is_new


def known_record_ids(conn, user_id) -> set:
    return {r[0] for r in conn.execute("SELECT id FROM workouts WHERE user_id=? AND id > 0", (user_id,))}


def ids_without_sets(conn, user_id) -> list:
    """Vendor workouts whose detail was never fetched (the sync worker fills these in)."""
    return [r[0] for r in conn.execute(
        "SELECT w.id FROM workouts w WHERE w.user_id=? AND w.id > 0 AND w.deleted_at IS NULL"
        " AND NOT EXISTS (SELECT 1 FROM sets s WHERE s.user_id=w.user_id AND s.workout_id=w.id)"
        " ORDER BY w.started_at", (user_id,))]


# ---- manual workouts, notes, deletion --------------------------------------

def add_manual_workout(conn, user_id, started_at, name, sets, duration_s=0, kcal=0) -> int:
    """sets: iterable of dicts {exercise, weight_kg, reps, duration_s?}."""
    sets = list(sets)
    with conn:
        wid = (conn.execute("SELECT MIN(id) FROM workouts WHERE user_id=?", (user_id,)).fetchone()[0] or 0)
        wid = min(wid, 0) - 1
        conn.execute(
            "INSERT INTO workouts (user_id, id, started_at, name, duration_s, kcal, volume_kg, source)"
            " VALUES (?,?,?,?,?,?,?, 'manual')",
            (user_id, wid, started_at, name, duration_s, kcal,
             round(sum(s["weight_kg"] * s["reps"] for s in sets), 1)),
        )
        for i, s in enumerate(sets):
            conn.execute(
                "INSERT INTO sets (user_id, workout_id, idx, action_id, exercise, weight_kg, reps, duration_s)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (user_id, wid, i, s.get("action_id"), s["exercise"], s["weight_kg"], s["reps"], s.get("duration_s", 0)),
            )
    return wid


def set_note(conn, user_id, workout_id, text="", rpe=None):
    with conn:
        conn.execute(
            "INSERT INTO notes (user_id, workout_id, rpe, text) VALUES (?,?,?,?)"
            " ON CONFLICT (user_id, workout_id) DO UPDATE SET rpe=excluded.rpe, text=excluded.text",
            (user_id, workout_id, rpe, text),
        )


def delete_workout(conn, user_id, workout_id):
    with conn:
        conn.execute("UPDATE workouts SET deleted_at=? WHERE user_id=? AND id=?", (_now(), user_id, workout_id))


# ---- reads -----------------------------------------------------------------

def list_workouts(conn, user_id, since=None, limit=None):
    """Newest first; `since` is a 'YYYY-MM-DD' lower bound. Includes set count and note."""
    sql = ("SELECT w.id, w.started_at, w.name, w.duration_s, w.kcal, w.volume_kg, w.source,"
           " (SELECT COUNT(*) FROM sets s WHERE s.user_id=w.user_id AND s.workout_id=w.id) AS set_count,"
           " n.rpe, n.text AS note"
           " FROM workouts w LEFT JOIN notes n ON n.user_id=w.user_id AND n.workout_id=w.id"
           " WHERE w.user_id=? AND w.deleted_at IS NULL")
    args = [user_id]
    if since:
        sql += " AND w.started_at >= ?"
        args.append(since)
    sql += " ORDER BY w.started_at DESC, w.id DESC"
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    return [dict(r) for r in conn.execute(sql, args)]


def get_workout(conn, user_id, workout_id):
    w = conn.execute(
        "SELECT w.id, w.started_at, w.name, w.duration_s, w.kcal, w.volume_kg, w.source, n.rpe, n.text AS note"
        " FROM workouts w LEFT JOIN notes n ON n.user_id=w.user_id AND n.workout_id=w.id"
        " WHERE w.user_id=? AND w.id=? AND w.deleted_at IS NULL", (user_id, workout_id)).fetchone()
    if w is None:
        return None
    out = dict(w)
    out["sets"] = [dict(r) for r in conn.execute(
        "SELECT idx, action_id, exercise, weight_kg, reps, duration_s FROM sets"
        " WHERE user_id=? AND workout_id=? ORDER BY idx", (user_id, workout_id))]
    return out


def list_exercises(conn, user_id):
    """Exercises the user has sets for, most used first."""
    return [dict(r) for r in conn.execute(
        "SELECT s.exercise, COUNT(*) AS sets, COUNT(DISTINCT s.workout_id) AS sessions FROM sets s"
        " JOIN workouts w ON w.user_id=s.user_id AND w.id=s.workout_id AND w.deleted_at IS NULL"
        " WHERE s.user_id=? GROUP BY s.exercise ORDER BY sessions DESC, s.exercise", (user_id,))]


def all_sets(conn, user_id, since=None):
    """Every set of every live workout, oldest first (small: a few thousand rows at most)."""
    sql = ("SELECT w.id AS workout_id, w.started_at, s.idx, s.exercise, s.weight_kg, s.reps FROM sets s"
           " JOIN workouts w ON w.user_id=s.user_id AND w.id=s.workout_id AND w.deleted_at IS NULL"
           " WHERE s.user_id=?")
    args = [user_id]
    if since:
        sql += " AND w.started_at >= ?"
        args.append(since)
    return [dict(r) for r in conn.execute(sql + " ORDER BY w.started_at, w.id, s.idx", args)]


def exercise_history(conn, user_id, exercise):
    """Every set of one exercise, oldest first, with its workout's date."""
    return [dict(r) for r in conn.execute(
        "SELECT w.id AS workout_id, w.started_at, s.idx, s.weight_kg, s.reps, s.duration_s FROM sets s"
        " JOIN workouts w ON w.user_id=s.user_id AND w.id=s.workout_id AND w.deleted_at IS NULL"
        " WHERE s.user_id=? AND s.exercise=? ORDER BY w.started_at, w.id, s.idx", (user_id, exercise))]


def sets_volume_kg(workout) -> float:
    """Volume computed from the stored sets. It is lower than the vendor's volume_kg for exercises
    that load two cables, because the machine reports the weight per cable."""
    return round(sum(s["weight_kg"] * s["reps"] for s in workout["sets"]), 1)


# ---- body weight, sync log -------------------------------------------------

def set_body_weight(conn, user_id, date, kg):
    with conn:
        conn.execute("INSERT INTO body_weight (user_id, date, kg) VALUES (?,?,?)"
                     " ON CONFLICT (user_id, date) DO UPDATE SET kg=excluded.kg", (user_id, date, kg))


def list_body_weight(conn, user_id):
    return [dict(r) for r in conn.execute(
        "SELECT date, kg FROM body_weight WHERE user_id=? ORDER BY date", (user_id,))]


def log_sync(conn, user_id, fetched=0, inserted=0, error=None):
    with conn:
        conn.execute("INSERT INTO sync_log (user_id, ts, fetched, inserted, error) VALUES (?,?,?,?,?)",
                     (user_id, _now(), fetched, inserted, error))


def last_sync(conn, user_id):
    r = conn.execute("SELECT * FROM sync_log WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    return dict(r) if r else None
