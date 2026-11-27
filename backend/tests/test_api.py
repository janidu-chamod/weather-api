def test_docs_available(client):
    assert client.get("/docs").status_code == 200


def test_all_high_warnings(client):
    response = client.get("/v1/alerts/all")
    assert response.status_code == 200
    data = response.json()
    assert data["alert_count"] == 1
    assert data["alerts"][0]["place"] == "Thimbirigasyaya"
    assert data["alerts"][0]["severity"] == "high"


def test_place_lookup(client):
    response = client.get("/v1/alerts/place", params={"name": "Thimbirigasyaya"})
    assert response.status_code == 200
    data = response.json()
    assert data["resolved_location"]["p_code"] == "LK1127"
    assert data["alert_count"] == 1
    assert data["has_active_warning"] is True


def test_province_lookup(client):
    response = client.get("/v1/alerts/province", params={"name": "Western"})
    assert response.status_code == 200
    data = response.json()
    assert data["resolved_province"]["p_code"] == "LK1"
    assert data["alert_count"] == 1


def test_point_lookup(client):
    response = client.get("/v1/alerts/point", params={"lat": 6.93, "lon": 79.85})
    assert response.status_code == 200
    data = response.json()
    assert data["resolved_location"]["ds_pcode"] == "LK1127"
    assert data["alert_count"] == 1


def test_unknown_place_returns_404(client):
    response = client.get("/v1/alerts/place", params={"name": "Nowhere"})
    assert response.status_code == 404
