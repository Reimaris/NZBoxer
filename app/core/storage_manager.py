import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def ensure_storage_path(
    base_dir: str | Path,
    media_type: str,
    item_title: str,
    item_author: str = "",
    year: str = "",
) -> Path:
    """
    Creates and returns the structured local folder path for downloading print media.
    Matches the schema defined in ADR-013.
    """
    base = Path(base_dir)

    if media_type == "manga":
        target = base / "manga" / item_title
    elif media_type == "book":
        author = item_author or "Unknown"
        target = base / "books" / author
    elif media_type == "magazine":
        target = base / "magazines" / item_title
        if year:
            target = target / str(year)
    else:
        target = base / "other"

    target.mkdir(parents=True, exist_ok=True)
    return target
