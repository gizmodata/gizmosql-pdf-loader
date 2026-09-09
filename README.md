# gizmosql-pdf-loader

Load PDF files into a [GizmoSQL](https://gizmodata.com/gizmosql) server over Arrow Flight SQL / ADBC so that
SQL clients (and GizmoSQL-connected MCP servers) can **search the text** of product manuals and **download the
original files**.

For every PDF the loader stores:

| What | Where | Notes |
|---|---|---|
| File metadata | `documents` | name, path, MIME type, size, SHA-256, page count, PDF info (title, author, creator, producer, dates), encryption, optional-content layers, load stats |
| File bytes | `document_chunks` | fixed-size chunks (default 8 MiB) so no Arrow Flight message exceeds the gRPC limit and any client can page through them |
| Page text | `document_pages` | one row per page, with page label, size, rotation, image count and how the text was obtained (`native`, `ocr`, `none`) |
| Whole text | `document_text` (view) | pages joined with form-feed characters |
| Search helpers | `search_pages(term)`, `search_pages_ranked(term, top_k)` (table macros) | case-insensitive substring search with a snippet; BM25-ranked full-text search (stemmed) |

Works with plain DuckDB catalogs and with **DuckLake** catalogs (no primary-key / unique constraints are used;
the loader de-duplicates on SHA-256 itself).

## Install

```shell
python3 -m venv .venv && . .venv/bin/activate
pip install gizmosql-pdf-loader          # or: pip install dist/gizmosql_pdf_loader-*.whl
```

Dependencies: `adbc-driver-gizmosql` (>= 2.0.13), `pyarrow`, `pymupdf`, `click`, `python-dotenv`.

OCR of image-only pages needs no extra install: the Tesseract engine ships inside the PyMuPDF wheel, and the
only external piece, the trained language data (`eng.traineddata`, about 4 MB), is downloaded on first use into
`~/.cache/gizmosql-pdf-loader/tessdata` from the [tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast)
repository. An installed Tesseract's language folder (or `--tessdata` / `TESSDATA_PREFIX`) is used instead when
present; `--no-ocr-download` forbids the download. Without language data, image-only pages are loaded with empty
text and a warning.

## Configure

Create a `.env` in the working directory (or export the variables):

```dotenv
GIZMOSQL_HOSTNAME=gizmosql.example.com
GIZMOSQL_PORT=443
GIZMOSQL_USERNAME=me@example.com
GIZMOSQL_PASSWORD=...
GIZMOSQL_CATALOG=my_lake          # target catalog (must already be attached on the server)
GIZMOSQL_SCHEMA=pdf_docs          # target schema (created if missing); default: pdf_docs
# optional
GIZMOSQL_USE_TLS=true             # false -> gizmosql://host:port?transport=tcp
GIZMOSQL_TLS_SKIP_VERIFY=false
GIZMOSQL_MAX_MESSAGE_SIZE_BYTES=67108864   # client-side gRPC limit (driver default is 16 MiB)
```

Set the catalog explicitly. A session's default catalog is whatever database the server was started with,
which on some deployments is the ephemeral in-memory `memory` catalog; the loader refuses to load into `memory`
or `temp` so tables can't silently land somewhere that vanishes on restart.

## Use

```shell
# Load every *.pdf in a directory (or pass explicit file paths)
gizmosql-pdf-loader load --source-dir source_pdf_files

# Inspect/extract without connecting to the server
gizmosql-pdf-loader load --dry-run --source-dir source_pdf_files

# What is loaded?
gizmosql-pdf-loader list

# Search page text: substring match, or BM25-ranked via the full-text index
gizmosql-pdf-loader search "hydraulic pump" --limit 5
gizmosql-pdf-loader search "hydraulic pump relief valves" --ranked --limit 5

# (Re)build the full-text index (also done automatically at the end of `load`)
gizmosql-pdf-loader index
```

Output of a real load (six equipment manuals, 641 MB in total, into a DuckLake catalog backed by S3):

```
status            size chunks  pages w/text   ocr       chars    secs  file
loaded      73,588,282      9    457    457     0     561,719    46.7  operator-manual-1.pdf
loaded      74,815,458      9    424    424     0     556,911    52.5  operator-manual-2.pdf
loaded     113,837,480     14   1688   1688     0   1,217,423    29.8  parts-manual-1.pdf
loaded       6,783,584      1     84     84     0      60,788    10.0  parts-manual-2.pdf
loaded     359,084,382     43   3004      6     0          26   105.6  shop-manual-1.pdf
loaded      12,740,509      2     94      0     0           0    12.5  shop-manual-2.pdf
```

Useful `load` options (see `gizmosql-pdf-loader load --help` for all):

| Option | Default | Purpose |
|---|---|---|
| `--chunk-size-mib N` | 8 | Size of each stored chunk; keep it below the gRPC message limit of the clients that will download |
| `--replace` | off | Reload documents whose SHA-256 is already present (otherwise they are skipped) |
| `--ocr/--no-ocr`, `--ocr-language`, `--ocr-dpi`, `--ocr-workers` | on, `eng`, 200, CPUs-1 | OCR of pages that have images but no text (`eng+jpn` style multi-language works if each file is available) |
| `--tessdata DIR`, `--ocr-download/--no-ocr-download` | auto, on | Where Tesseract language data comes from (see Install) |
| `--reveal-hidden-layers` | off | See [Hidden layers](#hidden-layers-secure-pdfs) |
| `--max-pages N` | all | Only extract the first N pages (testing) |
| `--dry-run` | off | Extract locally, print a summary, upload nothing |
| `--fts-index/--no-fts-index` | on | Rebuild the full-text index after loading; built in place for a DuckDB-file target, skipped for a DuckLake target unless `--fts-catalog` is given |
| `--fts-catalog`, `--fts-schema` | none, `pdf_fts` | DuckDB catalog (and schema) to hold a copy-based index when the target is a DuckLake (see below) |

Documents are identified by a UUID derived from the file's SHA-256, so the same bytes always map to the same
`document_id`; re-running the loader is idempotent.

## Use as a Python library

Everything the CLI does is a plain function call; the top-level package exports the pieces you need
(`GizmoSQLSettings`, `LoadOptions`, `OcrSettings`, `ensure_schema`, `load_pdf`, `plan_fts`, `build_fts_index`,
`inspect_pdf`, `extract_pages`, ...). `examples/load_with_python.py` is the runnable version of this:

```python
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
```

`load_pdf` returns a `LoadResult` (`status` is `"loaded"` or `"skipped"`, plus chunk/page/text counts and the
`document_id`). `inspect_pdf(path)` and `extract_pages(path, ocr=...)` work without any server connection if
you only want the metadata or the text.

## Query from SQL

```sql
-- Ranked full-text search (BM25, English stemming) via the fts index
SELECT file_name, page_number, score, snippet
  FROM my_lake.pdf_docs.search_pages_ranked('hydraulic pump relief valves', top_k := 10);

-- Ranked search by hand: call the index's match_bm25 macro directly. The index schema is
-- fts_<schema>_document_pages in the catalog that holds the indexed table: the target catalog
-- for a DuckDB-file target (below), or the --fts-catalog for a DuckLake target, e.g.
-- memory.fts_pdf_fts_document_pages over memory.pdf_fts.document_pages.
-- conjunctive := 1 requires every term on the page (default: any term, ranked).
SELECT d.file_name, p.page_number, round(p.score, 2) AS score
  FROM (SELECT document_id, page_number,
               my_db.fts_pdf_docs_document_pages.match_bm25(page_id, 'hydraulic relief valve', conjunctive := 1) AS score
          FROM my_db.pdf_docs.document_pages) p
  JOIN my_db.pdf_docs.documents d USING (document_id)
 WHERE p.score IS NOT NULL
 ORDER BY p.score DESC
 LIMIT 10;

-- Substring search with a snippet (case-insensitive, no index needed)
SELECT file_name, page_number, snippet
  FROM my_lake.pdf_docs.search_pages('relief valve')
 LIMIT 10;

-- Same thing by hand
SELECT d.file_name, p.page_number, p.page_text
  FROM my_lake.pdf_docs.document_pages p
  JOIN my_lake.pdf_docs.documents d USING (document_id)
 WHERE p.page_text ILIKE '%relief valve%';

-- Metadata
SELECT file_name, mime_type, file_size_bytes, page_count, pages_with_text, title, producer, pdf_created_at
  FROM my_lake.pdf_docs.documents;

-- Fetch a file's chunks in order (each row is one chunk, so any client message limit works)
SELECT chunk_index, content
  FROM my_lake.pdf_docs.document_chunks
 WHERE document_id = (SELECT document_id FROM my_lake.pdf_docs.documents
                       WHERE file_name = 'parts-manual-2.pdf')
 ORDER BY chunk_index;
```

### Full-text index

`search_pages_ranked` is backed by DuckDB's [`fts`](https://duckdb.org/docs/stable/core_extensions/full_text_search)
extension (BM25 scoring, English stemmer). The loader looks up the target catalog's type in `duckdb_databases()`
and logs what it decides:

| Target catalog type | What happens |
|---|---|
| `duckdb` (a DuckDB database file) | The index is built **in place** on `document_pages` and persists with the file. `search_pages_ranked` is created next to the tables. |
| `ducklake` | **Skipped**, with the reason logged: an FTS index cannot be created inside a DuckLake catalog (the extension's internal tables use a NULL-typed column DuckLake rejects: "unsupported type NULL"). |
| `ducklake` + `--fts-catalog <duckdb catalog>` | The page text is **copied** into `<fts-catalog>.<fts-schema>.document_pages` (a few MB for these manuals), the index is built there in under a second, and `search_pages_ranked` in the target schema points at it. |

Loading the `fts` extension needs the GizmoSQL **admin** role; *querying* an existing index does not. The
index is a snapshot: `load` refreshes it after loading new documents, and `gizmosql-pdf-loader index` rebuilds
it on demand. If the copy catalog is the server's in-memory `memory` catalog (as in the example below), the copy
and index vanish on a server restart, so run `index` again afterwards (or point `--fts-catalog` at an attached DuckDB file).

```shell
# DuckLake target: opt in to a copy-based index in the shared in-memory catalog
gizmosql-pdf-loader index --fts-catalog memory
```

## Download a file from Python

`examples/download_document.py` streams the chunks in order and verifies the SHA-256, so it works with the
driver's default 16 MiB message limit:

```shell
python examples/download_document.py "parts-manual-2.pdf" /tmp/parts-manual-2.pdf
```

The core of it:

```python
cur.execute(
    f"SELECT content FROM {qs}.document_chunks WHERE document_id = ? AND chunk_index = ?",
    parameters=[document_id, chunk_index],
)
(content,) = cur.fetchone()
out.write(content)
```

## Hidden layers ("secure" PDFs)

Some vendor products (e.g. HyperGEAR PDFLib "secure documents") do not encrypt a PDF; they put the real
content on an optional-content layer whose default view state is OFF and rely on viewer JavaScript to switch it
on after checking a license window. Outside that viewer every page looks blank, and OCR finds nothing.

The loader records such layers in `documents.hidden_layer_names` and, by default, leaves them alone. With
`--reveal-hidden-layers` it writes a temporary copy with those layers switched ON, extracts text from the copy,
deletes it, and sets `documents.hidden_layers_revealed = true`. The stored file bytes are always the original.
Use this only for documents you are entitled to read outside the vendor's viewer.

## How the gRPC message limit is handled

- Uploads use `cursor.adbc_ingest` with a `RecordBatchReader`, one record batch per chunk, so each Flight
  message carries at most one chunk (8 MiB by default) plus a little overhead.
- The 16 MiB figure is the **client driver's default** (`adbc.flight.sql.client_option.with_max_msg_size`),
  not a server limit: a GizmoSQL server accepted and served back a single 256 MiB message in testing. The
  loader raises its own limit to 64 MiB (`GIZMOSQL_MAX_MESSAGE_SIZE_BYTES` overrides it); other clients can
  keep their defaults because they only ever read one chunk per row.
- Page text goes up 200 pages per batch.
- There is intentionally **no server-side view that reassembles a whole file**. DuckDB needs roughly
  ten times the file size in memory to concatenate large BLOBs; reassembling the 359 MB file above
  pushed the PoC server past its 4.4 GiB limit and restarted it. Reassemble on the client
  (`examples/download_document.py`) instead.

## Development

```shell
pip install -e ".[dev]"
ruff check src tests examples
pytest -q                      # the integration test runs only when GIZMOSQL_HOSTNAME is set
python -m build                # wheel + sdist in dist/
```

Releases: bump `version` in `pyproject.toml`, move the `[Unreleased]` notes in `CHANGELOG.md` into a version
section, commit, then `git push origin main vX.Y.Z`. CI runs the tests on Python 3.10 through 3.14, builds the
wheel and sdist, creates a GitHub Release with that CHANGELOG section as the notes and the artifacts attached,
and publishes the package to PyPI via
[trusted publishing](https://docs.pypi.org/trusted-publishers/) (the `pypi` GitHub environment; no token
secrets).
