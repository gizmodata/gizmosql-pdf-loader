import gizmosql_pdf_loader as pkg


def test_public_exports_are_importable():
    for name in pkg.__all__:
        assert hasattr(pkg, name), name
    for name in (
        "GizmoSQLSettings",
        "LoadOptions",
        "OcrSettings",
        "ensure_schema",
        "load_pdf",
        "plan_fts",
        "build_fts_index",
        "inspect_pdf",
        "extract_pages",
    ):
        assert name in pkg.__all__
