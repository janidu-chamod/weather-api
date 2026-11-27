import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from extractor import DB_PATH, ingest_pdf

BASE_URL = "https://www.dmc.gov.lk"

DMC_FEEDS = [
    {
        "source_system": "dmc_sitrep",
        "url": f"{BASE_URL}/index.php?option=com_dmcreports&view=reports&report_type_id=1&Itemid=273&limit=5&lang=en",
    },
    {
        "source_system": "meteo_advisory",
        "url": f"{BASE_URL}/index.php?option=com_dmcreports&view=reports&report_type_id=2&Itemid=274&limit=5&lang=en",
    },
    {
        "source_system": "nbro_landslide",
        "url": f"{BASE_URL}/index.php?option=com_dmcreports&view=reports&report_type_id=5&Itemid=276&limit=5&lang=en",
    },
]

HEADERS = {
    "User-Agent": "SL-Disaster-OpenAPI-Prototype/1.0 (+https://github.com/open-civic-sl; polite-poller)",
    "Accept": "text/html,application/pdf,*/*;q=0.8",
}

REQUEST_DELAY_SEC = 4.0
TIMEOUT_SEC = 12.0


def fetch_with_ssl_fallback(url: str) -> httpx.Response:
    try:
        with httpx.Client(headers=HEADERS, timeout=TIMEOUT_SEC, follow_redirects=True, verify=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp
    except httpx.ConnectError as exc:
        if "SSL" in str(exc).upper() or "CERTIFICATE" in str(exc).upper():
            with httpx.Client(headers=HEADERS, timeout=TIMEOUT_SEC, follow_redirects=True, verify=False) as client:
                resp = client.get(url)
                resp.raise_for_status()
                return resp
        raise


def update_system_status(conn: sqlite3.Connection, source_system: str, ok: bool):
    now = datetime.now(timezone.utc).isoformat()
    if ok:
        conn.execute(
            """
            INSERT INTO system_status (source_system, last_checked_at, last_success_at, status)
            VALUES (?, ?, ?, 'live')
            ON CONFLICT(source_system) DO UPDATE SET
                last_checked_at = excluded.last_checked_at,
                last_success_at = excluded.last_success_at,
                status = 'live'
            """,
            (source_system, now, now),
        )
    else:
        conn.execute(
            """
            INSERT INTO system_status (source_system, last_checked_at, status)
            VALUES (?, ?, 'degraded')
            ON CONFLICT(source_system) DO UPDATE SET
                last_checked_at = excluded.last_checked_at,
                status = 'degraded'
            """,
            (source_system, now),
        )
    conn.commit()


def extract_latest_pdf_link(html: str) -> tuple[str, str] | None:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return None

    for row in table.find_all("tr"):
        cols = [c.get_text(" ", strip=True) for c in row.find_all("td")]
        link = row.find("a", href=True)
        if not link or len(cols) < 2:
            continue

        title = cols[0]
        href = link["href"].strip()

        if any(tag in title.lower() for tag in ("(tamil", "(sinhala", "_si.", "_ta.")):
            continue

        return title, urljoin(BASE_URL, href)

    return None


def poll_once():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")

    for idx, feed in enumerate(DMC_FEEDS):
        if idx > 0:
            time.sleep(REQUEST_DELAY_SEC)

        source_system = feed["source_system"]
        print(f"[{source_system}] Checking latest bulletin list...")

        try:
            page_resp = fetch_with_ssl_fallback(feed["url"])
            latest = extract_latest_pdf_link(page_resp.text)

            if not latest:
                print(f"[{source_system}] No report link found in table.")
                update_system_status(conn, source_system, ok=True)
                continue

            title, pdf_url = latest

            already_seen = conn.execute(
                "SELECT id FROM documents WHERE source_url = ?", (pdf_url,)
            ).fetchone()
            if already_seen:
                print(f"[{source_system}] Up to date ('{title}' already ingested).")
                update_system_status(conn, source_system, ok=True)
                continue

            time.sleep(REQUEST_DELAY_SEC)
            print(f"[{source_system}] Downloading new PDF: {title}")
            pdf_resp = fetch_with_ssl_fallback(pdf_url)

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(pdf_resp.content)
                tmp_path = Path(tmp.name)

            try:
                result = ingest_pdf(tmp_path, source_system=source_system, source_url=pdf_url)
                print(f"[{source_system}] Result: {result}")
            finally:
                tmp_path.unlink(missing_ok=True)

            update_system_status(conn, source_system, ok=True)

        except Exception as exc:
            print(f"[{source_system}] Failed gently (serving cached DB data): {exc}")
            update_system_status(conn, source_system, ok=False)

    conn.close()


def run_scheduler(interval_minutes: int = 20):
    print(f"Starting gentle DMC poller (interval: {interval_minutes} min)...")
    while True:
        poll_once()
        print(f"Sleeping {interval_minutes} minutes until next check...\n")
        time.sleep(interval_minutes * 60)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        run_scheduler(interval_minutes=20)
    else:
        poll_once()
