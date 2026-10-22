"""(Re-)index the product catalogue and price lists.

    python scripts/ingest_catalogue.py           # only changed files
    python scripts/ingest_catalogue.py --force   # re-parse everything

Safe to run while the service is running (SQLite + atomic per-file replace).
The service also re-checks the folder every KB_REFRESH_INTERVAL_SECONDS.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.knowledge.document_loader import DocumentLoader  # noqa: E402
from app.knowledge.product_repository import ProductRepository  # noqa: E402
from app.config import load_business_profile  # noqa: E402
from app.repositories.database import Database  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-parse unchanged files too")
    parser.add_argument("--list", action="store_true", help="print the parsed products")
    args = parser.parse_args()
    settings = load_settings()
    db = Database(settings.database_path)
    db.init()
    profile = load_business_profile(settings)
    repo = ProductRepository(db, DocumentLoader(default_currency=profile.known("pricing.default_currency"),
                                                ocr_enabled=settings.ocr_enabled,
                                                ocr_language=settings.ocr_language))
    report = repo.ingest_directory(settings.knowledge_base_dir, force=args.force)
    print(json.dumps(report.as_dict(), indent=2))
    for source in repo.sources():
        print(f"{source['status']:8} {source['source_file']}: {source['product_count']} products, "
              f"{source['chunk_count']} chunks {('- ' + source['message']) if source['message'] else ''}")
    if args.list:
        for p in repo.all_products():
            price = f"{p.currency} {p.listed_price:,.0f}" if p.listed_price else "no price"
            print(f"  {p.model:12} {p.configuration or '':10} {price:16} {p.tax_treatment():40} "
                  f"valid until {p.price_valid_until or '-'}  [{p.source_file} {p.source_reference}]")
    sys.exit(1 if report.errors else 0)


if __name__ == "__main__":
    main()
