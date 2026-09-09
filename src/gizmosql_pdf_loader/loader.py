"""Target schema management and bulk loading into GizmoSQL via ADBC ``adbc_ingest``.

Large files are streamed as fixed-size chunks so no single Arrow Flight message exceeds
the gRPC limit, and so that any SQL client (even one with the default 16 MiB limit) can
page through ``document_chunks`` to download a file.

There is deliberately no server-side "reassemble the whole file" view: DuckDB needs roughly
ten times the file size in memory to concatenate large BLOBs (a 360 MB file took the PoC
server past its 4.4 GiB limit), and a stray ``SELECT *`` on such a view would do that for
every document at once. Reassemble on the client instead (see examples/download_document.py).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
from adbc_driver_gizmosql import dbapi as gizmosql

from . import __version__
from .pdf_extract import OcrSettings, PageRecord, PdfFileInfo, extract_pages, inspect_pdf, open_for_extraction

log = logging.getLogger(__name__)

DEFAULT_SCHEMA = "pdf_docs"
DEFAULT_CHUNK_SIZE_BYTES = 8 * 1024 * 1024
PAGES_PER_BATCH = 200

# Deterministic document ids: the same file bytes always map to the same UUID.
_DOCUMENT_ID_NAMESPACE = uuid.UUID("2d0f9a3e-7c4b-4d6e-9f11-5b3f0c1a2e77")


def document_id_for(sha256_hex: str) -> uuid.UUID:
    return uuid.uuid5(_DOCUMENT_ID_NAMESPACE, sha256_hex)


def qualified_schema(catalog: str | None, schema: str) -> str:
    return f"{catalog}.{schema}" if catalog else schema


DOCUMENTS_SCHEMA = pa.schema(
    [
        ("document_id", pa.string()),
        ("file_name", pa.string()),
        ("file_path", pa.string()),
        ("file_extension", pa.string()),
        ("mime_type", pa.string()),
        ("file_size_bytes", pa.int64()),
        ("sha256_hex", pa.string()),
        ("file_modified_at", pa.timestamp("us", tz="UTC")),
        ("page_count", pa.int32()),
        ("pdf_format", pa.string()),
        ("title", pa.string()),
        ("author", pa.string()),
        ("subject", pa.string()),
        ("keywords", pa.string()),
        ("creator", pa.string()),
        ("producer", pa.string()),
        ("pdf_created_at", pa.timestamp("us", tz="UTC")),
        ("pdf_modified_at", pa.timestamp("us", tz="UTC")),
        ("is_encrypted", pa.bool_()),
        ("encryption_method", pa.string()),
        ("needs_password", pa.bool_()),
        ("layer_names", pa.list_(pa.string())),
        ("hidden_layer_names", pa.list_(pa.string())),
        ("hidden_layers_revealed", pa.bool_()),
        ("chunk_size_bytes", pa.int32()),
        ("chunk_count", pa.int32()),
        ("pages_with_text", pa.int32()),
        ("pages_ocr", pa.int32()),
        ("total_text_chars", pa.int64()),
        ("loaded_at", pa.timestamp("us", tz="UTC")),
        ("loaded_by", pa.string()),
        ("loader_version", pa.string()),
    ]
)

CHUNKS_SCHEMA = pa.schema(
    [
        ("document_id", pa.string()),
        ("chunk_index", pa.int32()),
        ("byte_offset", pa.int64()),
        ("chunk_length_bytes", pa.int32()),
        ("content", pa.binary()),
    ]
)

PAGES_SCHEMA = pa.schema(
    [
        ("document_id", pa.string()),
        ("page_number", pa.int32()),
        ("page_id", pa.string()),
        ("page_label", pa.string()),
        ("width_pt", pa.float64()),
        ("height_pt", pa.float64()),
        ("rotation", pa.int32()),
        ("image_count", pa.int32()),
        ("extraction_method", pa.string()),
        ("char_count", pa.int32()),
        ("page_text", pa.string()),
    ]
)


def schema_ddl(schema: str, *, catalog: str | None = None) -> list[str]:
    """DDL for the target objects. Idempotent (IF NOT EXISTS / OR REPLACE).

    No primary-key or unique constraints are declared: DuckLake catalogs reject them, and
    the loader enforces one-document-per-SHA-256 itself before inserting.
    """
    s = qualified_schema(catalog, schema)
    return [
        f"CREATE SCHEMA IF NOT EXISTS {s}",
        f"""
        CREATE TABLE IF NOT EXISTS {s}.documents (
            document_id            UUID NOT NULL,
            file_name              VARCHAR NOT NULL,
            file_path              VARCHAR,
            file_extension         VARCHAR,
            mime_type              VARCHAR NOT NULL,
            file_size_bytes        BIGINT NOT NULL,
            sha256_hex             VARCHAR NOT NULL,
            file_modified_at       TIMESTAMPTZ,
            page_count             INTEGER,
            pdf_format             VARCHAR,
            title                  VARCHAR,
            author                 VARCHAR,
            subject                VARCHAR,
            keywords               VARCHAR,
            creator                VARCHAR,
            producer               VARCHAR,
            pdf_created_at         TIMESTAMPTZ,
            pdf_modified_at        TIMESTAMPTZ,
            is_encrypted           BOOLEAN,
            encryption_method      VARCHAR,
            needs_password         BOOLEAN,
            layer_names            VARCHAR[],
            hidden_layer_names     VARCHAR[],
            hidden_layers_revealed BOOLEAN,
            chunk_size_bytes       INTEGER NOT NULL,
            chunk_count            INTEGER NOT NULL,
            pages_with_text        INTEGER,
            pages_ocr              INTEGER,
            total_text_chars       BIGINT,
            loaded_at              TIMESTAMPTZ NOT NULL,
            loaded_by              VARCHAR,
            loader_version         VARCHAR
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {s}.document_chunks (
            document_id        UUID NOT NULL,
            chunk_index        INTEGER NOT NULL,
            byte_offset        BIGINT NOT NULL,
            chunk_length_bytes INTEGER NOT NULL,
            content            BLOB NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {s}.document_pages (
            document_id       UUID NOT NULL,
            page_number       INTEGER NOT NULL,
            page_id           VARCHAR NOT NULL,
            page_label        VARCHAR,
            width_pt          DOUBLE,
            height_pt         DOUBLE,
            rotation          INTEGER,
            image_count       INTEGER,
            extraction_method VARCHAR,
            char_count        INTEGER,
            page_text         VARCHAR
        )
        """,
        # Whole document text (pages joined with form-feed characters).
        f"""
        CREATE OR REPLACE VIEW {s}.document_text AS
        SELECT d.document_id,
               d.file_name,
               d.title,
               d.page_count,
               string_agg(p.page_text, chr(12) ORDER BY p.page_number) AS document_text
          FROM {s}.documents d
          JOIN {s}.document_pages p USING (document_id)
         GROUP BY ALL
        """,
        # Case-insensitive substring search across all pages, with a snippet around the hit.
        f"""
        CREATE OR REPLACE MACRO {s}.search_pages(search_term) AS TABLE
        SELECT d.file_name,
               p.page_number,
               p.page_label,
               substr(p.page_text,
                      greatest(1, instr(lower(p.page_text), lower(search_term)) - 120),
                      300) AS snippet,
               d.document_id
          FROM {s}.document_pages p
          JOIN {s}.documents d USING (document_id)
         WHERE p.page_text ILIKE '%' || search_term || '%'
         ORDER BY d.file_name, p.page_number
        """,
    ]


def page_id_for(document_id: str, page_number: int) -> str:
    """Single-column key for a page (DuckDB FTS indexes need one)."""
    return f"{document_id}:{page_number}"


def catalog_type(conn: gizmosql.Connection, catalog: str) -> str:
    """The catalog's engine type per duckdb_databases(): 'duckdb', 'ducklake', 'postgres', ..."""
    with conn.cursor() as cur:
        cur.execute("SELECT type FROM duckdb_databases() WHERE database_name = ?", parameters=[catalog])
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"Catalog {catalog!r} is not attached on the server")
    return row[0]


def ensure_schema(
    conn: gizmosql.Connection, *, schema: str = DEFAULT_SCHEMA, catalog: str | None = None
) -> None:
    with conn.cursor() as cur:
        for statement in schema_ddl(schema, catalog=catalog):
            cur.execute(statement)
    _migrate_page_id(conn, schema=schema, catalog=catalog)


def _migrate_page_id(conn: gizmosql.Connection, *, schema: str, catalog: str | None) -> None:
    """Add and backfill ``document_pages.page_id`` on tables created before the column existed."""
    qs = qualified_schema(catalog, schema)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_catalog = COALESCE(?, current_catalog()) AND table_schema = ? "
            "AND table_name = 'document_pages' AND column_name = 'page_id'",
            parameters=[catalog, schema],
        )
        if int(cur.fetchone()[0]):
            return
        log.info("%s.document_pages: adding page_id column (schema migration)", qs)
        cur.execute(f"ALTER TABLE {qs}.document_pages ADD COLUMN page_id VARCHAR")
        cur.execute(f"UPDATE {qs}.document_pages SET page_id = document_id::VARCHAR || ':' || page_number")


