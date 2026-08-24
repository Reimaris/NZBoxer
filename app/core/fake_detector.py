import re
from typing import Any

# Video resolution dimensions
MIN_RESOLUTION_MB = {
    "2160p": 2500,  # ~2.5GB minimum for 4K
    "1080p": 800,   # ~800MB minimum for 1080p
    "720p": 300,    # ~300MB minimum for 720p
    "480p": 100,    # ~100MB minimum for SD
}

# General media constraints
SIZE_CONSTRAINTS: dict[str, dict[str, float]] = {
    "manga": {"min_mb": 5, "max_mb": 2500},
    "book": {"min_mb": 0.2, "max_mb": 500},
    "magazine": {"min_mb": 2, "max_mb": 1000},
    "movie": {"min_mb": 500, "max_mb": 150000},
    "episode": {"min_mb": 100, "max_mb": 20000},
}

# Fake/Executable extensions
BAD_EXTENSIONS = {
    ".exe", ".scr", ".bat", ".vbs", ".iso", ".msi", ".cmd", ".com"
}
PHISHING_FILES = {
    "password.txt", "pass.txt", "instructions.txt", "readme_first.txt"
}
DOCUMENT_EXTENSIONS = {
    ".epub", ".pdf", ".cbz", ".cbr", ".azw3", ".mobi"
}
VIDEO_ARCHIVE_EXTENSIONS = {
    ".mkv", ".mp4", ".ts", ".rar", ".r00", ".r01", ".part01.rar", ".part1.rar"
}

def _bytes_to_mb(size_bytes: int) -> float:
    return size_bytes / (1024 * 1024)

def is_indexer_metadata_fake(
    item_metadata: dict[str, Any],
    resolution: str | None = None,
    media_type: str = "movie"
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
            return True, f"Size {size_mb:.1f}MB is below minimum {constraints['min_mb']}MB for {media_type}"
        if size_mb > constraints["max_mb"]:
            return True, f"Size {size_mb:.1f}MB exceeds maximum {constraints['max_mb']}MB for {media_type}"

    # Check video resolution bounds
    if media_type in ["movie", "episode"] and resolution:
        min_mb = MIN_RESOLUTION_MB.get(resolution, 0)
        if size_mb < (min_mb * 0.5):
            return True, f"Size {size_mb:.1f}MB is unrealistically small for {resolution}"

    return False, None


def is_nzb_content_fake(nzb_xml_bytes: bytes, media_type: str = "movie") -> tuple[bool, str | None]:
    """Layer 2: Parses raw in-memory .nzb XML to inspect <file subject="..."> strings."""
    try:
        content = nzb_xml_bytes.decode('utf-8', errors='ignore')
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
        if media_type in ["manga", "book", "magazine"]:
            for doc_ext in DOCUMENT_EXTENSIONS:
                if doc_ext in subject_lower:
                    has_valid_payload = True
                    break
        else:
            for vid_ext in VIDEO_ARCHIVE_EXTENSIONS:
                if vid_ext in subject_lower:
                    has_valid_payload = True
                    break

    if not has_valid_payload:
        expected = "document" if media_type in ["manga", "book", "magazine"] else "video/archive"
        return True, f"Does not contain expected {expected} segments"

    return False, None


def is_torbox_filelist_fake(tb_files: list[dict[str, Any]], media_type: str = "movie") -> tuple[bool, str | None]:
    """Layer 3: Evaluates completed TorBox files list."""
    if not tb_files:
        return True, "TorBox returned empty file list"

    has_valid_payload = False
    
    for f in tb_files:
        fname = f.get("name", "").lower()
        
        for bad_ext in BAD_EXTENSIONS:
            if fname.endswith(bad_ext):
                return True, f"Downloaded executable file: {fname}"

        if media_type in ["manga", "book", "magazine"]:
            for doc_ext in DOCUMENT_EXTENSIONS:
                if fname.endswith(doc_ext):
                    has_valid_payload = True
                    break
        else:
            for vid_ext in [".mkv", ".mp4", ".ts", ".avi"]:
                if fname.endswith(vid_ext):
                    has_valid_payload = True
                    break

    if not has_valid_payload:
        expected = "document" if media_type in ["manga", "book", "magazine"] else "playable video"
        return True, f"Downloaded payload lacks a valid {expected} file"

    return False, None
