#!/usr/bin/env python3
"""
DEBUG SCRIPT FOR IMMICH ULTRA-SYNC
===================================

This is a manual debug script for testing and debugging single asset processing.
Configure all settings directly in this file - NO command-line arguments needed!

Instructions:
1. Edit the CONFIG section below to set your server and asset details
2. Run the script: python debug_single_asset.py
3. Check the output and logs to debug issues
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TypedDict

# Add script directory to path for imports
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from api import fetch_asset_details_batch, fetch_assets,fetch_asset_nr
from exif import (ExifToolHelper, build_exif_args, check_exiftool,
                  execute_with_sidecar_and_msphoto, get_current_exif_values)
from immich_ultra_sync import load_album_map
# Import from local modules
from utils import (load_last_successfull_run, save_last_successfull_run,
                   set_log_level)

# ==============================================================================
# CONFIGURATION - EDIT THIS SECTION!
# ==============================================================================


class DebugConfig(TypedDict):
    SERVER_IP: str
    SERVER_PORT: int
    API_KEY: str
    ASSET_ID: str
    FILE_PATH: str
    ENABLE_PEOPLE: bool
    ENABLE_GPS: bool
    ENABLE_TAGS: bool
    ENABLE_CAPTION: bool
    ENABLE_TIME: bool
    ENABLE_RATING: bool
    ENABLE_ALBUMS: bool
    ENABLE_FACE_COORDINATES: bool
    DRY_RUN: bool
    ONLY_NEW: bool
    PHOTO_DIR: str
    PATH_SEGMENTS: int
    CAPTION_MAX_LEN: int
    LOG_LEVEL: str
    LOG_FILE: str


CONFIG: DebugConfig = {
    # Server Configuration
    "SERVER_IP": "192.168.2.56",  # IP address or hostname
    "SERVER_PORT": 2283,  # Immich server port
    "API_KEY": "KlIuJEtoGdSd0lZbfs6DtNINPFK4pCN4WA62u7fSrVw",  # Get this from Immich Settings > Account
    #"API_KEY": "GgAv6npSlUTHCJKGsZEEXHIcI3ThcZB0p8sJBl7F8Jc",  # Get this from Immich Settings > Account
    # Asset Configuration
    "ASSET_ID": "e39c66e8-c0a7-4139-8b30-75bf06f51630",  # UUID of the asset to debug
    # "FILE_PATH": r"C:\Users\Peter\PycharmProjects\immich-metadata-sync\foto_test\20230911_205936_P9110347.JPG",  # Local file path (used for writing EXIF)
    "FILE_PATH": r"Y:\temp_unsortiert\unterwasser\2026\10\immich_sync_test\20221009_142028_217.JPG",  # Local file path (used for writing EXIF)
    # Processing Modes (set to True to enable)
    "ENABLE_PEOPLE": True,
    "ENABLE_TAGS": True,
    "ENABLE_GPS": True,
    "ENABLE_CAPTION": True,
    "ENABLE_TIME": False,
    "ENABLE_RATING": True,
    "ENABLE_ALBUMS": True,
    "ENABLE_FACE_COORDINATES": True,
    # Processing Options
    "DRY_RUN": False,  # If True, don't actually write to files
    "ONLY_NEW": False,  # Skip if file already has metadata
    "PHOTO_DIR": r"C:\Users\Peter\PycharmProjects\immich-metadata-sync\foto_test",  # Photo directory (base path)
    "PATH_SEGMENTS": 3,  # Path depth for file resolution
    "CAPTION_MAX_LEN": 2100,  # Max caption length
    # Logging
    "LOG_LEVEL": "DEBUG",  # DEBUG, INFO, WARNING, ERROR
    "LOG_FILE": "debug_single_asset.log",  # Log file name
}

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================


def build_server_url(ip: str, port: int) -> str:
    """Build server URL from IP and port."""
    return f"http://{ip}:{port}"


def debug_print(title: str, data: Any):
    """Pretty print debug information."""
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}")
    if isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print(str(data))


def create_mock_asset_from_path(
    asset_id: str, file_path: str, original_path: Optional[str] = None
) -> dict:
    """Create a mock asset structure for testing."""
    if original_path is None:
        original_path = file_path

    return {
        "id": asset_id,
        "exifInfo": {},
        "originalPath": original_path,
    }


def create_mock_asset_details(file_path: str) -> dict:
    """Create a mock asset details structure."""
    return {
        "id": "mock-asset",
        "originalPath": file_path,
        "exifInfo": {
            "description": "Test description",
            "rating": 3,
            "dateTimeOriginal": "2024-01-15T10:30:45Z",
        },
        "people": [],
        "fileCreatedAt": "2024-01-15T10:30:45Z",
        "fileModifiedAt": "2024-01-15T10:30:45Z",
        "originalFileName": Path(file_path).name,
    }


# ==============================================================================
# MAIN DEBUG FUNCTION
# ==============================================================================


def main():
    """Main debug function."""

    print("\n" + "=" * 80)
    print("  IMMICH ULTRA-SYNC - DEBUG SINGLE ASSET")
    print("=" * 80)

    # Setup
    set_log_level(CONFIG["LOG_LEVEL"])
    log_file = CONFIG["LOG_FILE"]

    server_url = build_server_url(CONFIG["SERVER_IP"], CONFIG["SERVER_PORT"])
    asset_id = CONFIG["ASSET_ID"]
    file_path = CONFIG["FILE_PATH"]
    api_key = CONFIG["API_KEY"]

    # Validate configuration
    print(f"\n[1] Validating configuration...")
    if not api_key or api_key == "your-api-key-here":
        print("ERROR: API_KEY not configured! Edit CONFIG section.")
        return False

    if asset_id == "abc123def456ghi789":
        print("ERROR: ASSET_ID not configured! Edit CONFIG section.")
        return False

    if file_path == "/path/to/your/file.jpg":
        print("ERROR: FILE_PATH not configured! Edit CONFIG section.")
        return False

    print(f"  ✓ Server: {server_url}")
    print(f"  ✓ Asset ID: {asset_id}")
    print(f"  ✓ File Path: {file_path}")
    print(f"  ✓ API Key: {'*' * (len(api_key)-4) + api_key[-4:]}")

    # Build active modes
    active_modes = []
    if CONFIG["ENABLE_PEOPLE"]:
        active_modes.append("people")
    if CONFIG["ENABLE_GPS"]:
        active_modes.append("gps")
    if CONFIG["ENABLE_CAPTION"]:
        active_modes.append("caption")
    if CONFIG["ENABLE_TAGS"]:
        active_modes.append("tags")
    if CONFIG["ENABLE_TIME"]:
        active_modes.append("time")
    if CONFIG["ENABLE_RATING"]:
        active_modes.append("rating")
    if CONFIG["ENABLE_ALBUMS"]:
        active_modes.append("albums")
    if CONFIG["ENABLE_FACE_COORDINATES"]:
        active_modes.append("face_coordinates")

    if not active_modes:
        print("ERROR: No processing modes enabled!")
        return False

    debug_print("ACTIVE MODES", active_modes)
    debug_print("CONFIGURATION", CONFIG)



    # Check exiftool
    print(f"\n[2] Checking exiftool...")
    try:
        check_exiftool(log_file)
        print("  ✓ exiftool found")
    except Exception as e:
        print(f"  ✗ exiftool check failed: {e}")
        print("  → Make sure exiftool is installed and accessible")
        if CONFIG["DRY_RUN"]:
            print("  → Proceeding in dry_run mode (no file writes)")
        else:
            return False

    current_runtime = datetime.now(timezone.utc).isoformat()
    last_runtime = load_last_successfull_run(log_file)
    auth_header = {"x-api-key": api_key}

    try:
        nr_assets = fetch_asset_nr(auth_header, server_url, log_file, last_runtime)
    except Exception as e:
        print(f"Error fetching asset count: {e}")
        sys.exit(1)

    album_map = {}
    if CONFIG["ENABLE_ALBUMS"]:
        print(f"\n[4.1] Loading album map...")
        try:
            album_map = load_album_map(log_file, auth_header, server_url)
            debug_print("ALBUM MAP", album_map)
        except Exception as e:
            print(f"  ⚠ Error loading album map: {e}")

    # Fetch asset from API
    print(f"\n[3] Fetching asset details from API...")
    try:
        # Find our asset in the response
        asset = None

        # Fetch asset basic info
        print(f"  Fetching asset '{asset_id}'...")
        for asset_batch in fetch_assets(
            auth_header,
            server_url,
            page_size=1000,
            log_file=log_file,
            filter_datetime=last_runtime,
        ):
            filtered_batch = [
                current_asset
                for current_asset in asset_batch
                if (
                    current_asset.get("updatedAt", last_runtime) >= last_runtime
                    or any(
                        person.get("updatedAt", last_runtime) > last_runtime
                        for person in current_asset.get("people", [])
                    )
                    or any(
                        albdate > last_runtime
                        for _, albdate in album_map.get(current_asset["id"], [])
                    )
                )
            ]
            if (nr_filtered := len(asset_batch) - len(filtered_batch)) > 0:
                asset_batch = filtered_batch
            if not asset_batch:
                continue
            for item in asset_batch:
                if item.get("id") == asset_id:
                    asset = item
                    break
            if asset is not None:
                break

        if not asset:
            print(f"  ⚠ Asset '{asset_id}' not found in API response")
            print(f"  → Using mock asset for testing")
            asset = create_mock_asset_from_path(asset_id, file_path)
        else:
            print(f"  ✓ Asset found")

        debug_print("ASSET BASIC INFO", asset)

        # Fetch detailed asset info
        print(f"\n[4] Fetching detailed asset information...")
        for asset, details in fetch_asset_details_batch(
            [asset],
            auth_header,
            server_url,
            log_file,
        ):
            break

        if not details:
            print(f"  ⚠ Detailed info not fetched, using mock")
            details = create_mock_asset_details(file_path)
        else:
            print(f"  ✓ Details fetched")

        debug_print("ASSET DETAILED INFO", details)

    except Exception as e:
        print(f"  ✗ Error fetching asset: {e}")
        print(f"  → Using mock data for testing")
        asset = create_mock_asset_from_path(asset_id, file_path)
        details = create_mock_asset_details(file_path)
        debug_print("MOCK ASSET INFO", asset)
        debug_print("MOCK ASSET DETAILS", details)

    # Build EXIF arguments
    print(f"\n[5] Building EXIF arguments...")
    try:
        exif_args, changes = build_exif_args(
            asset,
            details,
            active_modes,
            caption_max_len=CONFIG["CAPTION_MAX_LEN"],
            album_map=album_map,  # No albums in debug mode for simplicity
        )

        debug_print("EXIF CHANGES", changes)
        debug_print("EXIF ARGUMENTS", exif_args)

    except Exception as e:
        print(f"  ✗ Error building EXIF args: {e}")
        import traceback

        traceback.print_exc()
        return False

    # Show what would be written
    if not exif_args:
        print(f"  ⚠ No EXIF changes needed for this asset")
    else:
        print(f"  ✓ {len(exif_args)} EXIF arguments prepared")
    # Check if any changes are needed
    if not changes:
        print(f"  ⚠ No EXIF changes detected for this asset")
        if CONFIG["ONLY_NEW"]:
            print(f"  → Skipping write due to ONLY_NEW mode")
            return True
    # check current exif data
    current_values = get_current_exif_values(file_path, active_modes)

    fileupdate_needed = any(
        new_val != current_values.get(category) for category, new_val in changes.items()
    )
    # Execute (if not dry_run and file exists)
    print(f"\n[6] Execution...")
    if fileupdate_needed:
        print(f"  ✓ EXIF changes detected, proceeding to write")
        if CONFIG["DRY_RUN"]:
            print(f"  [dry_run MODE] Not writing to file")
            print(f"  Would execute: exiftool {' '.join(exif_args)} '{file_path}'")
        else:
            if not os.path.exists(file_path):
                print(f"  ✗ File not found: {file_path}")
                return False

            try:
                exiftool = ExifToolHelper(delete_sidecar=True)
                exiftool.start()

                # Prepare exiftool arguments

                full_args = ["-overwrite_original", "-P"] + exif_args

                print(f"  Writing EXIF data to: {file_path}")
                _, stderr = execute_with_sidecar_and_msphoto(
                    full_args, file_path, exiftool, log_file
                )

                exiftool.close()

                if stderr:
                    print(f"  ⚠ Warnings/Errors: {stderr}")
                else:
                    print(f"  ✓ Successfully wrote EXIF data")

            except Exception as e:
                print(f"  ✗ Error executing exiftool: {e}")
                import traceback

                traceback.print_exc()
                return False
    else:
        print(f"  ⚠ No EXIF changes needed, skipping write")

    save_last_successfull_run(current_runtime, log_file)
    # Summary
    print(f"\n[7] Summary")
    print(f"  Asset ID: {asset_id}")
    print(f"  File Path: {file_path}")
    print(f"  EXIF Changes: {len(changes)}")
    print(f"  dry_run: {CONFIG['DRY_RUN']}")
    print(f"  Log File: {log_file}")

    print(f"\n{'='*80}")
    print(f"  Debug session completed!")
    print(f"{'='*80}\n")

    return True


if __name__ == "__main__":
    # change working directory to script directory for relative paths
    os.chdir(SCRIPT_DIR)
    success = main()
    sys.exit(0 if success else 1)
