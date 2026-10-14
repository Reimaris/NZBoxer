"""Alpine x-data attributes must survive HTML parsing (no raw double quotes inside the attribute)."""

import re
from html.parser import HTMLParser

import pytest

from tests.conftest import make_indexer_provider


class _XData(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.values += [v or "" for k, v in attrs if k == "x-data"]


def _xdata(html: str) -> list[str]:
    parser = _XData()
    parser.feed(html)
    return parser.values


@pytest.mark.parametrize("url", ["/settings/provider/new", "/onboarding/modal"])
async def test_modal_root_xdata_is_not_truncated(async_client, db_session, url):
    await make_indexer_provider(db_session)
    await db_session.commit()
    html = (await async_client.get(url)).text
    root = max(_xdata(html), key=len)
    # A broken attribute is cut at the first raw double quote and loses its closing brace.
    assert root.rstrip().endswith("}")
    assert re.search(r"indexerTypes|indexerPresets", root)
