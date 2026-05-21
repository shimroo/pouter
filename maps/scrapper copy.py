"""
========================================================
  GOOGLE MAPS SCRAPER — PASS 1: URL DISCOVERY
  ─────────────────────────────────────────────────────
  For every URL in SEARCH_URLS:
    - close the consent / signed-out popup
    - for every keyword in KEYWORDS:
        - type into the search box, submit
        - turn on "Update results when map moves"
        - scroll the results feed to the end
        - record every tile's href into urls.json
  Pass 2 (visiting each url for details) is separate.
========================================================
"""

import json
import logging
from time import sleep
from pathlib import Path
from datetime import datetime

import undetected_chromedriver as uc
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# ─── CONFIG ──────────────────────────────────────────────────────────────────

# Pre-built Google Maps URLs — each is visited in order.
SEARCH_URLS = [
    "https://www.google.com/maps/place/Greenville,+SC",
]

# Keywords typed into the search box on each URL.
KEYWORDS = [
    "dentist",
    "orthodontist",
]

OUTPUT_FILE  = "urls.json"
WAIT_TIME    = 10
HEADLESS     = False

SCROLL_PAUSE       = 1.5    # seconds between scroll ticks
SCROLL_STALL_LIMIT = 6      # give up after this many scrolls with no new tiles


# ─── LOGGING ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("scraper.log"),
    ],
)
log = logging.getLogger(__name__)


# ─── SELECTORS ───────────────────────────────────────────────────────────────

CLOSE_POPUP_XPATH   = "//button[@aria-label='Close']"
SEARCH_BOX_XPATH    = "//input[@role='combobox' and @name='q']"
MAP_MOVE_TOGGLE     = ("//button[@role='checkbox' and @aria-checked='false' "
                       "and contains(., 'Update results when map moves')]")

FEED_XPATH          = "//div[@role='feed']"
TILE_LINK_XPATH     = ".//div[@role='article']//a[contains(@class,'hfpxzc')]"
END_OF_LIST_XPATH   = ".//span[contains(@class,'HlvSq')]"


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def safe_attr(el, attr: str) -> str:
    try:
        return (el.get_attribute(attr) or "").strip()
    except Exception:
        return ""


def try_click(driver, xpath: str, label: str, timeout: float = 3.0) -> bool:
    """Click an element by xpath if it shows up within `timeout`. Returns success."""
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
    Readable JSON store keyed by Google Maps place URL.

    Shape:
        {
          "<href>": {
            "name": "...",
            "keyword": "...",
            "source_url": "...",
            "discovered_at": "2026-05-20T14:31:02",
            "scraped": false
          },
          ...
        }
    """

    def __init__(self, filepath: str):
        self.path = Path(filepath)
        self.data: dict = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            with self.path.open() as f:
                loaded = json.load(f)
            log.info("Resuming — %d urls already saved", len(loaded))
            return loaded
        return {}

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False, sort_keys=True)
        tmp.replace(self.path)
        log.info("Saved %d urls → %s", len(self.data), self.path)

    def add(self, href: str, name: str, keyword: str, source_url: str) -> bool:
        """Insert a new url. Returns True if it was new, False if already present."""
        if href in self.data:
            return False
        self.data[href] = {
            "name": name,
            "keyword": keyword,
            "source_url": source_url,
            "discovered_at": datetime.now().isoformat(timespec="seconds"),
            "scraped": False,
        }
        return True

    def __len__(self): return len(self.data)


# ─── DRIVER ──────────────────────────────────────────────────────────────────

def build_driver() -> uc.Chrome:
    options = webdriver.ChromeOptions()
    options.add_argument("--incognito")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    if HEADLESS:
        options.add_argument("--headless=new")

    driver = uc.Chrome(options=options)
    log.info("Driver started (incognito)")
    return driver


# ─── PAGE ACTIONS ────────────────────────────────────────────────────────────

def dismiss_popup(driver) -> None:
    try_click(driver, CLOSE_POPUP_XPATH, "close popup", timeout=4.0)


def enable_map_move_updates(driver) -> None:
    """Tick the 'Update results when map moves' checkbox if it's unchecked."""
    try_click(driver, MAP_MOVE_TOGGLE, "'update results when map moves'", timeout=2.0)


