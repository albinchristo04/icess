#!/usr/bin/env python3
"""
fetch_tracksino.py

Fetches data from Tracksino API and stores results to a JSON file.

Features:
- Uses Bearer token (from --token or TRACKSINO_TOKEN env var)
- Pages through results
- Optionally appends to an existing JSON file (--append)
- Optionally deduplicates on a specified key (--dedupe-key)
- Pretty-print option (--pretty)

Example:
  export TRACKSINO_TOKEN=b8f11ee0-70ec-419f-91b0-76fac8714a14
  python fetch_tracksino.py --table-id 170 --period 24hours --per-page 100 \
    --output tracksino_data.json --append --pretty --dedupe-key id
"""
import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://api.tracksino.com/icefishing_history"


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
        print(f"Fetched page {page}: {len(items)} items (total {len(all_items)})")

        total_pages = get_total_pages_from_meta(resp_json)
        if total_pages is not None:
            if page >= total_pages:
                print(f"Reached last page according to metadata: {page}/{total_pages}")
                break

        page += 1
        if max_pages is not None and page > max_pages:
            print(f"Reached user max_pages limit: {max_pages}")
            break

        time.sleep(sleep_between)

    return all_items


def load_existing(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"fetched_at": None, "count": 0, "items": []}
    with open(path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except Exception:
            # corrupted or non-JSON: back up and start fresh
            backup = path + ".bak"
            print(f"Warning: failed to parse existing {path}. Backing up to {backup}", file=sys.stderr)
            os.rename(path, backup)
            return {"fetched_at": None, "count": 0, "items": []}
    if "items" not in data or not isinstance(data["items"], list):
        # Unexpected shape: back up and start fresh
        backup = path + ".bak"
        print(f"Warning: existing {path} has unexpected format. Backing up to {backup}", file=sys.stderr)
        os.rename(path, backup)
        return {"fetched_at": None, "count": 0, "items": []}
    return data


def save_out(path: str, items: List[Any], pretty: bool = False) -> None:
    out_obj = {"fetched_at": int(time.time()), "count": len(items), "items": items}
    with open(path, "w", encoding="utf-8") as fh:
        if pretty:
            json.dump(out_obj, fh, ensure_ascii=False, indent=2)
        else:
            json.dump(out_obj, fh, ensure_ascii=False)


def append_items(existing: Dict[str, Any], new_items: List[Any], dedupe_key: Optional[str]) -> List[Any]:
    existing_list = existing.get("items", []) or []
    if not dedupe_key:
        return existing_list + new_items
    seen = set()
    out = []
    # keep existing items in order
    for it in existing_list:
        key = it.get(dedupe_key) if isinstance(it, dict) else None
        out.append(it)
        seen.add(key)
    # append only new items whose dedupe_key is not in seen
    for it in new_items:
        key = it.get(dedupe_key) if isinstance(it, dict) else None
        if key in seen:
            continue
        out.append(it)
        seen.add(key)
    return out


def main():
    p = argparse.ArgumentParser(description="Fetch Tracksino icefishing history and save to JSON")
    p.add_argument("--token", "-t", help="Bearer token. If omitted, read TRACKSINO_TOKEN env var.")
    p.add_argument("--table-id", "-T", type=int, required=True, help="table_id parameter (e.g., 170)")
    p.add_argument("--period", "-p", default="24hours", help="period parameter (default: 24hours)")
    p.add_argument("--per-page", type=int, default=100, help="per_page parameter (default: 100)")
    p.add_argument("--start-page", type=int, default=1, help="page_num to start from (default: 1)")
    p.add_argument("--max-pages", type=int, default=None, help="Optional limit of pages to fetch")
    p.add_argument("--output", "-o", default="tracksino_data.json", help="output JSON filename")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    p.add_argument("--append", action="store_true", help="Append new items to existing JSON file instead of overwriting")
    p.add_argument("--dedupe-key", type=str, default=None, help="If provided, deduplicate by this key (e.g., 'id')")
    args = p.parse_args()

    token = args.token or os.getenv("TRACKSINO_TOKEN")
    if not token:
        print("Error: Bearer token not provided. Use --token or set TRACKSINO_TOKEN env var.", file=sys.stderr)
        sys.exit(2)

    try:
        new_items = fetch_all(
            token=token,
            table_id=args.table_id,
            period=args.period,
            per_page=args.per_page,
            start_page=args.start_page,
            max_pages=args.max_pages,
        )
    except Exception as e:
        print("Failed to fetch data:", e, file=sys.stderr)
        sys.exit(1)

    if args.append:
        existing = load_existing(args.output)
        combined = append_items(existing, new_items, args.dedupe_key)
        save_out(args.output, combined, pretty=args.pretty)
        print(f"Appended {len(new_items)} items -> total {len(combined)} items saved to {args.output}")
    else:
        save_out(args.output, new_items, pretty=args.pretty)
        print(f"Saved {len(new_items)} items to {args.output}")


if __name__ == "__main__":
    main()
