"""PDF inspection and text extraction built on PyMuPDF.

Three concerns live here:

* file-level metadata (size, SHA-256, MIME type, PDF document info, layers),
* per-page text extraction (native text layer, with optional Tesseract OCR fallback
  for pages that have images but no text), and
* an *opt-in* helper that reveals optional-content layers whose default view
  state is OFF, which some "secure PDF" products use to hide the real content.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import multiprocessing
import os
import re
import shutil
import tempfile
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pymupdf

log = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"
_HASH_READ_SIZE = 4 * 1024 * 1024

# "D:YYYYMMDDHHmmSSOHH'mm'" per the PDF spec; every field after the year is optional.
_PDF_DATE_RE = re.compile(
    r"^D:(?P<year>\d{4})(?P<month>\d{2})?(?P<day>\d{2})?(?P<hour>\d{2})?(?P<minute>\d{2})?"
    r"(?P<second>\d{2})?(?P<sign>[Zz+-])?(?P<tzh>\d{2})?'?(?P<tzm>\d{2})?'?"
)


def parse_pdf_date(value: str | None) -> datetime | None:
    """Parse a PDF /CreationDate style string into an aware datetime (UTC if no offset)."""
    if not value:
        return None
    match = _PDF_DATE_RE.match(value.strip())
    if not match:
        return None
    g = match.groupdict()
    try:
        naive = datetime(
            int(g["year"]),
            int(g["month"] or 1),
            int(g["day"] or 1),
            int(g["hour"] or 0),
            int(g["minute"] or 0),
            int(g["second"] or 0),
        )
    except ValueError:
        return None
    tz = timezone.utc
    if g["sign"] in ("+", "-") and g["tzh"]:
        offset = timedelta(hours=int(g["tzh"]), minutes=int(g["tzm"] or 0))
        tz = timezone(-offset if g["sign"] == "-" else offset)
    return naive.replace(tzinfo=tz)


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(_HASH_READ_SIZE)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def guess_mime_type(path: Path) -> str:
    mime, _ = mimetypes.guess_type(url=str(path))
    if mime:
        return mime
    with open(path, "rb") as f:
        head = f.read(len(PDF_MAGIC))
    return "application/pdf" if head == PDF_MAGIC else "application/octet-stream"


@dataclass
class PdfFileInfo:
    file_name: str
    file_path: str
    file_extension: str
    mime_type: str
    file_size_bytes: int
    sha256_hex: str
    file_modified_at: datetime
    page_count: int
    pdf_format: str | None = None
    title: str | None = None
    author: str | None = None
    subject: str | None = None
    keywords: str | None = None
    creator: str | None = None
    producer: str | None = None
    pdf_created_at: datetime | None = None
    pdf_modified_at: datetime | None = None
    is_encrypted: bool = False
    encryption_method: str | None = None
    needs_password: bool = False
    layer_names: list[str] = field(default_factory=list)
    hidden_layer_names: list[str] = field(default_factory=list)


@dataclass
class PageRecord:
    page_number: int  # 1-based
    page_label: str | None
    width_pt: float
    height_pt: float
    rotation: int
    image_count: int
    extraction_method: str  # "native" | "ocr" | "none"
    page_text: str

    @property
    def char_count(self) -> int:
        return len(self.page_text)


@dataclass
class OcrSettings:
    enabled: bool = True
    language: str = "eng"
    dpi: int = 200
    workers: int = max(1, (os.cpu_count() or 2) - 1)
    tessdata: str | None = None  # resolved (and language data downloaded) by resolve_tessdata()
    download: bool = True


# Tesseract's engine ships inside the PyMuPDF wheel; only the trained language data is external.
TESSDATA_URL = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/{lang}.traineddata"


def default_tessdata_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "gizmosql-pdf-loader" / "tessdata"


def _languages(language: str) -> list[str]:
    return [lang for lang in language.split("+") if lang]


def _has_languages(directory: Path, language: str) -> bool:
    return all((directory / f"{lang}.traineddata").is_file() for lang in _languages(language))


def resolve_tessdata(settings: OcrSettings) -> str:
    """Return a tessdata directory containing every language in ``settings.language``.

    Order: ``settings.tessdata`` / ``TESSDATA_PREFIX`` (must contain the languages), an installed
    Tesseract's tessdata folder if it has them, else the loader's cache directory, downloading any
    missing ``<lang>.traineddata`` from the tessdata_fast repository (when ``settings.download``).
    Raises RuntimeError if no usable directory can be produced.
    """
    explicit = settings.tessdata or os.environ.get("TESSDATA_PREFIX")
    if explicit:
        if _has_languages(Path(explicit), settings.language):
            return explicit
        raise RuntimeError(f"tessdata directory {explicit!r} lacks language data for {settings.language!r}")

    try:
        installed = pymupdf.get_tessdata()
    except Exception:
        installed = None
    if installed and _has_languages(Path(installed), settings.language):
        return installed

    cache = default_tessdata_dir()
    missing = [
        lang for lang in _languages(settings.language) if not (cache / f"{lang}.traineddata").is_file()
    ]
    if missing and not settings.download:
        raise RuntimeError(
            f"language data for {missing} not found in {cache} and downloading is disabled "
            "(pass --tessdata or set TESSDATA_PREFIX)"
        )
    cache.mkdir(parents=True, exist_ok=True)
    for lang in missing:
        url = TESSDATA_URL.format(lang=lang)
        target = cache / f"{lang}.traineddata"
        log.info("downloading Tesseract language data %s -> %s", url, target)
        tmp = target.with_suffix(".part")
        try:
            urllib.request.urlretrieve(url, tmp)
            tmp.replace(target)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"could not download {url}: {exc}") from exc
    return str(cache)


def _layer_state(doc: pymupdf.Document) -> tuple[list[int], list[int]]:
    """Return (on_xrefs, off_xrefs) for the document's default optional-content config."""
    layer = doc.get_layer() or {}
    return list(layer.get("on") or []), list(layer.get("off") or [])


