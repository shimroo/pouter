"""
========================================================
  GOOGLE MAPS SCRAPER — PASS 2: DETAIL EXTRACTION
  ─────────────────────────────────────────────────────
  Usage:
    python pass2.py                 # 1 worker
    python pass2.py -w 4            # 4 parallel workers
    python pass2.py -d places.db    # custom DB path

  Each worker atomically claims the next un-processed URL
  from places.db (SQLite WAL — no two workers collide).
  If a worker dies, its lease expires after LEASE_SECONDS
  and another worker re-claims that URL automatically.

  Workers pull new URLs from urls.json each time they
  start, so you can run pass 1 and pass 2 simultaneously.
========================================================
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing as mp
import os
import re
import shutil
import signal
import subprocess
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

URLS_JSON       = "urls.json"
DB_PATH         = "places.db"
HTML_DUMP_DIR   = "html"
SAVE_RAW_HTML   = False     # opt-in via --save-html

WAIT_TIME       = 15
HEADLESS        = False

IDLE_SLEEP      = 5.0
HEARTBEAT_EVERY = 60
LOG_EVERY       = 25    # print queue stats every N completions
PER_URL_PAUSE   = 0.0   # parallel workers already provide rate-limiting headroom


# ─── LOGGING ─────────────────────────────────────────────────────────────────

def setup_logging(worker_id: int | None = None) -> logging.Logger:
    tag    = f"W{worker_id}" if worker_id is not None else "main"
    suffix = f"_{worker_id}" if worker_id is not None else ""
    fmt    = f"%(asctime)s  [{tag}]  %(levelname)-8s  %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(f"pass2{suffix}.log"),
        ],
        force=True,
    )
    return logging.getLogger(__name__)

log = setup_logging()


# ─── GRACEFUL SHUTDOWN ───────────────────────────────────────────────────────

_stop = threading.Event()

def _handle_signal(signum, _frame):
    log.info("Signal %s received — finishing current URL then exiting", signum)
    _stop.set()

signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ─── DRIVER (mirrors scrapper.py pattern) ────────────────────────────────────

def _chrome_major_version() -> int | None:
    for cmd in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        try:
            out = subprocess.check_output(
                [cmd, "--version"], stderr=subprocess.DEVNULL
            ).decode()
            m = re.search(r"(\d+)\.\d+\.\d+", out)
            if m:
                return int(m.group(1))
        except Exception:
            continue
    return None


_DRIVER_DIR  = Path.home() / ".local/share/undetected_chromedriver"
_BASE_DRIVER = _DRIVER_DIR / "undetected_chromedriver"


def _ensure_base_driver(version: int | None) -> None:
    """Download and patch chromedriver once in the main process."""
    if _BASE_DRIVER.exists():
        with _BASE_DRIVER.open("rb") as f:
            if b"cdc_" not in f.read(2_000_000):
                log.info("Base chromedriver already patched — skipping download")
                return
    log.info("Downloading/patching base chromedriver (version %s) …", version)
    opts = webdriver.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    tmp = uc.Chrome(options=opts, version_main=version)
    tmp.quit()
    log.info("Base chromedriver ready: %s", _BASE_DRIVER)


def _copy_driver_for_worker(worker_id: int) -> str:
    dst = _DRIVER_DIR / f"undetected_chromedriver_{worker_id}_{os.getpid()}"
    shutil.copy2(_BASE_DRIVER, dst)
    dst.chmod(0o755)
    return str(dst)


def build_driver(worker_id: int = 0) -> uc.Chrome:
    options = webdriver.ChromeOptions()
    options.add_argument("--incognito")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-notifications")
    options.add_argument("--blink-settings=imagesEnabled=false")

    # Don't wait for full network idle — we wait explicitly for the h1 instead.
    options.page_load_strategy = "eager"

    # Block heavy resources we never read (images, CSS, fonts).
    prefs = {
        "profile.managed_default_content_settings.images":      2,
        "profile.managed_default_content_settings.stylesheets": 2,
        "profile.managed_default_content_settings.fonts":       2,
        "profile.default_content_setting_values.notifications": 2,
    }
    options.add_experimental_option("prefs", prefs)

    if HEADLESS:
        options.add_argument("--headless=new")
    version     = _chrome_major_version()
    driver_path = _copy_driver_for_worker(worker_id)
    driver = uc.Chrome(options=options, version_main=version,
                       driver_executable_path=driver_path)
    log.info("Driver started (incognito, lightweight)")
    return driver


# ─── HEARTBEAT ───────────────────────────────────────────────────────────────

class Heartbeat(threading.Thread):
    """Refresh the lease on the currently claimed URL until told to stop."""

    def __init__(self, store: DetailsStore, wid: str, url: str):
        super().__init__(daemon=True, name=f"hb-{wid[-6:]}")
        self.store = store
        self.wid   = wid
        self.url   = url
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
    if not SAVE_RAW_HTML:
        return None
    try:
        Path(HTML_DUMP_DIR).mkdir(parents=True, exist_ok=True)
        safe = str(abs(hash(url))) + ".html"
        path = Path(HTML_DUMP_DIR) / safe
        path.write_text(driver.page_source, encoding="utf-8")
        return str(path)
    except Exception as e:
        log.debug("HTML dump failed for %s: %s", url, e)
        return None


# ─── WORKER LOOP ─────────────────────────────────────────────────────────────

def run_worker(worker_id: int = 0, db_path: str = DB_PATH) -> None:
    global log
    log = setup_logging(worker_id)

    wid   = details_store.worker_id()
    store = DetailsStore(db_path)

    added = store.import_from_urls_json(URLS_JSON)
    log.info("Imported %d new URLs from %s", added, URLS_JSON)
    log.info("Queue snapshot: %s", store.stats())

    driver = build_driver(worker_id)
    _wait  = WebDriverWait(driver, WAIT_TIME)  # noqa: F841

    completed_this_run = 0

    try:
        while not _stop.is_set():
            row = store.claim_next(wid)
            if row is None:
                # Re-import in case pass 1 has discovered new URLs since startup.
                new = store.import_from_urls_json(URLS_JSON)
                if new:
                    log.info("Picked up %d new URLs from pass 1 — resuming", new)
                    continue
                log.info("Nothing to do — sleeping %.1fs", IDLE_SLEEP)
                if _stop.wait(IDLE_SLEEP):
                    break
                continue

            url     = row["url"]
            attempt = row["attempts"]
            log.info("→ claim (attempt %d): %s", attempt, url)

            hb = Heartbeat(store, wid, url)
            hb.start()
            try:
                details   = extract_details(driver, url)
                html_path = _dump_html(driver, url)

                ok = store.mark_completed(url, wid, details, html_path=html_path)
                if not ok:
                    log.warning("Could not mark completed — lease lost: %s", url)
                else:
                    n_fields = len(details) - 1  # minus source_url
                    completed_this_run += 1
                    log.info("✓ [%d] completed (%d fields): %s",
                             completed_this_run, n_fields, url)
                    if completed_this_run % LOG_EVERY == 0:
                        log.info("Progress: %s", store.stats())

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
    parser = argparse.ArgumentParser(
        description="Google Maps detail scraper — pass 2")
    parser.add_argument("-w", "--workers",  type=int, default=1,
                        help="number of parallel Chrome workers (default: 1)")
    parser.add_argument("-d", "--db",       default=DB_PATH,
                        help=f"SQLite DB path (default: {DB_PATH})")
    parser.add_argument("-H", "--headless", action="store_true",
                        help="run Chrome in headless mode")
    parser.add_argument("--save-html",      action="store_true",
                        help="dump raw page source per URL (~1 MB each)")
    parser.add_argument("-s", "--stats",   action="store_true",
                        help="print DB queue stats and exit (no scraping)")
    parser.add_argument("-r", "--reset",   action="store_true",
                        help="reset stale in_progress rows back to pending and exit")
    parser.add_argument("--reset-failed",  action="store_true",
                        help="also reset failed rows back to pending (use with -r)")
    args = parser.parse_args()

    if args.reset or args.reset_failed:
        if not Path(args.db).exists():
            sys.exit(f"No database found at {args.db}")
        affected = DetailsStore(args.db).reset_stale(reset_failed=args.reset_failed)
        for status, n in affected.items():
            print(f"Reset {n} {status} → pending")
        sys.exit(0)

    if args.stats:
        if not Path(args.db).exists():
            sys.exit(f"No database found at {args.db}")
        s = DetailsStore(args.db).stats()
        total = sum(s.values())
        print(f"\nDB: {args.db}  ({total:,} total)\n")
        for status in ("completed", "pending", "in_progress", "failed"):
            n = s.get(status, 0)
            bar = "█" * (n * 40 // total) if total else ""
            print(f"  {status:<12} {n:>7,}  {bar}")
        print()
        sys.exit(0)

    global HEADLESS, SAVE_RAW_HTML
    if args.headless:
        HEADLESS = True
    if args.save_html:
        SAVE_RAW_HTML = True

    if not Path(URLS_JSON).exists() and not Path(args.db).exists():
        log.error("Neither %s nor %s exists — nothing to scrape", URLS_JSON, args.db)
        sys.exit(1)

    n = max(1, args.workers)
    _ensure_base_driver(_chrome_major_version())

    if n == 1:
        run_worker(0, args.db)
    else:
        log.info("Spawning %d workers", n)
        procs = []
        for i in range(n):
            p = mp.Process(
                target=run_worker,
                args=(i, args.db),
                name=f"worker-{i}",
            )
            p.start()
            log.info("Started worker %d (pid %d)", i, p.pid)
            procs.append(p)

        for p in procs:
            p.join()

        log.info("All workers finished")


if __name__ == "__main__":
    main()
