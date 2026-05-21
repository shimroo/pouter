"""
========================================================
  GOOGLE MAPS SCRAPER — PASS 1: URL DISCOVERY
  ─────────────────────────────────────────────────────
  Usage:
    python scrapper.py                   # 1 worker, inline config
    python scrapper.py -w 4              # 4 parallel workers
    python scrapper.py -l locations.txt -k keywords.txt
    python scrapper.py -w 3 -l locs.txt -k kws.txt

  locations.txt / keywords.txt — one entry per line, # = comment.
  If a file is missing the inline SEARCH_URLS / KEYWORDS lists are used.

  Each worker writes urls_N.json; on completion all files are merged
  into urls.json (deduped by href).  Re-runs resume from existing files.
========================================================
"""

import argparse
import json
import logging
import math
import multiprocessing as mp
import os
import re
import shutil
import subprocess
from time import sleep
from pathlib import Path
from datetime import datetime

import undetected_chromedriver as uc
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException


# ─── FALLBACK CONFIG ─────────────────────────────────────────────────────────
# Loaded when locations.txt / keywords.txt are not present.
# Create those files to manage your lists without touching this code.

SEARCH_URLS = [
    # "https://www.google.com/maps/place/Indianapolis,+IN",
    # "https://www.google.com/maps/place/Columbus,+OH",
    # "https://www.google.com/maps/place/Cincinnati,+OH",
    # "https://www.google.com/maps/place/Cleveland,+OH",
    # "https://www.google.com/maps/place/Pittsburgh,+PA",
    # "https://www.google.com/maps/place/Louisville,+KY",
    # "https://www.google.com/maps/place/Birmingham,+AL",
    # "https://www.google.com/maps/place/Memphis,+TN",
    # "https://www.google.com/maps/place/Oklahoma+City,+OK",
    # "https://www.google.com/maps/place/Tulsa,+OK",
    # "https://www.google.com/maps/place/Albuquerque,+NM",
    # "https://www.google.com/maps/place/Tucson,+AZ",
    # "https://www.google.com/maps/place/Fresno,+CA",
    # "https://www.google.com/maps/place/Bakersfield,+CA",
    # "https://www.google.com/maps/place/Riverside,+CA",
    # "https://www.google.com/maps/place/Boise,+ID",
    # "https://www.google.com/maps/place/Spokane,+WA",
    # "https://www.google.com/maps/place/Reno,+NV",
    # "https://www.google.com/maps/place/Colorado+Springs,+CO",
    # "https://www.google.com/maps/place/Provo,+UT",
    # "https://www.google.com/maps/place/Omaha,+NE",
    # "https://www.google.com/maps/place/Des+Moines,+IA",
    # "https://www.google.com/maps/place/Wichita,+KS",
    # "https://www.google.com/maps/place/Overland+Park,+KS",
    # "https://www.google.com/maps/place/Grand+Rapids,+MI",
    "https://www.google.com/maps/place/Toledo,+OH",
    "https://www.google.com/maps/place/Dayton,+OH",
    "https://www.google.com/maps/place/Fort+Wayne,+IN",
    "https://www.google.com/maps/place/Knoxville,+TN",
    "https://www.google.com/maps/place/Chattanooga,+TN",
    "https://www.google.com/maps/place/Huntsville,+AL",
    "https://www.google.com/maps/place/Mobile,+AL",
    "https://www.google.com/maps/place/Lexington,+KY",
    "https://www.google.com/maps/place/Greensboro,+NC",
    "https://www.google.com/maps/place/Winston-Salem,+NC",
    "https://www.google.com/maps/place/Asheville,+NC",
    "https://www.google.com/maps/place/Fayetteville,+NC",
    "https://www.google.com/maps/place/Columbia,+SC",
    "https://www.google.com/maps/place/Greenville,+SC",
    "https://www.google.com/maps/place/Augusta,+GA",
    "https://www.google.com/maps/place/Savannah,+GA",
    "https://www.google.com/maps/place/Pensacola,+FL",
    "https://www.google.com/maps/place/Lakeland,+FL",
    "https://www.google.com/maps/place/Cape+Coral,+FL",
    "https://www.google.com/maps/place/Port+St.+Lucie,+FL",
    "https://www.google.com/maps/place/Sarasota,+FL",
    "https://www.google.com/maps/place/McAllen,+TX",
    "https://www.google.com/maps/place/El+Paso,+TX",
    "https://www.google.com/maps/place/Lubbock,+TX",
    "https://www.google.com/maps/place/Corpus+Christi,+TX",
]

