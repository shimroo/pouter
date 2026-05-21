"""
========================================================
  GOOGLE MAPS SCRAPER — PASS 2: DETAIL EXTRACTION
  ─────────────────────────────────────────────────────
  Reads pending URLs from places.db, opens each one, calls
  extractor.extract_details(), and writes the result back.

  Run as many copies as you like:
      python pass2.py &
      python pass2.py &
      python pass2.py &

  Each instance picks up the next un-claimed URL. If a
  worker dies mid-fetch, its lease expires after
  LEASE_SECONDS and another worker re-claims that URL.
========================================================
"""

from __future__ import annotations

import json
import logging
import signal
import sys
import threading
from pathlib import Path
from time import sleep

import undetected_chromedriver as uc
from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait

import details_store
from details_store import DetailsStore
from extractor import extract_details


# ─── CONFIG ──────────────────────────────────────────────────────────────────

URLS_JSON       = "urls.json"           # pass-1 output to import from
DB_PATH         = "places.db"
HTML_DUMP_DIR   = "html"                # saved raw HTML per URL (re-extract w/o re-fetch)
SAVE_RAW_HTML   = True                  # set False to skip the HTML dump

WAIT_TIME       = 15
HEADLESS        = False

IDLE_SLEEP      = 5.0                   # pause when the queue is empty before polling again
HEARTBEAT_EVERY = 60                    # lease refresh interval (s)
PER_URL_PAUSE   = 1.5                   # polite delay between URLs


# ─── LOGGING ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(threadName)s] %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("pass2.log"),
    ],
)
log = logging.getLogger(__name__)


# ─── GRACEFUL SHUTDOWN ───────────────────────────────────────────────────────

_stop = threading.Event()

def _handle_signal(signum, _frame):
    log.info("Signal %s received — finishing current URL then exiting", signum)
    _stop.set()

signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ─── DRIVER ──────────────────────────────────────────────────────────────────

def build_driver() -> uc.Chrome:
    options = webdriver.ChromeOptions()
    options.add_argument("--incognito")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1400,1000")
    if HEADLESS:
        options.add_argument("--headless=new")
    driver = uc.Chrome(options=options)
    log.info("Driver started (incognito)")
    return driver


# ─── HEARTBEAT ───────────────────────────────────────────────────────────────

class Heartbeat(threading.Thread):
    """Refresh the lease on the currently claimed URL until told to stop."""

    def __init__(self, store: DetailsStore, wid: str, url: str):
        super().__init__(daemon=True, name=f"hb-{wid[-6:]}")
        self.store = store
        self.wid = wid
        self.url = url
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(HEARTBEAT_EVERY):
            try:
                ok = self.store.refresh_lease(self.url, self.wid)
                if not ok:
                    log.warning("Lost lease on %s — another worker has it", self.url)
                    return
            except Exception as e:
                log.warning("Heartbeat error: %s", e)

    def stop(self) -> None:
        self._stop.set()


# ─── HTML DUMP ───────────────────────────────────────────────────────────────

def _dump_html(driver, url: str) -> str | None:
    """
    Save the page source under HTML_DUMP_DIR keyed by a safe filename.
    Returns the saved path so we can record it in the DB and re-extract
    later without re-fetching the page.
    """
    if not SAVE_RAW_HTML:
        return None
    try:
        Path(HTML_DUMP_DIR).mkdir(parents=True, exist_ok=True)
        # crude but stable: hash the URL into a filename
        safe = str(abs(hash(url))) + ".html"
        path = Path(HTML_DUMP_DIR) / safe
        path.write_text(driver.page_source, encoding="utf-8")
        return str(path)
    except Exception as e:
        log.debug("HTML dump failed for %s: %s", url, e)
        return None


# ─── WORKER LOOP ─────────────────────────────────────────────────────────────

def run_worker() -> None:
    wid = details_store.worker_id()
    log.info("Worker id: %s", wid)

    store = DetailsStore(DB_PATH)

    # Idempotent — pulls in any new URLs that pass 1 has discovered.
    added = store.import_from_urls_json(URLS_JSON)
    log.info("Imported %d new URLs from %s", added, URLS_JSON)
    log.info("Queue snapshot: %s", store.stats())

    driver = build_driver()
    wait   = WebDriverWait(driver, WAIT_TIME)  # noqa: F841 -- available for extractor

    try:
        while not _stop.is_set():
            row = store.claim_next(wid)
            if row is None:
                log.info("Nothing to do — sleeping %.1fs", IDLE_SLEEP)
                # Interruptible sleep so SIGINT is responsive.
                if _stop.wait(IDLE_SLEEP):
                    break
                continue

            url = row["url"]
            attempt = row["attempts"]
            log.info("→ claim (attempt %d): %s", attempt, url)

            hb = Heartbeat(store, wid, url)
            hb.start()
            try:
                details = extract_details(driver, url)
                html_path = _dump_html(driver, url)

                ok = store.mark_completed(url, wid, details, html_path=html_path)
                if not ok:
                    log.warning("Could not mark completed — lease lost: %s", url)
                else:
                    n_fields = len(details) - 1  # minus source_url
                    log.info("✓ completed (%d fields): %s", n_fields, url)

            except Exception as e:
                log.exception("Extraction failed: %s", url)
                store.mark_failed(url, wid, repr(e))
            finally:
                hb.stop()

            if _stop.is_set():
                break
            sleep(PER_URL_PAUSE)

    finally:
        log.info("Final queue snapshot: %s", store.stats())
        log.info("Quitting driver")
        try:
            driver.quit()
        except Exception:
            pass
        store.close()


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main() -> None:
    if not Path(URLS_JSON).exists() and not Path(DB_PATH).exists():
        log.error("Neither %s nor %s exists — nothing to scrape", URLS_JSON, DB_PATH)
        sys.exit(1)
    run_worker()


if __name__ == "__main__":
    main()
