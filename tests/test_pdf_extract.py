from datetime import datetime, timedelta, timezone
from pathlib import Path

import pymupdf
import pytest

from gizmosql_pdf_loader.pdf_extract import (
    OcrSettings,
    extract_pages,
    inspect_pdf,
    open_for_extraction,
    parse_pdf_date,
    resolve_tessdata,
    sha256_of_file,
    write_revealed_copy,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("D:20250609143818+09'00'", datetime(2025, 6, 9, 14, 38, 18, tzinfo=timezone(timedelta(hours=9)))),
        (
            "D:20240704151138-05'30'",
            datetime(2024, 7, 4, 15, 11, 38, tzinfo=timezone(-timedelta(hours=5, minutes=30))),
        ),
        ("D:20240704151138Z", datetime(2024, 7, 4, 15, 11, 38, tzinfo=timezone.utc)),
        ("D:2024", datetime(2024, 1, 1, tzinfo=timezone.utc)),
        ("", None),
        (None, None),
        ("not a date", None),
        ("D:20241399", None),
    ],
)
def test_parse_pdf_date(raw, expected):
    assert parse_pdf_date(raw) == expected


@pytest.fixture
def simple_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "simple.pdf"
    doc = pymupdf.open()
    for i in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), f"Hello page {i + 1}: hydraulic pump")
    doc.set_metadata({"title": "Test Doc", "author": "pytest", "creationDate": "D:20250101120000Z"})
    doc.save(path)
    doc.close()
    return path


def test_inspect_pdf(simple_pdf: Path):
    info = inspect_pdf(simple_pdf)
    assert info.file_name == "simple.pdf"
    assert info.mime_type == "application/pdf"
    assert info.file_extension == "pdf"
    assert info.page_count == 3
    assert info.title == "Test Doc"
    assert info.author == "pytest"
    assert info.pdf_created_at == datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert info.file_size_bytes == simple_pdf.stat().st_size
    assert info.sha256_hex == sha256_of_file(simple_pdf)
    assert not info.is_encrypted
    assert info.hidden_layer_names == []


def test_extract_pages_native(simple_pdf: Path):
    pages = extract_pages(simple_pdf, ocr=None)
    assert [p.page_number for p in pages] == [1, 2, 3]
    assert all(p.extraction_method == "native" for p in pages)
    assert "hydraulic pump" in pages[0].page_text
    assert pages[2].char_count == len(pages[2].page_text)


def test_extract_pages_max_pages(simple_pdf: Path):
    assert len(extract_pages(simple_pdf, ocr=None, max_pages=2)) == 2


@pytest.fixture
def hidden_layer_pdf(tmp_path: Path) -> Path:
    """A PDF whose only text lives on an optional-content layer that is OFF by default."""
    path = tmp_path / "hidden.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    ocg = doc.add_ocg(name="SecretLayer", on=False)
    page.insert_text((72, 72), "hidden treasure", oc=ocg)
    doc.save(path)
    doc.close()
    return path


def test_hidden_layer_is_detected_and_not_extracted_by_default(hidden_layer_pdf: Path):
    info = inspect_pdf(hidden_layer_pdf)
    assert info.layer_names == ["SecretLayer"]
    assert info.hidden_layer_names == ["SecretLayer"]
    pages = extract_pages(hidden_layer_pdf, ocr=None)
    assert pages[0].page_text.strip() == ""
    assert pages[0].extraction_method == "none"


def test_reveal_hidden_layers(hidden_layer_pdf: Path, tmp_path: Path):
    revealed = write_revealed_copy(src=hidden_layer_pdf, dst=tmp_path / "revealed.pdf")
    assert revealed == ["SecretLayer"]
    pages = extract_pages(tmp_path / "revealed.pdf", ocr=None)
    assert "hidden treasure" in pages[0].page_text
    # The original is untouched.
    assert extract_pages(hidden_layer_pdf, ocr=None)[0].page_text.strip() == ""


def test_open_for_extraction_cleans_up_temp_copy(hidden_layer_pdf: Path):
    with open_for_extraction(hidden_layer_pdf, reveal_hidden_layers=True) as (extract_path, revealed):
        assert revealed == ["SecretLayer"]
        assert extract_path != hidden_layer_pdf
        assert extract_path.exists()
    assert not extract_path.exists()


def test_open_for_extraction_without_reveal_returns_original(hidden_layer_pdf: Path):
    with open_for_extraction(hidden_layer_pdf, reveal_hidden_layers=False) as (extract_path, revealed):
        assert extract_path == hidden_layer_pdf
        assert revealed == []


@pytest.fixture
def image_only_pdf(tmp_path: Path) -> Path:
    """A one-page PDF whose only content is a rendered picture of some text."""
    src = pymupdf.open()
    page = src.new_page()
    page.insert_text((72, 100), "Relief valve pressure 34.3 MPa", fontsize=20)
    pix = page.get_pixmap(dpi=150)
    doc = pymupdf.open()
    target = doc.new_page(width=page.rect.width, height=page.rect.height)
    target.insert_image(target.rect, pixmap=pix)
    path = tmp_path / "scan.pdf"
    doc.save(path)
    return path


def test_resolve_tessdata_explicit_dir_must_have_language(tmp_path: Path):
    with pytest.raises(RuntimeError, match="lacks language data"):
        resolve_tessdata(OcrSettings(language="eng", tessdata=str(tmp_path)))
    (tmp_path / "eng.traineddata").write_bytes(b"x")
    assert resolve_tessdata(OcrSettings(language="eng", tessdata=str(tmp_path))) == str(tmp_path)
    with pytest.raises(RuntimeError, match="lacks language data"):
        resolve_tessdata(OcrSettings(language="eng+jpn", tessdata=str(tmp_path)))


def test_resolve_tessdata_uses_cache_without_downloading(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    # No installed Tesseract: get_tessdata() finds nothing (PyMuPDF also calls it with an explicit dir).
    monkeypatch.setattr(
        "gizmosql_pdf_loader.pdf_extract.pymupdf.get_tessdata", lambda tessdata=None: tessdata
    )
    cache = tmp_path / "gizmosql-pdf-loader" / "tessdata"
    with pytest.raises(RuntimeError, match="downloading is disabled"):
        resolve_tessdata(OcrSettings(language="eng", download=False))
    cache.mkdir(parents=True)
    (cache / "eng.traineddata").write_bytes(b"x")
    assert resolve_tessdata(OcrSettings(language="eng", download=False)) == str(cache)


def test_ocr_with_language_data_only(image_only_pdf: Path, tmp_path: Path, monkeypatch):
    """OCR needs no Tesseract binary: the engine is inside PyMuPDF, only eng.traineddata is external."""
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    # No installed Tesseract: get_tessdata() finds nothing (PyMuPDF also calls it with an explicit dir).
    monkeypatch.setattr(
        "gizmosql_pdf_loader.pdf_extract.pymupdf.get_tessdata", lambda tessdata=None: tessdata
    )
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    try:
        tessdata = resolve_tessdata(OcrSettings(language="eng"))  # downloads eng.traineddata (~4 MB)
    except RuntimeError as exc:
        pytest.skip(f"language data unavailable: {exc}")
    assert extract_pages(image_only_pdf, ocr=None)[0].extraction_method == "none"
    pages = extract_pages(image_only_pdf, ocr=OcrSettings(language="eng", workers=1, tessdata=tessdata))
    assert pages[0].extraction_method == "ocr"
    assert "Relief valve" in pages[0].page_text
