"""Import vendor JSON that was saved earlier (a sport/record/list reply plus any sport/record/detail replies)
into the database. Used by scripts/import_backup.py and by the panel's "Import a backup" button.
Records are keyed by the vendor's record id, so importing the same files again changes nothing."""
from __future__ import annotations

import db


def import_docs(conn, user_id, docs):
    """docs: parsed JSON values, each either a whole API reply ({"data": ...}) or just its data.
    Returns {'new', 'updated', 'withSets', 'skipped'}; skipped counts documents or rows that are not workouts."""
    records, details, skipped = {}, {}, 0
    for d in docs:
        data = d.get("data", d) if isinstance(d, dict) else d
        rows = data if isinstance(data, list) else [data]
        for r in rows:
            if not (isinstance(r, dict) and isinstance(r.get("sportRecordId"), int) and isinstance(r.get("startTime"), str)
                    and len(r["startTime"]) >= 19):
                skipped += 1
                continue
            (details if r.get("extData") else records)[r["sportRecordId"]] = r
    for rid, r in details.items():                      # a detail alone is enough to create a workout
        records.setdefault(rid, r)
    new = updated = with_sets = 0
    for rid, rec in records.items():
        ext = (details.get(rid) or rec).get("extData")
        if db.upsert_vendor_record(conn, user_id, rec, ext):
            new += 1
        else:
            updated += 1
        with_sets += bool(ext)
    return {"new": new, "updated": updated, "withSets": with_sets, "skipped": skipped}
