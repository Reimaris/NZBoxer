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
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
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
    """Distinguishes movies from series and anime."""

    MOVIE = "movie"
    SHOW = "show"
    ANIME = "anime"


class AutomationState(str, enum.Enum):
    """Global state of background automation runner."""

    ACTIVE = "active"
    UPGRADES_ONLY = "upgrades_only"
    PAUSED = "paused"
    DISABLED = "disabled"


class MediaStatus(str, enum.Enum):
    """Lifecycle status of a MediaItem within NZBoxer."""

    FUTURE = "future"
    PENDING = "pending"
    SEARCHING = "searching"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    COMPLETED = "completed"
    CANCELED = "canceled"
    IGNORED = "ignored"
    MANUAL_GRAB = (
        "manual_grab"  # Title-search fallback found a candidate, needs user approval
    )


class SeasonStatus(str, enum.Enum):
    """Lifecycle status of an individual season."""

    FUTURE = "future"
    PENDING = "pending"
    SEARCHING = "searching"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    COMPLETED = "completed"
    CANCELED = "canceled"
    IGNORED = "ignored"
    MANUAL_GRAB = (
        "manual_grab"  # Title-search fallback found a candidate, needs user approval
    )


class EpisodeStatus(str, enum.Enum):
    """Lifecycle status of an individual episode."""

    FUTURE = "future"
    PENDING = "pending"
    SEARCHING = "searching"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    COMPLETED = "completed"
    CANCELED = "canceled"
    IGNORED = "ignored"
    MANUAL_GRAB = (
        "manual_grab"  # Title-search fallback found a candidate, needs user approval
    )


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

    This is designed as a single-row table (id=1).
    """

    __tablename__ = "system_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    treasure_maps_api_key: Mapped[str] = mapped_column(String(200), default="")
    torbox_api_key: Mapped[str] = mapped_column(String(200), default="")
    tmdb_api_key: Mapped[str] = mapped_column(String(200), default="")

    # Self-Healing & Automation (Defaults)
    automation_state: Mapped[AutomationState] = mapped_column(
        Enum(AutomationState),
        nullable=False,
        default=AutomationState.ACTIVE,
        server_default="ACTIVE",
    )
    scan_interval_multiplier: Mapped[int] = mapped_column(Integer, default=1)
    self_healing_interval: Mapped[int] = mapped_column(
        Integer, default=15, server_default="15"
    )
    video_search_interval: Mapped[int] = mapped_column(
        Integer, default=60, server_default="60"
    )
    print_search_interval: Mapped[int] = mapped_column(
        Integer, default=60, server_default="60"
    )
    sh_max_retries: Mapped[int] = mapped_column(Integer, default=3)
    sh_max_time_hours: Mapped[float] = mapped_column(
        Float, nullable=False, default=12.0
    )
    sh_auto_retry: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sh_retry_wait_hours: Mapped[float] = mapped_column(
        Float, nullable=False, default=24.0
    )
    upgrade_search_interval_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=24, server_default="24"
    )
    max_upgrade_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=7, server_default="7"
    )
    download_timeout_hours: Mapped[int] = mapped_column(
        Integer, default=24, server_default="24", nullable=False
    )
    dry_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    auto_grab_title_fallbacks: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    # Structured Scoring Settings
    scoring_settings: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=True)

    # Core Automation (ADR-012)
    upgrade_threshold: Mapped[int] = mapped_column(
        Integer, default=500, server_default="500"
    )
    backoff_tier2_skip: Mapped[int] = mapped_column(
        Integer, default=6, server_default="6"
    )
    backoff_tier3_skip: Mapped[int] = mapped_column(
        Integer, default=24, server_default="24"
    )

    # Print Media (ADR-013)
    reading_download_dir: Mapped[str] = mapped_column(
        String(500), default="downloads", server_default="downloads"
    )
    manga_blacklisted_formats: Mapped[str] = mapped_column(
        String(200), default="", server_default=""
    )
    book_blacklisted_formats: Mapped[str] = mapped_column(
        String(200), default="", server_default=""
    )
    magazine_blacklisted_formats: Mapped[str] = mapped_column(
        String(200), default="", server_default=""
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


# ---------------------------------------------------------------------------
# Provider & Notification Models
# ---------------------------------------------------------------------------


class NotificationChannel(Base):
    """Represents a notification target, e.g., a Telegram bot."""

    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g., 'telegram'
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Provider specific fields (could be JSON, but let's keep them explicit for Telegram)
    bot_token: Mapped[str] = mapped_column(String(500), nullable=True)
    chat_id: Mapped[str] = mapped_column(String(200), nullable=True)

    provider_profiles: Mapped[list[ProviderProfile]] = relationship(
        "ProviderProfile", back_populates="notification_channel"
    )


class ProviderCategory(str, enum.Enum):
    """Broad functional category for third-party providers and integrations."""

    WATCHLIST = "watchlist"
    PRINT_MEDIA = "print_media"
    METADATA = "metadata"
    DOWNLOADER = "downloader"
    INDEXER = "indexer"


class Provider(Base):
    """Represents a third-party integration or service provider (Watchlist, Reading, Metadata, Downloader, Indexer)."""

    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="watchlist",
        server_default="watchlist",
        index=True,
    )
    type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # e.g., 'simkl', 'torbox', 'treasure_maps', 'tmdb', 'anilist', 'hardcover', 'openlibrary'
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Generic API & Endpoint Credentials
    api_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    api_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Priority & Lifecycle State
    priority: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")

    # Simkl specific fields
    username: Mapped[str | None] = mapped_column(String(200), nullable=True)
    client_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    access_token: Mapped[str | None] = mapped_column(String(500), nullable=True)

    movie_category_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    series_category_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    anime_category_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bandwidth_mbit: Mapped[int | None] = mapped_column(Integer, nullable=True)

    profiles: Mapped[list[ProviderProfile]] = relationship(
        "ProviderProfile",
        back_populates="provider",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    media_items: Mapped[list[MediaItem]] = relationship(
        "MediaItem", back_populates="provider", cascade="all, delete-orphan"
    )


class ProviderProfile(Base):
    """Specific configuration for a provider's media type (Movies or Series)."""

    __tablename__ = "provider_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("providers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    media_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # 'movies' or 'shows'

    # Download & Automation Settings
    path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    mode: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Kept with default for SQLite DB backwards compatibility
    search_cycle_skip: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1"
    )
    current_cycle_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    resolution: Mapped[str | None] = mapped_column(
        String(100), default="Automatisch / beste"
    )
    source: Mapped[str | None] = mapped_column(String(100), default="any")
    video_codec: Mapped[str | None] = mapped_column(String(100), default="any")
    hdr: Mapped[str | None] = mapped_column(String(100), default="any")
    audio_tier: Mapped[str | None] = mapped_column(String(100), default="any")
    audio_channels: Mapped[str | None] = mapped_column(String(100), default="any")
    languages_csv: Mapped[str | None] = mapped_column(String(500), nullable=True)
    primary_language: Mapped[str | None] = mapped_column(
        String(50), nullable=True, default=None
    )
    fallback_language: Mapped[str | None] = mapped_column(
        String(50), nullable=True, default=None
    )
    min_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reject_words_csv: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Series specific
    prefer_complete_seasons: Mapped[bool] = mapped_column(Boolean, default=False)
    episode_block_size: Mapped[int] = mapped_column(Integer, default=0)
    # Kept for DB compat — the actual per-series flag lives on MediaItem
    auto_monitor_next_season: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )

    # Notification link
    notification_channel_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("notification_channels.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    provider: Mapped[Provider] = relationship("Provider", back_populates="profiles")
    notification_channel: Mapped[NotificationChannel | None] = relationship(
        "NotificationChannel", back_populates="provider_profiles"
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

    # --- Foreign key ---
    provider_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("providers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # --- External identifiers ---
    simkl_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    imdb_id: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    tvdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    mal_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    anilist_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # --- Metadata ---
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    alt_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    media_type: Mapped[MediaType] = mapped_column(
        Enum(MediaType, name="media_type_enum"), nullable=False
    )
    is_anime_movie: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
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
    release_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    auto_monitor_next_season: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    # --- Sync tracking ---
    simkl_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Error Tracking ---
    fail_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Manual Grab (title-search fallback) ---
    pending_candidate_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # --- Search Automation Tracking ---
    empty_search_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    upgrade_attempts_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    last_searched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_upgrade_search_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_metadata_refreshed_at: Mapped[datetime | None] = mapped_column(
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
    provider: Mapped[Provider] = relationship("Provider", back_populates="media_items")
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

    @property
    def total_seasons(self) -> int:
        """Total seasons excluding season 0 (specials)."""
        if self.media_type not in (MediaType.SHOW, MediaType.ANIME) or not self.seasons:
            return 0
        return sum(1 for s in self.seasons if s.season_number > 0)

    @property
    def downloaded_seasons(self) -> int:
        """Total downloaded or completed seasons excluding season 0."""
        if self.media_type not in (MediaType.SHOW, MediaType.ANIME) or not self.seasons:
            return 0
        return sum(
            1
            for s in self.seasons
            if s.season_number > 0
            and s.status in [SeasonStatus.DOWNLOADED, SeasonStatus.COMPLETED]
        )

    @property
    def aggregated_upgrade_attempts(self) -> int:
        """Aggregated upgrade attempts for the item (movies) or its seasons."""
        if self.media_type == MediaType.MOVIE:
            return self.upgrade_attempts_count
        elif self.media_type in (MediaType.SHOW, MediaType.ANIME):
            if not self.seasons:
                return 0
            return sum(
                s.upgrade_attempts_count for s in self.seasons if s.season_number > 0
            )
        return 0

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
    def partition(self) -> str:
        """Dashboard partition ('missing' or 'upgrading')."""
        if hasattr(self, "_partition"):
            return self._partition
        from app.core.automation import classify_video_item_partition

        return classify_video_item_partition(self)

    @partition.setter
    def partition(self, val: str) -> None:
        self._partition = val

    @property
    def best_score(self) -> float | None:
        """Returns the highest score from all associated download history entries."""
        if hasattr(self, "_best_score_override"):
            return self._best_score_override
        from sqlalchemy.orm import attributes

        hist = attributes.instance_state(self).dict.get("download_history")
        if not hist:
            return None
        scores = [h.score for h in hist if getattr(h, "score", None) is not None]
        return max(scores) if scores else None

    @best_score.setter
    def best_score(self, value: float | None) -> None:
        self._best_score_override = value

    # ---------------------------------------------------------------------------
    # Season Model
    # ---------------------------------------------------------------------------
    failure_logs: Mapped[list["FailureLog"]] = relationship(
        "FailureLog",
        back_populates="media_item",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def is_fully_completed(self) -> bool:
        """Returns True if the item requires no further automated actions (History)."""
        from app.config import scoring_config

        target_score = scoring_config.get("cutoffs", {}).get("target_score", 8000)

        if self.status in [MediaStatus.COMPLETED, MediaStatus.IGNORED]:
            return True

        if self.media_type == MediaType.MOVIE:
            if (
                self.status == MediaStatus.DOWNLOADED
                and self.best_score is not None
                and self.best_score >= target_score
            ):
                return True
            return False

        elif self.media_type in (MediaType.SHOW, MediaType.ANIME):
            if not self.seasons or self.total_seasons == 0:
                return False

            for season in self.seasons:
                if season.season_number == 0:
                    continue
                if season.status in [SeasonStatus.COMPLETED, SeasonStatus.IGNORED]:
                    continue
                if (
                    season.status == SeasonStatus.DOWNLOADED
                    and season.best_score is not None
                    and season.best_score >= target_score
                ):
                    continue
                return False
            return True
        return False


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
        UniqueConstraint(
            "media_item_id", "season_number", name="uq_season_item_number"
        ),
    )

    # --- Primary key ---
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- Foreign key ---
    media_item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("media_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # --- Season data ---
    season_number: Mapped[int] = mapped_column(Integer, nullable=False)
    simkl_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    anilist_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    monitored: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[SeasonStatus] = mapped_column(
        Enum(SeasonStatus, name="season_status_enum"),
        nullable=False,
        default=SeasonStatus.PENDING,
        server_default=SeasonStatus.PENDING.value,
    )
    episode_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    air_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Error Tracking ---
    fail_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Manual Grab (title-search fallback) ---
    pending_candidate_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # --- Search Automation Tracking ---
    empty_search_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    upgrade_attempts_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    last_searched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_upgrade_search_at: Mapped[datetime | None] = mapped_column(
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
    media_item: Mapped[MediaItem] = relationship("MediaItem", back_populates="seasons")
    episodes: Mapped[list[Episode]] = relationship(
        "Episode",
        back_populates="season",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
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
        if hasattr(self, "_best_score_override"):
            return self._best_score_override
        from sqlalchemy.orm import attributes

        hist = attributes.instance_state(self).dict.get("download_history")
        if not hist:
            return None
        scores = [h.score for h in hist if getattr(h, "score", None) is not None]
        return max(scores) if scores else None

    @best_score.setter
    def best_score(self, value: float | None) -> None:
        self._best_score_override = value

    # ---------------------------------------------------------------------------
    # Episode Model
    # ---------------------------------------------------------------------------
    failure_logs: Mapped[list["FailureLog"]] = relationship(
        "FailureLog",
        back_populates="season",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class Episode(Base):
    """Represents a single episode of a season.

    Attributes:
        id:             Auto-incremented primary key.
        season_id:      FK → Season.id.
        episode_number: 1-based episode index.
        monitored:      Whether the automation engine should search this episode.
        status:         Current automation lifecycle status.
        air_date:       Original air date.
        created_at:     Row creation timestamp.
        updated_at:     Row last-modified timestamp.

        season:           Back-reference to the parent Season.
        download_history: One-to-many to DownloadHistory rows for this episode.
    """

    __tablename__ = "episodes"
    __table_args__ = (
        UniqueConstraint(
            "season_id", "episode_number", name="uq_episode_season_number"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("seasons.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    episode_number: Mapped[int] = mapped_column(Integer, nullable=False)
    monitored: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[EpisodeStatus] = mapped_column(
        Enum(EpisodeStatus, name="episode_status_enum"),
        nullable=False,
        default=EpisodeStatus.PENDING,
        server_default=EpisodeStatus.PENDING.value,
    )
    air_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Error Tracking ---
    fail_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Manual Grab (title-search fallback) ---
    pending_candidate_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # --- Search Automation Tracking ---
    empty_search_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    upgrade_attempts_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    last_searched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_upgrade_search_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    season: Mapped[Season] = relationship("Season", back_populates="episodes")
    download_history: Mapped[list[DownloadHistory]] = relationship(
        "DownloadHistory",
        back_populates="episode",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return (
            f"<Episode id={self.id} season_id={self.season_id} "
            f"E{self.episode_number:02d} monitored={self.monitored} status={self.status}>"
        )

    @property
    def best_score(self) -> float | None:
        """Returns the highest score from all download history entries for this episode."""
        from sqlalchemy.orm import attributes

        hist = attributes.instance_state(self).dict.get("download_history")
        if not hist:
            return None
        scores = [h.score for h in hist if getattr(h, "score", None) is not None]
        return max(scores) if scores else None

    @property
    def is_released(self) -> bool:
        """True if the air date is in the past (or not set)."""
        if self.air_date is None:
            return True
        return datetime.now(tz=self.air_date.tzinfo) >= self.air_date

    # ---------------------------------------------------------------------------
    # DownloadHistory Model
    # ---------------------------------------------------------------------------
    failure_logs: Mapped[list["FailureLog"]] = relationship(
        "FailureLog",
        back_populates="episode",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


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
        Integer,
        ForeignKey("media_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    season_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("seasons.id", ondelete="CASCADE"), nullable=True, index=True
    )
    episode_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("episodes.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
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
    torbox_hash: Mapped[str | None] = mapped_column(
        String(200), nullable=True, index=True
    )
    torbox_id: Mapped[str | None] = mapped_column(
        String(200), nullable=True, index=True
    )
    torbox_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Language tracking ---
    is_fallback: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    grabbed_language: Mapped[str | None] = mapped_column(
        String(50), nullable=True, default=None
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
    episode: Mapped[Episode | None] = relationship(
        "Episode", back_populates="download_history"
    )

    def __repr__(self) -> str:
        return (
            f"<DownloadHistory id={self.id} item_id={self.media_item_id} "
            f"score={self.score} title={self.nzb_title!r:.40}>"
        )


# ---------------------------------------------------------------------------
# BlacklistedRelease Model
# ---------------------------------------------------------------------------


class BlacklistedRelease(Base):
    """Tracks failed NZBs (e.g. removed by TorBox or invalid) so they are
    skipped during the next search cycle (Self-Healing).
    """

    __tablename__ = "blacklisted_releases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    media_item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("media_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    nzb_guid: Mapped[str | None] = mapped_column(String(500), nullable=True, index=True)
    nzb_title: Mapped[str] = mapped_column(String(1000), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# DailyGrabCounter Model
# ---------------------------------------------------------------------------


class DailyGrabCounter(Base):
    """Tracks daily NZB downloads (grabs) to enforce a hard daily limit of 400."""

    __tablename__ = "daily_grab_counters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date_str: Mapped[str] = mapped_column(
        String(10), unique=True, index=True
    )  # YYYY-MM-DD
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


# ---------------------------------------------------------------------------
# Print Media Models (ADR-013)
# ---------------------------------------------------------------------------


class MangaItem(Base):
    """Represents a tracked Manga series (e.g. from AniList)."""

    __tablename__ = "manga_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    anilist_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[MediaStatus] = mapped_column(
        Enum(MediaStatus), default=MediaStatus.PENDING
    )

    # Metadata
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_image: Mapped[str | None] = mapped_column(String(500), nullable=True)
    start_year: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Tracking & Errors
    best_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    empty_search_count: Mapped[int] = mapped_column(Integer, default=0)
    upgrade_attempts_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    last_searched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    volumes: Mapped[list["MangaVolume"]] = relationship(
        "MangaVolume",
        back_populates="manga",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def is_fully_completed(self) -> bool:
        """Returns True if the item requires no further automated actions (History)."""
        from app.config import scoring_config

        target_score = scoring_config.get("cutoffs", {}).get("target_score", 8000)

        if self.status in [MediaStatus.COMPLETED, MediaStatus.IGNORED]:
            return True

        if (
            self.status == MediaStatus.DOWNLOADED
            and self.best_score is not None
            and self.best_score >= target_score
        ):
            return True
        return False


class MangaVolume(Base):
    """Represents a specific volume of a Manga."""

    __tablename__ = "manga_volumes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    manga_id: Mapped[int] = mapped_column(
        ForeignKey("manga_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    volume_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[EpisodeStatus] = mapped_column(
        Enum(EpisodeStatus), default=EpisodeStatus.PENDING
    )

    best_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    empty_search_count: Mapped[int] = mapped_column(Integer, default=0)
    upgrade_attempts_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    last_searched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_upgrade_search_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    manga: Mapped[MangaItem] = relationship("MangaItem", back_populates="volumes")
    failure_logs: Mapped[list["FailureLog"]] = relationship(
        "FailureLog",
        back_populates="manga_volume",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class BookItem(Base):
    """Represents a standalone Book (e.g. from Hardcover/OpenLibrary)."""

    __tablename__ = "book_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    isbn: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[MediaStatus] = mapped_column(
        Enum(MediaStatus), default=MediaStatus.PENDING
    )

    best_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    empty_search_count: Mapped[int] = mapped_column(Integer, default=0)
    upgrade_attempts_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    last_searched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_upgrade_search_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_logs: Mapped[list["FailureLog"]] = relationship(
        "FailureLog",
        back_populates="book_item",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def is_fully_completed(self) -> bool:
        """Returns True if the item requires no further automated actions (History)."""
        from app.config import scoring_config

        target_score = scoring_config.get("cutoffs", {}).get("target_score", 8000)

        if self.status in [MediaStatus.COMPLETED, MediaStatus.IGNORED]:
            return True

        if (
            self.status == MediaStatus.DOWNLOADED
            and self.best_score is not None
            and self.best_score >= target_score
        ):
            return True
        return False


class MagazineSubscription(Base):
    """Represents a recurring subscription to a Magazine."""

    __tablename__ = "magazine_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    publisher: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[MediaStatus] = mapped_column(
        Enum(MediaStatus), default=MediaStatus.PENDING
    )

    best_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    empty_search_count: Mapped[int] = mapped_column(Integer, default=0)
    upgrade_attempts_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    last_searched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    issues: Mapped[list["MagazineIssue"]] = relationship(
        "MagazineIssue",
        back_populates="subscription",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class MagazineIssue(Base):
    """Represents a single downloaded issue of a Magazine."""

    __tablename__ = "magazine_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("magazine_subscriptions.id"), nullable=False
    )
    issue_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    issue_number: Mapped[str | None] = mapped_column(String(50), nullable=True)

    status: Mapped[EpisodeStatus] = mapped_column(
        Enum(EpisodeStatus), default=EpisodeStatus.COMPLETED
    )

    subscription: Mapped[MagazineSubscription] = relationship(
        "MagazineSubscription", back_populates="issues"
    )


# ---------------------------------------------------------------------------
# SeenTorboxDownload Model
# ---------------------------------------------------------------------------


class SeenTorboxDownload(Base):
    """Caches evaluated TorBox Usenet downloads to prevent redundant title parsing.

    Attributes:
        id:             Auto-incremented primary key.
        torbox_id:      Unique TorBox download ID (string/int).
        raw_title:      Original release name from TorBox.
        parsed_title:   Extracted title / normalized name.
        parsed_year:    Extracted release year (if available).
        season_number:  Extracted season number (if season pack or episode).
        episode_number: Extracted episode number (if individual episode).
        media_type:     Inferred media type ('movie', 'series_season', 'series_episode', 'book', 'manga', 'unknown').
        download_state: TorBox state ('completed', 'cached', 'downloading', 'queued', 'processing', 'failed', 'error', etc.).
        size_bytes:     File size in bytes.
        progress:       Download progress percentage (0.0 to 1.0 or 0 to 100).
        created_at:     Row creation timestamp.
        updated_at:     Last state update timestamp.
    """

    __tablename__ = "seen_torbox_downloads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    torbox_id: Mapped[str] = mapped_column(
        String(200), unique=True, index=True, nullable=False
    )
    raw_title: Mapped[str] = mapped_column(String(1000), nullable=False)
    parsed_title: Mapped[str | None] = mapped_column(
        String(500), index=True, nullable=True
    )
    parsed_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    media_type: Mapped[str] = mapped_column(
        String(50), default="unknown", nullable=False
    )
    download_state: Mapped[str] = mapped_column(
        String(50), default="unknown", nullable=False
    )
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<SeenTorboxDownload id={self.id} torbox_id={self.torbox_id!r} "
            f"state={self.download_state!r} title={self.raw_title!r:.40}>"
        )


class FailureLog(Base):
    """Centralized audit trail for transient and hard failures."""

    __tablename__ = "failure_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    media_item_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("media_items.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    season_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("seasons.id", ondelete="CASCADE"), index=True, nullable=True
    )
    episode_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("episodes.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    book_item_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("book_items.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    manga_volume_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("manga_volumes.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )

    category: Mapped[str] = mapped_column(String(100), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    media_item: Mapped[MediaItem | None] = relationship(
        "MediaItem", back_populates="failure_logs"
    )
    season: Mapped[Season | None] = relationship(
        "Season", back_populates="failure_logs"
    )
    episode: Mapped[Episode | None] = relationship(
        "Episode", back_populates="failure_logs"
    )
    book_item: Mapped[BookItem | None] = relationship(
        "BookItem", back_populates="failure_logs"
    )
    manga_volume: Mapped[MangaVolume | None] = relationship(
        "MangaVolume", back_populates="failure_logs"
    )
