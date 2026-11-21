"""
Sync worker: copies each user's workouts from the vendor cloud into the local
database. Read-only towards the vendor (GET requests only, plus the login call).
Afterwards, users with a Hevy API key get new workouts pushed to Hevy (see hevy.py).

Per user, one run:
  1. sport/record/list from (newest stored workout - LOOKBACK_DAYS) to tomorrow,
     or from the beginning when nothing is stored yet. Rows are upserted by
     vendor record id, so overlap never duplicates.
  2. sport/record/detail for every workout that is new, has no sets yet, or
     started inside the look-back window (the vendor may fill in or fix sets
     after the fact), at most MAX_DETAILS per run.
  3. one sync_log row: how many records were seen, how many were new, and the
     first error if any.
Nothing is ever deleted locally because the vendor stopped returning it.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import db
import hevy

BASE = "https://power.renhesports.com"
LOOKBACK_DAYS = 7
PAGE_SIZE = 500
MAX_PAGES = 20          # safety stop: 10,000 workouts
MAX_DETAILS = 100
FAR_PAST = "2020-01-01 00:00:00"
FMT = "%Y-%m-%d %H:%M:%S"


class VendorError(Exception):
    def __init__(self, message, auth=False):
        super().__init__(message)
        self.auth = auth          # the token was rejected; retrying with it is pointless


class VendorClient:
    """Minimal client for power.renhesports.com. Errors come back as HTTP 200 with
    {"success": false, "msg": ...} (see docs/plan-api.md), so both shapes are checked."""

    def __init__(self, token=None, base=BASE, timeout=20):
        self.token, self.base, self.timeout = token, base, timeout

    def _call(self, method, path, body=None):
        sep = "&" if "?" in path else "?"
        req = urllib.request.Request(
            self.base + "/api" + path + sep + "languageId=en",
            data=json.dumps(body).encode() if body is not None else None, method=method,
            headers={"Content-Type": "application/json",
                     **({"Authorization": "Bearer " + self.token} if self.token else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise VendorError("HTTP %d" % e.code, auth=e.code in (401, 403))
        except (urllib.error.URLError, OSError) as e:
            raise VendorError("network: %s" % getattr(e, "reason", e))
        try:
            data = json.loads(raw)
        except ValueError:
            raise VendorError("reply was not JSON")
        if isinstance(data, dict) and data.get("success") is False:
            raise VendorError(str(data.get("msg") or "request failed"), auth=data.get("code") in (401, 403))
        return data

    def get(self, path):
        return self._call("GET", path)

    def login(self, email, password):
        """Exchange the account login for a token. Returns the token string."""
        data = self._call("POST", "/app/login", {
            "accountType": 2, "accountVal": email, "password": password,
            "device": "Web", "deviceId": "innodigym-ha-addon", "languageId": "en"})
        return data["data"]["token"]


def _q(**params):
    return urllib.parse.urlencode(params)


def fetch_records(client, start, end):
    """All list rows between two 'YYYY-MM-DD HH:MM:SS' strings. If a page comes back
    full, keep going backwards from its oldest row (the server's paging parameter is
    not documented, so the window is narrowed instead)."""
    out, seen = [], set()
    for _ in range(MAX_PAGES):
        rows = client.get("/sport/record/list?" + _q(startTime=start, endTime=end, size=PAGE_SIZE)).get("data") or []
        fresh = [r for r in rows if r["sportRecordId"] not in seen]
        for r in fresh:
            seen.add(r["sportRecordId"])
        out.extend(fresh)
        if len(rows) < PAGE_SIZE or not fresh:
            break
        oldest = min(datetime.strptime(r["startTime"], FMT) for r in rows)
        end = (oldest - timedelta(seconds=1)).strftime(FMT)
    return out


def sync_user(conn, user, client, now=None, log=True):
    """One sync run for one user. Returns {'fetched', 'inserted', 'details', 'error'}.
    Never raises for vendor problems; they are logged and returned."""
    now = now or datetime.now()
    uid = user["id"]
    result = {"fetched": 0, "inserted": 0, "details": 0, "error": None, "new_ids": []}
    try:
        latest = conn.execute("SELECT MAX(started_at) FROM workouts WHERE user_id=? AND id > 0", (uid,)).fetchone()[0]
        window_start = now - timedelta(days=LOOKBACK_DAYS)
        start = FAR_PAST if latest is None else (
            datetime.strptime(latest, FMT) - timedelta(days=LOOKBACK_DAYS)).strftime(FMT)
        records = fetch_records(client, start, (now + timedelta(days=1)).strftime(FMT))
        result["fetched"] = len(records)
        new_ids = set()
        for r in records:
            if db.upsert_vendor_record(conn, uid, r):
                new_ids.add(r["sportRecordId"])
        result["inserted"] = len(new_ids)
        result["new_ids"] = sorted(new_ids)

        recent = {r["sportRecordId"] for r in records
                  if datetime.strptime(r["startTime"], FMT) >= window_start}
        wanted = list(dict.fromkeys(sorted(new_ids | recent) + db.ids_without_sets(conn, uid)))[:MAX_DETAILS]
        errors = []
        for rid in wanted:
            try:
                d = client.get("/sport/record/detail?" + _q(recordId=rid)).get("data")
                if d and d.get("extData"):
                    db.upsert_vendor_record(conn, uid, _merge(d, records, rid), d["extData"])
                    result["details"] += 1
            except VendorError as e:
                if e.auth:
                    raise
                errors.append("detail %s: %s" % (rid, e))
        if errors:
            result["error"] = "; ".join(errors[:3])
    except VendorError as e:
        result["error"] = ("auth: " if e.auth else "") + str(e)
    except Exception as e:  # noqa: BLE001 - one user's bad data must not stop the others
        result["error"] = "unexpected: %s: %s" % (type(e).__name__, e)
    if log:
        db.log_sync(conn, uid, result["fetched"], result["inserted"], result["error"])
    return result


def _merge(detail, records, rid):
    """The detail reply carries the record fields too, but its name can be null (see the
    saved fixtures), so prefer the list row for the summary and keep extData from detail."""
    base = next((r for r in records if r["sportRecordId"] == rid), None)
    return {**detail, **{k: v for k, v in (base or {}).items() if v is not None}}


def _login(conn, user, make_client):
    """Log in with the password from the add-on options and store the new token. Returns the token or raises VendorError."""
    token = make_client(None).login(user["vendor_email"], user["vendor_password"])
    db.set_token(conn, user["id"], token)
    return token


def _sync_one(conn, u, make_client):
    """One user's vendor sync. With credentials in the options, a missing or rejected token is replaced by a fresh login."""
    can_login = bool(u["vendor_email"] and u["vendor_password"])
    token = u["token"]
    if not token and can_login:
        try:
            token = _login(conn, u, make_client)
        except VendorError as e:
            err = ("auth: " if e.auth else "") + "login: " + str(e)
            db.log_sync(conn, u["id"], 0, 0, err)
            return {"fetched": 0, "inserted": 0, "details": 0, "error": err, "new_ids": []}
    r = sync_user(conn, u, make_client(token), log=not can_login)
    if can_login and r["error"] and r["error"].startswith("auth:"):
        try:
            r = sync_user(conn, u, make_client(_login(conn, u, make_client)))
        except VendorError as e:
            r = {**r, "error": ("auth: " if e.auth else "") + "login: " + str(e)}
            db.log_sync(conn, u["id"], 0, 0, r["error"])
    elif can_login:
        db.log_sync(conn, u["id"], r["fetched"], r["inserted"], r["error"])
    return r


