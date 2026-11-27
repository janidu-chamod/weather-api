import sqlite3
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

import scraper
import seed_places

FILTER_TABLE = """
<table class="namePanel sandcomp">
  <tr>
    <td><input type="text" id="fromdate" /></td>
    <td><button type="button">Search</button></td>
  </tr>
</table>
"""


def report_table(rows, *, css_class="mtable phrmaciesdir", header_tag="td"):
    body = "".join(
        (
            f"<tr class='rowdate_{i % 2}'><td>{title}</td><td>{date}</td>"
            f"<td>{time}</td><td class='sandplustd'>"
            f"<a href='{href}'>Download</a></td></tr>"
        )
        for i, (title, date, time, href) in enumerate(rows)
    )
    return (
        f"<table class='{css_class}'><tr class='mhead'>"
        f"<{header_tag}>Title</{header_tag}><{header_tag}>Date</{header_tag}>"
        f"<{header_tag}>Time (24h)</{header_tag}><{header_tag}></{header_tag}>"
        f"</tr>{body}</table>"
    )


def feed_page(rows):
    return f"<html><body>{FILTER_TABLE}{report_table(rows)}</body></html>"


@pytest.fixture
def scraper_db(tmp_path):
    path = tmp_path / "scraper.db"
    conn = sqlite3.connect(path)
    conn.executescript(seed_places.SCHEMA_SQL)
    conn.close()
    return path


def one_feed(monkeypatch, url="https://example.test/feed"):
    feed = {"source_system": "meteo_advisory", "url": url}
    monkeypatch.setattr(scraper, "DMC_FEEDS", [feed])
    return url


def status_of(db_path, source_system="meteo_advisory"):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT status FROM system_status WHERE source_system = ?", (source_system,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_extract_pdf_links_ignores_filter_table_tamil_and_sorts_by_datetime():
    html = feed_page(
        [
            ("Weather Forecast (Tamil Language)", "2026-11-27", "16:00", "/images/ta.pdf"),
            ("Weather Forecast", "2026-11-27", "14:00", "/images/1400.pdf"),
            ("Weather Forecast", "2026-11-27", "16:00", "/images/1600.pdf"),
        ]
    )

    items = scraper.extract_pdf_links(html)

    assert len(items) == 2
    assert items[0].title == "Weather Forecast"
    assert items[0].published_at == datetime(2026, 11, 27, 16, 0)
    assert items[0].url == "https://www.dmc.gov.lk/images/1600.pdf"
    assert items[1].published_at == datetime(2026, 11, 27, 14, 0)
    assert all("ta.pdf" not in item.url for item in items)


def test_extract_pdf_links_returns_empty_when_only_filter_table_exists():
    assert scraper.extract_pdf_links(FILTER_TABLE) == []


def test_extract_pdf_links_header_fallback_without_css_class():
    html = report_table(
        [("Weather Forecast", "2026-11-27", "16:00", "/images/1600.pdf")],
        css_class="reports",
        header_tag="th",
    )

    items = scraper.extract_pdf_links(html)

    assert len(items) == 1
    assert items[0].url.endswith("/images/1600.pdf")
    assert scraper.extract_latest_pdf_link(html)[1] == items[0].url


def test_poll_once_marks_degraded_when_no_pdf_found(scraper_db, monkeypatch):
    feed_url = one_feed(monkeypatch)
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return SimpleNamespace(text=FILTER_TABLE)

    scraper.poll_once(db_path=scraper_db, fetcher=fake_fetch, sleep=lambda _: None)

    assert calls == [feed_url]
    assert status_of(scraper_db) == "degraded"
    conn = sqlite3.connect(scraper_db)
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    conn.close()


