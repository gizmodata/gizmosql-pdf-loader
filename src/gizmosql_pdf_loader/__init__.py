"""Load PDF files into a GizmoSQL server: file bytes (chunked), extracted page text, and metadata.

Library use::

    from gizmosql_pdf_loader import GizmoSQLSettings, LoadOptions, ensure_schema, load_pdf

    settings = GizmoSQLSettings(hostname="gizmosql.example.com", port=443, username="me", password="...")
    with settings.connect(catalog="my_lake") as conn:
        ensure_schema(conn, catalog="my_lake", schema="pdf_docs")
        result = load_pdf(conn, path="manual.pdf", options=LoadOptions(catalog="my_lake", schema="pdf_docs"))
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("gizmosql-pdf-loader")
except PackageNotFoundError:  # running from a source checkout without an install
    __version__ = "0.0.0+dev"

from .config import GizmoSQLSettings, resolve_target_catalog  # noqa: E402
from .fts import FtsPlan, FtsUnavailableError, build_fts_index, plan_fts  # noqa: E402
from .loader import (  # noqa: E402
    DEFAULT_CHUNK_SIZE_BYTES,
    DEFAULT_SCHEMA,
    LoadOptions,
    LoadResult,
    catalog_type,
    delete_document,
    ensure_schema,
    find_existing_document,
    load_pdf,
)
from .pdf_extract import OcrSettings, PageRecord, PdfFileInfo, extract_pages, inspect_pdf  # noqa: E402

__all__ = [
    "DEFAULT_CHUNK_SIZE_BYTES",
    "DEFAULT_SCHEMA",
    "FtsPlan",
    "FtsUnavailableError",
    "GizmoSQLSettings",
    "LoadOptions",
    "LoadResult",
    "OcrSettings",
    "PageRecord",
    "PdfFileInfo",
    "__version__",
    "build_fts_index",
    "catalog_type",
    "delete_document",
    "ensure_schema",
    "extract_pages",
    "find_existing_document",
    "inspect_pdf",
    "load_pdf",
    "plan_fts",
    "resolve_target_catalog",
]