def iter_chunk_batches(path: Path, *, document_id: str, chunk_size: int) -> Iterator[pa.RecordBatch]:
    """Yield one single-row RecordBatch per file chunk, reading the file lazily."""
    offset = 0
    index = 0
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk_size)
            if not block:
                break
            yield pa.RecordBatch.from_pydict(
                {
                    "document_id": [document_id],
                    "chunk_index": [index],
                    "byte_offset": [offset],
                    "chunk_length_bytes": [len(block)],
                    "content": [block],
                },
                schema=CHUNKS_SCHEMA,
            )
            offset += len(block)
            index += 1


def iter_page_batches(pages: list[PageRecord], *, document_id: str) -> Iterator[pa.RecordBatch]:
    for start in range(0, len(pages), PAGES_PER_BATCH):
        group = pages[start : start + PAGES_PER_BATCH]
        yield pa.RecordBatch.from_pydict(
            {
                "document_id": [document_id] * len(group),
                "page_number": [p.page_number for p in group],
                "page_id": [page_id_for(document_id, p.page_number) for p in group],
                "page_label": [p.page_label for p in group],
                "width_pt": [p.width_pt for p in group],
                "height_pt": [p.height_pt for p in group],
                "rotation": [p.rotation for p in group],
                "image_count": [p.image_count for p in group],
                "extraction_method": [p.extraction_method for p in group],
                "char_count": [p.char_count for p in group],
                "page_text": [p.page_text for p in group],
            },
            schema=PAGES_SCHEMA,
        )


