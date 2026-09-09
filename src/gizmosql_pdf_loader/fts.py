"""Full-text (BM25) search support via DuckDB's ``fts`` extension.

Where the index can live depends on the *type* of the target catalog:

* ``duckdb`` (a DuckDB database file): the index is built directly on
  ``<catalog>.<schema>.document_pages`` and persists with the file.
* ``ducklake``: DuckDB FTS indexes cannot be created inside a DuckLake catalog (the extension's
  internal tables use a NULL-typed column DuckLake refuses: "unsupported type NULL"). The index is
  skipped unless the caller names a regular DuckDB catalog (``fts_catalog``) to hold a copy of the
  page text, in which case the index is built over that copy.

Either way the index is a snapshot: rebuild it after loading documents (``load`` does this
automatically) and, for an in-memory copy catalog, after a server restart.

Loading the ``fts`` extension needs the GizmoSQL admin role; querying an existing index does not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from adbc_driver_gizmosql import dbapi as gizmosql

from .loader import catalog_type, qualified_schema

log = logging.getLogger(__name__)

DEFAULT_FTS_SCHEMA = "pdf_fts"
FTS_TABLE = "document_pages"

MODE_IN_CATALOG = "in-catalog"
MODE_COPY = "copy"
MODE_SKIP = "skip"


class FtsUnavailableError(RuntimeError):
    """The fts extension is neither loaded nor loadable with this login."""


@dataclass
class FtsPlan:
    mode: str  # MODE_IN_CATALOG | MODE_COPY | MODE_SKIP
    target_catalog: str
    target_schema: str
    target_type: str
    reason: str
    fts_catalog: str | None = None  # copy mode only
    fts_schema: str | None = None  # copy mode only

    @property
    def indexed_table(self) -> str | None:
        if self.mode == MODE_IN_CATALOG:
            return f"{self.target_catalog}.{self.target_schema}.{FTS_TABLE}"
        if self.mode == MODE_COPY:
            return f"{self.fts_catalog}.{self.fts_schema}.{FTS_TABLE}"
        return None

    @property
    def index_schema(self) -> str | None:
        # DuckDB names the index schema fts_<schema>_<table>, in the indexed table's catalog.
        if self.mode == MODE_IN_CATALOG:
            return f"{self.target_catalog}.fts_{self.target_schema}_{FTS_TABLE}"
        if self.mode == MODE_COPY:
            return f"{self.fts_catalog}.fts_{self.fts_schema}_{FTS_TABLE}"
        return None


def plan_fts(
    conn: gizmosql.Connection,
    *,
    catalog: str,
    schema: str,
    fts_catalog: str | None = None,
    fts_schema: str = DEFAULT_FTS_SCHEMA,
) -> FtsPlan:
    """Decide where (or whether) to build the index, based on the target catalog's type."""
    target_type = catalog_type(conn, catalog)
    base = dict(target_catalog=catalog, target_schema=schema, target_type=target_type)
    if fts_catalog and fts_catalog != catalog:
        copy_type = catalog_type(conn, fts_catalog)
        if copy_type != "duckdb":
            return FtsPlan(
                MODE_SKIP,
                **base,
                reason=f"--fts-catalog {fts_catalog!r} is a {copy_type!r} catalog; "
                "only 'duckdb' catalogs can hold an FTS index",
            )
        return FtsPlan(
            MODE_COPY,
            **base,
            fts_catalog=fts_catalog,
            fts_schema=fts_schema,
            reason=f"index built over a copy of the page text in the 'duckdb' catalog {fts_catalog!r}",
        )
    if target_type == "duckdb":
        return FtsPlan(
            MODE_IN_CATALOG,
            **base,
            reason=f"target catalog {catalog!r} is a DuckDB database; "
            f"index built in place on {catalog}.{schema}.{FTS_TABLE}",
        )
    return FtsPlan(
        MODE_SKIP,
        **base,
        reason=f"target catalog {catalog!r} is a {target_type!r} catalog, which cannot hold a DuckDB "
        "FTS index; pass --fts-catalog <duckdb catalog> to build one over a copy of the page text",
    )


def ensure_fts_loaded(conn: gizmosql.Connection) -> None:
    """Make sure the fts extension is loaded, installing/loading it if this login may."""
    with conn.cursor() as cur:
        cur.execute("SELECT loaded FROM duckdb_extensions() WHERE extension_name = 'fts'")
        row = cur.fetchone()
        if row and row[0]:
            return
        try:
            cur.execute("INSTALL fts")
            cur.execute("LOAD fts")
        except Exception as exc:
            raise FtsUnavailableError(
                "The DuckDB 'fts' extension is not loaded and loading it requires the GizmoSQL admin role. "
                "Ask an administrator to run 'INSTALL fts; LOAD fts' on the server."
            ) from exc


def fts_ddl(plan: FtsPlan) -> list[str]:
    """Statements that (re)build the index (and the copy table, in copy mode) plus the ranked macro."""
    if plan.mode == MODE_SKIP:
        return []
    source = qualified_schema(plan.target_catalog, plan.target_schema)
    statements: list[str] = []
    if plan.mode == MODE_COPY:
        statements += [
            f"CREATE SCHEMA IF NOT EXISTS {plan.fts_catalog}.{plan.fts_schema}",
            f"""
            CREATE OR REPLACE TABLE {plan.indexed_table} AS
            SELECT page_id, document_id, page_number, page_text
              FROM {source}.document_pages
            """,
        ]
    statements += [
        f"PRAGMA create_fts_index('{plan.indexed_table}', 'page_id', 'page_text', "
        "stemmer = 'english', overwrite = 1)",
        # Ranked search, discoverable next to the data. Fails with "does not exist" if the
        # index has not been (re)built since a server restart: run `gizmosql-pdf-loader index`.
        f"""
        CREATE OR REPLACE MACRO {source}.search_pages_ranked(search_term, top_k := 20) AS TABLE
        SELECT d.file_name,
               p.page_number,
               round(p.score, 3) AS score,
               substr(dp.page_text, 1, 300) AS snippet,
               d.document_id
          FROM (SELECT document_id, page_number,
                       {plan.index_schema}.match_bm25(page_id, search_term) AS score
                  FROM {plan.indexed_table}) p
          JOIN {source}.documents d USING (document_id)
          JOIN {source}.document_pages dp USING (document_id, page_number)
         WHERE p.score IS NOT NULL
         ORDER BY p.score DESC
         LIMIT top_k
        """,
    ]
    return statements


def build_fts_index(conn: gizmosql.Connection, plan: FtsPlan) -> int:
    """Execute the plan; returns the number of pages indexed (0 when skipped)."""
    log.info(
        "FTS plan for %s (type=%s): %s -> %s", plan.target_catalog, plan.target_type, plan.mode, plan.reason
    )
    if plan.mode == MODE_SKIP:
        return 0
    ensure_fts_loaded(conn)
    with conn.cursor() as cur:
        for statement in fts_ddl(plan):
            cur.execute(statement)
        cur.execute(f"SELECT count(*) FROM {plan.indexed_table}")
        indexed = int(cur.fetchone()[0])
    log.info("FTS index rebuilt over %d page(s) in %s", indexed, plan.index_schema)
    return indexed
