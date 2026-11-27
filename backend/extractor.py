import hashlib
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Literal, Optional

import pymupdf
from google import genai
from google.genai import types
from google.genai.errors import APIError
from pydantic import BaseModel, Field
from rapidfuzz import fuzz, process

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "disaster.db"

DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_FALLBACK_MODEL = "gemini-3.5-flash-lite"


def get_genai_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable is not set.")
    return genai.Client(api_key=api_key)


class ExtractedAlertItem(BaseModel):
    location_name: str = Field(
        description="Name of the Province, District, or DS Division. Expand grouped lists into individual entries."
    )
    parent_district: Optional[str] = Field(
        None,
        description="If admin_level is 'ds_division', specify its parent District name to disambiguate duplicate names.",
    )
    admin_level: Literal["province", "district", "ds_division"] = Field(
        description="Use 'district' if the table says 'All DSs' or lists a whole district."
    )
    hazard: Literal[
        "flood", "landslide", "heavy_rain", "strong_wind", "lightning", "heat", "general_disaster"
    ]
    severity: Literal["high", "low"] = Field(
        description=(
            "'high' for Red/Amber advisories, Landslide Level 2 or 3, Major/Minor Floods, "
            "or Situation Report districts with deaths, active safety centers, or >100 affected families. "
            "'low' for Yellow/Level 1/Alert level or minor routine mentions."
        )
    )
    native_level: str = Field(
        description="Original warning label or key metric from the PDF, e.g., 'Level 3 (Red)', '142 families affected'."
    )
    reason: str = Field(
        description="Concise 1-sentence reason for the warning or impact summary."
    )
    source_snippet: str = Field(
        description="Exact verbatim line or table row copied from the PDF text supporting this alert."
    )
    affected_families: Optional[int] = None
    affected_persons: Optional[int] = None
    deaths: Optional[int] = None
    missing: Optional[int] = None
    safety_centers: Optional[int] = None
    rainfall_mm: Optional[float] = None
    water_level_m: Optional[float] = None


class ExtractedReport(BaseModel):
    issued_at: str = Field(description="ISO-8601 timestamp or YYYY-MM-DD HH:MM from report header.")
    expires_at: Optional[str] = Field(
        None, description="ISO-8601 expiration timestamp if 'Valid Until' is stated, else null."
    )
    headline_summary: str = Field(description="One-sentence summary of the overall bulletin.")
    alerts: list[ExtractedAlertItem]


SYSTEM_INSTRUCTION = """
You are a disaster data normalizer for Sri Lanka (DMC, NBRO, Dept of Meteorology).
Extract structured warnings and affected locations from the provided English PDF text.

Rules:
1. Ignore garbled non-Unicode Sinhala/Tamil font blocks; read only English text and tables.
2. Ignore 'Total' summary rows at the bottom of Situation Report tables.
3. Expand grouped regions into individual entries (e.g., 'Western and Sabaragamuwa provinces' -> two separate province items).
4. If a landslide or disaster table specifies 'All DSs' under a District, output one item with admin_level='district'.
5. Populate numeric metric fields (affected_families, deaths, rainfall_mm, etc.) ONLY when explicitly stated; otherwise leave null.
6. Copy an exact verbatim substring from the PDF into source_snippet for every alert. Never invent locations or numbers.
"""


def extract_pdf_text(pdf_path: Path) -> str:
    doc = pymupdf.open(pdf_path)
    pages = [page.get_text("text") for page in doc]
    doc.close()
    return "\n--- PAGE BREAK ---\n".join(pages).strip()


def normalize_name(name: str) -> str:
    cleaned = re.sub(r"\(.*?\)", "", name)
    cleaned = re.sub(r"\s+\d+$", "", cleaned)
    return re.sub(r"[^a-z0-9]", "", cleaned.lower())