KEYWORDS = [
    "Dental Clinics",
    "Orthodontists",
    "Med Spas",
    "Aesthetic Clinics",
    "Therapy, Mental Health",
    "Rehab Practices",
    "Chiropractic",
    "Physical Therapy Clinics",
    "Veterinary Clinics",
    "HVAC Contractors",
    "Plumbing Services",
    "Roofing Contractors",
    "Pest Control",
    "Termite Services",
    "Landscaping",
    "Lawn Care",
    "Home Cleaning",
    "Janitorial Services",
    "Auto Repair",
    "Body Shops",
    "Personal Injury",
    "Family Law Firms",
    "Bridal, Catering",
    "Event Planning Vendors",
    "Property Management",
]

OUTPUT_FILE  = "urls.json"
TRACKER_FILE = "scan_progress.json"
WAIT_TIME      = 10
FEED_WAIT_TIME = 25   # longer budget for post-search feed to appear
HEADLESS     = os.environ.get("DISPLAY") is None  # auto-headless on SSH/no-display

SCROLL_PAUSE       = 1.5
SCROLL_STALL_LIMIT = 6

PAN_RINGS       = 0
PAN_BASE_POINTS = 6
PAN_STEP_PX     = 600
PAN_SUBSTEPS    = 12
PAN_PAUSE       = 2.5


# ─── SELECTORS ───────────────────────────────────────────────────────────────

CLOSE_POPUP_XPATH = "//button[@aria-label='Close']"
SEARCH_BOX_XPATH  = "//input[@role='combobox' and @name='q']"
MAP_MOVE_TOGGLE   = ("//button[@role='checkbox' and @aria-checked='false' "
                     "and contains(., 'Update results when map moves')]")

FEED_XPATH        = "//div[@role='feed']"
TILE_LINK_XPATH   = ".//div[@role='article']//a[contains(@class,'hfpxzc')]"
END_OF_LIST_XPATH = ".//span[contains(@class,'HlvSq')]"

MAP_CANVAS_XPATHS = [
    "//div[@role='application' and contains(@jsaction,'scene.viewport')]",
    "//div[contains(@aria-label,'Map') and @role='application']",
    "//div[@role='application' and .//canvas]",
    "//canvas[contains(@class,'widget-scene-canvas')]",
]


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
            logging.FileHandler(f"scraper{suffix}.log"),
        ],
        force=True,
    )
    return logging.getLogger(__name__)

log = setup_logging()


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def load_lines(filepath: str, fallback: list[str]) -> list[str]:
    """Read non-blank, non-comment lines from `filepath`; fall back to list."""
    p = Path(filepath)
    if p.exists():
        lines = [l.strip() for l in p.read_text().splitlines()
                 if l.strip() and not l.strip().startswith("#")]
        if lines:
            log.info("Loaded %d entries from %s", len(lines), p)
            return lines
        log.warning("%s exists but is empty — using inline fallback", p)
    return fallback


def safe_attr(el, attr: str) -> str:
    try:
        return (el.get_attribute(attr) or "").strip()
    except Exception:
        return ""


def try_click(driver, xpath: str, label: str, timeout: float = 3.0) -> bool:
    try:
        el = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, xpath))
        )
        el.click()
        log.info("Clicked: %s", label)
        sleep(0.6)
        return True
    except Exception:
        return False


# ─── URL STORE ───────────────────────────────────────────────────────────────

class UrlStore:
    """
    JSON store keyed by Google Maps place href.

      "<href>": {
        "name": "...",
        "keyword": "...",
        "source_url": "...",
        "discovered_at": "2026-05-20T14:31:02",
        "scraped": false
      }
    """

    def __init__(self, filepath: str):
        self.path = Path(filepath)
        self.data: dict = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            with self.path.open() as f:
                loaded = json.load(f)
            log.info("Resuming from %s — %d urls", self.path, len(loaded))
            return loaded
        return {}

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False, sort_keys=True)
        tmp.replace(self.path)
        log.info("Saved %d urls → %s", len(self.data), self.path)

    def add(self, href: str, name: str, keyword: str, source_url: str) -> bool:
        if href in self.data:
            return False
        self.data[href] = {
            "name":         name,
            "keyword":      keyword,
            "source_url":   source_url,
            "discovered_at": datetime.now().isoformat(timespec="seconds"),
            "scraped":      False,
        }
        return True

    def __len__(self): return len(self.data)


# ─── PROGRESS TRACKER ────────────────────────────────────────────────────────

