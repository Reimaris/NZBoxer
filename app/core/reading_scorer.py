import logging
import re

logger = logging.getLogger(__name__)

# Point rankings per media type
PRINT_SCORES = {
    "manga": {
        "cbz": 1000,
        "cbr": 700,
        "pdf": 300,
    },
    "book": {
        "epub": 1000,
        "azw3": 800,
        "mobi": 700,
        "pdf": 400,
    },
    "magazine": {
        "pdf": 1000,
        "epub": 700,
    },
}


def detect_print_format(
    title: str,
    description: str = "",
    category_id: int | str | None = None,
    media_type: str = "manga",
) -> str:
    """Intelligently detects file format from release title, description, or media type.

    Prioritizes explicit file extensions, tags, and falls back to media-type defaults.
    """
    text = f"{title} {description}".lower()

    # 1. Look for explicit extension or enclosed tags (e.g. .cbz, [CBZ], (cbz))
    if re.search(r"(\.cbz\b|\[cbz\]|\(cbz\)|\bcbz\b)", text):
        return "cbz"
    if re.search(r"(\.cbr\b|\[cbr\]|\(cbr\)|\bcbr\b)", text):
        return "cbr"
    if re.search(r"(\.epub\b|\[epub\]|\(epub\)|\bepub\b)", text):
        return "epub"
    if re.search(r"(\.azw3\b|\[azw3\]|\bazw3\b|\.mobi\b|\[mobi\]|\bmobi\b)", text):
        return "mobi"
    if re.search(r"(\.pdf\b|\[pdf\]|\(pdf\)|\bpdf\b)", text):
        return "pdf"

    # 2. Category-based deduction
    cat_str = str(category_id) if category_id else ""
    if "7030" in cat_str:  # Comics / Manga
        return "cbz"
    if "7020" in cat_str:  # E-Books
        return "epub"
    if "7010" in cat_str:  # Magazines
        return "pdf"

    # 3. Media type natural default
    mtype = media_type.lower() if media_type else "manga"
    if mtype == "manga":
        return "cbz"
    elif mtype == "book":
        return "epub"
    elif mtype == "magazine":
        return "pdf"

    return "cbz"


def match_volume_or_issue(title: str, target_vol: int | str) -> tuple[bool, bool]:
    """Checks if a release title matches a requested volume/issue.

    Returns (is_match, is_exact_single):
      - is_match: True if the release is for target_vol or a pack containing target_vol.
      - is_exact_single: True if it is specifically the single volume release (not a multi-volume pack).
    """
    if isinstance(target_vol, str):
        clean_v = target_vol.strip().lower().replace("volume", "").replace("vol", "").replace("v", "").replace("#", "").strip()
        if not clean_v.isdigit():
            return target_vol.strip().lower() in title.lower(), True
        v_int = int(clean_v)
    else:
        v_int = int(target_vol)

    # 1. Multi-volume range check (e.g. Vol.01-10, 01-100, v01-v105, Vol 1 - 100)
    range_pattern = r"(?:vol(?:ume)?|v)?\.?\s*0*(\d+)\s*(?:-|–|to)\s*(?:vol(?:ume)?|v)?\.?\s*0*(\d+)"
    for match in re.finditer(range_pattern, title, re.IGNORECASE):
        try:
            start_v, end_v = int(match.group(1)), int(match.group(2))
            if start_v <= v_int <= end_v:
                return True, False
        except (ValueError, IndexError):
            pass

    # 2. Strict single volume matching
    single_patterns = [
        # Vol.1, Vol.01, Vol.001, Volume 1, Vol 01, Issue 01
        rf"\b(?:vol(?:ume)?|issue)\.?\s*0*{v_int}(?!\d)",
        # v1, v01, v001 preceded by separator
        rf"[\.\s_\[\(-]v0*{v_int}(?!\d)",
        # #1, #01
        rf"#0*{v_int}(?!\d)",
        # Chapter / c01, c1
        rf"\b(?:ch(?:apter)?|c)\.?\s*0*{v_int}(?!\d)",
        # Standalone volume number surrounded by separators
        rf"[\.\s_\(-]0*{v_int}[\.\s_\)-]",
    ]

    for pat in single_patterns:
        if re.search(pat, title, re.IGNORECASE):
            return True, True

    return False, False


def score_print_release(parsed_format: str, media_type: str) -> dict:
    """Scores a print media release based on format hierarchies. Higher score is better."""
    mtype = media_type.lower() if media_type else "manga"
    fmt = parsed_format.lower().strip()

    score_map = PRINT_SCORES.get(mtype, {})
    score = score_map.get(fmt, 0)

    # If not found directly, check generic baseline
    if score == 0:
        if fmt in ("cbz", "epub", "pdf"):
            score = 200
        else:
            score = 100

    return {"score": score, "format": fmt}