def documents_batch(
    info: PdfFileInfo,
    *,
    document_id: str,
    pages: list[PageRecord],
    chunk_size: int,
    chunk_count: int,
    hidden_layers_revealed: bool,
    loaded_by: str | None,
) -> pa.RecordBatch:
    row = {
        "document_id": document_id,
        "file_name": info.file_name,
        "file_path": info.file_path,
        "file_extension": info.file_extension,
        "mime_type": info.mime_type,
        "file_size_bytes": info.file_size_bytes,
        "sha256_hex": info.sha256_hex,
        "file_modified_at": info.file_modified_at,
        "page_count": info.page_count,
        "pdf_format": info.pdf_format,
        "title": info.title,
        "author": info.author,
        "subject": info.subject,
        "keywords": info.keywords,
        "creator": info.creator,
        "producer": info.producer,
        "pdf_created_at": info.pdf_created_at,
        "pdf_modified_at": info.pdf_modified_at,
        "is_encrypted": info.is_encrypted,
        "encryption_method": info.encryption_method,
        "needs_password": info.needs_password,
        "layer_names": info.layer_names,
        "hidden_layer_names": info.hidden_layer_names,
        "hidden_layers_revealed": hidden_layers_revealed,
        "chunk_size_bytes": chunk_size,
        "chunk_count": chunk_count,
        "pages_with_text": sum(1 for p in pages if p.page_text.strip()),
        "pages_ocr": sum(1 for p in pages if p.extraction_method == "ocr"),
        "total_text_chars": sum(p.char_count for p in pages),
        "loaded_at": datetime.now(tz=timezone.utc),
        "loaded_by": loaded_by,
        "loader_version": __version__,
    }
    return pa.RecordBatch.from_pydict({k: [v] for k, v in row.items()}, schema=DOCUMENTS_SCHEMA)


@dataclass
class LoadOptions:
    schema: str = DEFAULT_SCHEMA
    catalog: str | None = None
    chunk_size: int = DEFAULT_CHUNK_SIZE_BYTES
    replace: bool = False
    reveal_hidden_layers: bool = False
    ocr: OcrSettings | None = None
    max_pages: int | None = None


@dataclass
class LoadResult:
    file_name: str
    document_id: str
    status: str  # "loaded" | "skipped"
    file_size_bytes: int = 0
    chunk_count: int = 0
    page_count: int = 0
    pages_with_text: int = 0
    pages_ocr: int = 0
    total_text_chars: int = 0
    hidden_layers_revealed: bool = False
    seconds: float = 0.0


def find_existing_document(
    conn: gizmosql.Connection, *, schema: str, sha256_hex: str, catalog: str | None = None
) -> str | None:
    qs = qualified_schema(catalog, schema)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT document_id::VARCHAR FROM {qs}.documents WHERE sha256_hex = ?", parameters=[sha256_hex]
        )
        row = cur.fetchone()
    return row[0] if row else None