class ProgressTracker:
    """
    Shared across workers.  Records which (location, keyword) pairs are fully
    done and how many URLs were collected for each.

      "https://maps…Toledo,+OH|||Dental Clinics": {
        "count": 42,
        "completed_at": "2026-05-22T01:56:00"
      }

    All reads and writes go through `lock` so multiple workers never corrupt
    the file.
    """

    _SEP = "|||"

    def __init__(self, filepath: str, lock):
        self.path = Path(filepath)
        self.lock = lock

    @classmethod
    def _key(cls, location: str, keyword: str) -> str:
        return f"{location}{cls._SEP}{keyword}"

    def _load(self) -> dict:
        if self.path.exists():
            try:
                content = self.path.read_text().strip()
                if content:
                    return json.loads(content)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
        tmp.replace(self.path)

    def is_done(self, location: str, keyword: str) -> bool:
        with self.lock:
            return self._key(location, keyword) in self._load()

    def mark_done(self, location: str, keyword: str, count: int) -> None:
        with self.lock:
            data = self._load()
            data[self._key(location, keyword)] = {
                "count":        count,
                "completed_at": datetime.now().isoformat(timespec="seconds"),
            }
            self._save(data)

    def stats(self) -> tuple[int, int]:
        """Return (pairs_done, total_urls_collected)."""
        with self.lock:
            data = self._load()
            return len(data), sum(v["count"] for v in data.values())


# ─── DRIVER ──────────────────────────────────────────────────────────────────

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


_DRIVER_DIR = Path.home() / ".local/share/undetected_chromedriver"
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
    dst = _DRIVER_DIR / f"undetected_chromedriver_{worker_id}"
    shutil.copy2(_BASE_DRIVER, dst)
    dst.chmod(0o755)
    return str(dst)


def build_driver(worker_id: int = 0) -> uc.Chrome:
    options = webdriver.ChromeOptions()
    options.add_argument("--incognito")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    if HEADLESS:
        options.add_argument("--headless=new")
    version = _chrome_major_version()
    log.info("Detected Chrome major version: %s", version)
    driver_path = _copy_driver_for_worker(worker_id)
    driver = uc.Chrome(options=options, version_main=version,
                       driver_executable_path=driver_path)
    log.info("Driver started (incognito)")
    return driver


# ─── PAGE ACTIONS ────────────────────────────────────────────────────────────

def dismiss_popup(driver) -> None:
    try_click(driver, CLOSE_POPUP_XPATH, "close popup", timeout=4.0)


def enable_map_move_updates(driver) -> None:
    try_click(driver, MAP_MOVE_TOGGLE,
              "'update results when map moves'", timeout=2.0)


def search_keyword(driver, wait, keyword: str) -> bool:
    try:
        box = wait.until(EC.element_to_be_clickable((By.XPATH, SEARCH_BOX_XPATH)))
        box.click()
        box.send_keys(Keys.CONTROL, "a")
        box.send_keys(Keys.DELETE)
        box.send_keys(keyword)
        box.send_keys(Keys.RETURN)
        log.info("Searched: %s", keyword)
        # Wait for the feed to actually appear rather than sleeping blindly
        feed_wait = WebDriverWait(driver, FEED_WAIT_TIME)
        try:
            feed_wait.until(EC.presence_of_element_located((By.XPATH, FEED_XPATH)))
        except Exception:
            log.warning("Feed did not appear within %ds after searching '%s'",
                        FEED_WAIT_TIME, keyword)
        return True
    except Exception as e:
        log.warning("Search box not usable for '%s': %s", keyword, e)
        return False


def feed_has_end_marker(feed) -> bool:
    try:
        for m in feed.find_elements(By.XPATH, END_OF_LIST_XPATH):
            if "reached the end" in (m.text or "").lower():
                return True
    except Exception:
        pass
    return False


def _refetch_feed(wait):
    try:
        return wait.until(EC.presence_of_element_located((By.XPATH, FEED_XPATH)))
    except Exception:
        return None


