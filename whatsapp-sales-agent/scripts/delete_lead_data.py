"""Erase a lead's conversation data (e.g. for a data-deletion request).

    python scripts/delete_lead_data.py --lead-id L001 [--keep-dnc]

Deletes messages, conversations, summaries, quotes and sync state from SQLite
and anonymises the lead record. The phone number stays on the do-not-contact
list unless --remove-dnc is given (so the person is not contacted again).
The CSV / Google Sheet row is NOT modified; edit it manually if required.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.repositories.database import Database, now_iso  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lead-id", required=True)
    parser.add_argument("--remove-dnc", action="store_true")
    args = parser.parse_args()
    db = Database(load_settings().database_path)
    db.init()
    lead = db.query_one("SELECT * FROM leads WHERE lead_id=?", (args.lead_id,))
    if lead is None:
        sys.exit(f"lead {args.lead_id} not found")
    with db.tx() as conn:
        convs = [r["conversation_id"] for r in conn.execute(
            "SELECT conversation_id FROM conversations WHERE lead_id=?", (args.lead_id,))]
        for cid in convs:
            conn.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
        for table in ("summaries", "quotes_audit", "sync_status"):
            conn.execute(f"DELETE FROM {table} WHERE lead_id=?", (args.lead_id,))
        conn.execute("DELETE FROM conversations WHERE lead_id=?", (args.lead_id,))
        conn.execute("UPDATE jobs SET status='DONE', last_error='lead data deleted' "
                     "WHERE status IN ('PENDING','DEAD') AND payload_json LIKE ?", (f'%"{args.lead_id}"%',))
        conn.execute("UPDATE leads SET customer_name=NULL, company_name=NULL, email=NULL, mobile_raw=NULL, "
                     "requirement=NULL, raw_json='{}', status_reason='data deleted', updated_at=? "
                     "WHERE lead_id=?", (now_iso(), args.lead_id))
        if args.remove_dnc and lead["mobile_e164"]:
            conn.execute("DELETE FROM do_not_contact WHERE mobile_e164=?", (lead["mobile_e164"],))
        db.audit("lead_data_deleted", lead_id=args.lead_id, conn=conn, conversations=len(convs))
    print(f"Deleted conversation data for {args.lead_id} ({len(convs)} conversation(s)).")


if __name__ == "__main__":
    main()