def inspect_pdf(path: Path) -> PdfFileInfo:
    path = Path(path)
    stat = path.stat()
    with pymupdf.open(path) as doc:
        meta = {k: (v or None) for k, v in (doc.metadata or {}).items()}
        ocgs = doc.get_ocgs() or {}
        _, off = _layer_state(doc)
        return PdfFileInfo(
            file_name=path.name,
            file_path=str(path.resolve()),
            file_extension=path.suffix.lower().lstrip("."),
            mime_type=guess_mime_type(path),
            file_size_bytes=stat.st_size,
            sha256_hex=sha256_of_file(path),
            file_modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            page_count=doc.page_count,
            pdf_format=meta.get("format"),
            title=meta.get("title"),
            author=meta.get("author"),
            subject=meta.get("subject"),
            keywords=meta.get("keywords"),
            creator=meta.get("creator"),
            producer=meta.get("producer"),
            pdf_created_at=parse_pdf_date(meta.get("creationDate")),
            pdf_modified_at=parse_pdf_date(meta.get("modDate")),
            is_encrypted=bool(meta.get("encryption")),
            encryption_method=meta.get("encryption"),
            needs_password=bool(doc.needs_pass),
            layer_names=[info["name"] for info in ocgs.values()],
            hidden_layer_names=[ocgs[x]["name"] for x in off if x in ocgs],
        )


def write_revealed_copy(src: Path, dst: Path) -> list[str]:
    """Write a copy of ``src`` with every default-OFF optional-content layer switched ON.

    Returns the names of the layers that were revealed (empty if the file had none, in
    which case no copy is written).
    """
    with pymupdf.open(src) as doc:
        on, off = _layer_state(doc)
        if not off:
            return []
        ocgs = doc.get_ocgs() or {}
        for xref in off:
            for key in ("Usage/View/ViewState", "View/ViewState"):
                try:
                    doc.xref_set_key(xref, key, "/ON")
                except Exception:  # key path absent on this OCG; harmless
                    pass
        catalog = doc.pdf_catalog()
        all_on = " ".join(f"{xref} 0 R" for xref in on + off)
        doc.xref_set_key(catalog, "OCProperties/D/OFF", "[]")
        doc.xref_set_key(catalog, "OCProperties/D/ON", f"[{all_on}]")
        doc.xref_set_key(catalog, "OCProperties/D/AS", "[]")
        doc.xref_set_key(catalog, "OCProperties/AS", "[]")
        doc.save(dst, garbage=0)
        return [ocgs[x]["name"] for x in off if x in ocgs]


