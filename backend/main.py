import json
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from rapidfuzz import fuzz, process
from shapely import Point, STRtree, from_geojson

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "disaster.db"

SEVERITY_RANK = {"high": 0, "low": 1}
LEVEL_RANK = {"ds_division": 0, "district": 1, "province": 2}


def normalize_name(name: str) -> str:
    cleaned = re.sub(r"\(.*?\)", "", name)
    cleaned = re.sub(r"\s+\d+$", "", cleaned)
    return re.sub(r"[^a-z0-9]", "", cleaned.lower())


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = get_db()
    rows = conn.execute(
        """
        SELECT p_code, admin_level, name_en, normalized_name,
               province_pcode, province_name, district_pcode, district_name, geometry_json
        FROM places
        """
    ).fetchall()
    conn.close()

    all_places = [dict(r) for r in rows]
    ds_places = [p for p in all_places if p["admin_level"] == "ds_division"]
    ds_geometries = [from_geojson(p["geometry_json"]) for p in ds_places]

    app.state.all_places = all_places
    app.state.ds_places = ds_places
    app.state.ds_tree = STRtree(ds_geometries) if ds_geometries else None

    yield

    app.state.all_places.clear()
    app.state.ds_places.clear()
    app.state.ds_tree = None


app = FastAPI(
    title="Sri Lanka Disaster & Weather Alerts API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def get_meta(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT source_system, last_checked_at, status FROM system_status"
    ).fetchall()
    if not rows:
        return {"data_status": "seeded_or_local", "sources": {}}

    sources = {
        r["source_system"]: {
            "last_checked_at": r["last_checked_at"],
            "status": r["status"],
        }
        for r in rows
    }
    overall = (
        "degraded"
        if any(r["status"] == "degraded" for r in rows)
        else "live"
    )
    return {"data_status": overall, "sources": sources}


def format_alerts(rows: list[sqlite3.Row], include_geo: bool = False) -> list[dict]:
    sorted_rows = sorted(
        rows,
        key=lambda r: (
            SEVERITY_RANK.get(r["severity"], 9),
            LEVEL_RANK.get(r["admin_level"], 9),
        ),
    )
    results = []
    for r in sorted_rows:
        place_name = (
            r["ds_name"]
            if r["admin_level"] == "ds_division"
            else r["district_name"]
            if r["admin_level"] == "district"
            else r["province_name"]
        )
        item = {
            "id": r["id"],
            "place": place_name,
            "admin_level": r["admin_level"],
            "ds_division": r["ds_name"],
            "district": r["district_name"],
            "province": r["province_name"],
            "hazard": r["hazard"],
            "severity": r["severity"],
            "native_level": r["native_level"],
            "reason": r["reason"],
            "source_system": r["source_system"],
            "verification_status": r["verification_status"],
            "issued_at": r["issued_at"],
            "expires_at": r["expires_at"],
        }
        if include_geo and "geometry_json" in r.keys() and r["geometry_json"]:
            item["geometry"] = json.loads(r["geometry_json"])
        results.append(item)
    return results


def resolve_place_by_name(
    all_places: list[dict], name: str, allowed_levels: tuple[str, ...]
) -> Optional[dict]:
    norm_target = normalize_name(name)
    if not norm_target:
        return None

    candidates = [p for p in all_places if p["admin_level"] in allowed_levels]

    for level in allowed_levels:
        for p in candidates:
            if p["admin_level"] == level and p["normalized_name"] == norm_target:
                return p

    choices = {i: p["normalized_name"] for i, p in enumerate(candidates)}
    match = process.extractOne(norm_target, choices, scorer=fuzz.WRatio, score_cutoff=80)
    if match:
        _, _, idx = match
        return candidates[idx]
    return None


@app.get("/v1/alerts/point")
def alerts_by_point(
    request: Request,
    lat: float = Query(..., description="Latitude in decimal degrees (e.g., 5.9549)"),
    lon: float = Query(..., description="Longitude in decimal degrees (e.g., 80.5550)"),
):
    if not (5.8 <= lat <= 9.9 and 79.5 <= lon <= 82.0):
        raise HTTPException(
            status_code=400,
            detail="Coordinates fall outside Sri Lanka's bounding box (lat 5.8–9.9, lon 79.5–82.0).",
        )

    tree: Optional[STRtree] = request.app.state.ds_tree
    ds_places: list[dict] = request.app.state.ds_places
    if not tree:
        raise HTTPException(status_code=503, detail="Spatial index not initialized. Run seed_places.py first.")

    pt = Point(lon, lat)
    indices = tree.query(pt, predicate="contains")

    if len(indices) == 0:
        indices = tree.query(pt, predicate="dwithin", distance=0.02)

    if len(indices) == 0:
        raise HTTPException(status_code=404, detail="Point is in open water outside Sri Lanka land boundaries.")

    matched_ds = ds_places[int(indices[0])]

    conn = get_db()
    rows = conn.execute(
        """
        SELECT a.*, d.issued_at, d.expires_at
        FROM alerts a
        JOIN documents d ON a.document_id = d.id
        WHERE d.is_superseded = 0
          AND (d.expires_at IS NULL OR d.expires_at > CURRENT_TIMESTAMP)
          AND (
              a.ds_pcode = ?
              OR (a.admin_level = 'district' AND a.district_pcode = ?)
              OR (a.admin_level = 'province' AND a.province_pcode = ?)
          )
        """,
        (
            matched_ds["p_code"],
            matched_ds["district_pcode"],
            matched_ds["province_pcode"],
        ),
    ).fetchall()
    meta = get_meta(conn)
    conn.close()

    alerts = format_alerts(rows)
    return {
        "query": {"type": "point", "lat": lat, "lon": lon},
        "resolved_location": {
            "ds_division": matched_ds["name_en"],
            "ds_pcode": matched_ds["p_code"],
            "district": matched_ds["district_name"],
            "district_pcode": matched_ds["district_pcode"],
            "province": matched_ds["province_name"],
            "province_pcode": matched_ds["province_pcode"],
        },
        "has_active_warning": len(alerts) > 0,
        "alert_count": len(alerts),
        "alerts": alerts,
        "meta": meta,
    }


