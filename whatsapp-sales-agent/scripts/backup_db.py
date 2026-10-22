"""Consistent online backup of the SQLite database (safe while the service runs).

    python scripts/backup_db.py                      # -> backups/agent-YYYYmmdd-HHMMSS.db
    python scripts/backup_db.py --dest /mnt/backups --keep 14
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from app.repositories.database import Database  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest", default=str(ROOT / "backups"))
    parser.add_argument("--keep", type=int, default=14, help="number of backups to keep")
    args = parser.parse_args()
    settings = load_settings()
    dest_dir = Path(args.dest)
    target = dest_dir / f"agent-{datetime.now():%Y%m%d-%H%M%S}.db"
    Database(settings.database_path).backup(target)
    print(f"Backup written: {target}")
    backups = sorted(dest_dir.glob("agent-*.db"))
    for old in backups[:-args.keep] if args.keep > 0 else []:
        old.unlink()
        print(f"Removed old backup: {old.name}")


if __name__ == "__main__":
    main()
