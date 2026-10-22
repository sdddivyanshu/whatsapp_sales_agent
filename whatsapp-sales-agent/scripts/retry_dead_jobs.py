"""List or retry dead-lettered jobs.

    python scripts/retry_dead_jobs.py            # list DEAD jobs
    python scripts/retry_dead_jobs.py --retry    # move all DEAD jobs back to PENDING
    python scripts/retry_dead_jobs.py --retry --id 42
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.repositories.database import Database  # noqa: E402
from app.services.message_queue import JobQueue  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--retry", action="store_true")
    parser.add_argument("--id", type=int)
    args = parser.parse_args()
    db = Database(load_settings().database_path)
    db.init()
    queue = JobQueue(db)
    for row in db.query("SELECT id, job_type, attempts, last_error, payload_json FROM jobs "
                        "WHERE status='DEAD' ORDER BY id"):
        print(f"#{row['id']} {row['job_type']} attempts={row['attempts']} payload={row['payload_json']}\n"
              f"    error: {row['last_error']}")
    if args.retry:
        print(f"Re-queued {queue.retry_dead(args.id)} job(s); the running service will pick them up.")


if __name__ == "__main__":
    main()