def collect_urls(driver, wait, keyword: str,
                 source_url: str, store: UrlStore) -> tuple[int, bool]:
    """Scroll the results feed. Returns (new_url_count, feed_was_found)."""
    feed = _refetch_feed(wait)
    if feed is None:
        log.warning("Results feed not found")
        return 0, False

    enable_map_move_updates(driver)

    new_count  = 0
    stall      = 0
    last_total = -1

    while True:
        try:
            links = feed.find_elements(By.XPATH, TILE_LINK_XPATH)
        except StaleElementReferenceException:
            feed = _refetch_feed(wait)
            if feed is None:
                break
            continue

        for a in links:
            try:
                href = safe_attr(a, "href")
                name = safe_attr(a, "aria-label")
            except StaleElementReferenceException:
                continue
            if href and store.add(href, name=name,
                                  keyword=keyword, source_url=source_url):
                new_count += 1

        try:
            if feed_has_end_marker(feed):
                log.info("End of list reached")
                break
            driver.execute_script(
                "arguments[0].scrollTop = arguments[0].scrollHeight", feed)
        except StaleElementReferenceException:
            feed = _refetch_feed(wait)
            if feed is None:
                break
            continue

        sleep(SCROLL_PAUSE)

        try:
            current_total = len(feed.find_elements(By.XPATH, TILE_LINK_XPATH))
        except StaleElementReferenceException:
            feed = _refetch_feed(wait)
            if feed is None:
                break
            current_total = last_total

        if current_total == last_total:
            stall += 1
            if stall >= SCROLL_STALL_LIMIT:
                log.info("Stalled after %d scrolls — moving on", stall)
                break
        else:
            stall = 0
            last_total = current_total

        store.save()

    store.save()
    log.info("[%s] +%d new urls (total: %d)", keyword, new_count, len(store))
    return new_count, True


# ─── MAP PANNING ─────────────────────────────────────────────────────────────

