import logging
import os
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

import extractor

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
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

REQUEST_DELAY_SEC = 4.0
TIMEOUT_SEC = 12.0
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SEC = 2.0
MAX_NEW_PER_FEED = 5

NON_ENGLISH_MARKERS = (
    "(tamil",
    "tamil language",
    "(sinhala",
    "sinhala language",
    "_si.",
    "_ta.",
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeedItem:
    title: str
    published_at: datetime
    url: str


def fetch_url(url: str, *, sleep=time.sleep) -> httpx.Response:
    last_exc: Exception | None = None

    flag = os.environ.get("DMC_INSECURE_TLS", "").strip().lower()
    verify = flag not in {"1", "true", "yes", "on"}
    if not verify:
        logger.warning("DMC_INSECURE_TLS is set: SSL certificate verification is DISABLED")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        transport = httpx.HTTPTransport(local_address="0.0.0.0", verify=verify)
        try:
            with httpx.Client(
                headers=HEADERS,
                timeout=TIMEOUT_SEC,
                follow_redirects=True,
                transport=transport,
            ) as client:
                resp = client.get(url)
                resp.raise_for_status()
                return resp
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status != 429 and not (500 <= status < 600):
                raise
            last_exc = exc
        except httpx.RequestError as exc:
            last_exc = exc

        if attempt < MAX_ATTEMPTS:
            delay = RETRY_BACKOFF_SEC * (2 ** (attempt - 1))
            logger.warning(
                "Request failed (%s); retry %d/%d in %.1fs",
                last_exc,
                attempt,
                MAX_ATTEMPTS,
                delay,
            )
            sleep(delay)

    raise last_exc  # type: ignore[misc]


# Kept as an alias for callers that used the old name.
fetch_with_ssl_fallback = fetch_url


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


def _find_report_table(soup: BeautifulSoup):
    table = soup.select_one("table.mtable")
    if table is not None:
        return table

    for candidate in soup.find_all("table"):
        header = candidate.find("tr", class_="mhead") or candidate.find("tr")
        if header is None:
            continue
        labels = [
            cell.get_text(" ", strip=True).lower()
            for cell in header.find_all(["td", "th"])
        ]
        if any(label == "title" for label in labels) and any(
            "date" in label for label in labels
        ):
            return candidate
    return None


def _is_non_english(title: str, href: str) -> bool:
    haystack = f"{title} {href}".lower()
    return any(marker in haystack for marker in NON_ENGLISH_MARKERS)


def _parse_published_at(date_text: str, time_text: str) -> datetime | None:
    value = f"{date_text.strip()} {(time_text or '00:00').strip()}".strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def extract_pdf_links(html: str) -> list[FeedItem]:
    soup = BeautifulSoup(html, "html.parser")
    table = _find_report_table(soup)
    if table is None:
        return []

    items: list[FeedItem] = []
    for row in table.find_all("tr"):
        cols = row.find_all("td", recursive=False)
        if len(cols) < 3:
            continue

        link = row.find("a", href=True)
        if link is None:
            continue

        href = link["href"].strip()
        if ".pdf" not in href.lower():
            continue

        title = cols[0].get_text(" ", strip=True)
        if not title or _is_non_english(title, href):
            continue

        published_at = _parse_published_at(
            cols[1].get_text(" ", strip=True),
            cols[2].get_text(" ", strip=True) if len(cols) > 2 else "",
        )
        if published_at is None:
            logger.debug("Skipping '%s': unparseable date/time", title)
            continue

        items.append(FeedItem(title, published_at, urljoin(BASE_URL, href)))

    items.sort(key=lambda item: item.published_at, reverse=True)
    return items


def extract_latest_pdf_link(html: str) -> tuple[str, str] | None:
    items = extract_pdf_links(html)
    if not items:
        return None
    return items[0].title, items[0].url


def poll_once(
    *,
    db_path: str | Path | None = None,
    fetcher=None,
    ingester=None,
    sleep=time.sleep,
    max_new_per_feed: int = MAX_NEW_PER_FEED,
):
    fetcher = fetcher or fetch_url
    ingester = ingester or extractor.ingest_pdf

    conn = sqlite3.connect(db_path or extractor.DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        for idx, feed in enumerate(DMC_FEEDS):
            if idx > 0:
                sleep(REQUEST_DELAY_SEC)

            source_system = feed["source_system"]
            logger.info("[%s] Checking latest bulletin list...", source_system)
            ok = True

            try:
                page_resp = fetcher(feed["url"])
                items = extract_pdf_links(page_resp.text)
                if not items:
                    logger.warning(
                        "[%s] No eligible English PDF found; marking degraded", source_system
                    )
                    update_system_status(conn, source_system, ok=False)
                    continue

                unseen = []
                for item in items:
                    seen = conn.execute(
                        "SELECT 1 FROM documents WHERE source_url = ?", (item.url,)
                    ).fetchone()
                    if seen is None:
                        unseen.append(item)

                if not unseen:
                    logger.info(
                        "[%s] Up to date ('%s' already ingested).",
                        source_system,
                        items[0].title,
                    )
                    update_system_status(conn, source_system, ok=True)
                    continue

                if max_new_per_feed and max_new_per_feed > 0:
                    unseen = unseen[:max_new_per_feed]

                # Oldest first so the newest document stays active under the
                # supersede-all-for-source rule in extractor.ingest_pdf().
                batch = list(reversed(unseen))
                logger.info("[%s] %d new report(s) to ingest", source_system, len(batch))

                for item in batch:
                    sleep(REQUEST_DELAY_SEC)
                    logger.info(
                        "[%s] Downloading: %s (%s)",
                        source_system,
                        item.title,
                        item.published_at.isoformat(),
                    )
                    try:
                        pdf_resp = fetcher(item.url)
                        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                            tmp.write(pdf_resp.content)
                            tmp_path = Path(tmp.name)
                        try:
                            result = ingester(
                                tmp_path,
                                source_system=source_system,
                                source_url=item.url,
                            )
                            logger.info("[%s] Result: %s", source_system, result)
                        finally:
                            tmp_path.unlink(missing_ok=True)
                    except Exception:
                        logger.exception(
                            "[%s] Failed to ingest '%s'", source_system, item.title
                        )
                        ok = False

                update_system_status(conn, source_system, ok=ok)

            except Exception:
                logger.exception(
                    "[%s] Poll failed (serving cached DB data)", source_system
                )
                update_system_status(conn, source_system, ok=False)
    finally:
        conn.close()


def run_scheduler(interval_minutes: int = 20):
    logger.info("Starting gentle DMC poller (interval: %d min)...", interval_minutes)
    next_at = time.monotonic()
    while True:
        try:
            poll_once()
        except Exception:
            logger.exception("Unexpected scheduler error")
        next_at += interval_minutes * 60
        delay = max(0.0, next_at - time.monotonic())
        logger.info("Sleeping %.1f min until next check...", delay / 60)
        time.sleep(delay)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if "--loop" in sys.argv:
        run_scheduler(interval_minutes=20)
    else:
        poll_once()