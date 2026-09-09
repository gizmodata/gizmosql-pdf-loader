"""Command-line interface: ``gizmosql-pdf-loader load|list|search``."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

from . import __version__
from .config import GizmoSQLSettings, resolve_target_catalog
from .fts import DEFAULT_FTS_SCHEMA, MODE_SKIP, FtsUnavailableError, build_fts_index, plan_fts
from .loader import (
    DEFAULT_CHUNK_SIZE_BYTES,
    DEFAULT_SCHEMA,
    LoadOptions,
    ensure_schema,
    load_pdf,
    qualified_schema,
)
from .pdf_extract import OcrSettings, extract_pages, inspect_pdf, open_for_extraction


def _connection_options(func):
    func = click.option(
        "--catalog",
        envvar="GIZMOSQL_CATALOG",
        default=None,
        show_envvar=True,
        help="Target catalog (an attached database). Required unless the session default is persistent.",
    )(func)
    func = click.option(
        "--schema",
        envvar="GIZMOSQL_SCHEMA",
        default=DEFAULT_SCHEMA,
        show_default=True,
        show_envvar=True,
        help="Target schema holding the document tables.",
    )(func)
    return func


def _fts_options(func):
    func = click.option(
        "--fts-catalog",
        envvar="GIZMOSQL_FTS_CATALOG",
        default=None,
        show_envvar=True,
        help="DuckDB (non-DuckLake) catalog to hold a copy-based full-text index. Only needed when the "
        "target catalog is a DuckLake; a DuckDB-file target gets its index built in place.",
    )(func)
    func = click.option(
        "--fts-schema",
        envvar="GIZMOSQL_FTS_SCHEMA",
        default=DEFAULT_FTS_SCHEMA,
        show_default=True,
        show_envvar=True,
        help="Schema in --fts-catalog for the copied page text (copy mode only).",
    )(func)
    return func


def _rebuild_index(conn, *, catalog: str, schema: str, fts_catalog: str | None, fts_schema: str) -> bool:
    plan = plan_fts(conn, catalog=catalog, schema=schema, fts_catalog=fts_catalog, fts_schema=fts_schema)
    click.echo(
        f"full-text index: target catalog {catalog!r} is type {plan.target_type!r}; mode={plan.mode}",
        err=True,
    )
    if plan.mode == MODE_SKIP:
        click.echo(f"full-text index skipped: {plan.reason}", err=True)
        return False
    try:
        pages = build_fts_index(conn, plan)
    except FtsUnavailableError as exc:
        click.echo(f"warning: full-text index not built: {exc}", err=True)
        return False
    click.echo(f"full-text index rebuilt over {pages:,} page(s) in {plan.index_schema} ({plan.reason})")
    return True


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(version=__version__)
@click.option(
    "--env-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path(".env"),
    show_default=True,
    help="Dotenv file with GIZMOSQL_HOSTNAME/PORT/USERNAME/PASSWORD (and optionally CATALOG/SCHEMA).",
)
@click.option("-v", "--verbose", is_flag=True, help="Debug-level logging.")
def main(env_file: Path, verbose: bool) -> None:
    """Load PDF files (bytes, page text, metadata) into a GizmoSQL server."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if env_file.exists():
        load_dotenv(dotenv_path=env_file)


