import httpx
import pytest

from app.core import adhoc_search
from app.db.models import Provider
from app.services.indexers import newznab

URL = "https://indexer.example.com/api"
JSON_HDR = {"content-type": "application/json"}


@pytest.fixture
def mock_http(monkeypatch):
    """Route newznab's httpx.AsyncClient through a MockTransport handler."""

    async def no_wait():
        return None

    monkeypatch.setattr(newznab._limiter, "wait", no_wait)
    real_client = httpx.AsyncClient

    def install(handler):
        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(newznab.httpx, "AsyncClient", factory)

    return install


@pytest.mark.asyncio
async def test_search_503_raises_outage(mock_http):
    mock_http(lambda req: httpx.Response(503))
    with pytest.raises(newznab.IndexerOutageError) as ei:
        await newznab._execute_search(URL, {"t": "search"})
    assert ei.value.verdict.label == "HTTP 503"
    assert not newznab.is_indexer_in_cooldown(URL)


@pytest.mark.asyncio
async def test_search_read_timeout_raises_outage(mock_http):
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    mock_http(handler)
    with pytest.raises(newznab.IndexerOutageError) as ei:
        await newznab._execute_search(URL, {})
    assert ei.value.verdict.label == "ReadTimeout"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_search_auth_status_raises_auth(mock_http, status):
    mock_http(lambda req: httpx.Response(status))
    with pytest.raises(newznab.IndexerAuthError):
        await newznab._execute_search(URL, {})


@pytest.mark.asyncio
async def test_search_html_body_raises_outage(mock_http):
    mock_http(
        lambda req: httpx.Response(
            200, headers={"content-type": "text/html"}, text="<html>Parked</html>"
        )
    )
    with pytest.raises(newznab.IndexerOutageError) as ei:
        await newznab._execute_search(URL, {})
    assert ei.value.verdict.label == "Non-JSON response"


@pytest.mark.asyncio
async def test_search_invalid_json_raises_outage(mock_http):
    mock_http(lambda req: httpx.Response(200, headers=JSON_HDR, text="{oops"))
    with pytest.raises(newznab.IndexerOutageError) as ei:
        await newznab._execute_search(URL, {})
    assert ei.value.verdict.label == "JSONDecodeError"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,exc",
    [("100", newznab.IndexerAuthError), ("500", newznab.IndexerOutageError)],
)
async def test_search_newznab_error_payload(mock_http, code, exc):
    payload = {"@attributes": {"code": code, "description": "err"}}
    mock_http(lambda req: httpx.Response(200, json=payload, headers=JSON_HDR))
    with pytest.raises(exc):
        await newznab._execute_search(URL, {})


@pytest.mark.asyncio
async def test_search_429_and_404_unchanged(mock_http):
    mock_http(lambda req: httpx.Response(404))
    assert await newznab._execute_search(URL, {}) == []
    mock_http(lambda req: httpx.Response(429, headers={"Retry-After": "30"}))
    with pytest.raises(newznab.IndexerRateLimitError):
        await newznab._execute_search(URL, {})


@pytest.mark.asyncio
async def test_fetch_nzb_html_is_outage_text_is_generic(mock_http):
    kwargs = {"api_url": URL, "api_key": "k"}
    mock_http(
        lambda req: httpx.Response(
            200, headers={"content-type": "text/html"}, text="<html>Maint</html>"
        )
    )
    with pytest.raises(newznab.IndexerOutageError):
        await newznab.fetch_nzb_bytes("abc", **kwargs)

    mock_http(
        lambda req: httpx.Response(
            200, headers={"content-type": "text/plain"}, text="nope"
        )
    )
    with pytest.raises(newznab.IndexerError) as ei:
        await newznab.fetch_nzb_bytes("abc", **kwargs)
    assert not isinstance(ei.value, newznab.IndexerOutageError)


@pytest.mark.asyncio
async def test_fetch_nzb_503_and_401(mock_http):
    kwargs = {"api_url": URL, "api_key": "k"}
    mock_http(lambda req: httpx.Response(503))
    with pytest.raises(newznab.IndexerOutageError):
        await newznab.fetch_nzb_bytes("abc", **kwargs)
    mock_http(lambda req: httpx.Response(401))
    with pytest.raises(newznab.IndexerAuthError):
        await newznab.fetch_nzb_bytes("abc", **kwargs)


@pytest.mark.asyncio
async def test_adhoc_failover_and_all_outage(monkeypatch):
    down = Provider(name="Down", api_url="https://down.example/api", api_key="k")
    up = Provider(name="Up", api_url="https://up.example/api", api_key="k")

    async def active(_session):
        return [down, up]

    async def search_movie(**kw):
        if "down.example" in kw["api_url"]:
            raise newznab.IndexerOutageError("HTTP 503")
        return [{"title": "R1", "guid": "g1"}]

    monkeypatch.setattr(adhoc_search.provider_service, "get_active_indexers", active)
    monkeypatch.setattr(adhoc_search.newznab_client, "search_movie", search_movie)
    args = dict(
        kind="movie",
        titles=["X"],
        imdb_id=None,
        tmdb_id=1,
        tvdb_id=None,
        season=None,
        episode=None,
    )
    merged, err = await adhoc_search._fetch_raw_releases(**args)
    assert [r["guid"] for r in merged] == ["g1"] and err is None

    async def active_down(_session):
        return [down]

    monkeypatch.setattr(
        adhoc_search.provider_service, "get_active_indexers", active_down
    )
    merged, err = await adhoc_search._fetch_raw_releases(**args)
    assert merged == [] and err == "HTTP 503"
