#!/usr/bin/env python3
"""
IMMICH ULTRA-SYNC - Sync metadata from Immich back to original media files.

This is the main orchestration script that coordinates fetching metadata from
Immich API and writing it to EXIF/XMP tags in media files.
"""

import argparse
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add script directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api import (build_asset_album_map, fetch_asset_details_batch,
                 fetch_asset_nr, fetch_assets, retrieve_list_albums)
from exif import (ExifToolHelper, build_exif_args, check_exiftool,
                  execute_with_sidecar_and_msphoto, get_current_exif_values)
# Import from local modules
from utils import (CHECKPOINT_FILE, DEFAULT_ALBUM_CACHE_MAX_STALE,
                   DEFAULT_CAPTION_MAX_LEN, DEFAULT_LOG_FILE,
                   DEFAULT_PAGE_SIZE, DEFAULT_PATH_SEGMENTS, DEFAULT_PHOTO_DIR,
                   MAX_PAGE_SIZE, MAX_PATH_SEGMENTS, LogLevel,
                   _shutdown_requested, check_mount_issues, clear_album_cache,
                   export_statistics, get_env_int, load_album_cache,
                   load_checkpoint, load_config, load_last_successfull_run,
                   load_stale_album_cache, log, normalize_caption_limit,
                   sanitize_path, save_album_cache, save_checkpoint,
                   save_last_successfull_run, set_log_level, signal_handler,
                   validate_path_in_boundary, validate_photo_directory)

# ==============================================================================
# TQDM PROGRESS BAR
# ==============================================================================
try:
    from tqdm import tqdm

    TQDM_AVAILABLE = True
except ImportError:
    tqdm = None
    TQDM_AVAILABLE = False


# ==============================================================================
# CORE PROCESSING LOGIC
# ==============================================================================
def process_asset(
    asset: Dict[str, Any],
    details: Optional[Dict[str, Any]],
    active_modes: List[str],
    dry_run: bool,
    only_new: bool,
    photo_dir: str,
    path_segments: int,
    caption_max_len: int,
    log_file: str,
    exiftool: ExifToolHelper,
    album_map: Optional[Dict[str, List[str]]] = None,
) -> Optional[str]:
    """Process a single asset and return a statistics key for the outcome."""
    if not details:
        return "errors"

    asset_id = asset.get("id")
    orig_path = details.get("originalPath", "")
    sanitized_path = sanitize_path(orig_path)
    if not sanitized_path:
        log(
            f"Invalid path for asset {asset_id}: {orig_path}",
            log_file,
            LogLevel.WARNING,
        )
        return "errors"

    path_parts = sanitized_path.split("/")
    parts_count = len(path_parts)
    if parts_count < path_segments:
        log(
            f'Skipping asset {asset_id}: path "{sanitized_path}" has {parts_count} parts, expected at least {path_segments}',
            log_file,
            LogLevel.DEBUG,
        )
        log(
            f"HINT: Adjust IMMICH_PATH_SEGMENTS to {parts_count} or verify your mount structure matches the expected path depth.",
            log_file,
            LogLevel.DEBUG,
        )
        return "path_segment_mismatch"

    if any(part.startswith("/") or part.startswith("\\") for part in path_parts):
        log(
            f"SECURITY ERROR: Absolute path component detected for asset {asset_id}",
            log_file,
            LogLevel.ERROR,
        )
        return "errors"

    try:
        clean_rel = os.path.join(*path_parts[-path_segments:])
        full_path = os.path.join(photo_dir, clean_rel)
    except (IndexError, TypeError):
        log(f"Path mapping error for asset {asset_id}", log_file, LogLevel.ERROR)
        return "errors"

    if not validate_path_in_boundary(full_path, photo_dir):
        log(
            f"SECURITY ERROR: Path outside allowed boundaries for asset {asset_id}",
            log_file,
            LogLevel.ERROR,
        )
        return "errors"

    if not os.path.exists(full_path):
        if os.path.exists(os.path.join(photo_dir, sanitized_path)):
            full_path = os.path.join(photo_dir, sanitized_path)
        else:
            log(
                f"Skipping asset {asset_id}: file not found at {full_path}",
                log_file,
                LogLevel.DEBUG,
            )
            log(
                f"HINT: Verify IMMICH_PHOTO_DIR is set correctly (current: {photo_dir})",
                log_file,
                LogLevel.DEBUG,
            )
            return "file_not_found"

    exif_args, change_list = build_exif_args(
        asset, details, active_modes, caption_max_len, album_map
    )
    if not change_list:
        return None

    # ALWAYS check if update is needed by comparing current vs desired values
    current_values = get_current_exif_values(full_path, active_modes)

    # Determine which fields actually need updating
    fields_to_update = []
    for category, desired in change_list.items():
        # For comparison, try both full tag name and short name (without namespace)

        if (current_val:=current_values.get(category)) != desired:
            # DEBUG: Uncomment for troubleshooting
            log(f"DEBUG Tag={category} | Current='{current_val}' | Desired='{desired}'", log_file, LogLevel.DEBUG)
            fields_to_update.append(category)

    # Skip if no updates needed (applies to both modes)
    if not fields_to_update:
        log(f"SKIP: {clean_rel} - Already up to date", log_file, LogLevel.DEBUG)
        return "skipped"

    if dry_run:
        log(
            f"[DRY] {clean_rel} - Would update: {', '.join(fields_to_update)}",
            log_file,
            LogLevel.INFO,
        )
        return "simulated"

    try:
        log(
            f"UPDATE: {clean_rel} - Changing: {', '.join(fields_to_update)}",
            log_file,
            LogLevel.INFO,
        )

        # Use the sidecar-aware executor which also retries without MicrosoftPhoto:Rating if necessary
        stdout, stderr = execute_with_sidecar_and_msphoto(
            ["-overwrite_original", "-P"] + exif_args, full_path, exiftool, log_file
        )

        # exiftool.execute returned combined stdout/stderr (or from retry)
        combined = (stdout or "") + (stderr or "")
        if combined:
            # If there are warnings in ExifTool output, log them
            if (
                "Warning" in combined
                or "not writable" in combined
                or "doesn't exist" in combined
                or "Error" in combined
            ):
                log(
                    f"ExifTool output for {clean_rel}: {combined}",
                    log_file,
                    LogLevel.WARNING,
                )
        return "updated"
    except Exception as e:
        log(f"ERROR: ExifTool failed for {clean_rel}: {e}", log_file, LogLevel.ERROR)
        return "errors"


