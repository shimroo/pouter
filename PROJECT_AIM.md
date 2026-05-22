# Project: pouter — Google Maps Business Intelligence Scraper

## Mission
Systematically extract structured business data from Google Maps across US mid-sized cities for a defined set of high-value B2B target industries. Output: a clean Excel spreadsheet of leads with contact info, hours, ratings, booking URLs, and foot-traffic patterns.

---

## Pipeline Architecture (2-Pass)

### Pass 1 — URL Discovery (`scrapper.py`)
- Drives N parallel Chrome instances (undetected_chromedriver, bot-evasion patched) in incognito mode
- For each `(city_url × keyword)` pair: navigates to the city, types the keyword into the Maps search box, waits for the results feed, then infinite-scrolls until the end-of-list sentinel appears or scroll stalls
- Optionally executes a **spiral map-pan** (configurable rings × points × step_px) after each search to surface results outside the initial viewport
- Workers split cities via interleaved assignment (worker 0 → cities 0,N,2N,…) for geographic diversity
- Progress persisted in `scan_progress.json` (location‖keyword → count + timestamp); re-runs skip completed pairs
- Each worker writes `urls_N.json`; on completion all are merged and deduplicated into `urls.json`
- Output schema per URL: `{name, keyword, source_url, discovered_at, scraped: false}`

### Pass 2 — Detail Extraction (`pass2.py` + `extractor.py`)
- Workers atomically claim URLs from SQLite (`places.db`) via `UPDATE … WHERE url = (SELECT … LIMIT 1) RETURNING *` inside an `IMMEDIATE` transaction — race-free at any worker count
- **Lease system**: each claim has a 5-minute TTL; a per-claim `Heartbeat` daemon thread refreshes it every 60s; crashed workers' leases expire and the row is re-claimed automatically
- **Retry**: up to 3 attempts per URL; permanently failed rows are left in status `'failed'` and skipped
- On each page: waits for the `h1.DUwDvf` sentinel, scrolls the side panel to trigger lazy-loaded sections, then parses with **lxml** (avoids `StaleElementReferenceException`, supports `/text()` and `/@attr` XPath natively)
- Saves raw HTML dumps to `html/<hash>.html` for offline debugging
- Pass 1 and Pass 2 can run **simultaneously** — Pass 2 re-imports `urls.json` whenever its queue is empty

### Extracted Fields (per place)
| Field | Notes |
|---|---|
| name, category | h1 + category chip |
| rating, review_count | numeric, parsed from aria-labels |
| phone, address, website | data-item-id attributes |
| booking_url | prefers `online-booking` action links |
| wheelchair_accessible | boolean from aria-label |
| hours | dict keyed by day name; `"Closed"` if no hours |
| hours_might_differ | list of holiday/irregular days |
| busy_hours | dict: day → {hour → % busy}; `null` if closed |

### Storage (`details_store.py`)
- SQLite WAL mode, `synchronous=NORMAL` — crash-safe, concurrent readers
- Status FSM: `pending → in_progress → completed | failed`
- `import_from_urls_json()` is idempotent (`INSERT OR IGNORE`) — safe to call repeatedly as Pass 1 discovers more URLs
- `stats()` gives live count per status

### Export (`to_excel.py`)
- Reads all `completed` rows, flattens JSON details into one row per place
- Columns: 14 basic fields + 7 `hours_*` columns + 7 `peak_*` columns (peak hour + % busy per day) + `hours_might_differ`
- Styled Excel: frozen header row, dark-blue header, alternating-row fill, auto-width columns

### Maintenance (`reset_zeros.py`)
- Removes entries from `scan_progress.json` whose `count < N` (default 0) so Pass 1 re-scrapes sparse results on next run

---

## Target Scope

**Cities** (~25 US mid-sized metros, active config):
Toledo OH, Dayton OH, Fort Wayne IN, Knoxville TN, Chattanooga TN, Huntsville AL, Mobile AL, Lexington KY, Greensboro NC, Winston-Salem NC, Asheville NC, Fayetteville NC, Columbia SC, Greenville SC, Augusta GA, Savannah GA, Pensacola FL, Lakeland FL, Cape Coral FL, Port St. Lucie FL, Sarasota FL, McAllen TX, El Paso TX, Lubbock TX, Corpus Christi TX
*(+25 larger metros commented out — previously scraped or queued for next batch)*

**Keywords** (25 B2B target categories):
Healthcare: Dental Clinics, Orthodontists, Med Spas, Aesthetic Clinics, Mental Health Therapy, Rehab Practices, Chiropractic, Physical Therapy, Veterinary
Home Services: HVAC, Plumbing, Roofing, Pest Control, Termite, Landscaping, Lawn Care, Home Cleaning, Janitorial
Auto: Auto Repair, Body Shops
Legal: Personal Injury, Family Law
Events: Bridal/Catering, Event Planning
Real Estate: Property Management

**Theoretical max**: 25 cities × 25 keywords = **625 (city, keyword) pairs** per batch

---

## Tech Stack
| Layer | Library |
|---|---|
| Browser automation | `selenium` + `undetected_chromedriver` (CDCpatch) |
| HTML parsing | `lxml` (XPath) |
| Concurrency | `multiprocessing` (workers) + `threading` (heartbeat) |
| Storage | `sqlite3` WAL |
| Export | `pandas` + `openpyxl` |
| Config | CLI args (`argparse`) + flat text files (`locations.txt`, `keywords.txt`) |

---

## Key Design Decisions
- **undetected_chromedriver** patches the `cdc_` string from chromedriver binary; one patched binary is downloaded in the main process, then copied per-worker to avoid concurrent download races
- **Interleaved city split** prevents one worker from exhausting a dense metro while another gets sparse cities
- **IMMEDIATE transactions** for claim atomicity — SQLite serializes these, no two workers can grab the same row even without an application-level lock
- **lxml over Selenium element refs** for extraction — the page source snapshot is immutable, so no stale-element errors during parsing
- **Dual-run compatibility** (Pass 1 + Pass 2 simultaneously) via idempotent DB import and lease-based work stealing