@app.get("/v1/alerts/place")
def alerts_by_place(
    request: Request,
    name: str = Query(..., min_length=2, description="District or DS Division name (e.g., Matara, Kotapola)"),
):
    place = resolve_place_by_name(
        request.app.state.all_places, name, allowed_levels=("district", "ds_division")
    )
    if not place:
        raise HTTPException(status_code=404, detail=f"Could not match '{name}' to a Sri Lankan District or DS Division.")

    conn = get_db()
    if place["admin_level"] == "district":
        rows = conn.execute(
            """
            SELECT a.*, d.issued_at, d.expires_at
            FROM alerts a
            JOIN documents d ON a.document_id = d.id
            WHERE d.is_superseded = 0
              AND (d.expires_at IS NULL OR d.expires_at > CURRENT_TIMESTAMP)
              AND (
                  a.district_pcode = ?
                  OR (a.admin_level = 'province' AND a.province_pcode = ?)
              )
            """,
            (place["p_code"], place["province_pcode"]),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT a.*, d.issued_at, d.expires_at
            FROM alerts a
            JOIN documents d ON a.document_id = d.id
            WHERE d.is_superseded = 0
              AND (d.expires_at IS NULL OR d.expires_at > CURRENT_TIMESTAMP)
              AND (
                  a.ds_pcode = ?
                  OR (a.admin_level = 'district' AND a.district_pcode = ?)
                  OR (a.admin_level = 'province' AND a.province_pcode = ?)
              )
            """,
            (place["p_code"], place["district_pcode"], place["province_pcode"]),
        ).fetchall()

    meta = get_meta(conn)
    conn.close()

    alerts = format_alerts(rows)
    return {
        "query": {"type": "place", "name": name},
        "resolved_location": {
            "name": place["name_en"],
            "admin_level": place["admin_level"],
            "p_code": place["p_code"],
            "district": place["district_name"],
            "province": place["province_name"],
        },
        "has_active_warning": len(alerts) > 0,
        "alert_count": len(alerts),
        "alerts": alerts,
        "meta": meta,
    }


@app.get("/v1/alerts/all")
def all_high_warnings(
    include_geo: bool = Query(False, description="Include GeoJSON polygons for map rendering"),
):
    conn = get_db()
    geo_select = ", p.geometry_json" if include_geo else ""
    geo_join = (
        "LEFT JOIN places p ON p.p_code = COALESCE(a.ds_pcode, a.district_pcode, a.province_pcode)"
        if include_geo
        else ""
    )

    rows = conn.execute(
        f"""
        SELECT a.*, d.issued_at, d.expires_at{geo_select}
        FROM alerts a
        JOIN documents d ON a.document_id = d.id
        {geo_join}
        WHERE d.is_superseded = 0
          AND (d.expires_at IS NULL OR d.expires_at > CURRENT_TIMESTAMP)
          AND a.severity = 'high'
        """
    ).fetchall()
    meta = get_meta(conn)
    conn.close()

    alerts = format_alerts(rows, include_geo=include_geo)
    return {
        "query": {"type": "all", "severity_filter": "high"},
        "alert_count": len(alerts),
        "alerts": alerts,
        "meta": meta,
    }


@app.get("/v1/alerts/province")
def alerts_by_province(
    request: Request,
    name: str = Query(..., min_length=2, description="Province name (e.g., Southern, Western, Central)"),
    include_geo: bool = Query(False, description="Include GeoJSON polygons for map rendering"),
):
    province = resolve_place_by_name(
        request.app.state.all_places, name, allowed_levels=("province",)
    )
    if not province:
        raise HTTPException(status_code=404, detail=f"Could not match '{name}' to a Sri Lankan Province.")

    conn = get_db()
    geo_select = ", p.geometry_json" if include_geo else ""
    geo_join = (
        "LEFT JOIN places p ON p.p_code = COALESCE(a.ds_pcode, a.district_pcode, a.province_pcode)"
        if include_geo
        else ""
    )

    rows = conn.execute(
        f"""
        SELECT a.*, d.issued_at, d.expires_at{geo_select}
        FROM alerts a
        JOIN documents d ON a.document_id = d.id
        {geo_join}
        WHERE d.is_superseded = 0
          AND (d.expires_at IS NULL OR d.expires_at > CURRENT_TIMESTAMP)
          AND a.province_pcode = ?
        """,
        (province["p_code"],),
    ).fetchall()
    meta = get_meta(conn)
    conn.close()

    alerts = format_alerts(rows, include_geo=include_geo)
    return {
        "query": {"type": "province", "name": name},
        "resolved_province": {
            "name": province["name_en"],
            "p_code": province["p_code"],
        },
        "has_active_warning": len(alerts) > 0,
        "alert_count": len(alerts),
        "alerts": alerts,
        "meta": meta,
    }