# ==============================================================================
# CLI ARGUMENT PARSING
# ==============================================================================
def create_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="IMMICH ULTRA-SYNC PRO – Sync people, GPS, captions, time, and rating from Immich into EXIF/XMP.",
    )
    parser.add_argument("--config", action="store_true", help="Enable sync modules based on config file.")
    parser.add_argument("--all", action="store_true", help="Enable all sync modules.")
    parser.add_argument("--people", action="store_true", help="Sync detected people.")
    parser.add_argument("--tags", action="store_true", help="Sync detected tags.")
    parser.add_argument("--gps", action="store_true", help="Sync GPS data.")
    parser.add_argument(
        "--caption", action="store_true", help="Sync descriptions/captions."
    )
    parser.add_argument("--time", action="store_true", help="Sync timestamps.")
    parser.add_argument("--rating", action="store_true", help="Sync favorites/ratings.")
    parser.add_argument(
        "--albums", action="store_true", help="Sync album information to XMP metadata."
    )
    parser.add_argument(
        "--face_coordinates",
        action="store_true",
        help="Sync face bounding boxes as MWG-RS regions to XMP metadata.",
    )
    parser.add_argument(
        "--dry_run",
        action="store_true",
        help="Simulation: log planned changes without writing.",
    )
    parser.add_argument(
        "--only_new",
        action="store_true",
        help="Skip files that already have ANY EXIF metadata.",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Set logging verbosity",
    )
    parser.add_argument(
        "--resume", action="store_true", help="Resume from previous run"
    )
    parser.add_argument(
        "--clear-checkpoint",
        action="store_true",
        help="Clear checkpoint and start fresh",
    )
    parser.add_argument(
        "--clear-album-cache",
        action="store_true",
        help="Clear album cache before running",
    )
    parser.add_argument(
        "--config_file", help="Path to config file, else immich-sync.yaml", default=None
    )
    parser.add_argument(
        "--export-stats", choices=["json", "csv"], help="Export statistics to file"
    )
    return parser


