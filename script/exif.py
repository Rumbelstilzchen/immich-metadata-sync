"""EXIF/XMP metadata handling for Immich Ultra-Sync."""

import datetime
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

# Add script directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import MWGRS_COORDINATE_PRECISION  # MWGRS_COMPARE_PRECISION,
from utils import (  # GPS_COORDINATE_PRECISION, GPS_ALTITUDE_PRECISION,
    DEFAULT_CAPTION_MAX_LEN, DEFAULT_LOG_FILE, VALID_RATING_VALUES, LogLevel,
    extract_error_message, log, normalize_caption_limit)


# ==============================================================================
# EXIFTOOL STAY-OPEN MODE
# ==============================================================================
class ExifToolHelper:
    """ExifTool wrapper using stay-open mode for better performance."""

    def __init__(self, delete_sidecar=True):
        self.process = None
        self.delete_sidecar = delete_sidecar
        self.filecounter = 0

    def start(self):
        """Start ExifTool in stay-open mode."""
        self.process = subprocess.Popen(
            ["exiftool", "-stay_open", "True", "-charset", "UTF8", "-@", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )

    def execute(self, args: List[str]) -> tuple:
        """Execute ExifTool command and return (stdout, stderr)."""
        if not self.process:
            self.start()

        # Filter ONLY the virtual tag with JSON data (used for internal comparison)
        # Keep "-XMP-mwg-rs:RegionInfo=" (empty) because it's used to clear old metadata
        real_args = [
            a
            for a in args
            if not (a.startswith("-XMP-mwg-rs:RegionInfo=") and len(a) > 23)
        ]

        command = "\n".join(real_args) + "\n-execute\n"
        self.process.stdin.write(command)
        self.process.stdin.flush()

        output = []
        while True:
            line = self.process.stdout.readline()
            if not line or line.strip() == "{ready}":
                break
            output.append(line)

        self.filecounter += 1

        if self.filecounter > 10:
            self.filecounter = 0
            self.close()

        # Optional: Hier könnte man stderr separat auslesen,
        # aber meistens reicht stdout für die Fehlerdiagnose bei ExifTool
        return "".join(output), ""

    def close(self):
        """Close ExifTool process."""
        if self.process:
            self.process.stdin.write("-stay_open\nFalse\n")
            self.process.stdin.flush()
            self.process.wait()
        self.process = None


# overwride sidcar
def execute_with_sidecar_and_msphoto(
    args: list, full_path: str, exif_tool_helper: ExifToolHelper, log_file: str
) -> tuple:
    """
    Execute ExifTool write with sidecar-awareness and MicrosoftPhoto:Rating fallback.

    - If a .xmp sidecar exists it will be read and its previous rating logged.
    - JPG and sidecar are written together to keep metadata consistent.
    - If ExifTool reports MicrosoftPhoto:Rating not writable, retry without that tag.
    - Returns (stdout, stderr) combined from attempts.
    """
    targets = [full_path]
    sidecar_path = f"{full_path}.xmp"

    # Read & log sidecar previous rating (if present) and add it to targets
    if os.path.exists(sidecar_path):
        try:
            proc = subprocess.run(
                [
                    "exiftool",
                    "-json",
                    "-n",
                    "-Rating",
                    "-XMP:Rating",
                    "-RatingPercent",
                    sidecar_path,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            side_info = json.loads(proc.stdout)[0] if proc.stdout else {}
            prev_rating = side_info.get("Rating", side_info.get("XMP:Rating", None))
            prev_percent = side_info.get("RatingPercent", None)

            if prev_rating is not None or prev_percent is not None:
                log(
                    f"[SIDE-CAR] {sidecar_path} previous rating: Rating={prev_rating} RatingPercent={prev_percent}",
                    log_file,
                    LogLevel.INFO,
                )
            else:
                log(
                    f"[SIDE-CAR] {sidecar_path} previous rating: (not set)",
                    log_file,
                    LogLevel.DEBUG,
                )

            targets.append(sidecar_path)
        except Exception as e:
            log(f"Failed to read sidecar {sidecar_path}: {e}", log_file, LogLevel.DEBUG)

    # Optional: read and log in-file previous rating for audit (debug level)
    try:
        proc_file = subprocess.run(
            [
                "exiftool",
                "-json",
                "-n",
                "-Rating",
                "-XMP:Rating",
                "-RatingPercent",
                full_path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        file_info = json.loads(proc_file.stdout)[0] if proc_file.stdout else {}
        file_prev_rating = file_info.get("Rating", file_info.get("XMP:Rating", None))
        file_prev_percent = file_info.get("RatingPercent", None)
        if file_prev_rating is not None or file_prev_percent is not None:
            log(
                f"[FILE-BEFORE] {full_path} previous rating: Rating={file_prev_rating} RatingPercent={file_prev_percent}",
                log_file,
                LogLevel.DEBUG,
            )
    except Exception:
        # ignore read failures here
        pass

    # First write attempt (JPG [+ sidecar if present])
    stdout, stderr = exif_tool_helper.execute(args + targets)
    combined_out = (stdout or "") + (stderr or "")
    log(
        f"combined_out for {args+targets}:\nout\t{combined_out}\n",
        log_file,
        LogLevel.DEBUG,
    )

    if (
        exif_tool_helper.delete_sidecar
        and os.path.exists(sidecar_path)
        and combined_out.strip("\n\t ") == "2 image files updated"
        and sidecar_path.lower().endswith(".jpg.xmp")
    ):
        try:
            os.remove(sidecar_path)
        except Exception:
            log(f"Could not delete sidecar: {sidecar_path}", log_file, LogLevel.WARNING)
        else:
            log(f"deleted sidecar: {sidecar_path}", log_file, LogLevel.DEBUG)

    # If ExifTool complains about MicrosoftPhoto:Rating not writable, retry without that tag
    ms_tag = "MicrosoftPhoto:Rating"
    if any(
        keyword in combined_out
        for keyword in [
            "MicrosoftPhoto:Rating",
            "MicrosoftPhoto:Rating' doesn't exist",
            "not writable",
            "Sorry",
        ]
    ):
        # Filter out MicrosoftPhoto:Rating entries (they look like "-MicrosoftPhoto:Rating=...")
        filtered_args = [a for a in args if not a.startswith(f"-{ms_tag}")]
        log(
            f"[MSPHOTO] {full_path}: MicrosoftPhoto:Rating not writable; retrying without {ms_tag}",
            log_file,
            LogLevel.WARNING,
        )
        stdout2, stderr2 = exif_tool_helper.execute(filtered_args + targets)
        combined_stdout = (stdout or "") + (stdout2 or "")
        combined_stderr = (stderr or "") + (stderr2 or "")
        return combined_stdout, combined_stderr

    return stdout, stderr


def check_exiftool(log_file: str) -> bool:
    """Verify ExifTool availability with robust error handling."""
    try:
        result = subprocess.run(
            ["exiftool", "-ver"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        version = result.stdout.strip()
        log(f"ExifTool detected: Version {version}", log_file, LogLevel.INFO)
        return True
    except FileNotFoundError:
        log(
            "ERROR: ExifTool is not installed. Please install ExifTool to continue.",
            log_file,
            LogLevel.ERROR,
        )
        return False
    except subprocess.TimeoutExpired:
        log("ERROR: ExifTool check timed out.", log_file, LogLevel.ERROR)
        return False
    except subprocess.CalledProcessError as e:
        log(
            f"ERROR: ExifTool check failed: {extract_error_message(e)}",
            log_file,
            LogLevel.ERROR,
        )
        return False
    except subprocess.SubprocessError:
        log(
            "ERROR: ExifTool verification failed due to subprocess error.",
            log_file,
            LogLevel.ERROR,
        )
        return False


# ← VERBESSERUNG 7: Funktion als "derzeit ungenutzt" markiert
def parse_rating_output(output: str) -> str:
    """Extract numeric rating value from an ExifTool output string.

    NOTE: This function is currently unused but kept for potential future use
    or legacy compatibility. It was used in earlier versions for rating extraction.
    """
    if not output:
        return ""
    lines = output.strip().splitlines()
    if not lines:
        return ""
    first_line = lines[0]
    match = re.search(r"\b(\d+)\b", first_line)
    if match:
        return match.group(1)
    digits = "".join(ch for ch in first_line if ch.isdigit())
    return digits[0] if digits and digits[0] in VALID_RATING_VALUES else ""


def get_current_exif_values(full_path: str, active_modes: List[str]) -> Dict[str, Any]:
    """Read current EXIF values from file based on active modes using JSON output for reliable parsing."""
    tags_to_read = {}

    if "people" in active_modes:
        tags_to_read["People"] = [
            "Keywords",
            "XMP-dc:Subject",
            "XMP-digiKam:TagsList",
            "XMP:HierarchicalSubject",
        ]  # ← FIX: Kein Namespace
    if "tags" in active_modes:
        tags_to_read["Tags"] = ["Keywords"]  # ← FIX: Kein Namespace
    if "gps" in active_modes:
        tags_to_read["GPS"] = ["GPSLatitude", "GPSLongitude"]  # , "GPSAltitude"]
    if "caption" in active_modes:
        tags_to_read["Caption"] = ["Description", "Caption-Abstract"]
    # if "time" in active_modes:
    #     tags_to_read.extend([
    #         "DateTimeOriginal", "CreateDate", "ModifyDate", "DateCreated",
    #         "XMP:CreateDate", "XMP:ModifyDate", "XMP:MetadataDate",
    #         "IPTC:DateCreated", "IPTC:TimeCreated",
    #         "QuickTime:CreateDate", "QuickTime:ModifyDate",
    #         "FileCreateDate", "FileModifyDate"
    #     ])
    if "rating" in active_modes:
        tags_to_read["Rating"] = ["Rating", "RatingPercent"]
        tags_to_read["Label"] = ["XMP:Label"]  # <-- Hinzugefügt: XMP:Favorite

    if "albums" in active_modes:
        tags_to_read["Albums"] = [
            "Keywords",
            "XMP-dc:Subject",
            "XMP-digiKam:TagsList",
            "XMP:HierarchicalSubject",
            "Event",
            "XMP:Album",
        ]
    if "face_coordinates" in active_modes:
        tags_to_read["FaceCoordinates"] = [
            "XMP-mwg-rs:RegionInfo",
            "RegionInfoMP",
        ]  # Expliziter Namespace

    if not tags_to_read:
        return {}
    combined_tags = [tag for sublist in tags_to_read.values() for tag in sublist]

    try:
        # ← VERBESSERUNG 1: import json entfernt (jetzt oben)
        cmd = (
            ["exiftool", "-json", "-n", "-charset", "UTF8", "-struct"]
            + [f"-{tag}" for tag in set(combined_tags)]
            + [full_path]
        )
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
            encoding="utf-8",
        )

        data = json.loads(result.stdout)
        if not data or not isinstance(data, list) or len(data) == 0:
            return {}

        file_data = data[0]
        output = {}

        for category, tags in tags_to_read.items():
            # Try full tag name first, then short name (without namespace)
            values = []
            for tag in tags:
                tag_short = tag.split(":")[-1]
                value = file_data.get(tag, file_data.get(tag_short))
                if tag_short == "RegionInfoMP":
                    if value is None:
                        output[category] = None
                    else:
                        values.append(
                            sorted(
                                [
                                    faces["PersonDisplayName"]
                                    for faces in value.get("Regions", [])
                                    if faces.get("PersonDisplayName") is not None
                                ]
                            )
                        )
                    continue
                if tag_short == "RegionInfo":
                    if value is None:
                        output[category] = None
                    else:
                        values.append(
                            sorted(
                                [
                                    faces["Name"]
                                    for faces in value.get("RegionList", [])
                                    if faces.get("Name") is not None
                                ]
                            )
                        )
                    continue
                if category == "People":
                    if isinstance(value, list):
                        values.append(
                            sorted(
                                [
                                    name.replace("|", "/").split("/")[-1]
                                    for name in value
                                    if isinstance(name, str)
                                    and name.startswith("Personen")
                                ]
                            )
                        )
                    else:
                        output[category] = None
                    continue
                if category == "Tags":
                    if isinstance(value, list):
                        values.append(
                            sorted(
                                [
                                    name.replace("|", "/")
                                    for name in value
                                    if isinstance(name, str)
                                    and not name.startswith("Personen")
                                    and not name.startswith("Albums")
                                ]
                            )
                        )
                    else:
                        output[category] = []
                    continue
                if category in ["GPS", "Rating"]:
                    if category not in output:
                        output[category] = []
                    output[category].append(value)
                    continue
                if category == "Label":
                    if value is not None:
                        output[category] = value == "Favorite"
                    else:
                        output[category] = False
                    continue
                if category in ["Caption"]:
                    values.append(value)
                if category == "Albums":
                    if isinstance(value, list):
                        values.append(
                            sorted(
                                [
                                    name.replace("|", "/").split("/")[-1]
                                    for name in value
                                    if isinstance(name, str)
                                    and name.startswith("Albums")
                                ]
                            )
                        )
                    elif isinstance(value, str):
                        values.append([value.replace("|", "/").split("|")[-1]])
                    else:
                        output[category] = []
                    continue
            if category not in output:
                if values and all(v == values[0] for v in values):
                    output[category] = values[0]
                else:
                    output[category] = None

        return output
    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
    ):
        return {}


# def extract_desired_values(exif_args: List[str]) -> Dict[str, str]:
#     """Extract tag-value pairs from ExifTool arguments for comparison."""
#     desired = {}
#     for arg in exif_args:
#         if arg.startswith("-") and "=" in arg:
#             tag_value = arg[1:]  # Remove leading dash
#             tag, value = tag_value.split("=", 1)
#             # Skip individual structural parts for comparison to avoid false positives
#             # We only want to compare the composite RegionInfo tag
#             if any(x in tag for x in ["RegionName", "RegionType", "RegionArea", "RegionApplied"]):
#                 continue
#             # Strip trailing plus from tag name for comparison
#             tag = tag.rstrip('+')
#             # Normalize tag to short name (without namespace prefix)
#             tag_short = tag.split(":")[-1]
#             desired[tag_short] = value
#     return desired
#
# def normalize_exif_value(value: str, tag: str) -> str:
#     """Normalize EXIF values for comparison."""
#     if not value:
#         return ""
#
#     value = str(value).strip()
#     tag_short = tag.split(":")[-1]  # ← NEU: Extrahiere kurzen Namen für Vergleiche
#
#     # GPS coordinates: normalize format and round to configured precision
#     if tag in ["GPSLatitude", "GPSLongitude"]:
#         # Extract numeric value from various formats like "51 deg 30' 15.00\" N" or "51.504167"
#         match = re.search(r"[-+]?\d+(?:\.\d+)?", value)
#         if match:
#             try:
#                 # ← VERBESSERUNG 5: Konstante verwenden
#                 return str(round(float(match.group(0)), GPS_COORDINATE_PRECISION))
#             except ValueError:
#                 return match.group(0)
#
#     # GPS Altitude: extract numeric value and round to configured precision
#     if tag == "GPSAltitude":
#         # ExifTool might return "0" as default - check first
#         if value == "0" or value == "0 m":
#             return "0"
#         match = re.search(r"[-+]?\d+(?:\.\d+)?", value)
#         if match:
#             try:
#                 return str(round(float(match.group(0)), GPS_ALTITUDE_PRECISION))
#             except ValueError:
#                 return match.group(0)
#
#     # Rating: extract digit
#     if tag in ["Rating", "XMP:Rating", "MicrosoftPhoto:Rating"] or tag_short == "Rating":
#         match = re.search(r"\d", value)
#         if match:
#             return match.group(0)
#
#     # RatingPercent: extract number
#     if tag == "RatingPercent" or tag_short == "RatingPercent":
#         match = re.search(r"\d+", value)
#         if match:
#             return match.group(0)
#
#     # XMP:Label: normalize to string
#     if tag == "XMP:Label" or tag_short == "Label":
#         return value.strip()
#
#     # NEU: XMP:Favorite normalisieren (auf "0" oder "1")
#     if tag == "XMP:Favorite" or tag_short == "Favorite":
#         match = re.search(r"\d", value)
#         return match.group(0) if match else "0"
#
#
#     # DateTime fields: normalize separators
#     if tag in ["DateTimeOriginal", "CreateDate", "ModifyDate"] or tag_short in [
#         "DateTimeOriginal", "CreateDate", "ModifyDate", "XMP:CreateDate",
#         "XMP:ModifyDate", "XMP:MetadataDate", "MetadataDate",
#         "QuickTime:CreateDate", "QuickTime:ModifyDate"
#     ]:
#         # Convert "YYYY:MM:DD HH:MM:SS" format variations
#         normalized = value.replace("-", ":").replace("T", " ")
#         # Remove trailing 'Z' if present
#         normalized = normalized.rstrip('Z').strip()
#         # Take first 19 characters (YYYY:MM:DD HH:MM:SS)
#         if len(normalized) >= 19:
#             return normalized[:19]
#         return normalized
#
#     # Photoshop:DateCreated: ISO date format (YYYY-MM-DD only, no time)
#     if tag_short == "DateCreated":  # ← FIX: ohne Namespace
#         # Normalize to YYYY-MM-DD format
#         normalized = value.replace(":", "-")
#         # Take first 10 characters (YYYY-MM-DD)
#         if len(normalized) >= 10:
#             return normalized[:10]
#     # IPTC:TimeCreated: normalize time
#     if tag == "IPTC:TimeCreated" or tag_short == "TimeCreated":
#         # Normalize to HH:MM:SS format
#         normalized = value.strip()
#         if len(normalized) >= 8:
#             return normalized[:8]
#         return normalized
#     # File timestamps: normalize ISO format
#     if tag_short in ["FileCreateDate", "FileModifyDate"]:
#         normalized = value.replace("-", ":").replace("T", " ")
#         normalized = normalized.rstrip('Z').strip()
#         if len(normalized) >= 19:
#             return normalized[:19]
#         return normalized
#     # MWG-RS RegionInfo: normalize to canonical name:coordinates representation
#     if tag_short == "RegionInfo":
#         try:
#             # If it's already a dict from JSON, use it, otherwise parse it
#             data = json.loads(value) if isinstance(value, str) else value
#             if isinstance(data, dict):
#                 regions = data.get("RegionList", [])
#                 # Ensure it's a list even if only one region exists
#                 if isinstance(regions, dict): regions = [regions]
#                 canonical = []
#                 for r in sorted(regions, key=lambda r: str(r.get("Name", ""))):
#                     area = r.get("Area", {})
#                     canonical.append(
#                         f"{r.get('Name', '')}:"
#                         f"{round(float(area.get('X', 0)), MWGRS_COMPARE_PRECISION)},"
#                         f"{round(float(area.get('Y', 0)), MWGRS_COMPARE_PRECISION)},"
#                         f"{round(float(area.get('W', 0)), MWGRS_COMPARE_PRECISION)},"
#                         f"{round(float(area.get('H', 0)), MWGRS_COMPARE_PRECISION)}"
#                     )
#                 return "|".join(canonical)
#         except (json.JSONDecodeError, ValueError, TypeError):
#             pass
#
#     return value
#


def convert_bbox_to_mwg_rs(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    image_width: int,
    image_height: int,
) -> Optional[Dict[str, float]]:
    """Convert pixel bounding box (X1/Y1/X2/Y2) to MWG-RS normalized region coordinates.

    Returns dict with X, Y (center), W, H as values in [0..1] or None on invalid input.
    """
    if image_width <= 0 or image_height <= 0:
        return None
    bbox_w = x2 - x1
    bbox_h = y2 - y1
    if bbox_w <= 0 or bbox_h <= 0:
        return None
    center_x = x1 + bbox_w / 2
    center_y = y1 + bbox_h / 2
    return {
        "X_C": round(center_x / image_width, MWGRS_COORDINATE_PRECISION),
        "Y_C": round(center_y / image_height, MWGRS_COORDINATE_PRECISION),
        "W": round(bbox_w / image_width, MWGRS_COORDINATE_PRECISION),
        "H": round(bbox_h / image_height, MWGRS_COORDINATE_PRECISION),
        "X1": round(x1 / image_width, MWGRS_COORDINATE_PRECISION),
        "Y1": round(y1 / image_height, MWGRS_COORDINATE_PRECISION),
        "image_width": image_width,
        "image_height": image_height,
    }


def extract_date_from_filename(filename: str) -> Optional[datetime.datetime]:
    """Extract a date from a filename using common patterns.

    Supports patterns like YYYYMMDD, YYYY-MM-DD, YYYY_MM_DD, IMG_YYYYMMDD_HHMM, etc.
    Returns a naive datetime or None if no pattern matches.
    """
    if not filename:
        return None
    # Strip directory and extension
    basename = os.path.splitext(os.path.basename(filename))[0]
    # Try various patterns in order of specificity
    patterns = [
        # YYYYMMDD_HHMMSS or YYYYMMDD-HHMMSS
        (
            r"(\d{4})[\-_]?(\d{2})[\-_]?(\d{2})[\-_](\d{2})[\-_]?(\d{2})[\-_]?(\d{2})",
            lambda m: datetime.datetime(
                int(m.group(1)),
                int(m.group(2)),
                int(m.group(3)),
                int(m.group(4)),
                int(m.group(5)),
                int(m.group(6)),
            ),
        ),
        # YYYYMMDD_HHMM (no seconds)
        (
            r"(\d{4})[\-_]?(\d{2})[\-_]?(\d{2})[\-_](\d{2})[\-_]?(\d{2})(?!\d)",
            lambda m: datetime.datetime(
                int(m.group(1)),
                int(m.group(2)),
                int(m.group(3)),
                int(m.group(4)),
                int(m.group(5)),
            ),
        ),
        # YYYY-MM-DD or YYYY_MM_DD or YYYYMMDD
        (
            r"(\d{4})[\-_]?(\d{2})[\-_]?(\d{2})",
            lambda m: datetime.datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3))
            ),
        ),
    ]
    for pattern, builder in patterns:
        match = re.search(pattern, basename)
        if match:
            try:
                dt = builder(match)
                # Basic sanity check
                if (
                    1900 <= dt.year <= 2100
                    and 1 <= dt.month <= 12
                    and 1 <= dt.day <= 31
                ):
                    return dt
            except (ValueError, OverflowError):
                continue
    return None


def _parse_datetime_str(date_str: str) -> Optional[datetime.datetime]:
    """Parse a datetime string tolerantly, returning a naive datetime or None."""
    if not date_str or not isinstance(date_str, str):
        return None
    date_str = date_str.strip()
    if not date_str:
        return None

    # Try common ISO 8601 formats
    for fmt in [
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y:%m:%d %H:%M:%S",
    ]:
        try:
            return datetime.datetime.strptime(date_str, fmt)
        except ValueError:
            continue

    # Fallback: strip timezone offsets and try fromisoformat
    try:
        clean_str = date_str.replace("Z", "")
        clean_str = re.sub(r"[+-]\d{2}:?\d{2}$", "", clean_str)
        return datetime.datetime.fromisoformat(clean_str)
    except (ValueError, AttributeError):
        return None


def select_oldest_date_from_asset(
    asset: Dict[str, Any], log_file: str = DEFAULT_LOG_FILE
) -> Optional[datetime.datetime]:
    """Select the oldest (earliest) date from an asset's metadata fields.

    Priority order:
      1. exifInfo.dateTimeOriginal
      2. exifInfo.dateTimeCreated (CreateDate)
      3. exifInfo.modifyDate
      4. fileCreatedAt
      5. fileModifiedAt
      6. Fallback: date from filename (originalPath / originalFileName)

    Returns a naive datetime (timezone stripped) or None.
    """
    details = asset if "exifInfo" in asset else {}
    exif = details.get("exifInfo", {}) or {}

    candidates: List[Tuple[str, Optional[str]]] = [
        ("exifInfo.dateTimeOriginal", exif.get("dateTimeOriginal")),
        ("exifInfo.dateTimeCreated", exif.get("dateTimeCreated")),
        ("exifInfo.modifyDate", exif.get("modifyDate")),
        ("fileCreatedAt", asset.get("fileCreatedAt") or details.get("fileCreatedAt")),
        (
            "fileModifiedAt",
            asset.get("fileModifiedAt") or details.get("fileModifiedAt"),
        ),
    ]

    valid_dates: List[Tuple[str, datetime.datetime]] = []
    for source_name, raw_value in candidates:
        if raw_value:
            parsed = _parse_datetime_str(str(raw_value))
            if parsed:
                valid_dates.append((source_name, parsed))

    if valid_dates:
        source, oldest = min(valid_dates, key=lambda x: x[1])
        log(
            f"Selected date from {source}: {oldest.isoformat()}",
            log_file,
            LogLevel.DEBUG,
        )
        return oldest

    # Fallback: extract date from filename
    original_path = asset.get("originalPath") or details.get("originalPath") or ""
    original_filename = (
        asset.get("originalFileName") or details.get("originalFileName") or ""
    )
    filename = original_filename or os.path.basename(original_path)
    if filename:
        dt = extract_date_from_filename(filename)
        if dt:
            log(
                f"Selected date from filename '{filename}': {dt.isoformat()}",
                log_file,
                LogLevel.DEBUG,
            )
            return dt

    return None


def build_exif_args(
    asset: dict[str, Any],
    details: dict[str, Any],
    active_modes: list[str],
    caption_max_len: int = DEFAULT_CAPTION_MAX_LEN,
    album_map: Optional[dict[str, list[str]]] = None,
) -> tuple[list[str], dict]:
    """Build ExifTool arguments based on selected modes. Populates XMP (modern) and IPTC fields simultaneously."""
    args: List[str] = []
    changes = {}
    exif = details.get("exifInfo", {}) or {}

    # 1. PEOPLE SYNC (face recognition)
    if "people" in active_modes:
        changes["People"] = []
        people = [p["name"] for p in details.get("people", []) if p.get("name")]
        if people:
            # Sortiere Namen alphabetisch für konsistente Reihenfolge
            people_sorted = sorted(people)
            args.extend([f"-Keywords=Personen/{val}" for val in people_sorted])
            args.extend([f"-XMP-dc:Subject=Personen/{val}" for val in people_sorted])
            args.extend(
                [f"-XMP-digiKam:TagsList=Personen/{val}" for val in people_sorted]
            )
            args.extend(
                [f"-XMP:HierarchicalSubject=Personen|{val}" for val in people_sorted]
            )
            changes["People"] = people_sorted

    # 2. LOCATION SYNC (GPS & altitude)
    if "gps" in active_modes:
        lat, lon = exif.get("latitude"), exif.get("longitude")
        if lat is not None and lon is not None:
            args.extend([f"-GPSLatitude={lat}", f"-GPSLongitude={lon}"])
            alt = exif.get("altitude")
            if alt is not None:
                args.append(f"-GPSAltitude={alt}")
            changes["GPS"] = [lat, lon]

    # 3. CAPTION SYNC
    if "caption" in active_modes:
        changes["Caption"] = None
        cap = exif.get("description")
        if cap:
            safe_limit = normalize_caption_limit(caption_max_len)
            clean_cap = str(cap).replace("\n", " ").strip()[:safe_limit]
            args.extend(
                [
                    f"-XMP-dc:Description={clean_cap}",
                    f"-EXIF:ImageDescription={clean_cap}",
                    f"-IPTC:Caption-Abstract={clean_cap}",
                ]
            )
            changes["Caption"] = clean_cap
        else:
            # Clear caption if none provided
            args.extend(
                [
                    "-XMP-dc:Description=",
                    "-EXIF:ImageDescription=",
                    "-IPTC:Caption-Abstract=",
                ]
            )

    # 4. TIME SYNC (deterministic oldest-date selection and broad timestamp writing)
    # if "time" in active_modes:
    # Combine asset-level and detail-level data for select_oldest_date_from_asset
    # combined = {**asset}
    # combined["exifInfo"] = exif
    # if "fileCreatedAt" not in combined and "fileCreatedAt" in details:
    #     combined["fileCreatedAt"] = details["fileCreatedAt"]
    # if "fileModifiedAt" not in combined and "fileModifiedAt" in details:
    #     combined["fileModifiedAt"] = details["fileModifiedAt"]
    # if "originalPath" not in combined and "originalPath" in details:
    #     combined["originalPath"] = details["originalPath"]
    # if "originalFileName" not in combined and "originalFileName" in details:
    #     combined["originalFileName"] = details["originalFileName"]
    #
    # parsed_date = select_oldest_date_from_asset(combined)
    #
    # if parsed_date:
    #     # Format to EXIF: YYYY:MM:DD HH:MM:SS (no timezone)
    #     clean_date = parsed_date.strftime("%Y:%m:%d %H:%M:%S")
    #     iso_date = parsed_date.strftime("%Y-%m-%d")
    #     iso_time = parsed_date.strftime("%H:%M:%S")
    #     iso_datetime = parsed_date.strftime("%Y-%m-%dT%H:%M:%S")
    #     args.extend([
    #         f"-AllDates={clean_date}",
    #         f"-XMP:CreateDate={clean_date}",
    #         f"-XMP:ModifyDate={clean_date}",
    #         f"-XMP:MetadataDate={clean_date}",
    #         f"-IPTC:DateCreated={iso_date}",
    #         f"-IPTC:TimeCreated={iso_time}",
    #         f"-QuickTime:CreateDate={iso_datetime}",
    #         f"-QuickTime:ModifyDate={iso_datetime}",
    #         f"-FileCreateDate={iso_datetime}",
    #         f"-FileModifyDate={iso_datetime}",
    #         f"-XMP-photoshop:DateCreated={iso_date}",
    #     ])
    #     changes.append("Time")

    # 5. RATING & FAVORITE SYNC (independent tracking)
    if "rating" in active_modes:
        # Star rating: from exifInfo.rating or asset.rating, fallback favorite → 5
        star_rating = exif.get("rating")
        if star_rating is None:
            star_rating = asset.get("rating")
        is_favorite = asset.get("isFavorite", False)

        if star_rating is not None:
            star_rating = int(star_rating)
        elif is_favorite:
            star_rating = 5

        if star_rating is None:
            args.extend(
                [
                    # f"-XMP:Rating=",
                    # f"-MicrosoftPhoto:Rating=",
                    f"-Rating=",
                    f"-RatingPercent=",
                ]
            )
            changes["Rating"] = [None, None]
        else:
            rating_percent = star_rating * 20
            args.extend(
                [
                    # f"-XMP:Rating={star_rating}",
                    # f"-MicrosoftPhoto:Rating={star_rating}",
                    f"-Rating={star_rating}",
                    f"-RatingPercent={rating_percent}",
                ]
            )
            changes["Rating"] = [star_rating, rating_percent]

        changes["Label"] = False
        if is_favorite:
            args.append("-XMP:Label=Favorite")
            changes["Label"] = True
        else:
            # Wenn kein Favorit: Label löschen und Favorite auf 0 setzen
            args.append("-XMP:Label=")

    # 6. ALBUM SYNC (new section after rating)
    if "albums" in active_modes:
        changes["Albums"] = []
        if album_map:
            album_names = sorted(
                [name for name, _ in album_map.get(asset.get("id", ""), [])]
            )
            if album_names:
                if len(album_names) > 1:
                    args.append("-XMP-iptcExt:Event=")
                    args.append("-XMP:Album=")
                else:
                    args.append(f"-XMP-iptcExt:Event={album_names[0]}")
                    args.append(f"-XMP:Album={album_names[0]}")

                # All albums as hierarchical keywords
                args.extend([f"-Keywords=Albums/{name}" for name in album_names])
                args.extend([f"-XMP-dc:Subject=Albums/{name}" for name in album_names])
                args.extend(
                    [f"-XMP-digiKam:TagsList=Albums/{name}" for name in album_names]
                )
                args.extend(
                    [f"-XMP:HierarchicalSubject=Albums|{name}" for name in album_names]
                )

                # NEW: Write album names to EXIF:UserComment (for Windows Comments)
                args.append(f"-EXIF:UserComment={','.join(album_names)}")

                changes["Albums"] = album_names
        else:
            # Clear album-related fields if no albums
            args.append("-XMP:Album=")
            args.append("-XMP-iptcExt:Event=")
            args.append("-EXIF:UserComment=")

    # 7. FACE COORDINATES SYNC (MWG-RS regions)
    if "face_coordinates" in active_modes:
        people_data = details.get("people", [])
        changes["FaceCoordinates"] = []
        region_list = []  # For the virtual comparison tag
        for person in people_data:
            name = person.get("name")
            if not name:
                continue
            for face in person.get("faces", []):
                area = convert_bbox_to_mwg_rs(
                    face.get("boundingBoxX1"),
                    face.get("boundingBoxY1"),
                    face.get("boundingBoxX2"),
                    face.get("boundingBoxY2"),
                    face.get("imageWidth"),
                    face.get("imageHeight"),
                )
                if area:
                    # Structure for virtual comparison
                    region_list.append({**area, "Name": name})
                    changes["FaceCoordinates"].append(name)
        changes["FaceCoordinates"] = (
            sorted(changes["FaceCoordinates"]) if changes["FaceCoordinates"] else None
        )
        if region_list:
            # Add -struct flag for proper structure handling
            # args.append("-struct")

            args.append(write_mwg_rs(region_list))
            args.append(write_mpri_regions(region_list))

        else:
            # Clear MWG-RS region info if no faces
            args.append("-XMP-mwg-rs:RegionInfo=")
            args.append("-XMP-mwg-rs:RegionInfoMP=")

    # 8. TAGs SYNC
    if "tags" in active_modes:
        tags = [
            t["value"]
            for t in details.get("tags", [])
            if isinstance(t.get("value"), str)
            and not t.get("value").startswith("Personen/")
            and not t.get("value").startswith("Albums/")
        ]
        changes["Tags"] = []
        if tags:
            # Sortiere Tags alphabetisch für konsistente Reihenfolge
            tags_sorted = sorted(tags)
            args.extend([f"-Keywords={val}" for val in tags_sorted])
            args.extend([f"-XMP-dc:Subject={val}" for val in tags_sorted])
            args.extend([f"-XMP-digiKam:TagsList={val}" for val in tags_sorted])
            args.extend(
                [
                    f"-XMP:HierarchicalSubject={val.replace('/','|')}"
                    for val in tags_sorted
                ]
            )
            changes["Tags"] = tags_sorted

    if "Tags" in changes or "People" in changes or "Albums" in changes:
        # IF both are empty or not present, clear IPTC:Keywords to avoid stale data
        if (
            changes.get("Tags") is None
            and changes.get("FaceCoordinates") is None
            and changes.get("Albums") is None
        ):
            args.append("-Keywords=")
            args.append("-XMP-dc:Subject=")
            args.append("-XMP-digiKam:TagsList=")
            args.append("-XMP:HierarchicalSubject=")

    return args, changes


def write_mwg_rs(region_list):
    """
    Fügt XMP-MPRI Regions als vollständiges struct hinzu.
    ExifTool akzeptiert MPRI nur als komplettes struct, nicht feldweise.
    """

    regions = []
    for region in region_list:

        rectangle = f'{{X={region["X_C"]},Y={region["Y_C"]},W={region["W"]},H={region["H"]},Unit=normalized}}'
        name = region["Name"].replace(",", "|,")

        regions.append(f"{{Name={name},Area={rectangle},Type=Face}}")

    dimensions = f'{{W={region_list[0]["image_width"]},H={region_list[0]["image_height"]},Unit=pixel}}'

    # ExifTool benötigt *struct*, nicht als String
    string_struct = (
        f'{{AppliedToDimensions={dimensions},RegionList=[{",".join(regions)}]}}'
    )

    # ExifTool: struct append
    ex_arg = f"-RegionInfo={string_struct}"

    return ex_arg


def write_mpri_regions(region_list):
    """
    Fügt XMP-MPRI Regions als vollständiges struct hinzu.
    ExifTool akzeptiert MPRI nur als komplettes struct, nicht feldweise.
    """

    regions = []
    for region in region_list:
        area = [region["X1"], region["Y1"], region["W"], region["H"]]
        rectangle = "|,".join(str(v) for v in area)
        name = region["Name"].replace(",", "|,")

        regions.append(f"{{PersonDisplayName={name},Rectangle={rectangle}}}")

    # ExifTool benötigt *struct*, nicht als String
    string_struct = f'{{Regions=[{",".join(regions)}]}}'

    # ExifTool: struct append
    ex_arg = f"-RegionInfoMP={string_struct}"

    return ex_arg