def test_poll_once_ingests_unseen_oldest_first_and_skips_seen_url(scraper_db, monkeypatch):
    feed_url = one_feed(monkeypatch)
    html = feed_page(
        [
            ("Weather Forecast", "2026-11-27", "16:00", "/images/1600.pdf"),
            ("Weather Forecast", "2026-11-27", "14:00", "/images/1400.pdf"),
            ("Weather Forecast", "2026-11-27", "12:30", "/images/1230.pdf"),
        ]
    )
    seen_url = "https://www.dmc.gov.lk/images/1600.pdf"

    conn = sqlite3.connect(scraper_db)
    conn.execute(
        """
        INSERT INTO documents (source_system, source_url, content_hash, issued_at)
        VALUES (?, ?, ?, ?)
        """,
        ("meteo_advisory", seen_url, "oldhash", "2026-11-27 16:00"),
    )
    conn.commit()
    conn.close()

    calls = []

    def fake_fetch(url):
        calls.append(url)
        if url == feed_url:
            return SimpleNamespace(text=html)
        return SimpleNamespace(content=b"%PDF-fake")

    ingested = []

    def fake_ingest(path, *, source_system, source_url):
        ingested.append((source_system, source_url, path.read_bytes()))
        return {"status": "ingested"}

    scraper.poll_once(
        db_path=scraper_db,
        fetcher=fake_fetch,
        ingester=fake_ingest,
        sleep=lambda _: None,
    )

    assert ingested == [
        ("meteo_advisory", "https://www.dmc.gov.lk/images/1230.pdf", b"%PDF-fake"),
        ("meteo_advisory", "https://www.dmc.gov.lk/images/1400.pdf", b"%PDF-fake"),
    ]
    assert calls[0] == feed_url
    assert seen_url not in calls
    assert status_of(scraper_db) == "live"


def test_poll_once_marks_degraded_when_ingest_fails(scraper_db, monkeypatch):
    one_feed(monkeypatch)
    html = feed_page(
        [("Weather Forecast", "2026-11-27", "16:00", "/images/1600.pdf")]
    )

    def fake_fetch(url):
        if url.endswith("feed"):
            return SimpleNamespace(text=html)
        return SimpleNamespace(content=b"%PDF-fake")

    def fake_ingest(path, *, source_system, source_url):
        raise RuntimeError("gemini unavailable")

    scraper.poll_once(
        db_path=scraper_db,
        fetcher=fake_fetch,
        ingester=fake_ingest,
        sleep=lambda _: None,
    )

    assert status_of(scraper_db) == "degraded"
    conn = sqlite3.connect(scraper_db)
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    conn.close()


def test_fetch_url_retries_server_errors(monkeypatch):
    attempts = {"count": 0}
    client_kwargs = []

    class FakeClient:
        def __init__(self, **kwargs):
            client_kwargs.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            attempts["count"] += 1
            request = httpx.Request("GET", url)
            if attempts["count"] < 3:
                return httpx.Response(503, request=request)
            return httpx.Response(200, text="ok", request=request)

    monkeypatch.setattr(scraper.httpx, "Client", FakeClient)

    resp = scraper.fetch_url("https://example.test", sleep=lambda _: None)

    assert resp.text == "ok"
    assert attempts["count"] == 3
    assert client_kwargs[0]["verify"] is True


def test_fetch_url_does_not_retry_client_errors(monkeypatch):
    attempts = {"count": 0}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            attempts["count"] += 1
            return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(scraper.httpx, "Client", FakeClient)

    with pytest.raises(httpx.HTTPStatusError):
        scraper.fetch_url("https://example.test", sleep=lambda _: None)

    assert attempts["count"] == 1


def test_fetch_url_respects_insecure_tls_opt_in(monkeypatch):
    monkeypatch.setenv("DMC_INSECURE_TLS", "1")
    client_kwargs = []

    class FakeClient:
        def __init__(self, **kwargs):
            client_kwargs.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            return httpx.Response(200, text="ok", request=httpx.Request("GET", url))

    monkeypatch.setattr(scraper.httpx, "Client", FakeClient)

    scraper.fetch_url("https://example.test", sleep=lambda _: None)

    assert client_kwargs[0]["verify"] is False
