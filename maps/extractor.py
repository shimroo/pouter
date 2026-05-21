"""
========================================================
  FIELD EXTRACTION — runs on a loaded place page
  ─────────────────────────────────────────────────────
  This is a SCAFFOLD. The user is providing example HTML
  separately; selectors will be filled in to match it.

  Contract:
    extract_details(driver, url) -> dict of strings/lists

  Conventions:
    • Missing fields → key absent (don't write empty strings).
    • All values are JSON-serializable.
    • Selectors live in SELECTORS so they're easy to revise
      when Google rotates its DOM.
========================================================
"""

from __future__ import annotations

import logging
from time import sleep
from typing import Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


log = logging.getLogger(__name__)


# ─── SELECTORS ───────────────────────────────────────────────────────────────
# TODO: fill in once example HTML is shared. Keys are the field names we'll
# write into details_json; values are best-effort xpaths. Multiple xpaths per
# field are tried in order — first hit wins.

SELECTORS: dict[str, list[str]] = {
    # "name":        ["//h1[contains(@class,'DUwDvf')]"],
    # "rating":      ["//div[contains(@class,'F7nice')]//span[@aria-hidden='true']"],
    # "review_count":["//div[contains(@class,'F7nice')]//span[contains(@aria-label,'reviews')]"],
    # "category":    ["//button[contains(@jsaction,'category')]"],
    # "address":     ["//button[@data-item-id='address']//div[contains(@class,'fontBodyMedium')]"],
    # "phone":       ["//button[contains(@data-item-id,'phone:tel:')]//div[contains(@class,'fontBodyMedium')]"],
    # "website":     ["//a[@data-item-id='authority']"],
    # "plus_code":   ["//button[@data-item-id='oloc']//div[contains(@class,'fontBodyMedium')]"],
    # "hours":       ["//div[@aria-label and contains(@aria-label,'Hours')]"],
}

# Anchor element we wait for before reading fields. The h1 title is a good
# signal that the side panel has loaded.
PAGE_READY_XPATH = "//h1[contains(@class,'DUwDvf')]"
PAGE_READY_TIMEOUT = 15


# ─── ENTRYPOINT ──────────────────────────────────────────────────────────────

def extract_details(driver, url: str) -> dict:
    """
    Visit `url`, wait for the place panel to load, and pull every field
    we care about. Caller is responsible for retry / error policy.
    """
    driver.get(url)

    # Wait for the side panel to render at least its title before scraping.
    try:
        WebDriverWait(driver, PAGE_READY_TIMEOUT).until(
            EC.presence_of_element_located((By.XPATH, PAGE_READY_XPATH))
        )
    except Exception:
        log.warning("Page never reached ready state: %s", url)

    sleep(1.0)  # let lazy-loaded chips (phone, hours, etc.) settle

    details: dict = {"source_url": url}

    for field, xpaths in SELECTORS.items():
        value = _first_match(driver, xpaths, field=field)
        if value:
            details[field] = value

    # TODO: add custom extractors for fields that don't fit the
    # "first matching xpath → text" pattern (reviews list, opening hours
    # table, image URLs, "Services"/"From the business" sections, etc.).
    # Each gets its own helper, called here.

    return details


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def _first_match(driver, xpaths: list[str], field: str) -> Optional[str]:
    """Try each xpath; return the first non-empty text we find."""
    for xp in xpaths:
        try:
            els = driver.find_elements(By.XPATH, xp)
            for el in els:
                txt = (el.text or el.get_attribute("aria-label") or "").strip()
                if txt:
                    return txt
                href = (el.get_attribute("href") or "").strip()
                if href:
                    return href
        except Exception as e:
            log.debug("Selector failed for %s (%s): %s", field, xp, e)
    return None
