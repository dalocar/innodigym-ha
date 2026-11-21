"""
Users defined in the add-on options (the `users:` list) become database users.

Each entry: name, ha_user (optional Home Assistant login name, links the sidebar panel to this
user), innogym_email, innogym_password, hevy_api_key, hevy_since (optional date),
hevy_actions (optional text 'InnoGym action=Hevy exercise; ...' choosing the Hevy exercise for an InnoGym action).
Options are the source of truth for the fields they set; users that are not listed (created from
the panel) are left alone. Secrets are stored but never returned by the API.
"""
from __future__ import annotations

import json
import re
import time

import db

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clean(v):
    v = (v or "").strip() if isinstance(v, str) else ""
    return v or None


def apply_options(conn, entries):
    """Create or update one database user per options entry. Returns the list of user ids."""
    ids = []
    for e in entries:
        name = _clean(e.get("name"))
        if not name:
            continue
        ha_username = _clean(e.get("ha_user"))
        row = conn.execute("SELECT * FROM users WHERE lower(name) = lower(?)", (name,)).fetchone()
        if row is None and ha_username:
            row = conn.execute("SELECT * FROM users WHERE lower(ha_username) = lower(?)", (ha_username,)).fetchone()
        email, password, key = _clean(e.get("innogym_email")), _clean(e.get("innogym_password")), _clean(e.get("hevy_api_key"))
        since = _clean(e.get("hevy_since"))
        if since and not DATE_RE.match(since):
            print("options: user %s: hevy_since %r is not YYYY-MM-DD, ignored" % (name, since), flush=True)
            since = None
        mapping = {}
        for part in re.split(r"[;\n]", e.get("hevy_actions") or ""):
            a, _, b = part.partition("=")
            a, b = a.strip(), b.strip()
            if a and b:
                mapping[a.lower()] = b
        uid = row["id"] if row else db.create_user(conn, name)
        old = db.get_user(conn, uid)
        # a pushed-from date defaults to the day the key first appears, so enabling Hevy never uploads old history
        hevy_since = since or (old["hevy_since"] if key and old["hevy_since"] else (time.strftime("%Y-%m-%d") if key else None))
        with conn:
            conn.execute(
                "UPDATE users SET managed=1, ha_username=COALESCE(?, ha_username), vendor_email=COALESCE(?, vendor_email),"
                " vendor_password=?, hevy_api_key=?, hevy_since=?, hevy_map=? WHERE id=?",
                (ha_username, email, password, key, hevy_since, json.dumps(mapping) if mapping else None, uid))
            # new credentials invalidate the stored token so the next run logs in again
            if password and email and email != old["vendor_email"]:
                conn.execute("UPDATE users SET token=NULL WHERE id=?", (uid,))
        ids.append(uid)
    return ids
