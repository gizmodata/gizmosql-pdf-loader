import uuid
from pathlib import Path

import pyarrow as pa

from gizmosql_pdf_loader.loader import (
    CHUNKS_SCHEMA,
    PAGES_PER_BATCH,
    document_id_for,
    iter_chunk_batches,
    iter_page_batches,
    qualified_schema,
    schema_ddl,
)
from gizmosql_pdf_loader.pdf_extract import PageRecord


def test_document_id_is_deterministic():
    a = document_id_for("ab" * 32)
    assert a == document_id_for("ab" * 32)
    assert a != document_id_for("cd" * 32)
    assert isinstance(a, uuid.UUID) and a.version == 5


def test_iter_chunk_batches_round_trip(tmp_path: Path):
    payload = bytes(range(256)) * 41  # 10,496 bytes: not a multiple of the chunk size
    path = tmp_path / "blob.bin"
    path.write_bytes(payload)
    batches = list(iter_chunk_batches(path, document_id="doc-1", chunk_size=4096))
    assert len(batches) == 3
    assert all(b.schema.equals(CHUNKS_SCHEMA) for b in batches)
    rows = pa.Table.from_batches(batches).to_pylist()
    assert [r["chunk_index"] for r in rows] == [0, 1, 2]
    assert [r["byte_offset"] for r in rows] == [0, 4096, 8192]
    assert [r["chunk_length_bytes"] for r in rows] == [4096, 4096, 2304]
    assert b"".join(r["content"] for r in rows) == payload


def test_iter_chunk_batches_empty_file(tmp_path: Path):
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    assert list(iter_chunk_batches(path, document_id="doc-1", chunk_size=4096)) == []


def _page(n: int) -> PageRecord:
    return PageRecord(
        page_number=n,
        page_label=str(n),
        width_pt=595.0,
        height_pt=842.0,
        rotation=0,
        image_count=0,
        extraction_method="native",
        page_text=f"text {n}",
    )


def test_iter_page_batches_splits_by_batch_size():
    pages = [_page(n) for n in range(1, PAGES_PER_BATCH * 2 + 6)]
    batches = list(iter_page_batches(pages, document_id="doc-1"))
    assert [b.num_rows for b in batches] == [PAGES_PER_BATCH, PAGES_PER_BATCH, 5]
    table = pa.Table.from_batches(batches)
    assert table.column("page_number").to_pylist() == list(range(1, len(pages) + 1))
    assert table.column("char_count").to_pylist()[0] == len("text 1")


def test_qualified_schema():
    assert qualified_schema("lake", "s") == "lake.s"
    assert qualified_schema(None, "s") == "s"


def test_schema_ddl_targets_requested_schema():
    statements = schema_ddl("my_schema")
    assert statements[0] == "CREATE SCHEMA IF NOT EXISTS my_schema"
    assert all("my_schema." in s for s in statements[1:])
    assert not any("pdf_docs" in s for s in statements)


def test_schema_ddl_qualifies_with_catalog_and_is_ducklake_safe():
    statements = schema_ddl("my_schema", catalog="lake")
    assert statements[0] == "CREATE SCHEMA IF NOT EXISTS lake.my_schema"
    assert all("lake.my_schema." in s for s in statements[1:])
    # DuckLake rejects these; the loader enforces uniqueness itself.
    assert not any("PRIMARY KEY" in s or "UNIQUE" in s for s in statements)
