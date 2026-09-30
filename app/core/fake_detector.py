import re
from typing import Any

# Video resolution dimensions
MIN_RESOLUTION_MB = {
    "2160p": 2500,  # ~2.5GB minimum for 4K
    "1080p": 800,  # ~800MB minimum for 1080p
    "720p": 300,  # ~300MB minimum for 720p
    "480p": 100,  # ~100MB minimum for SD
}

# General media constraints
SIZE_CONSTRAINTS: dict[str, dict[str, float]] = {
    "movie": {"min_mb": 500, "max_mb": 150000},
    "episode": {"min_mb": 100, "max_mb": 20000},
}

# Fake/Executable extensions
BAD_EXTENSIONS = {
    ".exe",
    ".scr",
    ".bat",
    ".vbs",
    ".iso",
    ".msi",
    ".cmd",
    ".com",
    ".sh",
    ".bin",
    ".jar",
    ".apk",
    ".lnk",
    ".dll",
    ".ps1",
}
PHISHING_FILES = {"password.txt", "pass.txt", "instructions.txt", "readme_first.txt"}
VIDEO_ARCHIVE_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".ts",
    ".m4v",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".mpg",
    ".mpeg",
    ".vob",
    ".iso",
    ".rar",
    ".r00",
    ".r01",
    ".part01.rar",
    ".part1.rar",
    ".7z",
    ".zip",
    ".par2",
}


def _bytes_to_mb(size_bytes: int) -> float:
    return size_bytes / (1024 * 1024)


def is_indexer_metadata_fake(
    item_metadata: dict[str, Any],
    resolution: str | None = None,
    media_type: str = "movie",
) -> tuple[bool, str | None]:
    """Layer 1: Evaluates Newznab item XML attributes (password, size bounds)."""

    # Check password flag
    if item_metadata.get("password", 0) > 0:
        return True, "Indexer reports release is password protected"

    size_bytes = item_metadata.get("size", 0)
    if not size_bytes:
        return False, None

    size_mb = _bytes_to_mb(int(size_bytes))

    # Check general bounds
    constraints = SIZE_CONSTRAINTS.get(media_type)
    if constraints:
        if size_mb < constraints["min_mb"]:
            return (
                True,
                f"Size {size_mb:.1f}MB is below minimum {constraints['min_mb']}MB for {media_type}",
            )
        if size_mb > constraints["max_mb"]:
            return (
                True,
                f"Size {size_mb:.1f}MB exceeds maximum {constraints['max_mb']}MB for {media_type}",
            )

    # Check video resolution bounds
    if media_type in ["movie", "episode"] and resolution:
        min_mb = MIN_RESOLUTION_MB.get(resolution, 0)
        if size_mb < (min_mb * 0.5):
            return (
                True,
                f"Size {size_mb:.1f}MB is unrealistically small for {resolution}",
            )

    return False, None


def is_nzb_content_fake(
    nzb_xml_bytes: bytes, media_type: str = "movie"
) -> tuple[bool, str | None]:
    """Layer 2: Parses raw in-memory .nzb XML to inspect <file subject="..."> strings."""
    try:
        content = nzb_xml_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return True, "Failed to decode NZB bytes"

    # Extract all file subjects
    subjects = re.findall(r'<file.*?subject="(.*?)"', content, re.IGNORECASE)
    if not subjects:
        return True, "No <file> subjects found in NZB XML"

    has_valid_payload = False

    for subject in subjects:
        subject_lower = subject.lower()

        # Check bad extensions
        for bad_ext in BAD_EXTENSIONS:
            if bad_ext in subject_lower:
                return True, f"Contains executable/malicious extension: {bad_ext}"

        # Check phishing files
        for phish in PHISHING_FILES:
            if phish in subject_lower:
                return True, f"Contains phishing instructions: {phish}"

        # Check payload types
        for vid_ext in VIDEO_ARCHIVE_EXTENSIONS:
            if vid_ext in subject_lower:
                has_valid_payload = True
                break
        if not has_valid_payload:
            # Check for multipart rar / par2 / numeric extensions like .001, .r02, .vol01, or yEnc
            if re.search(r"\.(par2|r\d{2}|\d{3}|part\d+|vol\d+)|yenc", subject_lower):
                has_valid_payload = True
                break

    if not has_valid_payload:
        # If segments exist and no bad extension or phishing file was detected, allow obfuscated usenet files
        has_segments = bool(re.search(r"<segment", content, re.IGNORECASE))
        if has_segments:
            has_valid_payload = True

    if not has_valid_payload:
        return True, "Does not contain expected video/archive segments"

    return False, None


def _extract_file_names(files: list[Any]) -> list[str]:
    """Recursively extract file names from flat or nested TorBox file entries."""
    names: list[str] = []
    for f in files:
        if isinstance(f, str):
            names.append(f)
        elif isinstance(f, dict):
            name = f.get("name") or f.get("short_name") or f.get("path")
            if name and isinstance(name, str):
                names.append(name)
            nested = f.get("files")
            if isinstance(nested, list):
                names.extend(_extract_file_names(nested))
    return names


def is_torbox_filelist_fake(
    tb_files: list[Any] | None, media_type: str = "movie"
) -> tuple[bool, str | None]:
    """Layer 3: Evaluates completed TorBox files list."""
    if not tb_files:
        return True, "TorBox returned empty file list"

    file_names = _extract_file_names(tb_files)
    if not file_names:
        return True, "TorBox returned empty file list"

    has_valid_payload = False

    for raw_name in file_names:
        fname = raw_name.lower().strip()

        for bad_ext in BAD_EXTENSIONS:
            if fname.endswith(bad_ext):
                return True, f"Downloaded executable file: {raw_name}"

        for vid_ext in [".mkv", ".mp4", ".ts", ".avi"]:
            if fname.endswith(vid_ext):
                has_valid_payload = True
                break

    if not has_valid_payload:
        return True, "Downloaded payload lacks a valid playable video file"

    return False, None
