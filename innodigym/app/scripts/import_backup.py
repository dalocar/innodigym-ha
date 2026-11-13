#!/usr/bin/env python3
"""Load vendor JSON saved earlier into the add-on database from the command line.

    python3 scripts/import_backup.py --db data/innodigym.db --user Alex records_list.json record_detail_22401.json ...
    python3 scripts/import_backup.py --db data/innodigym.db --list-users

--user must name an existing person (the name Home Assistant gave them the first time they opened the panel)
or, for a standalone database, a new name. Inside the add-on the panel's "Import a backup" button does the
same without a terminal. Safe to repeat; the files are never modified."""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import backup  # noqa: E402
import db  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="path of the SQLite file (created if missing)")
    ap.add_argument("--user", help="name of the person these workouts belong to")
    ap.add_argument("--list-users", action="store_true")
    ap.add_argument("files", nargs="*")
    a = ap.parse_args()
    conn = db.connect(a.db)
    if a.list_users:
        for u in db.list_users(conn):
            print("%d\t%s\t%s" % (u["id"], u["name"], "linked to a Home Assistant user" if u["ha_user_id"] else "no Home Assistant user"))
        return
    if not a.user or not a.files:
        ap.error("--user and at least one file are required")
    row = next((u for u in db.list_users(conn) if u["name"] == a.user), None)
    uid = row["id"] if row else db.create_user(conn, a.user)
    docs = []
    for p in a.files:
        with open(p, encoding="utf-8") as f:
            docs.append(json.load(f))
    r = backup.import_docs(conn, uid, docs)
    print("user %r: %d new, %d already known, %d with sets, %d skipped" % (a.user, r["new"], r["updated"], r["withSets"], r["skipped"]))


if __name__ == "__main__":
    main()