def verify_snippet(snippet: str, raw_pdf_text: str) -> str:
    clean_snip = re.sub(r"\s+", " ", snippet).strip().lower()
    clean_pdf = re.sub(r"\s+", " ", raw_pdf_text).strip().lower()
    if len(clean_snip) >= 6 and clean_snip in clean_pdf:
        return "verified"
    return "unverified_snippet"


def validate_metric_bounds(item: ExtractedAlertItem) -> Optional[str]:
    int_bounds = {
        "affected_families": (0, 500_000),
        "affected_persons": (0, 2_000_000),
        "deaths": (0, 50_000),
        "missing": (0, 50_000),
        "safety_centers": (0, 5_000),
    }
    for field, (low, high) in int_bounds.items():
        val = getattr(item, field)
        if val is not None and not (low <= val <= high):
            return f"Metric {field}={val} outside bounds [{low}, {high}]"

    if item.rainfall_mm is not None and not (0.0 <= item.rainfall_mm <= 2_000.0):
        return f"Metric rainfall_mm={item.rainfall_mm} outside bounds [0, 2000]"

    if item.water_level_m is not None and not (-5.0 <= item.water_level_m <= 100.0):
        return f"Metric water_level_m={item.water_level_m} outside bounds [-5, 100]"

    return None


class PlaceResolver:
    def __init__(self, conn: sqlite3.Connection):
        rows = conn.execute(
            """
            SELECT p_code, admin_level, name_en, normalized_name,
                   province_pcode, province_name, district_pcode, district_name
            FROM places
            """
        ).fetchall()

        self.places = [
            {
                "p_code": r[0],
                "admin_level": r[1],
                "name_en": r[2],
                "normalized_name": r[3],
                "province_pcode": r[4],
                "province_name": r[5],
                "district_pcode": r[6],
                "district_name": r[7],
            }
            for r in rows
        ]

    def resolve(
        self, raw_name: str, admin_level: str, parent_district: Optional[str] = None
    ) -> Optional[dict]:
        norm_target = normalize_name(raw_name)
        if not norm_target:
            return None

        candidates = [p for p in self.places if p["admin_level"] == admin_level]

        if admin_level == "ds_division" and parent_district:
            norm_parent = normalize_name(parent_district)
            scoped = [
                p for p in candidates
                if p["district_name"] and normalize_name(p["district_name"]) == norm_parent
            ]
            if scoped:
                candidates = scoped

        for p in candidates:
            if p["normalized_name"] == norm_target:
                return p

        choices = {i: p["normalized_name"] for i, p in enumerate(candidates)}
        match = process.extractOne(norm_target, choices, scorer=fuzz.WRatio, score_cutoff=85)
        if match:
            _, _, idx = match
            return candidates[idx]

        return None


def call_gemini_with_retry(
    client: genai.Client,
    raw_text: str,
    source_system: str,
    max_attempts: int = 3,
) -> ExtractedReport:
    primary = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    fallback_env = os.environ.get("GEMINI_FALLBACK_MODEL")
    fallback = (
        DEFAULT_FALLBACK_MODEL
        if fallback_env is None
        else fallback_env.strip()
    )

    models = [primary]
    if fallback and fallback != primary:
        models.append(fallback)

    last_err: Exception | None = None
    for model_name in models:
        for attempt in range(max_attempts):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=f"Extract disaster alert data from this {source_system} document:\n\n{raw_text}",
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION,
                        response_mime_type="application/json",
                        response_schema=ExtractedReport,
                    ),
                )
                return ExtractedReport.model_validate_json(response.text)
            except APIError as exc:
                last_err = exc
                if exc.code in (429, 500, 503, 504) and attempt < max_attempts - 1:
                    wait = 2 ** (attempt + 1)
                    time.sleep(wait)
                    continue
                break

    raise last_err or RuntimeError("Gemini extraction failed.")


