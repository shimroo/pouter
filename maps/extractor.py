"""
========================================================
  FIELD EXTRACTION — runs on a loaded place page
  ─────────────────────────────────────────────────────
  Contract:
    extract_details(driver, url) -> dict of JSON-serializable values

  Conventions:
    • Missing fields → key absent (never empty strings).
    • All values are JSON-serializable.
    • Uses lxml on driver.page_source — avoids StaleElementReferenceException
      and supports /text() + /@attr XPath natively.
========================================================
"""

from __future__ import annotations

import logging
import re
from time import sleep
from typing import Optional

import lxml.html
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


log = logging.getLogger(__name__)

PAGE_READY_XPATH    = "//h1[contains(@class,'DUwDvf')]"
PAGE_READY_TIMEOUT  = 15
POST_LOAD_SLEEP     = 2.0   # lets busy-hours bars and lazy chips settle

DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


# ─── ENTRYPOINT ──────────────────────────────────────────────────────────────

def extract_details(driver, url: str) -> dict:
    driver.get(url)

    try:
        WebDriverWait(driver, PAGE_READY_TIMEOUT).until(
            EC.presence_of_element_located((By.XPATH, PAGE_READY_XPATH))
        )
    except Exception:
        log.warning("Page never reached ready state: %s", url)

    # Scroll the side panel to trigger lazy-loaded sections (busy hours, etc.)
    try:
        panels = driver.find_elements(By.XPATH, "//div[@role='main']")
        if panels:
            driver.execute_script(
                "arguments[0].scrollTop = arguments[0].scrollHeight", panels[0]
            )
    except Exception:
        pass

    sleep(POST_LOAD_SLEEP)

    tree = lxml.html.fromstring(driver.page_source)
    details: dict = {"source_url": url}

    # ── Basic fields ─────────────────────────────────────────────────────────

    name = _xstr(tree, "//h1[contains(@class,'DUwDvf')]//text()")
    if name:
        details["name"] = name

    rating_raw = _xstr(tree,
        "//div[contains(@class,'F7nice')]//span[@aria-hidden='true'][1]/text()")
    if rating_raw:
        try:
            details["rating"] = float(rating_raw)
        except ValueError:
            pass

    review_label = _xstr(tree,
        "//span[@role='img'][contains(@aria-label,'reviews')]/@aria-label")
    if review_label:
        m = re.search(r"[\d,]+", review_label)
        if m:
            details["review_count"] = int(m.group().replace(",", ""))

    category = _xstr(tree, "//button[contains(@class,'DkEaL')]/text()")
    if category:
        details["category"] = category

    phone = _xstr(tree,
        "//button[contains(@data-item-id,'phone:tel')]"
        "//div[contains(@class,'Io6YTe')]/text()")
    if phone:
        details["phone"] = phone

    address = _xstr(tree,
        "//button[@data-item-id='address']"
        "//div[contains(@class,'Io6YTe')]/text()")
    if address:
        details["address"] = address

    website = _xstr(tree, "//a[@data-item-id='authority']/@href")
    if website:
        details["website"] = website

    booking_urls = tree.xpath("//a[contains(@data-item-id,'action')]/@href")
    if booking_urls:
        preferred = [u for u in booking_urls if "online-booking" in u]
        details["booking_url"] = (preferred[0] if preferred else booking_urls[0])

    details["wheelchair_accessible"] = bool(
        tree.xpath("//span[@aria-label='Wheelchair accessible entrance']")
    )

    # ── Structured fields ────────────────────────────────────────────────────

    hours, hours_might_differ = _extract_hours(tree)
    if hours:
        details["hours"] = hours
    if hours_might_differ:
        details["hours_might_differ"] = hours_might_differ

    busy = _extract_busy_hours(tree)
    if busy:
        details["busy_hours"] = busy

    return details


# ─── HOURS TABLE ─────────────────────────────────────────────────────────────

def _extract_hours(tree) -> tuple[dict, list]:
    hours: dict  = {}
    might_differ: list = []

    rows = tree.xpath("//table[contains(@class,'eK4R0e')]//tr")
    for tr in rows:
        day_parts = tr.xpath(".//td[1]//div[1]/text()")
        day = " ".join(p.strip() for p in day_parts).strip()
        if not day:
            continue

        holiday = tr.xpath(".//td[1]//div[contains(@class,'GfF3rf')]/text()")
        hour_texts = tr.xpath(".//td[2]//li/text()")
        has_differ_flag = bool(tr.xpath(".//td[2]//*[contains(@class,'zdqHHd')]"))

        hours[day] = (
            ", ".join(t.strip() for t in hour_texts if t.strip())
            if hour_texts else "Closed"
        )

        if holiday:
            might_differ.append(f"{day} ({holiday[0].strip()})")
        elif has_differ_flag:
            might_differ.append(day)

    return hours, might_differ


# ─── BUSY HOURS ──────────────────────────────────────────────────────────────

def _extract_busy_hours(tree) -> dict:
    busy: dict = {}

    day_blocks = tree.xpath("//div[contains(@class,'g2BVhd')]")
    for i, block in enumerate(day_blocks[:7]):
        day = DAYS[i]

        closed_texts = block.xpath(
            ".//span[contains(text(),'Closed')]/text() | "
            ".//div[contains(text(),'Closed')]/text()"
        )
        if any("Closed" in t for t in closed_texts):
            busy[day] = None
            continue

        aria_labels = block.xpath(
            ".//div[@role='img'][contains(@aria-label,'busy at')]/@aria-label"
        )
        day_busy: dict = {}
        for label in aria_labels:
            m = re.search(r"(\d+)%\s*busy at\s+(.+?)\.?\s*$", label, re.IGNORECASE)
            if m:
                day_busy[m.group(2).strip()] = int(m.group(1))

        busy[day] = day_busy if day_busy else None

    return busy


# ─── HELPER ──────────────────────────────────────────────────────────────────

def _xstr(tree, xpath: str) -> Optional[str]:
    """Return first non-empty string from an xpath ending in /text() or /@attr."""
    for r in tree.xpath(xpath):
        if isinstance(r, str):
            txt = r.strip()
            if txt:
                return txt
    return None
