"""Privacy-safe environment diagnostics for the in-app feedback helper (ADR-127)."""

from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Provider
from app.version import APP_VERSION

GITHUB_NEW_ISSUE_URL: str = "https://github.com/Reimaris/NZBoxer/issues/new"

PROVIDER_TYPE_LABELS: dict[str, str] = {
    "simkl": "Simkl",
    "tmdb": "TMDB",
    "anilist": "AniList",
    "torbox": "TorBox",
    "treasure_maps": "Newznab",
    "newznab": "Newznab",
}


@dataclass(frozen=True)
class FeedbackDiagnostics:
    """The only environment facts exposed in a feedback report."""

    app_version: str
    python_version: str
    os_arch: str
    deployment: str
    active_provider_types: list[str]


def detect_deployment() -> str:
    """Return "Docker" if running inside a container, else "Native"."""
    return "Docker" if Path("/.dockerenv").exists() else "Native"


async def collect_feedback_diagnostics(session: AsyncSession) -> FeedbackDiagnostics:
    """Collect diagnostics; only `Provider.type` of active providers is read."""
    types = (
        (
            await session.execute(
                select(Provider.type).where(Provider.is_active.is_(True))
            )
        )
        .scalars()
        .all()
    )
    labels = sorted({PROVIDER_TYPE_LABELS.get(t, t.capitalize()) for t in types})
    return FeedbackDiagnostics(
        app_version=APP_VERSION,
        python_version=platform.python_version(),
        os_arch=f"{platform.system()} {platform.machine()}",
        deployment=detect_deployment(),
        active_provider_types=labels,
    )


def build_bug_markdown(diag: FeedbackDiagnostics) -> str:
    """Return the GitHub bug-report Markdown template with the diagnostics table."""
    providers = ", ".join(diag.active_provider_types) or "None"
    return (
        "### Summary\n"
        "<!-- Briefly describe the problem -->\n\n"
        "### Steps to Reproduce\n"
        "1.\n2.\n3.\n\n"
        "### Expected Behavior\n\n"
        "### Actual Behavior\n\n"
        "### Environment Diagnostics\n"
        "| Field | Value |\n"
        "| --- | --- |\n"
        f"| NZBoxer Version | {diag.app_version} |\n"
        f"| Python | {diag.python_version} |\n"
        f"| OS / Architecture | {diag.os_arch} |\n"
        f"| Deployment | {diag.deployment} |\n"
        f"| Active Providers | {providers} |\n"
    )


def build_feature_markdown(diag: FeedbackDiagnostics) -> str:
    """Return the GitHub feature-request Markdown template."""
    return (
        "### Problem Statement\n\n"
        "### Proposed Solution / Feature Idea\n\n"
        "### Alternatives Considered\n\n"
        "### Additional Context\n"
        f"NZBoxer version: {diag.app_version}\n"
    )
