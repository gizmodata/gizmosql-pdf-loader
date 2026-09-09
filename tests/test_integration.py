"""End-to-end test against a real GizmoSQL server.

Skipped unless GIZMOSQL_HOSTNAME is set (a .env file in the working directory is honoured).
Loads into a throw-away schema in GIZMOSQL_CATALOG (or the session default), then drops it.
"""

import os
import uuid
from pathlib import Path

import pymupdf
import pytest
from dotenv import load_dotenv

from gizmosql_pdf_loader.loader import qualified_schema

load_dotenv(dotenv_path=".env")

pytestmark = pytest.mark.skipif(not os.environ.get("GIZMOSQL_HOSTNAME"), reason="GIZMOSQL_HOSTNAME not set")


@pytest.fixture
def pdf_path(tmp_path: Path) -> Path:
    path = tmp_path / "integration.pdf"
    doc = pymupdf.open()
    for i in range(5):
        page = doc.new_page()
        page.insert_text((72, 72), f"Integration page {i + 1}. The swing motor relief valve.")
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def scratch_schema():
    from gizmosql_pdf_loader.config import GizmoSQLSettings, resolve_target_catalog

    schema = f"pdf_loader_test_{uuid.uuid4().hex[:8]}"
    settings = GizmoSQLSettings.from_env()
    with settings.connect(catalog=os.environ.get("GIZMOSQL_CATALOG")) as conn:
        catalog = resolve_target_catalog(conn, os.environ.get("GIZMOSQL_CATALOG"))
        yield conn, catalog, schema
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {qualified_schema(catalog, schema)} CASCADE")


def test_load_list_search_replace(pdf_path: Path, scratch_schema):
    from gizmosql_pdf_loader.loader import LoadOptions, ensure_schema, load_pdf

    conn, catalog, schema = scratch_schema
    ensure_schema(conn, schema=schema, catalog=catalog)
    qs = qualified_schema(catalog, schema)
    # tiny chunks to exercise multi-chunk streaming
    options = LoadOptions(schema=schema, catalog=catalog, chunk_size=1024)

    result = load_pdf(conn, path=pdf_path, options=options)
    assert result.status == "loaded"
    assert result.chunk_count > 1
    assert result.page_count == 5
    assert result.pages_with_text == 5

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT content FROM {qs}.document_chunks WHERE document_id = ? ORDER BY chunk_index",
            parameters=[result.document_id],
        )
        reassembled = b"".join(row[0] for row in cur.fetchall())
        assert reassembled == pdf_path.read_bytes()
        cur.execute(f"SELECT count(*) FROM {qs}.search_pages(?)", parameters=["relief VALVE"])
        assert cur.fetchone()[0] == 5
        cur.execute(f"SELECT page_count, mime_type, document_id::VARCHAR FROM {qs}.documents")
        page_count, mime_type, document_id = cur.fetchone()
        assert (page_count, mime_type, document_id) == (5, "application/pdf", result.document_id)

    # Second load of identical bytes is skipped; --replace reloads without duplicating rows.
    assert load_pdf(conn, path=pdf_path, options=options).status == "skipped"
    options.replace = True
    assert load_pdf(conn, path=pdf_path, options=options).status == "loaded"
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {qs}.documents")
        assert cur.fetchone()[0] == 1
        cur.execute(f"SELECT count(*) FROM {qs}.document_pages")
        assert cur.fetchone()[0] == 5


def test_fts_plan_and_ranked_search(pdf_path: Path, scratch_schema):
    """On a DuckDB-file target the index is built in place; on DuckLake it is skipped unless a
    DuckDB copy catalog is named. Both paths end in a working ranked search."""
    from gizmosql_pdf_loader.fts import (
        MODE_COPY,
        MODE_IN_CATALOG,
        MODE_SKIP,
        FtsUnavailableError,
        build_fts_index,
        plan_fts,
    )
    from gizmosql_pdf_loader.loader import LoadOptions, catalog_type, ensure_schema, load_pdf

    conn, catalog, schema = scratch_schema
    ensure_schema(conn, schema=schema, catalog=catalog)
    load_pdf(conn, path=pdf_path, options=LoadOptions(schema=schema, catalog=catalog))

    plan = plan_fts(conn, catalog=catalog, schema=schema)
    if catalog_type(conn, catalog) == "duckdb":
        assert plan.mode == MODE_IN_CATALOG
    else:
        assert plan.mode == MODE_SKIP
        assert build_fts_index(conn, plan) == 0
        plan = plan_fts(
            conn, catalog=catalog, schema=schema, fts_catalog="memory", fts_schema=f"{schema}_fts"
        )
        assert plan.mode == MODE_COPY
    try:
        indexed = build_fts_index(conn, plan)
    except FtsUnavailableError as exc:
        pytest.skip(str(exc))
    try:
        assert indexed == 5
        qs = qualified_schema(catalog, schema)
        with conn.cursor() as cur:
            # "valves" must match "valve" through stemming; results carry a score.
            cur.execute(
                f"SELECT page_number, score FROM {qs}.search_pages_ranked(?, top_k := ?)",
                parameters=["valves", 3],
            )
            rows = cur.fetchall()
        assert len(rows) == 3
        assert all(score > 0 for _, score in rows)
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {plan.index_schema} CASCADE")
            if plan.mode == MODE_COPY:
                cur.execute(f"DROP SCHEMA IF EXISTS {plan.fts_catalog}.{plan.fts_schema} CASCADE")
