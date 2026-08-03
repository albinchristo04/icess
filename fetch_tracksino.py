#!/usr/bin/env python3
"""
fetch_tracksino.py

Fetches Tracksino API and appends new rows to an NDJSON file (newline-delimited JSON).

Behavior:
- Default output: tracksino_data.ndjson
- If --dedupe-key is set (e.g., round_code) the script will avoid writing duplicate keys.
- Otherwise the script will append only items with "when" > latest saved when.
- Adds mapped fields: when_local (default Asia/Kolkata), result_label, multiplier_str, bonusmultiplier_str.

Usage examples:
  # Append new rows using dedupe key
  python fetch_tracksino.py --table-id 170 --per-page 100 --append --dedupe-key round_code

  # Append new rows using timestamp comparison (no dedupe key)
  python fetch_tracksino.py --table-id 170 --per-page 100 --append

Notes:
- Requires Python 3.9+ for zoneinfo (used for timezone conversion). On older Python, install backports.zoneinfo and adapt imports.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://api.tracksino.com/icefishing_history"

RESULT_LABELS = {
    1: "White tile (white)",
    2: "Blue tile (blue)",
    100: "Bonus game - Big Oranges",
    200: "Little Blues",
    300: "Huge Reds",
}

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None


def create_session(retries: int = 3, backoff_factor: float = 0.5) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def extract_items(resp_json: Any) -> List[Any]:
    if isinstance(resp_json, list):
        return resp_json
    if isinstance(resp_json, dict):
        for key in ("data", "results", "items", "rows"):
            if key in resp_json and isinstance(resp_json[key], list):
                return resp_json[key]
        lists = [v for v in resp_json.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
    return []


def get_total_pages_from_meta(resp_json: Any) -> Optional[int]:
    if isinstance(resp_json, dict):
        if "total_pages" in resp_json and isinstance(resp_json["total_pages"], int):
            return resp_json["total_pages"]
        if "meta" in resp_json and isinstance(resp_json["meta"], dict):
            meta = resp_json["meta"]
            if "total_pages" in meta and isinstance(meta["total_pages"], int):
                return meta["total_pages"]
            if "pages" in meta and isinstance(meta["pages"], int):
                return meta["pages"]
    return None


def fetch_all(
    token: str,
    table_id: int,
    period: str,
    per_page: int = 100,
    start_page: int = 1,
    max_pages: Optional[int] = None,
    sleep_between: float = 0.1,
) -> List[Any]:
    session = create_session()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    all_items: List[Any] = []
    page = start_page

    while True:
        params = {
            "page_num": page,
            "per_page": per_page,
            "period": period,
            "table_id": table_id,
            "sort_by": "",
            "sort_desc": "false",
        }
        resp = session.get(BASE_URL, headers=headers, params=params, timeout=30)
        try:
            resp.raise_for_status()
        except Exception as e:
            print(f"Request failed for page {page}: {e}", file=sys.stderr)
            try:
                print("Response body:", resp.text[:1000], file=sys.stderr)
            except Exception:
                pass
            raise

        resp_json = resp.json()
        items = extract_items(resp_json)
        if not items:
            print(f"Page {page}: no items, stopping.", file=sys.stderr)
            break

        all_items.extend(items)
        print(f"Fetched page {page}: {len(items)} items (total {len(all_items)})", file=sys.stderr)

        total_pages = get_total_pages_from_meta(resp_json)
        if total_pages is not None:
            if page >= total_pages:
                print(f"Reached last page according to metadata: {page}/{total_pages}", file=sys.stderr)
                break

        page += 1
        if max_pages is not None and page > max_pages:
            print(f"Reached user max_pages limit: {max_pages}", file=sys.stderr)
            break

        time.sleep(sleep_between)

    return all_items


def _format_multiplier(val: Any) -> Optional[str]:
    try:
        if val is None:
            return None
        return f"{int(val)}x"
    except Exception:
        return str(val)


def add_mappings_and_local_time(items: List[Any], tz_name: Optional[str]) -> None:
    tz = None
    if tz_name and ZoneInfo is not None:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = None

    for it in items:
        if not isinstance(it, dict):
            continue
        # when_local
        when_ts = it.get("when")
        if isinstance(when_ts, (int, float)):
            try:
                if tz is not None:
                    dt = datetime.fromtimestamp(int(when_ts), tz=timezone.utc).astimezone(tz)
                else:
                    dt = datetime.fromtimestamp(int(when_ts))
                it["when_local"] = dt.isoformat()
            except Exception:
                pass
        # result label
        r = it.get("result")
        try:
            if isinstance(r, (int, float)):
                it["result_label"] = RESULT_LABELS.get(int(r), f"Unknown ({r})")
            else:
                try:
                    rr = int(r)
                    it["result_label"] = RESULT_LABELS.get(rr, f"Unknown ({r})")
                except Exception:
                    it["result_label"] = f"Unknown ({r})"
        except Exception:
            pass
        # multipliers
        it["multiplier_str"] = _format_multiplier(it.get("multiplier"))
        it["bonusmultiplier_str"] = _format_multiplier(it.get("bonusmultiplier"))


def read_seen_keys_from_ndjson(path: str, key: str) -> Set[Any]:
    seen: Set[Any] = set()
    if not os.path.exists(path):
        return seen
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        seen.add(obj.get(key))
                except Exception:
                    continue
    except Exception:
        pass
    return seen


def read_last_when_from_ndjson(path: str) -> Optional[int]:
    """Try to read the last non-empty line and return its 'when' timestamp (int) if present."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            if end == 0:
                return None
            # read backward to find last newline
            offset = 1
            while True:
                if offset > end:
                    fh.seek(0)
                    last = fh.read().decode(errors="ignore").strip().splitlines()
                    if not last:
                        return None
                    last_line = last[-1]
                    break
                fh.seek(-offset, os.SEEK_END)
                chunk = fh.read(offset)
                if b"\n" in chunk:
                    # find last newline position
                    idx = chunk.rfind(b"\n")
                    fh.seek(- (offset - idx - 1), os.SEEK_END)
                    last_line = fh.readline().decode(errors="ignore").strip()
                    break
                offset *= 2
            if not last_line:
                return None
            obj = json.loads(last_line)
            if isinstance(obj, dict):
                w = obj.get("when")
                if isinstance(w, (int, float)):
                    return int(w)
    except Exception:
        # fallback: scan file normally (slower)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                last_line = None
                for line in fh:
                    line = line.strip()
                    if line:
                        last_line = line
                if not last_line:
                    return None
                obj = json.loads(last_line)
                w = obj.get("when")
                if isinstance(w, (int, float)):
                    return int(w)
        except Exception:
            return None
    return None