def sync_all(conn, make_client, on_user=None, make_hevy=hevy.HevyClient, tz=None):
    """Run every user that has a token or login. make_client(token) returns a vendor client,
    make_hevy(api_key) a Hevy client. on_user(conn, user, result, first_run) is called after each user;
    its errors never stop the loop."""
    out = {}
    for u in db.list_users(conn):
        has_vendor = bool(u["token"] or (u["vendor_email"] and u["vendor_password"]))
        if not has_vendor and not (u["hevy_api_key"] and make_hevy):
            continue
        first_run = db.last_sync(conn, u["id"]) is None
        # a user with only a Hevy key (manual workouts) has nothing to fetch but still gets the push
        out[u["name"]] = r = _sync_one(conn, u, make_client) if has_vendor else {
            "fetched": 0, "inserted": 0, "details": 0, "error": None, "new_ids": []}
        if u["hevy_api_key"] and make_hevy:
            r["hevy"] = hevy.push_user(conn, db.get_user(conn, u["id"]), make_hevy(u["hevy_api_key"]), tz=tz)
        if on_user:
            try:
                on_user(conn, u, r, first_run)
            except Exception as e:  # noqa: BLE001
                print("sync hook error: %s" % e, flush=True)
    return out


class SyncWorker(threading.Thread):
    """Background thread. Opens its own database connection (SQLite connections should not be
    shared between threads), runs sync_all every interval, and runs early when trigger() is called."""

    def __init__(self, db_path, interval_minutes=10, make_client=VendorClient, on_user=None, tz=None):
        super().__init__(daemon=True, name="sync-worker")
        self.on_user, self.tz = on_user, tz
        self.db_path, self.interval, self.make_client = db_path, interval_minutes * 60, make_client
        self._wake, self._stop_flag = threading.Event(), threading.Event()

    def trigger(self):
        self._wake.set()

    def stop(self):
        self._stop_flag.set()
        self._wake.set()

    def run(self):
        conn = db.connect(self.db_path)
        while not self._stop_flag.is_set():
            try:
                for name, r in sync_all(conn, self.make_client, self.on_user, tz=self.tz).items():
                    print("sync %s: %d seen, %d new, %d with sets%s" % (
                        name, r["fetched"], r["inserted"], r["details"],
                        ", error: " + r["error"] if r["error"] else "") +
                        (" | hevy: %d pushed%s" % (r["hevy"]["pushed"], ", error: " + r["hevy"]["error"] if r["hevy"]["error"] else "")
                         if r.get("hevy") else ""), flush=True)
            except Exception as e:  # noqa: BLE001 - keep the worker alive
                print("sync worker error: %s" % e, flush=True)
            self._wake.wait(self.interval)
            self._wake.clear()
        conn.close()
