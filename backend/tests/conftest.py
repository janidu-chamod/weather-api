import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import extractor
import main
import seed_places

GEOMETRY = json.dumps(
    {
        "type": "Polygon",
        "coordinates": [
            [
                [79.8, 6.9],
                [79.9, 6.9],
                [79.9, 7.0],
                [79.8, 7.0],
                [79.8, 6.9],
            ]
        ],
    }
)

PLACE_COLUMNS = (
    "p_code, admin_level, name_en, normalized_name, province_pcode, province_name, "
    "district_pcode, district_name, min_lon, min_lat, max_lon, max_lat, geometry_json"
)
PLACES = [
    ("LK1", "province", "Western", "western", "LK1", "Western", None, None, 79.8, 6.9, 79.9, 7.0, GEOMETRY),
    ("LK11", "district", "Colombo", "colombo", "LK1", "Western", "LK11", "Colombo", 79.8, 6.9, 79.9, 7.0, GEOMETRY),
    ("LK1127", "ds_division", "Thimbirigasyaya", "thimbirigasyaya", "LK1", "Western", "LK11", "Colombo", 79.8, 6.9, 79.9, 7.0, GEOMETRY),
]


def build_test_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(seed_places.SCHEMA_SQL)
    conn.executemany(f"INSERT INTO places ({PLACE_COLUMNS}) VALUES ({','.join('?' * 13)})", PLACES)
    conn.execute(
        """
        INSERT INTO documents
            (source_system, source_url, content_hash, issued_at, expires_at, is_superseded, headline_summary)
        VALUES (?, ?, ?, ?, ?, 0, ?)
        """,
        ("nbro_landslide", "test://bulletin", "testhash", "2026-11-27 08:00", "2999-01-01 00:00", "Test bulletin"),
    )
    conn.execute(
        """
        INSERT INTO alerts (
            document_id, source_system, admin_level,
            province_pcode, province_name, district_pcode, district_name,
            ds_pcode, ds_name, hazard, severity, native_level,
            reason, source_snippet, verification_status
        ) VALUES (1, 'nbro_landslide', 'ds_division', 'LK1', 'Western', 'LK11', 'Colombo',
                  'LK1127', 'Thimbirigasyaya', 'landslide', 'high', 'Level 2 (Amber)',
                  'Test reason', 'Thimbirigasyaya', 'verified')
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    build_test_db(path)
    monkeypatch.setattr(extractor, "DB_PATH", path)
    monkeypatch.setattr(main, "DB_PATH", path)
    return path


@pytest.fixture
def client(db_path):
    with TestClient(main.app) as test_client:
        yield test_client
