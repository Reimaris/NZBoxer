from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BookItem, Episode, FailureLog, MangaVolume, MediaItem, Season


def log_failure(
    session: AsyncSession, target: Any, category: str, message: str
) -> None:
    """Creates a FailureLog entry for the given target."""
    target_kwargs = {}
    if isinstance(target, MediaItem):
        target_kwargs["media_item_id"] = target.id
    elif isinstance(target, Season):
        target_kwargs["season_id"] = target.id
    elif isinstance(target, Episode):
        target_kwargs["episode_id"] = target.id
    elif isinstance(target, BookItem):
        target_kwargs["book_item_id"] = target.id
    elif isinstance(target, MangaVolume):
        target_kwargs["manga_volume_id"] = target.id

    # Still update last_error for legacy compatibility if it exists
    if hasattr(target, "last_error"):
        target.last_error = message

    f_log = FailureLog(category=category, message=message, **target_kwargs)
    session.add(f_log)