def parse_cli_args_and_config(
    argv: Optional[List[str]] = None,
) -> Tuple[argparse.Namespace, List[str], dict]:
    """Parse CLI arguments and derive active modes."""
    parser = create_arg_parser()
    args = parser.parse_args(argv)
    # Load config file if exists
    config = load_config(args.config_file)

    # Note: 'albums' is explicitly opt-in and not included in --all by default
    modes = ["people", "gps", "caption", "time", "rating", "tags"]
    if args.config:
        # Override default modes with config values
        active_modes = [mode for mode, enabled in config.get("CMD_PARAMETERS", {}).get("modes", {}).items() if enabled]
    else:
        active_modes = modes if args.all else [m for m in modes if getattr(args, m)]

        # Add albums if explicitly enabled
        if args.albums:
            active_modes.append("albums")

        # Add face_coordinates if explicitly enabled
        if args.face_coordinates:
            active_modes.append("face_coordinates")

    if args.config and (args.all or any(m for m in modes if getattr(args, m))):
        parser.error("Cannot use --config with --all or individual module flags.")

    #overritde default args with config values if --config is used
    if args.config:
        if config.get("CMD_PARAMETERS"):
            for param, value in config["CMD_PARAMETERS"].items():
                if param == 'modes':
                    continue
                if hasattr(args, param):
                    setattr(args, param, value)
                else:
                    log(f"Unknown config parameter '{param}' in config file, ignoring.", config.get("IMMICH_LOG_FILE", DEFAULT_LOG_FILE), LogLevel.WARNING)

    if not active_modes:
        parser.error("No mode selected. Use --all or individual module flags.")
    return args, active_modes, config


def load_album_map(log_file, headers, base_url) -> Dict[str, List[str]]:
    # Try to load from cache
    log("Checking album cache...", log_file, LogLevel.DEBUG)

    all_albums = retrieve_list_albums(headers, base_url, log_file)
    album_map = None
    if all_albums:
        last_album_update = max(
            album["updatedAt"]
            for album in all_albums
            if album.get("updatedAt") is not None
        )
        album_map = load_album_cache(last_album_update, log_file)

    if album_map is not None:
        log(
            f"Using cached album data ({len(album_map)} assets with album assignments)",
            log_file,
            LogLevel.INFO,
        )
        return album_map

    # Cache miss or expired - fetch from API
    log("Fetching album information from API...", log_file, LogLevel.INFO)
    try:
        album_map = build_asset_album_map(headers, base_url, log_file, all_albums)
        log(
            f"Loaded {len(album_map)} assets with album assignments",
            log_file,
            LogLevel.INFO,
        )

        # Save to cache
        save_album_cache(album_map, log_file)
    except Exception as e:
        log(f"Failed to fetch album information: {e}", log_file, LogLevel.ERROR)

        # Try to load stale cache as fallback
        log("Attempting to use stale cache as fallback...", log_file, LogLevel.WARNING)
        # Get cache TTL and max_stale from environment
        cache_max_stale = get_env_int(
            "IMMICH_ALBUM_CACHE_MAX_STALE", DEFAULT_ALBUM_CACHE_MAX_STALE
        )
        album_map = load_stale_album_cache(cache_max_stale, log_file)

        if album_map is None:
            log(
                "No fallback cache available, continuing without album sync",
                log_file,
                LogLevel.WARNING,
            )
            album_map = {}
    return album_map


