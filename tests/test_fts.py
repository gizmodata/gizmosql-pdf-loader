from gizmosql_pdf_loader.fts import MODE_COPY, MODE_IN_CATALOG, MODE_SKIP, FtsPlan, fts_ddl


def _plan(mode, **kw):
    base = dict(target_catalog="lake", target_schema="docs", target_type="ducklake", reason="test")
    base.update(kw)
    return FtsPlan(mode, **base)


def test_in_catalog_plan_names():
    plan = _plan(MODE_IN_CATALOG, target_catalog="db", target_type="duckdb")
    assert plan.indexed_table == "db.docs.document_pages"
    assert plan.index_schema == "db.fts_docs_document_pages"
    statements = fts_ddl(plan)
    assert len(statements) == 2  # index + macro, no copy table
    assert statements[0].startswith(
        "PRAGMA create_fts_index('db.docs.document_pages', 'page_id', 'page_text'"
    )
    assert "CREATE OR REPLACE MACRO db.docs.search_pages_ranked" in statements[1]
    assert "db.fts_docs_document_pages.match_bm25" in statements[1]


def test_copy_plan_reads_lake_and_writes_fts_catalog():
    plan = _plan(MODE_COPY, fts_catalog="memory", fts_schema="pdf_fts")
    assert plan.indexed_table == "memory.pdf_fts.document_pages"
    assert plan.index_schema == "memory.fts_pdf_fts_document_pages"
    statements = fts_ddl(plan)
    assert statements[0] == "CREATE SCHEMA IF NOT EXISTS memory.pdf_fts"
    assert "FROM lake.docs.document_pages" in statements[1]
    assert "CREATE OR REPLACE TABLE memory.pdf_fts.document_pages" in statements[1]
    assert statements[2].startswith("PRAGMA create_fts_index('memory.pdf_fts.document_pages'")
    # The ranked macro lives next to the data but points at the index catalog.
    assert "CREATE OR REPLACE MACRO lake.docs.search_pages_ranked" in statements[3]
    assert "memory.fts_pdf_fts_document_pages.match_bm25" in statements[3]


def test_skip_plan_has_no_ddl():
    plan = _plan(MODE_SKIP)
    assert plan.indexed_table is None and plan.index_schema is None
    assert fts_ddl(plan) == []
