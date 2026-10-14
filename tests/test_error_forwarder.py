"""Tests for the Error Forwarder logging handler (ADR-096)."""

import asyncio
import logging

import pytest

from app.core.error_forwarder import (
    MAX_BODY_CHARS,
    ErrorForwarderHandler,
    truncate_body,
)
from app.core.log_redaction import clear_secrets, register_secret
from app.db.models import NotificationChannel, SystemSettings
from app.services.discord import dispatch_alert_message


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def sent():
    return []


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def forwarder(sent, clock):
    async def sender(title, body):
        sent.append((title, body))

    return ErrorForwarderHandler(sender=sender, dedup_window=600.0, clock=clock)


@pytest.fixture
def log(forwarder):
    lg = logging.getLogger("tests.forwarder")
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    lg.addHandler(forwarder)
    yield lg
    lg.removeHandler(forwarder)


async def _drain(forwarder):
    await asyncio.gather(*list(forwarder._tasks))


async def test_error_is_forwarded_warning_is_not(log, forwarder, sent):
    log.warning("just a warning")
    log.error("boom happened")
    await _drain(forwarder)
    assert len(sent) == 1
    assert "boom happened" in sent[0][1]
    assert "ERROR" in sent[0][0]


async def test_traceback_included_and_redacted(log, forwarder, sent):
    clear_secrets()
    register_secret("supersecretkey12345")
    try:
        raise ValueError("bad url ?apikey=supersecretkey12345")
    except ValueError:
        log.exception("search failed")
    await _drain(forwarder)
    body = sent[0][1]
    assert "Traceback" in body
    assert "supersecretkey12345" not in body
    clear_secrets()


async def test_dedup_within_window_then_count_after(log, forwarder, sent, clock):
    for i in range(5):
        log.error("Indexer %d timed out", i)  # digits normalized -> same fingerprint
    await _drain(forwarder)
    assert len(sent) == 1

    clock.now += 601
    log.error("Indexer %d timed out", 9)
    await _drain(forwarder)
    assert len(sent) == 2
    assert "×5" in sent[1][0]


async def test_distinct_errors_both_sent(log, forwarder, sent):
    log.error("first problem")
    log.error("second different problem")
    await _drain(forwarder)
    assert len(sent) == 2


def test_truncation_keeps_head_and_tail():
    text = "H" * 2000 + "M" * 5000 + "TAILEND"
    out = truncate_body(text)
    assert len(out) <= MAX_BODY_CHARS
    assert out.startswith("H") and out.endswith("TAILEND")
    assert "truncated" in out


async def test_notifier_loggers_are_excluded(forwarder, sent):
    lg = logging.getLogger("app.services.telegram")
    lg.propagate = False
    lg.addHandler(forwarder)
    try:
        lg.error("Failed to send Telegram notification")
    finally:
        lg.removeHandler(forwarder)
    assert not forwarder._tasks and sent == []


async def test_sender_exception_does_not_propagate(log):
    async def bad_sender(title, body):
        raise RuntimeError("down")

    handler = ErrorForwarderHandler(sender=bad_sender)
    log.addHandler(handler)
    log.error("x")
    await _drain(handler)  # must not raise


async def test_dispatch_alert_respects_toggle(db_session, monkeypatch):
    sent_msgs = []

    async def fake_tg(msg, token, chat):
        sent_msgs.append(msg)
        return True

    monkeypatch.setattr("app.services.discord.telegram.send_notification", fake_tg)
    db_session.add(SystemSettings(id=1, notify_on_errors=False))
    db_session.add(
        NotificationChannel(type="telegram", name="t", bot_token="tok", chat_id="1")
    )
    await db_session.commit()

    res = await dispatch_alert_message(db_session, "notify_on_errors", "T", "<b>")
    assert res["dispatched"] is False and sent_msgs == []

    st = await db_session.get(SystemSettings, 1)
    st.notify_on_errors = True
    await db_session.commit()
    res = await dispatch_alert_message(db_session, "notify_on_errors", "T", "<b>")
    assert res["telegram_sent"] == 1
    assert "&lt;b&gt;" in sent_msgs[0]


async def test_notification_settings_endpoint_persists_notify_on_errors(
    async_client, db_session
):
    db_session.add(SystemSettings(id=1, notify_on_errors=True))
    await db_session.commit()

    resp = await async_client.post(
        "/api/notifications/settings", data={"notify_on_errors": "false"}
    )
    assert resp.status_code == 200
    await db_session.refresh(await db_session.get(SystemSettings, 1))
    assert (await db_session.get(SystemSettings, 1)).notify_on_errors is False

    resp = await async_client.post("/api/notifications/settings", data={})
    st = await db_session.get(SystemSettings, 1)
    await db_session.refresh(st)
    assert st.notify_on_errors is False  # absent key keeps current value