def ingest_pdf(
    pdf_path: str | Path,
    source_system: Literal["dmc_sitrep", "nbro_landslide", "meteo_advisory"],
    source_url: Optional[str] = None,
    report: Optional[ExtractedReport] = None,
) -> dict:
    pdf_path = Path(pdf_path)
    raw_text = extract_pdf_text(pdf_path)
    if len(raw_text) < 50:
        raise ValueError("Extracted PDF text is empty or unreadable.")

    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")

    existing = conn.execute(
        "SELECT id FROM documents WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    if existing:
        conn.close()
        return {"status": "skipped_duplicate", "document_id": existing[0]}

    if report is None:
        client = get_genai_client()
        report = call_gemini_with_retry(client, raw_text, source_system)
    resolver = PlaceResolver(conn)

    conn.execute(
        "UPDATE documents SET is_superseded = 1 WHERE source_system = ? AND is_superseded = 0",
        (source_system,),
    )

    cur = conn.execute(
        """
        INSERT INTO documents (source_system, source_url, content_hash, issued_at, expires_at, headline_summary)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            source_system,
            source_url or f"local://{pdf_path.name}",
            content_hash,
            report.issued_at,
            report.expires_at,
            report.headline_summary,
        ),
    )
    doc_id = cur.lastrowid

    inserted_count = 0
    unverified_count = 0
    rejected = []

    for item in report.alerts:
        bounds_err = validate_metric_bounds(item)
        if bounds_err:
            conn.execute(
                """
                INSERT INTO audit_rejected (document_id, raw_location_name, admin_level, rejection_reason, source_snippet)
                VALUES (?, ?, ?, ?, ?)
                """,
                (doc_id, item.location_name, item.admin_level, bounds_err, item.source_snippet),
            )
            rejected.append(f"{item.location_name}: {bounds_err}")
            continue

        place = resolver.resolve(item.location_name, item.admin_level, item.parent_district)
        if not place:
            conn.execute(
                """
                INSERT INTO audit_rejected (document_id, raw_location_name, admin_level, rejection_reason, source_snippet)
                VALUES (?, ?, ?, ?, ?)
                """,
                (doc_id, item.location_name, item.admin_level, "Unmatched place name", item.source_snippet),
            )
            rejected.append(f"{item.location_name} ({item.admin_level})")
            continue

        v_status = verify_snippet(item.source_snippet, raw_text)
        if v_status != "verified":
            unverified_count += 1

        ds_pcode = place["p_code"] if item.admin_level == "ds_division" else None
        ds_name = place["name_en"] if item.admin_level == "ds_division" else None

        conn.execute(
            """
            INSERT INTO alerts (
                document_id, source_system, admin_level,
                province_pcode, province_name, district_pcode, district_name,
                ds_pcode, ds_name, hazard, severity, native_level,
                affected_families, affected_persons, deaths, missing,
                safety_centers, rainfall_mm, water_level_m,
                reason, source_snippet, verification_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc_id,
                source_system,
                item.admin_level,
                place["province_pcode"],
                place["province_name"],
                place["district_pcode"],
                place["district_name"],
                ds_pcode,
                ds_name,
                item.hazard,
                item.severity,
                item.native_level,
                item.affected_families,
                item.affected_persons,
                item.deaths,
                item.missing,
                item.safety_centers,
                item.rainfall_mm,
                item.water_level_m,
                item.reason,
                item.source_snippet,
                v_status,
            ),
        )
        inserted_count += 1

    conn.commit()
    conn.close()

    return {
        "status": "ingested",
        "document_id": doc_id,
        "summary": report.headline_summary,
        "alerts_inserted": inserted_count,
        "unverified_snippets": unverified_count,
        "rejected": rejected,
    }


if __name__ == "__main__":
    sample = BASE_DIR / "fixtures" / "sample_landslide.pdf"
    if sample.exists():
        print(ingest_pdf(sample, source_system="nbro_landslide"))
    else:
        print(f"Place a sample PDF at {sample} to test ingestion.")