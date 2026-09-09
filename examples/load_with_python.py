"""Use gizmosql-pdf-loader as a library: load a directory of PDFs, index them, run a search.

Connection details come from GIZMOSQL_* environment variables (or a .env file); the catalog and
schema are given explicitly below. Run: python examples/load_with_python.py source_pdf_files
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

from gizmosql_pdf_loader import (
    GizmoSQLSettings,
    LoadOptions,
    OcrSettings,
    build_fts_index,
    ensure_schema,
    load_pdf,
    plan_fts,
)

load_dotenv(dotenv_path=".env")

CATALOG = "my_lake"
SCHEMA = "pdf_docs"
source_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "source_pdf_files")

# GizmoSQLSettings(hostname=..., port=..., username=..., password=...) also works without env vars.
settings = GizmoSQLSettings.from_env()

with settings.connect(catalog=CATALOG) as conn:
    ensure_schema(conn, catalog=CATALOG, schema=SCHEMA)  # idempotent: tables, views, macros, migrations

    options = LoadOptions(
        catalog=CATALOG,
        schema=SCHEMA,
        chunk_size=8 * 1024 * 1024,
        replace=False,  # True reloads files whose SHA-256 is already present
        ocr=OcrSettings(enabled=True, language="eng"),
    )
    for pdf_path in sorted(source_dir.glob("*.pdf")):
        result = load_pdf(conn, path=pdf_path, options=options)
        r = result
        print(f"{r.status:8} {r.file_name}: {r.page_count} pages, {r.total_text_chars:,} chars")

    # Full-text index: built in place on a DuckDB-file catalog, skipped on DuckLake unless a
    # DuckDB catalog is named to hold a copy of the page text.
    plan = plan_fts(conn, catalog=CATALOG, schema=SCHEMA, fts_catalog="memory")
    print(f"fts: {plan.mode} ({plan.reason})")
    pages_indexed = build_fts_index(conn, plan)

    with conn.cursor() as cur:
        if pages_indexed:
            cur.execute(
                f"SELECT file_name, page_number, score "
                f"FROM {CATALOG}.{SCHEMA}.search_pages_ranked(?, top_k := 3)",
                parameters=["relief valve"],
            )
        else:
            cur.execute(
                f"SELECT file_name, page_number, snippet FROM {CATALOG}.{SCHEMA}.search_pages(?) LIMIT 3",
                parameters=["relief valve"],
            )
        for row in cur.fetchall():
            print(row)
