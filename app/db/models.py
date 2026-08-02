"""
NZBoxer Database Models
=======================
SQLAlchemy 2.0 ORM definitions for all database entities.

Models:
    - MediaItem:       A movie or series tracked from the Simkl watchlist.
    - Season:          A season belonging to a series MediaItem.
    - DownloadHistory: A record of an NZB sent to TorBox (for upgrade logic).
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class MediaType(str, enum.Enum):
    """Distinguishes movies from series."""

    MOVIE = "movie"
    SHOW = "show"


class MediaStatus(str, enum.Enum):
    """Lifecycle status of a MediaItem within NZBoxer.

    pending    — release date has not yet passed; search not started.
    searching  — actively searched by the automation engine.
    downloaded — at least one NZB has been sent to TorBox.
    completed  — cutoff score reached; no further searching needed.
    canceled   — manually canceled by the user; automation skips this item.
    """

    PENDING = "pending"
    SEARCHING = "searching"
    DOWNLOADED = "downloaded"
    COMPLETED = "completed"
    CANCELED = "canceled"


class SeasonStatus(str, enum.Enum):
    """Lifecycle status of an individual season."""

    PENDING = "pending"
    SEARCHING = "searching"
    DOWNLOADED = "downloaded"
    COMPLETED = "completed"
    CANCELED = "canceled"


# ---------------------------------------------------------------------------
# Declarative Base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """Shared base class for all ORM models."""



# ---------------------------------------------------------------------------
# SystemSettings Model
# ---------------------------------------------------------------------------

class SystemSettings(Base):
    """Stores the global application configuration in the database.
    
    This is designed as a single-row table (id=1) so users can edit API keys
    and the scoring matrix dynamically via the UI without editing text files.
    """
    
    __tablename__ = "system_settings"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    
    simkl_client_id: Mapped[str] = mapped_column(String(200), default="")
    simkl_access_token: Mapped[str] = mapped_column(String(200), default="")
    tmdb_api_key: Mapped[str] = mapped_column(String(200), default="")
    treasure_maps_url: Mapped[str] = mapped_column(String(500), default="")
    treasure_maps_api_key: Mapped[str] = mapped_column(String(200), default="")
    torbox_api_key: Mapped[str] = mapped_column(String(200), default="")
    
    scoring_config_yaml: Mapped[str] = mapped_column(Text, default="")
    
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


# ---------------------------------------------------------------------------
# MediaItem Model
# ---------------------------------------------------------------------------


class MediaItem(Base):
    """Represents a movie or TV series tracked from the Simkl watchlist.

    The item is the primary unit of automation. Movies and series differ
    mainly in that series have associated Season rows.

    Attributes:
        id:              Auto-incremented primary key (internal).
        simkl_id:        Unique Simkl item ID.
        imdb_id:         IMDb identifier (e.g. 'tt1234567'). May be None.
        tmdb_id:         TMDB numeric identifier. May be None.
        title:           Canonical title (from Simkl).
        year:            Release year.
        media_type:      'movie' or 'show'.
        status:          Current automation lifecycle status.
        poster_url:      Remote URL for poster image (used in UI).
        overview:        Short plot description (from TMDB).
        runtime_minutes: Approximate runtime in minutes (for bitrate calc).
        release_date:    Digital release date (Type 4 from TMDB). Movies only.
                         Items with future release_date stay 'pending'.
        simkl_synced_at: Timestamp of last successful Simkl sync.
        created_at:      Row creation timestamp.
        updated_at:      Row last-modified timestamp (auto-updated).

        seasons:          One-to-many relationship to Season rows.
        download_history: One-to-many relationship to DownloadHistory rows.
    """

    __tablename__ = "media_items"

    # --- Primary key ---
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- External identifiers ---
    simkl_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
    imdb_id: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # --- Metadata ---
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    media_type: Mapped[MediaType] = mapped_column(
        Enum(MediaType, name="media_type_enum"), nullable=False
    )
    status: Mapped[MediaStatus] = mapped_column(
        Enum(MediaStatus, name="media_status_enum"),
        nullable=False,
        default=MediaStatus.PENDING,
        server_default=MediaStatus.PENDING.value,
        index=True,
    )
    poster_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    overview: Mapped[str | None] = mapped_column(Text, nullable=True)
    runtime_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- Release date (movies: digital release; shows: first air date) ---
    release_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- Sync tracking ---
    simkl_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Audit timestamps ---
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # --- Relationships ---
    seasons: Mapped[list[Season]] = relationship(
        "Season",
        back_populates="media_item",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    download_history: Mapped[list[DownloadHistory]] = relationship(
        "DownloadHistory",
        back_populates="media_item",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return (
            f"<MediaItem id={self.id} simkl_id={self.simkl_id} "
            f"title={self.title!r} type={self.media_type} status={self.status}>"
        )

    @property
    def is_released(self) -> bool:
        """True if the digital release date is in the past (or not set)."""
        if self.release_date is None:
            return True
        return datetime.now(tz=self.release_date.tzinfo) >= self.release_date

    @property
    def best_score(self) -> float | None:
        """Returns the highest score from all associated download history entries."""
        if not self.download_history:
            return None
        return max(h.score for h in self.download_history if h.score is not None)


# ---------------------------------------------------------------------------
# Season Model
# ---------------------------------------------------------------------------


class Season(Base):
    """Represents a single season of a TV series.

    Only seasons with ``monitored = True`` are searched by the automation
    engine. By default, Season 1 is monitored; all others are not.

    Attributes:
        id:            Auto-incremented primary key.
        media_item_id: FK → MediaItem.id.
        season_number: 1-based season index (0 = specials).
        monitored:     Whether the automation engine should search this season.
        status:        Current automation lifecycle status.
        episode_count: Total number of episodes (from TMDB), if known.
        air_date:      Season premiere date (from TMDB).
        created_at:    Row creation timestamp.
        updated_at:    Row last-modified timestamp.

        media_item:       Back-reference to the parent MediaItem.
        download_history: One-to-many to DownloadHistory rows for this season.
    """

    __tablename__ = "seasons"
    __table_args__ = (
        UniqueConstraint("media_item_id", "season_number", name="uq_season_item_number"),
    )

    # --- Primary key ---
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- Foreign key ---
    media_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # --- Season data ---
    season_number: Mapped[int] = mapped_column(Integer, nullable=False)
    monitored: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[SeasonStatus] = mapped_column(
        Enum(SeasonStatus, name="season_status_enum"),
        nullable=False,
        default=SeasonStatus.PENDING,
        server_default=SeasonStatus.PENDING.value,
    )
    episode_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    air_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- Audit timestamps ---
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # --- Relationships ---
    media_item: Mapped[MediaItem] = relationship("MediaItem", back_populates="seasons")
    download_history: Mapped[list[DownloadHistory]] = relationship(
        "DownloadHistory",
        back_populates="season",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return (
            f"<Season id={self.id} item_id={self.media_item_id} "
            f"S{self.season_number:02d} monitored={self.monitored} status={self.status}>"
        )

    @property
    def best_score(self) -> float | None:
        """Returns the highest score from all download history entries for this season."""
        if not self.download_history:
            return None
        return max(h.score for h in self.download_history if h.score is not None)


# ---------------------------------------------------------------------------
# DownloadHistory Model
# ---------------------------------------------------------------------------


class DownloadHistory(Base):
    """Records every NZB that has been sent to TorBox.

    Used by the upgrade logic to compare new candidates against what has
    already been downloaded. A new NZB is only sent if its score exceeds
    the current best score by at least the configured ``upgrade_threshold``.

    Attributes:
        id:             Auto-incremented primary key.
        media_item_id:  FK → MediaItem.id (always set).
        season_id:      FK → Season.id (set for series downloads, None for movies).
        nzb_title:      Original raw NZB release name (for audit / re-parsing).
        nzb_guid:       Unique identifier from the Newznab indexer.
        score:          The computed score at time of download decision.
        size_bytes:     Reported file size in bytes.
        resolution:     Parsed resolution string (e.g. '1080p').
        video_codec:    Parsed video codec string (e.g. 'h265').
        audio_codec:    Parsed audio codec string.
        source:         Parsed source string (e.g. 'Blu-ray').
        release_group:  Parsed release group name.
        torbox_hash:    Hash or ID returned by TorBox after successful upload.
        torbox_sent_at: Timestamp when the NZB was sent to TorBox.
        created_at:     Row creation timestamp.

        media_item:     Back-reference to parent MediaItem.
        season:         Back-reference to parent Season (if applicable).
    """

    __tablename__ = "download_history"

    # --- Primary key ---
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- Foreign keys ---
    media_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    season_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("seasons.id", ondelete="CASCADE"), nullable=True, index=True
    )

    # --- NZB metadata ---
    nzb_title: Mapped[str] = mapped_column(String(1000), nullable=False)
    nzb_guid: Mapped[str | None] = mapped_column(String(500), nullable=True, index=True)

    # --- Scoring at time of download ---
    score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- Parsed release attributes (denormalized for quick display/comparison) ---
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(20), nullable=True)
    video_codec: Mapped[str | None] = mapped_column(String(50), nullable=True)
    audio_codec: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    release_group: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bitrate_mbps: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- TorBox tracking ---
    torbox_hash: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    torbox_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Audit timestamps ---
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # --- Relationships ---
    media_item: Mapped[MediaItem] = relationship(
        "MediaItem", back_populates="download_history"
    )
    season: Mapped[Season | None] = relationship(
        "Season", back_populates="download_history"
    )

    def __repr__(self) -> str:
        return (
            f"<DownloadHistory id={self.id} item_id={self.media_item_id} "
            f"score={self.score} title={self.nzb_title!r:.40}>"
        )