# ==============================================================================
# MAIN FUNCTION
# ==============================================================================
def main() -> None:
    args, active_modes, config = parse_cli_args_and_config()

    # Set log level early
    set_log_level(args.log_level)

    # Setup signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


    base_url = os.getenv("IMMICH_INSTANCE_URL") or config.get(
        "IMMICH_INSTANCE_URL", ""
    ).rstrip("/")
    api_key = os.getenv("IMMICH_API_KEY") or config.get("IMMICH_API_KEY")
    photo_dir = os.getenv("IMMICH_PHOTO_DIR") or config.get(
        "IMMICH_PHOTO_DIR", DEFAULT_PHOTO_DIR
    )
    log_file = os.getenv("IMMICH_LOG_FILE") or config.get(
        "IMMICH_LOG_FILE", DEFAULT_LOG_FILE
    )
    path_segments_raw = get_env_int(
        "IMMICH_PATH_SEGMENTS",
        int(config.get("IMMICH_PATH_SEGMENTS", DEFAULT_PATH_SEGMENTS)),
    )
    path_segments = min(MAX_PATH_SEGMENTS, max(1, path_segments_raw))
    page_size_raw = get_env_int(
        "IMMICH_SEARCH_PAGE_SIZE",
        int(config.get("IMMICH_SEARCH_PAGE_SIZE", DEFAULT_PAGE_SIZE)),
    )
    caption_max_len_raw = get_env_int(
        "CAPTION_MAX_LEN", int(config.get("CAPTION_MAX_LEN", DEFAULT_CAPTION_MAX_LEN))
    )
    caption_max_len = normalize_caption_limit(caption_max_len_raw)
    # Align fetch size with processing throughput while keeping pagination bounded (at least batch_size, capped by MAX_PAGE_SIZE)
    page_size = min(MAX_PAGE_SIZE, page_size_raw)

    # Handle checkpoint operations
    if args.clear_checkpoint and Path(CHECKPOINT_FILE).exists():
        Path(CHECKPOINT_FILE).unlink()
        log("Checkpoint cleared", log_file, LogLevel.INFO)

    # Validate credentials before creating headers
    if not base_url or not api_key:
        log(
            "ERROR: IMMICH_INSTANCE_URL or IMMICH_API_KEY missing.",
            log_file,
            LogLevel.ERROR,
        )
        sys.exit(1)

    headers = {
        "x-api-key": api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    if path_segments != path_segments_raw:
        log(
            f"Adjusted path_segments to {path_segments} (requested {path_segments_raw})",
            log_file,
            LogLevel.INFO,
        )
    if page_size != page_size_raw:
        log(
            f"Adjusted search page size to {page_size} (requested {page_size_raw}, batch size {page_size})",
            log_file,
            LogLevel.INFO,
        )
    if caption_max_len != caption_max_len_raw:
        log(
            f"Adjusted caption max length to {caption_max_len} (requested {caption_max_len_raw})",
            log_file,
            LogLevel.INFO,
        )

    if not check_exiftool(log_file):
        log(
            "ABORT: ExifTool is not available. Please install ExifTool.",
            log_file,
            LogLevel.ERROR,
        )
        sys.exit(1)

    # Validate photo directory before starting
    log(f"Validating photo directory: {photo_dir}", log_file, LogLevel.INFO)
    if not validate_photo_directory(photo_dir, log_file):
        log(
            "ABORT: Photo directory validation failed. Please check your configuration.",
            log_file,
            LogLevel.ERROR,
        )
        sys.exit(1)

    # Initialize ExifTool in stay-open mode
    exiftool = ExifToolHelper()
    exiftool.start()
    log(
        "ExifTool started in stay-open mode for better performance",
        log_file,
        LogLevel.DEBUG,
    )

    dry_run = args.dry_run
    only_new = args.only_new

    # Load checkpoint if resuming
    processed_ids, current_runtime = (
        load_checkpoint(log_file) if args.resume else (set(), None)
    )

    log(
        f"START: modes={active_modes} | dry={dry_run} | only_new={only_new} | "
        f"batch_size={page_size} | path_segments={path_segments} | caption_max_len={caption_max_len}",
        log_file,
        LogLevel.INFO,
        extra={
            "modes": active_modes,
            "dry_run": dry_run,
            "only_new": only_new,
            "batch_size": page_size,
            "path_segments": path_segments,
            "caption_max_len": caption_max_len,
        },
    )
    if current_runtime is None:
        current_runtime = datetime.now(timezone.utc).isoformat()
    last_runtime = load_last_successfull_run(log_file)

    statistics = {
        "total": 0,
        "updated": 0,
        "simulated": 0,
        "skipped": 0,
        "file_not_found": 0,
        "path_segment_mismatch": 0,
        "errors": 0,
    }

    # Build album map if needed (before processing assets) with caching
    album_map = {}
    if "albums" in active_modes:
        # Clear cache if requested
        if args.clear_album_cache:
            clear_album_cache(log_file)
        album_map = load_album_map(log_file, headers, base_url)
    try:
        nr_assets = fetch_asset_nr(headers, base_url, log_file, last_runtime)
    except Exception as e:
        log(f"Error fetching asset count: {e}", log_file, LogLevel.ERROR)
        sys.exit(1)
    log(f"Total assets to process: {nr_assets}", log_file, LogLevel.INFO)
    # Initialize progress bar if available
    if TQDM_AVAILABLE and not dry_run:
        progress = tqdm(total=nr_assets, desc="Syncing", unit="file")
    else:
        progress = None

    total_batches = (nr_assets + page_size - 1) // page_size
    log(f"Processing in {total_batches} batches of up to {page_size} assets each", log_file, LogLevel.INFO)
    for batch_num, asset_batch in enumerate(
        fetch_assets(headers, base_url, page_size, log_file, last_runtime)
    ):
        log(f"Processing batch {batch_num+1} of {total_batches}", log_file, LogLevel.INFO)
        statistics["total"] += len(asset_batch)

        if last_runtime is not None:
            filtered_batch = [
                current_asset
                for current_asset in asset_batch
                if (
                    current_asset.get("updatedAt", last_runtime) >= last_runtime
                    or any(
                        person.get("updatedAt", last_runtime) > last_runtime
                        for person in current_asset.get("people", [])
                    )
                )
            ]
            if (nr_filtered := len(asset_batch) - len(filtered_batch)) > 0:
                log(
                    f"Filtered {nr_filtered} assets that have not changed since last run",
                    log_file,
                    LogLevel.DEBUG,
                )
                asset_batch = filtered_batch
                statistics["skipped"] += nr_filtered
                if progress:
                    progress.update(nr_filtered)
        if not asset_batch:
            log(
                f"No assets to process in batch {batch_num+1}/{total_batches}",
                log_file,
                LogLevel.DEBUG,
            )
            continue

        # Check for graceful shutdown
        if _shutdown_requested:
            log(
                "Shutdown requested, stopping gracefully...", log_file, LogLevel.WARNING
            )
            break
        if total_batches > 1:
            log(
                f"Processing batch {batch_num+1}/{total_batches} ({len(asset_batch)} assets)...",
                log_file,
                LogLevel.DEBUG,
            )

        for asset, asset_detail in fetch_asset_details_batch(
            asset_batch, headers, base_url, log_file
        ):
            asset_id = asset.get("id")
            # Skip already processed assets when resuming
            if asset_id in processed_ids:
                statistics["skipped"] += 1
                if progress:
                    progress.update(1)
                continue

            status_key = process_asset(
                asset,
                asset_detail,
                active_modes,
                dry_run,
                only_new,
                photo_dir,
                path_segments,
                caption_max_len,
                log_file,
                exiftool,
                album_map,
            )
            if status_key and status_key in statistics:
                statistics[status_key] += 1

            # Add to processed set
            processed_ids.add(asset_id)

            # Update progress bar
            if progress:
                progress.update(1)
                progress.set_postfix(
                    {
                        "Updated": statistics["updated"],
                        "Skipped": statistics["skipped"],
                        "Errors": statistics["errors"],
                    }
                )

            # Save checkpoint every 100 assets
            if len(processed_ids) % 100 == 0:
                save_checkpoint(processed_ids, log_file, current_runtime)
    # Close progress bar
    if progress:
        progress.close()
    # Close ExifTool
    exiftool.close()
    log("ExifTool stay-open mode closed", log_file, LogLevel.DEBUG)

    # Remove checkpoint after successful completion
    if args.resume and Path(CHECKPOINT_FILE).exists():
        Path(CHECKPOINT_FILE).unlink()
        log("Checkpoint removed after successful completion", log_file, LogLevel.INFO)

    # Check for potential mount/path configuration issues
    check_mount_issues(statistics, log_file, photo_dir, path_segments)

    log(
        f"FINISH: Total:{statistics['total']} Updated:{statistics['updated']} "
        f"Simulated:{statistics['simulated']} Skipped:{statistics['skipped']} "
        f"FileNotFound:{statistics['file_not_found']} PathMismatch:{statistics['path_segment_mismatch']} "
        f"Errors:{statistics['errors']}",
        log_file,
        LogLevel.INFO,
        extra={"statistics": statistics},
    )
    if not dry_run:
        save_last_successfull_run(current_runtime, log_file)

    # Export statistics if requested
    if args.export_stats:
        export_statistics(statistics, log_file, args.export_stats)


if __name__ == "__main__":
    main()