@contextmanager
def open_for_extraction(
    path: Path, *, reveal_hidden_layers: bool = False
) -> Iterator[tuple[Path, list[str]]]:
    """Yield ``(path_to_open, revealed_layer_names)``.

    When ``reveal_hidden_layers`` is set and the file has default-OFF layers, a temporary
    modified copy is written and its path is yielded instead; it is deleted on exit.
    The original file is never modified.
    """
    path = Path(path)
    if not reveal_hidden_layers:
        yield path, []
        return
    tmp_dir = Path(tempfile.mkdtemp(prefix="gizmosql-pdf-"))
    try:
        copy_path = tmp_dir / path.name
        revealed = write_revealed_copy(src=path, dst=copy_path)
        if revealed:
            log.info("%s: revealed hidden layers %s (temporary copy)", path.name, revealed)
            yield copy_path, revealed
        else:
            yield path, []
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _page_needs_ocr(page_text: str, image_count: int) -> bool:
    return not page_text.strip() and image_count > 0


def _ocr_page(page: pymupdf.Page, settings: OcrSettings) -> str:
    textpage = page.get_textpage_ocr(
        language=settings.language, dpi=settings.dpi, full=True, tessdata=settings.tessdata
    )
    return page.get_text("text", textpage=textpage)


def _ocr_worker(args: tuple[str, list[int], OcrSettings]) -> dict[int, str]:
    pdf_path, page_numbers, settings = args
    out: dict[int, str] = {}
    with pymupdf.open(pdf_path) as doc:
        for page_number in page_numbers:
            try:
                out[page_number] = _ocr_page(page=doc[page_number - 1], settings=settings)
            except Exception as exc:  # keep going; record the failure as empty text
                log.warning("OCR failed on page %d of %s: %s", page_number, pdf_path, exc)
                out[page_number] = ""
    return out


def _run_ocr(pdf_path: Path, page_numbers: list[int], settings: OcrSettings) -> dict[int, str]:
    if not page_numbers:
        return {}
    workers = max(1, min(settings.workers, len(page_numbers)))
    if workers == 1:
        return _ocr_worker((str(pdf_path), page_numbers, settings))
    batch = max(1, -(-len(page_numbers) // workers))  # ceil division
    jobs = [
        (str(pdf_path), page_numbers[i : i + batch], settings) for i in range(0, len(page_numbers), batch)
    ]
    results: dict[int, str] = {}
    with multiprocessing.get_context("spawn").Pool(processes=workers) as pool:
        for partial in pool.imap_unordered(_ocr_worker, jobs):
            results.update(partial)
    return results


def extract_pages(
    pdf_path: Path,
    *,
    ocr: OcrSettings | None = None,
    max_pages: int | None = None,
) -> list[PageRecord]:
    """Extract per-page text from ``pdf_path``.

    Pages with a native text layer are read directly. Pages with no text but at least one
    image are OCR'd (if ``ocr.enabled``) using Tesseract via PyMuPDF.
    """
    pdf_path = Path(pdf_path)
    records: list[PageRecord] = []
    with pymupdf.open(pdf_path) as doc:
        page_count = doc.page_count if max_pages is None else min(doc.page_count, max_pages)
        for index in range(page_count):
            page = doc[index]
            text = page.get_text("text")
            image_count = len(page.get_images())
            records.append(
                PageRecord(
                    page_number=index + 1,
                    page_label=page.get_label() or None,
                    width_pt=float(page.rect.width),
                    height_pt=float(page.rect.height),
                    rotation=int(page.rotation),
                    image_count=image_count,
                    extraction_method="native" if text.strip() else "none",
                    page_text=text,
                )
            )

    ocr_candidates = [r.page_number for r in records if _page_needs_ocr(r.page_text, r.image_count)]
    if ocr and ocr.enabled and ocr_candidates:
        try:
            tessdata = resolve_tessdata(ocr)
        except RuntimeError as exc:
            log.warning(
                "%s: %d page(s) need OCR but no language data: %s", pdf_path.name, len(ocr_candidates), exc
            )
        else:
            ocr = OcrSettings(**{**ocr.__dict__, "tessdata": tessdata})
            log.info(
                "%s: running OCR on %d page(s) with %d worker(s)",
                pdf_path.name,
                len(ocr_candidates),
                ocr.workers,
            )
            ocr_text = _run_ocr(pdf_path=pdf_path, page_numbers=ocr_candidates, settings=ocr)
            by_number = {r.page_number: r for r in records}
            for page_number, text in ocr_text.items():
                if text.strip():
                    by_number[page_number].page_text = text
                    by_number[page_number].extraction_method = "ocr"
    return records
