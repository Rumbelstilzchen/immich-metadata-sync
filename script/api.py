"""API-related functions and RateLimiter for Immich Ultra-Sync."""

import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import requests

# Add script directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import LogLevel, extract_asset_items, log, retry_on_failure

# Globale Variable für Batch-Endpoint-Verfügbarkeit
_BATCH_ENDPOINT_AVAILABLE = (
    None  # None = unbekannt, True = verfügbar, False = nicht verfügbar
)


# ==============================================================================
# RATE LIMITER
# ==============================================================================
class RateLimiter:
    """Simple rate limiter using token bucket algorithm."""

    def __init__(self, calls_per_second: float = 10.0):
        self.calls_per_second = calls_per_second
        self.min_interval = 1.0 / calls_per_second
        self.last_call = 0.0
        self.lock = threading.Lock()

    def wait(self):
        """Wait if necessary to respect rate limit."""
        with self.lock:
            now = time.time()
            time_since_last = now - self.last_call
            if time_since_last < self.min_interval:
                sleep_time = self.min_interval - time_since_last
                time.sleep(sleep_time)
            self.last_call = time.time()


_rate_limiter = RateLimiter(calls_per_second=10.0)


# ==============================================================================
# API FUNCTIONS
# ==============================================================================
@retry_on_failure(max_retries=3, delay=2.0)
def api_call(
    method: str,
    endpoint: str,
    headers: Dict[str, str],
    base_url: str,
    log_file: str,
    json_data: Optional[Dict[str, Any]] = None,
    silent_on_404: bool = False,
) -> Optional[Any]:
    """Perform API calls robustly (with/without /api prefix)."""
    _rate_limiter.wait()

    for path in [f"{base_url}/api{endpoint}", f"{base_url}{endpoint}"]:
        try:
            if method == "POST":
                r = requests.post(path, headers=headers, json=json_data, timeout=30)
            else:
                r = requests.get(path, headers=headers, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            if not silent_on_404:
                log(f"API timeout at {path}", log_file, LogLevel.WARNING)
            continue
        except requests.exceptions.HTTPError as e:
            # Nur loggen wenn NICHT (silent_on_404 UND Status ist 404)
            is_404 = (
                hasattr(e.response, "status_code") and e.response.status_code == 404
            )
            if not (silent_on_404 and is_404):
                log(
                    f"HTTP error at {path}: {e.response.status_code}",
                    log_file,
                    LogLevel.WARNING,
                )
            continue
        except requests.exceptions.RequestException as exc:
            if not silent_on_404:
                log(f"API request error at {path}: {exc}", log_file, LogLevel.WARNING)
            continue
        except ValueError:
            if not silent_on_404:
                log(f"JSON parsing error at {path}", log_file, LogLevel.WARNING)
            continue
    if not silent_on_404:
        log(
            f"API call failed for {endpoint} after all attempts",
            log_file,
            LogLevel.ERROR,
        )
    return None


def retrieve_list_albums(headers: Dict, base_url: str, log_file: str) -> list[dict]:
    """Retrieve a list of all albums."""
    raw_all_albums = api_call("GET", "/albums", headers, base_url, log_file)
    if isinstance(raw_all_albums, list) and raw_all_albums:
        return raw_all_albums
    return []


def build_asset_album_map(
    headers: Dict, base_url: str, log_file: str, all_albums: list
) -> Dict[str, List[str]]:
    """Build a mapping of asset IDs to album names."""

    asset_to_albums = {}
    for album in all_albums or []:
        album_name = album.get("albumName")
        album_update = album.get("updatedAt")
        # Skip albums without a name or with empty name
        if album_name is None or album_name.strip() == "":
            continue

        album_id = album.get("id")
        if album_id is None:
            continue

        # Check if assets are already in the album object (for testing or API responses that include them)
        assets = []

        # fetch assets of album
        payload = {
            "filter": {"albumIds": {"all": [album_id]}},
            "size": 1000,  # max per request
        }
        while True:
            raw = api_call(
                "POST",
                "/search/metadata",
                headers,
                base_url,
                log_file,
                json_data=payload,
            )
            if isinstance(raw, dict) and raw:
                assets.extend(raw.get("assets", {}).get("items", []))
                current_cursor = raw.get("assets", {}).get("nextCursor")
                if current_cursor is None:
                    break
                payload["cursor"] = current_cursor
            else:
                break

        # Map each asset to this album
        for asset in assets:
            asset_id = asset.get("id")
            if asset_id:
                if asset_id not in asset_to_albums:
                    asset_to_albums[asset_id] = []
                asset_to_albums[asset_id].append((album_name, album_update))

    return asset_to_albums


def fetch_asset_nr(
    headers: Dict[str, str], base_url: str, log_file: str, filter_datetime: str = None
) -> int:
    """Fetch the total number of assets."""

    # updatedat is not updated on changes to people, so we need to fetch all assets and then filter by chage date
    payload = {"filter": {}}
    # if filter_datetime is not None:
    #    payload = {"filter": {"updatedAt": {"gte": filter_datetime}}}
    raw = api_call(
        "POST", "/search/statistics", headers, base_url, log_file, {"filter": {}}
    )
    if isinstance(raw, dict):
        return raw.get("total", 0)
    return 0


def fetch_assets(
    headers: Dict[str, str],
    base_url: str,
    page_size: int,
    log_file: str,
    filter_datetime: str = None,
):
    """Fetch assets using pagination when supported; returns a list and falls back to a single call on failure."""
    assets: List[Dict[str, Any]] = []
    # assets2: List[Dict[str, Any]] = []
    # updatedat is not updated on changes to people, so we need to fetch all assets and then filter by chage date
    payload = {"size": page_size, "withPeople": True, "filter": {}}
    # if filter_datetime is not None:
    #    payload = {"size": page_size,"filter": {"updatedAt": {"gte": filter_datetime}}}
    while True:
        raw = api_call(
            "POST", "/search/metadata", headers, base_url, log_file, json_data=payload
        )

        if not raw:
            break
        page_assets = extract_asset_items(raw)
        if not page_assets:
            break
        if filter_datetime is None:
            yield page_assets
        else:
            yield page_assets
            # yield [current_asset for current_asset in page_assets if (current_asset.get('updatedAt', filter_datetime)>=filter_datetime or any(person.get('updatedAt',filter_datetime)>filter_datetime for person in current_asset.get('people',[])))]
        current_cursor = (
            raw.get("assets", {}).get("nextCursor") if isinstance(raw, dict) else None
        )
        if current_cursor is None:
            break
        payload["cursor"] = current_cursor

    # if not assets:
    #     raw = api_call(
    #         "POST",
    #         "/search/metadata",
    #         headers,
    #         base_url,
    #         log_file,
    #         json_data={"withArchived": True},
    #     )
    #     assets = extract_asset_items(raw)
    # return assets


def fetch_asset_details_batch(
    asset_batch: List[Dict[str, Any]],
    headers: Dict[str, str],
    base_url: str,
    log_file: str,
):
    """Fetch asset details in batches with bulk endpoint fallback, returning a mapping of asset_id -> detail."""
    global _BATCH_ENDPOINT_AVAILABLE

    # Fehlende Assets einzeln abrufen
    for asset in asset_batch:
        asset_id = asset.get("id")
        if asset_id is None:
            continue
        if detail := api_call(
            "GET", f"/assets/{asset_id}", headers, base_url, log_file
        ):
            try:
                if detail["people"] and (
                    people := api_call(
                        "GET", f"/faces?id={asset_id}", headers, base_url, log_file
                    )
                ):
                    people = {
                        person["person"]["id"]: [person]
                        for person in people
                        if person.get("person", None) is not None
                    }
                    for person in detail["people"]:
                        person["faces"] = people.get(person["id"], [])
            except Exception as e:
                log(
                    f"Error fetching faces for asset {asset_id}: {e}",
                    log_file,
                    LogLevel.WARNING,
                )
        yield asset, detail