def delete_document(
    conn: gizmosql.Connection, *, schema: str, document_id: str, catalog: str | None = None
) -> None:
    """Delete every row of a document from the three tables, verifying the rows are gone.

    Requires adbc-driver-gizmosql >= 2.0.13: earlier versions ran parameterized DML lazily,
    so a DELETE with bound parameters intermittently did nothing. The verification stays as
    a safety net.
    """
    qs = qualified_schema(catalog, schema)
    with conn.cursor() as cur:
        for table in ("document_pages", "document_chunks", "documents"):
            cur.execute(f"DELETE FROM {qs}.{table} WHERE document_id = ?", parameters=[document_id])
            cur.execute(f"SELECT count(*) FROM {qs}.{table} WHERE document_id = ?", parameters=[document_id])
            remaining = int(cur.fetchone()[0])
            if remaining:
                raise RuntimeError(
                    f"DELETE left {remaining} row(s) for document {document_id} in {qs}.{table}"
                )


def current_user(conn: gizmosql.Connection) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT GIZMOSQL_USER()")
        row = cur.fetchone()
    return row[0] if row else None


def load_pdf(conn: gizmosql.Connection, path: Path, options: LoadOptions) -> LoadResult:
    """Load one PDF: its bytes (chunked), its per-page text, and its metadata row."""
    started = time.perf_counter()
    path = Path(path)
    info = inspect_pdf(path)
    document_id = str(document_id_for(info.sha256_hex))
    qs = qualified_schema(options.catalog, options.schema)
    log.info(
        "%s: %d bytes, %d pages, sha256=%s",
        info.file_name,
        info.file_size_bytes,
        info.page_count,
        info.sha256_hex[:12],
    )

    existing = find_existing_document(
        conn, schema=options.schema, catalog=options.catalog, sha256_hex=info.sha256_hex
    )
    if existing is not None:
        if not options.replace:
            log.info("%s: already loaded as %s; skipping (use --replace to reload)", info.file_name, existing)
            return LoadResult(
                file_name=info.file_name,
                document_id=existing,
                status="skipped",
                file_size_bytes=info.file_size_bytes,
                page_count=info.page_count,
                seconds=time.perf_counter() - started,
            )
        log.info("%s: replacing existing document %s", info.file_name, existing)
        delete_document(conn, schema=options.schema, catalog=options.catalog, document_id=existing)

    with open_for_extraction(path, reveal_hidden_layers=options.reveal_hidden_layers) as (
        extract_path,
        revealed,
    ):
        pages = extract_pages(extract_path, ocr=options.ocr, max_pages=options.max_pages)
    hidden_layers_revealed = bool(revealed)

    try:
        with conn.cursor() as cur:
            chunk_reader = pa.RecordBatchReader.from_batches(
                CHUNKS_SCHEMA,
                iter_chunk_batches(path, document_id=document_id, chunk_size=options.chunk_size),
            )
            cur.adbc_ingest(
                table_name="document_chunks",
                data=chunk_reader,
                mode="append",
                catalog_name=options.catalog,
                db_schema_name=options.schema,
            )
            cur.execute(
                f"SELECT count(*) FROM {qs}.document_chunks WHERE document_id = ?", parameters=[document_id]
            )
            chunk_count = int(cur.fetchone()[0])
            log.info(
                "%s: uploaded %d chunk(s) of up to %d bytes", info.file_name, chunk_count, options.chunk_size
            )

            if pages:
                page_reader = pa.RecordBatchReader.from_batches(
                    PAGES_SCHEMA, iter_page_batches(pages, document_id=document_id)
                )
                cur.adbc_ingest(
                    table_name="document_pages",
                    data=page_reader,
                    mode="append",
                    catalog_name=options.catalog,
                    db_schema_name=options.schema,
                )
                log.info("%s: uploaded text for %d page(s)", info.file_name, len(pages))

            doc_batch = documents_batch(
                info,
                document_id=document_id,
                pages=pages,
                chunk_size=options.chunk_size,
                chunk_count=chunk_count,
                hidden_layers_revealed=hidden_layers_revealed,
                loaded_by=current_user(conn),
            )
            cur.adbc_ingest(
                table_name="documents",
                data=doc_batch,
                mode="append",
                catalog_name=options.catalog,
                db_schema_name=options.schema,
            )
    except Exception:
        log.exception("%s: load failed; removing partial rows", info.file_name)
        delete_document(conn, schema=options.schema, catalog=options.catalog, document_id=document_id)
        raise

    return LoadResult(
        file_name=info.file_name,
        document_id=document_id,
        status="loaded",
        file_size_bytes=info.file_size_bytes,
        chunk_count=chunk_count,
        page_count=len(pages),
        pages_with_text=sum(1 for p in pages if p.page_text.strip()),
        pages_ocr=sum(1 for p in pages if p.extraction_method == "ocr"),
        total_text_chars=sum(p.char_count for p in pages),
        hidden_layers_revealed=hidden_layers_revealed,
        seconds=time.perf_counter() - started,
    )