def append_ndjson(path: str, items: Iterable[Dict[str, Any]]) -> int:
    """Append list of objects (dict) to NDJSON file, return number appended."""
    appended = 0
    mode = "a"
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    with open(path, mode, encoding="utf-8") as fh:
        for it in items:
            try:
                fh.write(json.dumps(it, ensure_ascii=False) + "\n")
                appended += 1
            except Exception:
                continue
    return appended


def main():
    p = argparse.ArgumentParser(description="Fetch Tracksino and append to NDJSON")
    p.add_argument("--token", "-t", help="Bearer token. If omitted, read TRACKSINO_TOKEN env var.")
    p.add_argument("--table-id", "-T", type=int, required=True, help="table_id (e.g., 170)")
    p.add_argument("--period", "-p", default="24hours", help="period (default 24hours)")
    p.add_argument("--per-page", type=int, default=100, help="per_page (default 100)")
    p.add_argument("--start-page", type=int, default=1, help="page_num to start from")
    p.add_argument("--max-pages", type=int, default=None, help="max pages to fetch")
    p.add_argument("--output", "-o", default="tracksino_data.ndjson", help="output NDJSON filename")
    p.add_argument("--pretty-json", action="store_true", help="Also write a pretty JSON snapshot (tracksino_data.json)")
    p.add_argument("--append", action="store_true", help="Append only new rows to NDJSON")
    p.add_argument("--dedupe-key", type=str, default=None, help="Deduplicate on this key (recommended: round_code)")
    p.add_argument("--tz", type=str, default="Asia/Kolkata", help="IANA timezone for when_local (default Asia/Kolkata). Pass empty string to disable.")
    args = p.parse_args()

    token = args.token or os.getenv("TRACKSINO_TOKEN")
    if not token:
        print("Error: Bearer token not provided. Use --token or set TRACKSINO_TOKEN env var.", file=sys.stderr)
        sys.exit(2)

    new_items = fetch_all(
        token=token,
        table_id=args.table_id,
        period=args.period,
        per_page=args.per_page,
        start_page=args.start_page,
        max_pages=args.max_pages,
    )

    if not new_items:
        print("No items fetched.", file=sys.stderr)
        sys.exit(0)

    # Prepare mappings and local times for fetched items
    tz_name = args.tz if args.tz != "" else None
    add_mappings_and_local_time(new_items, tz_name)

    latest_fetched_when = max((int(it.get("when")) for it in new_items if isinstance(it.get("when"), (int, float))), default=None)
    print(f"Fetched {len(new_items)} items; latest_fetched_when={latest_fetched_when}", file=sys.stderr)

    output_path = args.output
    appended = 0

    if args.append:
        if args.dedupe_key:
            seen = read_seen_keys_from_ndjson(output_path, args.dedupe_key)
            # filter new items by key
            to_write = []
            for it in new_items:
                if not isinstance(it, dict):
                    continue
                key = it.get(args.dedupe_key)
                if key in seen:
                    continue
                to_write.append(it)
            appended = append_ndjson(output_path, to_write)
            print(f"Deduping by key '{args.dedupe_key}': fetched {len(new_items)}, to_write {len(to_write)}, appended {appended}", file=sys.stderr)
        else:
            # use last when timestamp to determine new rows
            last_when = read_last_when_from_ndjson(output_path)
            if last_when is None:
                # no prior data -> append everything
                to_write = new_items
            else:
                to_write = [it for it in new_items if isinstance(it, dict) and isinstance(it.get("when"), (int, float)) and int(it.get("when")) > last_when]
            appended = append_ndjson(output_path, to_write)
            print(f"No dedupe key: last_saved_when={last_when}; fetched {len(new_items)}, appended {appended}", file=sys.stderr)
    else:
        # not in append mode: overwrite JSON snapshot or NDJSON (here we write NDJSON fresh)
        # overwrite NDJSON file with fetched items
        try:
            with open(output_path, "w", encoding="utf-8") as fh:
                for it in new_items:
                    fh.write(json.dumps(it, ensure_ascii=False) + "\n")
            appended = len(new_items)
            print(f"Wrote {appended} items to {output_path} (overwrite mode)", file=sys.stderr)
        except Exception as e:
            print(f"Failed to write {output_path}: {e}", file=sys.stderr)
            sys.exit(1)

    # Optionally also produce a pretty JSON snapshot for easy human inspection
    if args.pretty_json:
        snapshot = {
            "fetched_at": int(time.time()),
            "fetched_at_local": None,
            "count": None,
            "items_preview": None,
        }
        snapshot["count"] = sum(1 for _ in open(output_path, "r", encoding="utf-8")) if os.path.exists(output_path) else appended
        if tz_name and ZoneInfo is not None:
            try:
                tz = ZoneInfo(tz_name)
                snapshot["fetched_at_local"] = datetime.fromtimestamp(snapshot["fetched_at"], tz=timezone.utc).astimezone(tz).isoformat()
            except Exception:
                pass
        # include the latest few items for quick glance
        last_preview = []
        try:
            with open(output_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            last_preview.append(json.loads(line))
                        except Exception:
                            pass
            snapshot["items_preview"] = last_preview[-10:]
        except Exception:
            snapshot["items_preview"] = []
        # write pretty JSON snapshot side-by-side
        try:
            with open(os.path.splitext(output_path)[0] + ".json", "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass

    print(f"Done. appended={appended}. Output file: {output_path}", file=sys.stderr)
    # Exit code 0 even if appended==0; the workflow will check git diff to avoid commits when unchanged.


if __name__ == "__main__":
    main()
