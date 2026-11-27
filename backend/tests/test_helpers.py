import sqlite3

import pymupdf

import extractor


def test_normalize_name():
    assert extractor.normalize_name("Ganga Ihala Korale") == "gangaihalakorale"
    assert extractor.normalize_name("Kotmale East (Stage 2)") == "kotmaleeast"
    assert extractor.normalize_name("Walapane 12") == "walapane"


def test_verify_snippet():
    text = "Bulathsinhala and\nPalindanuwara\nLevel 1 (Yellow)"
    assert extractor.verify_snippet("Bulathsinhala and Palindanuwara", text) == "verified"
    assert extractor.verify_snippet("Made up village", text) == "unverified_snippet"


def test_metric_bounds():
    item = extractor.ExtractedAlertItem(
        location_name="Colombo",
        admin_level="district",
        hazard="flood",
        severity="high",
        native_level="Level 2",
        reason="Test",
        source_snippet="Colombo",
        deaths=4,
        rainfall_mm=120.5,
    )
    assert extractor.validate_metric_bounds(item) is None
    item.deaths = 99_999_999
    assert "deaths" in extractor.validate_metric_bounds(item)


def test_place_resolver(db_path):
    conn = sqlite3.connect(db_path)
    resolver = extractor.PlaceResolver(conn)
    place = resolver.resolve("Thimbirigasyaya", "ds_division", "Colombo")
    assert place is not None
    assert place["p_code"] == "LK1127"
    assert place["province_name"] == "Western"
    assert resolver.resolve("Nowhere", "ds_division", "Colombo") is None
    conn.close()


def test_ingest_pdf_without_api_key(db_path, tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    def forbidden_client():
        raise AssertionError("Gemini client must not be called when a report is supplied")

    monkeypatch.setattr(extractor, "get_genai_client", forbidden_client)

    pdf_path = tmp_path / "bulletin.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "Landslide Early Warning\nThimbirigasyaya\nLevel 2 (Amber)\n"
        "Divisional Secretariat Division and surrounding areas.\n",
    )
    doc.save(pdf_path)
    doc.close()

    report = extractor.ExtractedReport.model_validate(
        {
            "issued_at": "2026-11-27 08:00",
            "expires_at": None,
            "headline_summary": "Test bulletin",
            "alerts": [
                {
                    "location_name": "Thimbirigasyaya",
                    "parent_district": "Colombo",
                    "admin_level": "ds_division",
                    "hazard": "landslide",
                    "severity": "high",
                    "native_level": "Level 2 (Amber)",
                    "reason": "Test reason",
                    "source_snippet": "Thimbirigasyaya",
                }
            ],
        }
    )

    result = extractor.ingest_pdf(pdf_path, "nbro_landslide", "test://new-bulletin", report=report)
    assert result["status"] == "ingested"
    assert result["alerts_inserted"] == 1
    assert result["unverified_snippets"] == 0
    assert result["rejected"] == []

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 2
    conn.close()
