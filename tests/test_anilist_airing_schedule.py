from datetime import datetime, timezone

import httpx
import pytest

from app.services import anilist

TS_5, TS_6 = 1792008000, 1792612800


@pytest.fixture
def mock_anilist(monkeypatch):
    """Route anilist's AsyncClient through a MockTransport; returns the request log."""
    calls: list[httpx.Request] = []
    responses: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses.pop(0)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        anilist.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(anilist._limiter, "wait", _noop)
    monkeypatch.setattr(anilist.asyncio, "sleep", _noop)
    return calls, responses


def _media(nodes):
    return httpx.Response(
        200, json={"data": {"Media": {"airingSchedule": {"nodes": nodes}}}}
    )


async def test_returns_sorted_utc_schedule(mock_anilist):
    _, responses = mock_anilist
    responses.append(
        _media(
            [
                {"episode": 6, "airingAt": TS_6},
                {"episode": 5, "airingAt": TS_5},
            ]
        )
    )
    assert await anilist.get_anime_airing_schedule(1) == [
        (5, datetime.fromtimestamp(TS_5, tz=timezone.utc)),
        (6, datetime.fromtimestamp(TS_6, tz=timezone.utc)),
    ]


@pytest.mark.parametrize(
    "response",
    [
        _media([]),
        httpx.Response(200, json={"data": {"Media": None}}),
        httpx.Response(200, json={"data": {"Media": {"airingSchedule": None}}}),
    ],
)
async def test_empty_finished_or_unknown_returns_empty(mock_anilist, response):
    mock_anilist[1].append(response)
    assert await anilist.get_anime_airing_schedule(1) == []


async def test_malformed_nodes_skipped(mock_anilist):
    mock_anilist[1].append(
        _media(
            [
                {"episode": 5},
                {"airingAt": TS_5},
                {"episode": "x", "airingAt": TS_5},
                {"episode": 7, "airingAt": None},
                "junk",
                {"episode": 6, "airingAt": TS_6},
            ]
        )
    )
    assert await anilist.get_anime_airing_schedule(1) == [
        (6, datetime.fromtimestamp(TS_6, tz=timezone.utc))
    ]


async def test_429_is_retried(mock_anilist):
    calls, responses = mock_anilist
    responses.extend(
        [
            httpx.Response(429, headers={"Retry-After": "1"}),
            _media([{"episode": 5, "airingAt": TS_5}]),
        ]
    )
    assert await anilist.get_anime_airing_schedule(1) == [
        (5, datetime.fromtimestamp(TS_5, tz=timezone.utc))
    ]
    assert len(calls) == 2
