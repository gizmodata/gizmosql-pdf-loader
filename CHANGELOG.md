# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] - 2026-09-09

### Changed
- Docs and messages no longer claim every GizmoSQL session defaults to the `memory` catalog; the default is
  the database the server was started with, and the loader only refuses the ephemeral `memory`/`temp` ones.
- README SQL examples include a hand-written `match_bm25` query (with `conjunctive := 1`).

## [0.1.0] - 2026-09-09

### Added
- Initial proof of concept: `gizmosql-pdf-loader load|list|search` CLI (also `python -m gizmosql_pdf_loader`).
- Loads each PDF's bytes as fixed-size chunks (default 8 MiB) through ADBC bulk ingest with a streaming
  `RecordBatchReader`, so no gRPC message exceeds the Flight SQL limit; `examples/download_document.py`
  shows a chunk-wise download. (No server-side reassembly view: concatenating large BLOBs needs ~10x the
  file size in server memory.)
- Extracts per-page text with PyMuPDF; optional Tesseract OCR fallback (parallel workers) for pages that
  have images but no text layer.
- Records file metadata (name, MIME type, size, SHA-256, PDF document info, encryption, optional-content
  layers, load statistics) in `documents`; document ids are UUIDv5 of the SHA-256 so reloads are idempotent
  (`--replace` to reload).
- `search_pages(term)` table macro and `document_text` view for text search from any SQL client.
- Importable library API (`from gizmosql_pdf_loader import ...`) with `examples/load_with_python.py`.
- BM25 full-text search via DuckDB's `fts` extension, with `search_pages_ranked(term, top_k)` macro and
  `search --ranked`. `gizmosql-pdf-loader index` (and `load`) detect the target catalog type and log the
  decision: DuckDB-file targets get the index built in place; DuckLake targets skip it (DuckLake cannot hold
  FTS indexes) unless `--fts-catalog` names a DuckDB catalog to hold a copy of the page text.
- `document_pages.page_id` single-column key (added and backfilled on existing tables).
- OCR needs no Tesseract install: language data is resolved from `--tessdata` / `TESSDATA_PREFIX`, an
  installed Tesseract, or downloaded on first use into `~/.cache/gizmosql-pdf-loader/tessdata`.
- Requires `adbc-driver-gizmosql` >= 2.0.13, whose fix makes parameterized DML execute immediately (earlier
  versions intermittently turned `--replace` deletes into no-ops); deletes are verified after execution.
- Targets an explicit catalog/schema (`GIZMOSQL_CATALOG` / `GIZMOSQL_SCHEMA`); refuses the ephemeral
  `memory` catalog. DDL is DuckLake-compatible (no primary-key / unique constraints).
- Opt-in `--reveal-hidden-layers` for PDFs whose content sits on default-hidden optional-content layers.
- CI: lint + unit tests on Python 3.10 through 3.14, wheel/sdist build, GitHub Release on `v*` tags with the
  CHANGELOG section as release notes, and PyPI publication through trusted publishing.
