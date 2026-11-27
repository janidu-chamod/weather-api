import json
import re
import sqlite3
from pathlib import Path
from shapely.geometry import shape
from shapely import simplify, to_geojson

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "disaster.db"
DATA_DIR = BASE_DIR / "data"

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS places (
    p_code TEXT PRIMARY KEY,
    admin_level TEXT NOT NULL,          -- 'province' | 'district' | 'ds_division'
    name_en TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    province_pcode TEXT NOT NULL,
    province_name TEXT NOT NULL,
    district_pcode TEXT,
    district_name TEXT,
    min_lon REAL NOT NULL,
    min_lat REAL NOT NULL,
    max_lon REAL NOT NULL,
    max_lat REAL NOT NULL,
    geometry_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_places_level_name ON places(admin_level, normalized_name);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_system TEXT NOT NULL,
    source_url TEXT UNIQUE,
    content_hash TEXT UNIQUE NOT NULL,
    issued_at DATETIME NOT NULL,
    expires_at DATETIME,
    is_superseded BOOLEAN DEFAULT 0,
    headline_summary TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    source_system TEXT NOT NULL,

    -- Spatial Hierarchy
    admin_level TEXT NOT NULL,
    province_pcode TEXT NOT NULL,
    province_name TEXT NOT NULL,
    district_pcode TEXT,
    district_name TEXT,
    ds_pcode TEXT,
    ds_name TEXT,

    -- Normalized Hazard & Severity
    hazard TEXT NOT NULL,
    severity TEXT NOT NULL,             -- 'high' | 'low'
    native_level TEXT,

    -- Typed Quantitative Metrics (NULL when not stated; ready for v2 SQL aggregations)
    affected_families INTEGER,
    affected_persons INTEGER,
    deaths INTEGER,
    missing INTEGER,
    safety_centers INTEGER,
    rainfall_mm REAL,
    water_level_m REAL,

    -- Grounding & Verification Audit
    reason TEXT NOT NULL,
    source_snippet TEXT,
    verification_status TEXT DEFAULT 'verified' -- 'verified' | 'unverified_snippet'
);

CREATE INDEX IF NOT EXISTS idx_alerts_pcodes ON alerts(province_pcode, district_pcode, ds_pcode);
CREATE INDEX IF NOT EXISTS idx_alerts_severity ON alerts(severity);

CREATE TABLE IF NOT EXISTS audit_rejected (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    raw_location_name TEXT NOT NULL,
    admin_level TEXT NOT NULL,
    rejection_reason TEXT NOT NULL,
    source_snippet TEXT
);

CREATE TABLE IF NOT EXISTS system_status (
    source_system TEXT PRIMARY KEY,
    last_checked_at DATETIME,
    last_success_at DATETIME,
    status TEXT
);
"""


def normalize_name(name: str) -> str:
    cleaned = re.sub(r"\(.*?\)", "", name)
    cleaned = re.sub(r"\s+\d+$", "", cleaned)
    return re.sub(r"[^a-z0-9]", "", cleaned.lower())


def get_prop(props: dict, *candidates: str) -> str | None:
    for key in candidates:
        if key in props and props[key]:
            return str(props[key]).strip()
    return None


def load_layer(conn: sqlite3.Connection, filepath: Path, admin_level: str):
    if not filepath.exists():
        raise FileNotFoundError(f"Missing GeoJSON file: {filepath}")

    with filepath.open("r", encoding="utf-8") as f:
        geojson = json.load(f)

    rows = []
    for feat in geojson["features"]:
        props = feat["properties"]
        geom = simplify(shape(feat["geometry"]), tolerance=0.001, preserve_topology=True)
        min_lon, min_lat, max_lon, max_lat = geom.bounds

        p1_code = get_prop(props, "admin1Pcode", "ADM1_PCODE")
        p1_name = get_prop(props, "admin1Name_en", "ADM1_EN")
        p2_code = get_prop(props, "admin2Pcode", "ADM2_PCODE")
        p2_name = get_prop(props, "admin2Name_en", "ADM2_EN")
        p3_code = get_prop(props, "admin3Pcode", "ADM3_PCODE")
        p3_name = get_prop(props, "admin3Name_en", "ADM3_EN")

        if admin_level == "province":
            p_code, name_en = p1_code, p1_name
            d_code, d_name = None, None
        elif admin_level == "district":
            p_code, name_en = p2_code, p2_name
            d_code, d_name = p2_code, p2_name
        else:
            p_code, name_en = p3_code, p3_name
            d_code, d_name = p2_code, p2_name

        rows.append((
            p_code,
            admin_level,
            name_en,
            normalize_name(name_en),
            p1_code,
            p1_name,
            d_code,
            d_name,
            min_lon,
            min_lat,
            max_lon,
            max_lat,
            to_geojson(geom),
        ))

    conn.executemany(
        """
        INSERT OR REPLACE INTO places (
            p_code, admin_level, name_en, normalized_name,
            province_pcode, province_name, district_pcode, district_name,
            min_lon, min_lat, max_lon, max_lat, geometry_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    print(f"Loaded {len(rows)} rows for {admin_level}")


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_SQL)

    load_layer(conn, DATA_DIR / "lka_admbnda_adm1.geojson", "province")
    load_layer(conn, DATA_DIR / "lka_admbnda_adm2.geojson", "district")
    load_layer(conn, DATA_DIR / "lka_admbnda_adm3.geojson", "ds_division")

    conn.commit()
    conn.close()
    print(f"Database ready at {DB_PATH}")


if __name__ == "__main__":
    main()