def pan_map_by(driver, dx: int, dy: int) -> bool:
    canvas = None
    for xp in MAP_CANVAS_XPATHS:
        try:
            els = driver.find_elements(By.XPATH, xp)
            if els:
                canvas = els[0]
                break
        except Exception:
            continue
    if canvas is None:
        log.warning("Map canvas not found — tried %d selectors",
                    len(MAP_CANVAS_XPATHS))
        return False
    try:
        size   = canvas.size
        max_dx = max(1, size["width"]  // 2 - 20)
        max_dy = max(1, size["height"] // 2 - 20)
        dx     = max(-max_dx, min(max_dx, dx))
        dy     = max(-max_dy, min(max_dy, dy))
        sx, sy = dx / PAN_SUBSTEPS, dy / PAN_SUBSTEPS
        ac = ActionChains(driver).move_to_element(canvas).click_and_hold()
        for _ in range(PAN_SUBSTEPS):
            ac = ac.move_by_offset(sx, sy).pause(0.02)
        ac.release().perform()
        sleep(PAN_PAUSE)
        return True
    except Exception as e:
        log.warning("Pan drag failed: %s", e)
        return False


def spiral_targets(rings: int, base: int,
                   step_px: int) -> list[tuple[int, int]]:
    pts: list[tuple[int, int]] = []
    for ring in range(1, rings + 1):
        n      = base * ring
        radius = step_px * ring
        phase  = (math.pi / n) * ring
        for i in range(n):
            a = 2 * math.pi * i / n + phase
            pts.append((int(radius * math.cos(a)), int(radius * math.sin(a))))
    return pts


def pan_and_explore(driver, wait, keyword: str,
                    source_url: str, store: UrlStore) -> None:
    if PAN_RINGS <= 0:
        return

    targets = spiral_targets(PAN_RINGS, PAN_BASE_POINTS, PAN_STEP_PX)
    log.info("Spiral: %d rings → %d stops", PAN_RINGS, len(targets))

    cur_x, cur_y = 0, 0
    for i, (tx, ty) in enumerate(targets, 1):
        dx, dy = tx - cur_x, ty - cur_y
        log.info("  stop %d/%d  target(%+d,%+d)  delta(%+d,%+d)",
                 i, len(targets), tx, ty, dx, dy)
        if not pan_map_by(driver, -dx, -dy):
            continue
        cur_x, cur_y = tx, ty
        enable_map_move_updates(driver)
        sleep(1.0)
        collect_urls(driver, wait, keyword=keyword,
                     source_url=source_url, store=store)

    if (cur_x, cur_y) != (0, 0):
        log.info("  returning to start")
        pan_map_by(driver, cur_x, cur_y)


# ─── WORKER ──────────────────────────────────────────────────────────────────

def run_worker(worker_id: int, cities: list[str],
               keywords: list[str], output_file: str,
               tracker_lock, tracker_file: str) -> None:
    """
    One parallel worker: owns its own Chrome instance and output file.
    Processes every (city, keyword) combination for its assigned cities.
    Skips pairs already recorded in the shared ProgressTracker.
    """
    global log
    log = setup_logging(worker_id)

    tracker = ProgressTracker(tracker_file, tracker_lock)
    store   = UrlStore(output_file)
    driver  = build_driver(worker_id)
    wait    = WebDriverWait(driver, WAIT_TIME)

    try:
        for url in cities:
            log.info("── City: %s", url)
            driver.get(url)
            sleep(4)
            dismiss_popup(driver)

            for kw in keywords:
                if tracker.is_done(url, kw):
                    log.info("  ── SKIP (done): %s", kw)
                    continue

                log.info("  ── Keyword: %s", kw)
                if not search_keyword(driver, wait, kw):
                    continue

                _, feed_found = collect_urls(driver, wait,
                                             keyword=kw, source_url=url, store=store)
                if not feed_found:
                    log.warning("  ── Feed never found for '%s' — will retry next run", kw)
                    continue

                pan_and_explore(driver, wait,
                                keyword=kw, source_url=url, store=store)
                total_for_pair = sum(
                    1 for v in store.data.values()
                    if v.get("source_url") == url and v.get("keyword") == kw
                )
                tracker.mark_done(url, kw, total_for_pair)
                log.info("  ── Done: %s (%d total urls)", kw, total_for_pair)

    except KeyboardInterrupt:
        log.info("Interrupted — saving …")
    except Exception as e:
        log.exception("Fatal: %s", e)
    finally:
        store.save()
        log.info("Worker done. Quitting driver.")
        sleep(1)
        driver.quit()


# ─── MERGE ───────────────────────────────────────────────────────────────────

def merge_outputs(n_workers: int, merged_path: str) -> None:
    """Combine urls_0.json … urls_N-1.json into one deduplicated file."""
    merged: dict = {}

    # preserve any existing merged output so re-runs accumulate
    p = Path(merged_path)
    if p.exists():
        with p.open() as f:
            merged = json.load(f)
        log.info("Existing %s: %d urls", merged_path, len(merged))

    for i in range(n_workers):
        worker_file = Path(f"urls_{i}.json")
        if not worker_file.exists():
            continue
        with worker_file.open() as f:
            data = json.load(f)
        before = len(merged)
        for href, meta in data.items():
            if href not in merged:
                merged[href] = meta
        log.info("Merged %s: +%d new (total %d)",
                 worker_file, len(merged) - before, len(merged))

    tmp = p.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False, sort_keys=True)
    tmp.replace(p)
    log.info("Final: %d unique urls → %s", len(merged), merged_path)


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Google Maps URL scraper — pass 1")
    parser.add_argument("-w", "--workers",   type=int, default=1,
                        help="number of parallel Chrome workers (default: 1)")
    parser.add_argument("-l", "--locations", default="locations.txt",
                        help="file with one Maps URL per line")
    parser.add_argument("-k", "--keywords",  default="keywords.txt",
                        help="file with one keyword per line")
    parser.add_argument("--headless", action="store_true", default=None,
                        help="force headless Chrome (default: auto-detect from $DISPLAY)")
    args = parser.parse_args()

    global HEADLESS
    if args.headless:
        HEADLESS = True

    cities   = load_lines(args.locations, SEARCH_URLS)
    keywords = load_lines(args.keywords,  KEYWORDS)
    n        = max(1, args.workers)

    log.info("%d cities × %d keywords = %d combos across %d worker(s)",
             len(cities), len(keywords),
             len(cities) * len(keywords), n)

    # interleaved split: worker 0 → cities 0,N,2N,…  worker 1 → cities 1,N+1,…
    # keeps each worker's geographic mix diverse so one worker doesn't exhaust
    # one dense metro while another gets sparse cities
    chunks = [cities[i::n] for i in range(n)]

    tracker_lock = mp.Lock()
    _ensure_base_driver(_chrome_major_version())

    if n == 1:
        run_worker(0, chunks[0], keywords, OUTPUT_FILE, tracker_lock, TRACKER_FILE)
    else:
        procs = []
        for i, chunk in enumerate(chunks):
            p = mp.Process(
                target=run_worker,
                args=(i, chunk, keywords, f"urls_{i}.json", tracker_lock, TRACKER_FILE),
                name=f"worker-{i}",
            )
            p.start()
            log.info("Started worker %d (pid %d) — %d cities", i, p.pid, len(chunk))
            procs.append(p)

        for p in procs:
            p.join()

        log.info("All workers finished — merging …")
        merge_outputs(n, OUTPUT_FILE)


if __name__ == "__main__":
    main()
