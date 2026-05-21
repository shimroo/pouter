"""
Remove low-count entries from scan_progress.json so the scraper retries them.
Usage: python3 reset_zeros.py [--min 15]
"""
import json
import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--min", type=int, default=0, help="Remove entries with count below this (default: 0)")
args = parser.parse_args()

tracker_file = Path("scan_progress.json")
if not tracker_file.exists():
    print("scan_progress.json not found — nothing to do")
    raise SystemExit(0)

data = json.loads(tracker_file.read_text())
before = len(data)
data = {k: v for k, v in data.items() if v.get("count", 0) >= args.min}
removed = before - len(data)

tmp = tracker_file.with_suffix(".tmp")
tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True))
tmp.replace(tracker_file)

print(f"Removed {removed} entries with fewer than {args.min} results. {len(data)} entries remain.")