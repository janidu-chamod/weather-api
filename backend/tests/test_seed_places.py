import json
import sqlite3

import pytest

import seed_places

RING = [(79.8, 6.9), (79.9, 6.9), (79.9, 7.0), (79.8, 7.0), (79.8, 6.9)]


def feature(props):
    return {
        "type": "Feature",
        "properties": props,
        "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in RING]]},
    }


def write_layer(path, *props_list):
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": [feature(p) for p in props_list]}),
        encoding="utf-8",
    )


@pytest.fixture
def conn(tmp_path):
    connection = sqlite3.connect(tmp_path / "seed.db")
    connection.row_factory = sqlite3.Row
    connection.executescript(seed_places.SCHEMA_SQL)
    yield connection
    connection.close()


def test_schema_creates_expected_tables(conn):
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"places", "documents", "alerts", "audit_rejected", "system_status"} <= tables


def test_get_prop_honours_candidate_order():
    props = {"ADM1_PCODE": "LK1", "adm1_pcode": "LK9", "adm1_name": "Western"}
    assert seed_places.get_prop(props, "adm1_pcode", "ADM1_PCODE") == "LK9"
    assert seed_places.get_prop(props, "ADM1_PCODE", "adm1_pcode") == "LK1"
    assert seed_places.get_prop(props, "missing") is None


def test_normalize_name():
    assert seed_places.normalize_name("Matale (District)") == "matale"
    assert seed_places.normalize_name("Ganga Ihala Korale 12") == "gangaihalakorale"


def test_load_lowercase_hdx_props(conn, tmp_path):
    path = tmp_path / "adm1.geojson"
    write_layer(path, {"adm1_pcode": "LK1", "adm1_name": "Western"})
    seed_places.load_layer(conn, path, "province")

    row = conn.execute("SELECT * FROM places").fetchone()
    assert row["p_code"] == "LK1"
    assert row["admin_level"] == "province"
    assert row["name_en"] == "Western"
    assert row["normalized_name"] == "western"
    assert row["province_pcode"] == "LK1"
    assert row["district_pcode"] is None
    assert (row["min_lon"], row["min_lat"], row["max_lon"], row["max_lat"]) == (79.8, 6.9, 79.9, 7.0)
    assert json.loads(row["geometry_json"])["type"] == "Polygon"


def test_load_codab_props(conn, tmp_path):
    path = tmp_path / "adm1.geojson"
    write_layer(path, {"ADM1_PCODE": "LK2", "ADM1_EN": "Central"})
    seed_places.load_layer(conn, path, "province")

    row = conn.execute("SELECT p_code, name_en FROM places").fetchone()
    assert (row["p_code"], row["name_en"]) == ("LK2", "Central")


def test_load_ds_division_keeps_parent_hierarchy(conn, tmp_path):
    path = tmp_path / "adm3.geojson"
    write_layer(
        path,
        {
            "adm1_pcode": "LK1",
            "adm1_name": "Western",
            "adm2_pcode": "LK11",
            "adm2_name": "Colombo",
            "adm3_pcode": "LK1127",
            "adm3_name": "Thimbirigasyaya",
        },
    )
    seed_places.load_layer(conn, path, "ds_division")

    row = conn.execute("SELECT * FROM places").fetchone()
    assert row["p_code"] == "LK1127"
    assert row["admin_level"] == "ds_division"
    assert row["province_name"] == "Western"
    assert row["district_pcode"] == "LK11"
    assert row["district_name"] == "Colombo"


def test_load_layer_is_idempotent(conn, tmp_path):
    path = tmp_path / "adm1.geojson"
    write_layer(path, {"adm1_pcode": "LK1", "adm1_name": "Western"})
    seed_places.load_layer(conn, path, "province")
    write_layer(path, {"adm1_pcode": "LK1", "adm1_name": "Western Province"})
    seed_places.load_layer(conn, path, "province")

    rows = conn.execute("SELECT name_en FROM places").fetchall()
    assert len(rows) == 1
    assert rows[0]["name_en"] == "Western Province"


def test_missing_geojson_raises(conn, tmp_path):
    with pytest.raises(FileNotFoundError):
        seed_places.load_layer(conn, tmp_path / "nope.geojson", "province")


def test_main_seeds_all_three_levels(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    write_layer(data_dir / "lka_admbnda_adm1.geojson", {"adm1_pcode": "LK1", "adm1_name": "Western"})
    write_layer(
        data_dir / "lka_admbnda_adm2.geojson",
        {"adm1_pcode": "LK1", "adm1_name": "Western", "adm2_pcode": "LK11", "adm2_name": "Colombo"},
    )
    write_layer(
        data_dir / "lka_admbnda_adm3.geojson",
        {
            "adm1_pcode": "LK1",
            "adm1_name": "Western",
            "adm2_pcode": "LK11",
            "adm2_name": "Colombo",
            "adm3_pcode": "LK1127",
            "adm3_name": "Thimbirigasyaya",
        },
    )
    db_path = tmp_path / "main.db"
    monkeypatch.setattr(seed_places, "DB_PATH", db_path)
    monkeypatch.setattr(seed_places, "DATA_DIR", data_dir)

    seed_places.main()

    check = sqlite3.connect(db_path)
    levels = check.execute("SELECT admin_level, COUNT(*) FROM places GROUP BY admin_level").fetchall()
    assert dict(levels) == {"province": 1, "district": 1, "ds_division": 1}
    check.close()