def search_keyword(wait, keyword: str) -> bool:
    try:
        box = wait.until(EC.element_to_be_clickable((By.XPATH, SEARCH_BOX_XPATH)))
        box.click()
        # clear any prior text
        box.send_keys(Keys.CONTROL, "a")
        box.send_keys(Keys.DELETE)
        box.send_keys(keyword)
        box.send_keys(Keys.RETURN)
        log.info("Searched: %s", keyword)
        sleep(3)
        return True
    except Exception as e:
        log.warning("Search box not usable for '%s': %s", keyword, e)
        return False


def feed_has_end_marker(feed) -> bool:
    try:
        markers = feed.find_elements(By.XPATH, END_OF_LIST_XPATH)
        for m in markers:
            txt = (m.text or "").lower()
            if "reached the end" in txt:
                return True
        return False
    except Exception:
        return False


def collect_urls(driver, wait, keyword: str, source_url: str, store: UrlStore) -> int:
    """Scroll the results feed for `keyword`, recording every tile href into store."""
    try:
        feed = wait.until(EC.presence_of_element_located((By.XPATH, FEED_XPATH)))
    except Exception as e:
        log.warning("Results feed not found: %s", e)
        return 0

    enable_map_move_updates(driver)

    new_for_this_keyword = 0
    stall_rounds = 0
    last_count = -1

    while True:
        # 1. record any tiles currently visible
        links = feed.find_elements(By.XPATH, TILE_LINK_XPATH)
        for a in links:
            href = safe_attr(a, "href")
            if not href:
                continue
            name = safe_attr(a, "aria-label")
            if store.add(href, name=name, keyword=keyword, source_url=source_url):
                new_for_this_keyword += 1

        # 2. end-of-list marker?
        if feed_has_end_marker(feed):
            log.info("End-of-list marker reached")
            break

        # 3. scroll the feed
        driver.execute_script(
            "arguments[0].scrollTop = arguments[0].scrollHeight", feed
        )
        sleep(SCROLL_PAUSE)

        # 4. stall detection
        current_count = len(feed.find_elements(By.XPATH, TILE_LINK_XPATH))
        if current_count == last_count:
            stall_rounds += 1
            if stall_rounds >= SCROLL_STALL_LIMIT:
                log.info("No new tiles after %d scrolls — stopping", stall_rounds)
                break
        else:
            stall_rounds = 0
            last_count = current_count

        # 5. periodic save while scrolling
        store.save()

    store.save()
    log.info("[%s] +%d new urls (store now: %d)",
             keyword, new_for_this_keyword, len(store))
    return new_for_this_keyword


# ─── SCRAPE ENTRY ────────────────────────────────────────────────────────────

def scrape(driver, wait, store: UrlStore) -> None:
    for url in SEARCH_URLS:
        log.info("── Opening: %s", url)
        driver.get(url)
        sleep(4)

        dismiss_popup(driver)

        if not KEYWORDS:
            log.warning("KEYWORDS is empty — nothing to search for on %s", url)
            continue

        for kw in KEYWORDS:
            log.info("── Keyword: %s", kw)
            if not search_keyword(wait, kw):
                continue
            collect_urls(driver, wait, keyword=kw, source_url=url, store=store)


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main() -> None:
    if not SEARCH_URLS:
        log.error("SEARCH_URLS is empty — add at least one URL and try again.")
        return
    if not KEYWORDS:
        log.error("KEYWORDS is empty — add at least one keyword.")
        return

    store  = UrlStore(OUTPUT_FILE)
    driver = build_driver()
    wait   = WebDriverWait(driver, WAIT_TIME)

    try:
        scrape(driver, wait, store)

    except KeyboardInterrupt:
        log.info("Interrupted — saving progress …")

    except Exception as e:
        log.exception("Fatal error: %s", e)

    finally:
        store.save()
        log.info("All done. Quitting driver.")
        sleep(2)
        driver.quit()


if __name__ == "__main__":
    main()
