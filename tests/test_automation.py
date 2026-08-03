"""Integration tests for the automation orchestrator."""
from unittest.mock import patch

import pytest

from app.core.automation import _evaluate_and_download
from app.db.models import MediaItem, MediaStatus, MediaType


@pytest.mark.asyncio
async def test_evaluate_and_download_new_movie(db_session):
    from app.db.models import Provider

    provider = Provider(type="simkl", name="Test")
    db_session.add(provider)
    await db_session.flush()

    movie = MediaItem(
        simkl_id=123,
        title="Test Movie",
        media_type=MediaType.MOVIE,
        status=MediaStatus.SEARCHING,
        provider_id=provider.id
    )
    movie.download_history = []
    db_session.add(movie)
    await db_session.flush()

    # Mock search results
    search_results = [
        {
            "title": "Test.Movie.2023.1080p.WEB-DL.h264-GROUP",
            "size": 5 * 1024 * 1024 * 1024,
            "guid": "guid-123"
        }
    ]

    async def mock_send_nzb(*args, **kwargs):
        return {"hash": "torbox-hash-123", "id": 12345}

    async def mock_get_url(*args, **kwargs):
        return "http://dl.com/guid-123"

    with patch('app.core.automation.torbox.send_nzb_link', side_effect=mock_send_nzb) as mock_send, \
         patch('app.core.automation.treasure_maps.get_download_url', side_effect=mock_get_url):
        await _evaluate_and_download(db_session, search_results, movie=movie)

    assert mock_send.call_count == 1
    assert mock_send.call_args[0][0] == "http://dl.com/guid-123"

    # Check DB history
    await db_session.refresh(movie, ["download_history"])
    assert len(movie.download_history) == 1
    assert movie.download_history[0].nzb_title == search_results[0]["title"]
    assert movie.status == MediaStatus.DOWNLOADED

@pytest.mark.asyncio
async def test_evaluate_and_download_upgrade(db_session):
    # Tests that a release is only downloaded if it exceeds the upgrade threshold
    from app.db.models import DownloadHistory, Provider

    provider = Provider(type="simkl", name="Test")
    db_session.add(provider)
    await db_session.flush()

    movie = MediaItem(
        simkl_id=456,
        title="Upgrade Movie",
        media_type=MediaType.MOVIE,
        status=MediaStatus.DOWNLOADED,
        provider_id=provider.id
    )
    db_session.add(movie)
    await db_session.flush()

    # Existing download with 1000 score
    hist = DownloadHistory(
        media_item_id=movie.id,
        nzb_title="Old.Release",
        score=1000.0
    )
    db_session.add(hist)
    await db_session.commit()
    await db_session.refresh(movie, ["download_history"])

    # Result that is only slightly better (e.g. +100 points, threshold is 300)
    search_results = [
        {
            "title": "Upgrade.Movie.2023.1080p.BluRay.x264-GROUP", # Maybe ~1200 points
            "size": 6 * 1024 * 1024 * 1024,
            "guid": "guid-456"
        }
    ]

    async def mock_send_nzb(*args, **kwargs):
        return {"hash": "torbox-hash-123", "id": 12345}

    with patch('app.core.automation.torbox.send_nzb_link', side_effect=mock_send_nzb) as mock_send:
        # Mock scorer to return exactly 1200
        with patch('app.core.automation.score_release') as mock_scorer:
            from app.core.scorer import ScoreResult
            mock_scorer.return_value = ScoreResult(score=1200.0, is_rejected=False, reject_reason=None, bitrate_mbps=5.0)

            await _evaluate_and_download(db_session, search_results, movie=movie)

            # Should NOT have downloaded because 1200 < 1000 + 300
            mock_send.assert_not_called()

        # Mock scorer to return exactly 1500 (meets 300 threshold)
        with patch('app.core.automation.score_release') as mock_scorer:
            mock_scorer.return_value = ScoreResult(score=1500.0, is_rejected=False, reject_reason=None, bitrate_mbps=5.0)

            await _evaluate_and_download(db_session, search_results, movie=movie)

            # SHOULD download
            mock_send.assert_called_once()