@main.command()
@click.argument("paths", nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option(
    "--source-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Directory to scan for *.pdf files (used when no PATHS are given).",
)
@_connection_options
@click.option(
    "--chunk-size-mib",
    type=click.IntRange(min=1, max=512),
    default=DEFAULT_CHUNK_SIZE_BYTES // (1024 * 1024),
    show_default=True,
    help="Size of each stored file chunk (must stay below the gRPC message limit).",
)
@click.option("--replace", is_flag=True, help="Reload documents whose SHA-256 is already present.")
@click.option(
    "--ocr/--no-ocr",
    default=True,
    show_default=True,
    help="OCR pages that have images but no text layer (requires Tesseract).",
)
@click.option(
    "--ocr-language", default="eng", show_default=True, help="Tesseract language code(s), e.g. eng+jpn."
)
@click.option("--ocr-dpi", type=int, default=200, show_default=True, help="Render resolution for OCR.")
@click.option(
    "--ocr-workers", type=int, default=OcrSettings.workers, show_default=True, help="Parallel OCR processes."
)
@click.option(
    "--tessdata",
    envvar="TESSDATA_PREFIX",
    default=None,
    show_envvar=True,
    type=click.Path(file_okay=False, path_type=str),
    help="Directory with Tesseract <lang>.traineddata files. Default: an installed Tesseract's folder if it "
    "has the language, else ~/.cache/gizmosql-pdf-loader/tessdata (downloaded on first use).",
)
@click.option(
    "--ocr-download/--no-ocr-download",
    default=True,
    show_default=True,
    help="Allow downloading missing language data from the tesseract-ocr/tessdata_fast repository.",
)
@click.option(
    "--reveal-hidden-layers",
    is_flag=True,
    help="Switch ON optional-content layers whose default view state is OFF before extracting text. "
    "Some vendor 'secure PDF' products hide the real content this way; only use this when you "
    "are entitled to the content.",
)
@click.option("--max-pages", type=int, default=None, help="Only extract the first N pages (testing aid).")
@click.option("--dry-run", is_flag=True, help="Inspect and extract only; do not connect or upload.")
@click.option(
    "--fts-index/--no-fts-index",
    default=True,
    show_default=True,
    help="Rebuild the full-text (BM25) index after loading. Built in place for DuckDB-file targets; "
    "skipped for DuckLake targets unless --fts-catalog is given.",
)
@_fts_options
def load(
    paths,
    source_dir,
    catalog,
    schema,
    chunk_size_mib,
    replace,
    ocr,
    ocr_language,
    ocr_dpi,
    ocr_workers,
    tessdata,
    ocr_download,
    reveal_hidden_layers,
    max_pages,
    dry_run,
    fts_index,
    fts_catalog,
    fts_schema,
) -> None:
    """Load PDFs from PATHS (or --source-dir) into the target catalog/schema."""
    files = list(paths)
    if source_dir is not None:
        files.extend(sorted(p for p in source_dir.iterdir() if p.suffix.lower() == ".pdf"))
    if not files:
        raise click.UsageError("No PDF files given. Pass file paths or --source-dir.")

    options = LoadOptions(
        schema=schema,
        catalog=catalog,
        chunk_size=chunk_size_mib * 1024 * 1024,
        replace=replace,
        reveal_hidden_layers=reveal_hidden_layers,
        ocr=OcrSettings(
            enabled=ocr,
            language=ocr_language,
            dpi=ocr_dpi,
            workers=ocr_workers,
            tessdata=tessdata,
            download=ocr_download,
        ),
        max_pages=max_pages,
    )

    if dry_run:
        for path in files:
            info = inspect_pdf(path)
            with open_for_extraction(path, reveal_hidden_layers=reveal_hidden_layers) as (
                extract_path,
                revealed,
            ):
                pages = extract_pages(extract_path, ocr=options.ocr, max_pages=max_pages)
            with_text = sum(1 for p in pages if p.page_text.strip())
            via_ocr = sum(1 for p in pages if p.extraction_method == "ocr")
            click.echo(
                f"{info.file_name}: {info.file_size_bytes:,} bytes, {info.page_count} pages, "
                f"{with_text} with text ({via_ocr} via OCR), {sum(p.char_count for p in pages):,} chars, "
                f"hidden layers={info.hidden_layer_names}, revealed={revealed}"
            )
        return

    settings = GizmoSQLSettings.from_env()
    with settings.connect(catalog=catalog) as conn:
        options.catalog = resolve_target_catalog(conn, catalog)
        ensure_schema(conn, schema=schema, catalog=options.catalog)
        results = [load_pdf(conn, path=path, options=options) for path in files]
        if fts_index and any(r.status == "loaded" for r in results):
            _rebuild_index(
                conn,
                catalog=options.catalog,
                schema=schema,
                fts_catalog=fts_catalog,
                fts_schema=fts_schema,
            )

    click.echo(
        f"{'status':8} {'size':>13} {'chunks':>6} {'pages':>6} {'w/text':>6} {'ocr':>5} "
        f"{'chars':>11} {'secs':>7}  file"
    )
    for r in results:
        click.echo(
            f"{r.status:8} {r.file_size_bytes:>13,} {r.chunk_count:>6} {r.page_count:>6} "
            f"{r.pages_with_text:>6} {r.pages_ocr:>5} {r.total_text_chars:>11,} {r.seconds:>7.1f}  "
            f"{r.file_name}"
        )


@main.command()
@_connection_options
@_fts_options
def index(catalog, schema, fts_catalog, fts_schema) -> None:
    """(Re)build the full-text BM25 index over the loaded page text.

    DuckDB-file target: the index is built in place. DuckLake target: skipped unless
    --fts-catalog names a DuckDB catalog to hold a copy. Loading the fts extension needs
    the admin role; rebuild after a restart if the copy catalog is the in-memory one.
    """
    settings = GizmoSQLSettings.from_env()
    with settings.connect(catalog=catalog) as conn:
        target_catalog = resolve_target_catalog(conn, catalog)
        ensure_schema(conn, schema=schema, catalog=target_catalog)  # idempotent; applies migrations
        ok = _rebuild_index(
            conn, catalog=target_catalog, schema=schema, fts_catalog=fts_catalog, fts_schema=fts_schema
        )
    if not ok:
        sys.exit(1)


@main.command("list")
@_connection_options
def list_documents(catalog, schema) -> None:
    """List documents already loaded into the target catalog/schema."""
    settings = GizmoSQLSettings.from_env()
    with settings.connect(catalog=catalog) as conn, conn.cursor() as cur:
        qs = qualified_schema(resolve_target_catalog(conn, catalog), schema)
        cur.execute(
            f"SELECT file_name, file_size_bytes, page_count, pages_with_text, total_text_chars, "
            f"chunk_count, hidden_layers_revealed, loaded_at, document_id::VARCHAR "
            f"FROM {qs}.documents ORDER BY file_name"
        )
        rows = cur.fetchall()
    click.echo(
        f"{'size':>13} {'pages':>6} {'w/text':>6} {'chars':>11} {'chunks':>6} {'revealed':>8}  "
        "loaded_at (UTC)      file"
    )
    for file_name, size, pages, with_text, chars, chunks, revealed, loaded_at, _doc_id in rows:
        click.echo(
            f"{size:>13,} {pages:>6} {with_text:>6} {chars:>11,} {chunks:>6} {bool(revealed)!s:>8}  "
            f"{loaded_at:%Y-%m-%d %H:%M:%S}  {file_name}"
        )
    click.echo(f"{len(rows)} document(s)")


@main.command()
@click.argument("term")
@_connection_options
@click.option("--limit", type=int, default=20, show_default=True)
@click.option(
    "--ranked/--substring",
    default=False,
    show_default=True,
    help="--ranked uses the BM25 full-text index (stemmed, relevance-ordered); "
    "--substring is a case-insensitive ILIKE match.",
)
def search(term: str, catalog, schema, limit: int, ranked: bool) -> None:
    """Search page text; prints file, page and a snippet."""
    settings = GizmoSQLSettings.from_env()
    with settings.connect(catalog=catalog) as conn, conn.cursor() as cur:
        qs = qualified_schema(resolve_target_catalog(conn, catalog), schema)
        if ranked:
            cur.execute(
                f"SELECT file_name, page_number, score, snippet FROM {qs}.search_pages_ranked(?, top_k := ?)",
                parameters=[term, limit],
            )
        else:
            cur.execute(
                f"SELECT file_name, page_number, page_label, snippet FROM {qs}.search_pages(?) LIMIT ?",
                parameters=[term, limit],
            )
        rows = cur.fetchall()
    for file_name, page_number, extra, snippet in rows:
        if ranked:
            label = f" (score {extra})"
        else:
            label = f" (label {extra})" if extra else ""
        click.echo(f"== {file_name} — page {page_number}{label}")
        click.echo("   " + " ".join(snippet.split()))
    click.echo(f"{len(rows)} hit(s)")


if __name__ == "__main__":
    main()
