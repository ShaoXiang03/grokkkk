"""Webhook signing, the outbox, and fail-closed reviews."""

from pathlib import Path

import pytest

from desk.data.store import Store
from desk.delivery.eligibility import ReviewError, expire_pending, submit_review
from desk.delivery.http import apply_http_review
from desk.delivery.outbox import backoff_ms, drain_outbox, enqueue_packet
from desk.delivery.signing import sign_payload, verify_signature, webhook_url
from desk.journal.decision import DecisionJournal
from desk.scanner.signals import packet_json
from desk_helpers import account_view, make_packet, make_settings, market_context

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_700_000_000_000


class FakeSession:
    def __init__(self, codes):
        self.codes = list(codes)
        self.calls = []

    def post(self, url, data, headers, timeout):
        self.calls.append({"url": url, "data": data, "headers": dict(headers), "timeout": timeout})
        code = self.codes.pop(0) if self.codes else 200

        class Response:
            status_code = code

        return Response()


def test_signature_roundtrip_rejects_tamper_and_stale_timestamp():
    body = packet_json({"signal_id": "abc", "status": "proposed"})
    signature = sign_payload(body, "secret", str(NOW))
    assert verify_signature(body, "secret", str(NOW), signature, now_ms=NOW, max_age_ms=60_000)
    assert not verify_signature(body + " ", "secret", str(NOW), signature, now_ms=NOW, max_age_ms=60_000)
    assert not verify_signature(body, "other", str(NOW), signature, now_ms=NOW, max_age_ms=60_000)
    assert not verify_signature(body, "secret", str(NOW + 1), signature, now_ms=NOW, max_age_ms=60_000)
    assert not verify_signature(body, "secret", str(NOW), signature, now_ms=NOW + 120_000, max_age_ms=60_000)
    assert not verify_signature(body, "", str(NOW), signature, now_ms=NOW, max_age_ms=60_000)
    assert not verify_signature(body, "secret", str(NOW), "nope", now_ms=NOW, max_age_ms=60_000)


def test_webhook_url_comes_from_the_environment(monkeypatch):
    settings = make_settings(Path("/tmp"), webhook={"url": "https://from-file.example/hook"})
    assert webhook_url(settings) == "https://from-file.example/hook"
    monkeypatch.setenv("WEBHOOK_URL", "https://from-env.example/hook")
    assert webhook_url(settings) == "https://from-env.example/hook"


def test_missing_secret_keeps_the_body_and_does_not_post(tmp_path, monkeypatch):
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    settings = make_settings(
        tmp_path,
        webhook={"enabled": True, "url": "https://market-bot.example/hook", "max_retries": 3},
    )
    store = Store(tmp_path / "desk.sqlite")
    packet = make_packet(now_ms=NOW)
    queued = enqueue_packet(store, packet, settings, NOW)
    assert queued["queued"] is True
    session = FakeSession([200])
    outcome = drain_outbox(store, settings, NOW, session=session)
    assert outcome[0]["reason"] == "missing_secret"
    assert session.calls == []
    row = store.outbox_by_key(packet["signal_id"])
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["body"] == packet_json(packet)
    store.close()


def test_retries_keep_the_idempotency_key_and_then_deliver(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "secret")
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    settings = make_settings(
        tmp_path,
        webhook={
            "enabled": True,
            "url": "https://market-bot.example/hook",
            "max_retries": 5,
            "retry_backoff_seconds": 1,
            "retry_backoff_max_seconds": 60,
        },
    )
    assert backoff_ms(1, settings) == 1000
    assert backoff_ms(2, settings) == 2000
    assert backoff_ms(3, settings) == 4000
    store = Store(tmp_path / "desk.sqlite")
    packet = make_packet(now_ms=NOW)
    enqueue_packet(store, packet, settings, NOW)
    again = enqueue_packet(store, packet, settings, NOW + 1)
    assert again["reason"] == "duplicate"
    session = FakeSession([500, 500, 200])
    first = drain_outbox(store, settings, NOW, session=session)[0]
    assert first["status"] == "pending"
    assert first["attempts"] == 1
    too_soon = drain_outbox(store, settings, NOW + 500, session=session)
    assert too_soon == []
    second = drain_outbox(store, settings, NOW + 1000, session=session)[0]
    assert second["status"] == "pending"
    assert second["attempts"] == 2
    third = drain_outbox(store, settings, NOW + 1000 + 2000, session=session)[0]
    assert third["sent"] is True
    assert third["status"] == "delivered"
    keys = [call["headers"]["Idempotency-Key"] for call in session.calls]
    bodies = [call["data"] for call in session.calls]
    assert keys == [packet["signal_id"], packet["signal_id"], packet["signal_id"]]
    assert bodies[0] == bodies[1] == bodies[2]
    for call in session.calls:
        assert verify_signature(
            call["data"],
            "secret",
            call["headers"]["X-Desk-Timestamp"],
            call["headers"]["X-Desk-Signature"],
            now_ms=int(call["headers"]["X-Desk-Timestamp"]),
            max_age_ms=1,
        )
    assert store.outbox_by_key(packet["signal_id"])["body"] == bodies[0]
    store.close()


def test_client_error_dead_letters_without_dropping_the_body(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "secret")
    settings = make_settings(
        tmp_path,
        webhook={"enabled": True, "url": "https://market-bot.example/hook", "max_retries": 5},
    )
    store = Store(tmp_path / "desk.sqlite")
    packet = make_packet(now_ms=NOW)
    enqueue_packet(store, packet, settings, NOW)
    session = FakeSession([400])
    outcome = drain_outbox(store, settings, NOW, session=session)[0]
    assert outcome["status"] == "dead"
    row = store.outbox_by_key(packet["signal_id"])
    assert row["status"] == "dead"
    assert row["body"] == packet_json(packet)
    assert drain_outbox(store, settings, NOW + 10_000, session=FakeSession([200])) == []
    store.close()


def test_duplicate_delivery_is_treated_as_success(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "secret")
    settings = make_settings(
        tmp_path,
        webhook={"enabled": True, "url": "https://market-bot.example/hook", "max_retries": 3},
    )
    store = Store(tmp_path / "desk.sqlite")
    packet = make_packet(now_ms=NOW)
    enqueue_packet(store, packet, settings, NOW)
    outcome = drain_outbox(store, settings, NOW, session=FakeSession([409]))[0]
    assert outcome["sent"] is True
    assert store.outbox_by_key(packet["signal_id"])["status"] == "delivered"
    store.close()


def _open(store, settings, packet, now_ms, reasoning="take the closed bar", context=None, account=None):
    store.save_signal(packet)
    DecisionJournal(store, settings).record_signal(packet, packet["ts_ms"])
    return submit_review(
        store,
        settings,
        packet["signal_id"],
        market_context(now_ms) if context is None else context,
        {"decision": "open", "reasoning": reasoning, "ts": None},
        now_ms=now_ms,
        account=account if account is not None else account_view(),
        open_positions=[],
    )


def test_open_is_refused_when_a_hard_gate_failed(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(tmp_path / "desk.sqlite")
    packet = make_packet(
        now_ms=NOW,
        gates={"news": {"result": "fail", "reason": "News Watcher veto", "ts": "t"}},
    )
    result = _open(store, settings, packet, NOW, account=account_view(news="ALLOW"))
    assert result["requested_decision"] == "open"
    assert result["effective_decision"] == "skip"
    assert result["override_refused"] is True
    assert "news" in result["blocking"]
    kinds = [row["kind"] for row in store.list_paper_events("violation")]
    assert "hard_gate_override" in kinds
    stored = store.get_signal(packet["signal_id"])
    assert stored["hard_gates"]["news"]["result"] == "fail"
    store.close()


def test_onchain_context_cannot_override_the_scanner_gate(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(tmp_path / "desk.sqlite")
    packet = make_packet(
        now_ms=NOW,
        gates={
            "on_chain": {
                "result": "fail",
                "reason": "missing",
                "ts": "t",
                "checks": {"not_honeypot": "MISSING"},
            }
        },
    )
    context = market_context(NOW, onchain_checks={"not_honeypot": "PASS"})
    result = _open(store, settings, packet, NOW, context=context)
    assert result["effective_decision"] == "skip"
    assert "on_chain" in result["blocking"]
    assert result["market_context"]["onchain_checks"]["not_honeypot"] == "PASS"
    assert store.get_signal(packet["signal_id"])["hard_gates"]["on_chain"]["result"] == "fail"
    store.close()


def test_custom_gate_blocks_a_setup_the_plumbing_does_not_know(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(tmp_path / "desk.sqlite")
    packet = make_packet(
        now_ms=NOW,
        setup="setup_z",
        gates={"custom_gate": {"result": "fail", "reason": "new setup veto", "ts": "t"}},
    )
    result = _open(store, settings, packet, NOW)
    assert result["effective_decision"] == "skip"
    assert "custom_gate" in result["blocking"]
    store.close()


def test_open_without_reasoning_is_skip(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(tmp_path / "desk.sqlite")
    packet = make_packet(now_ms=NOW)
    result = _open(store, settings, packet, NOW, reasoning="   ")
    assert result["effective_decision"] == "skip"
    assert result["reason"] == "missing_reasoning"
    assert store.list_paper_events("violation")[0]["kind"] == "missing_reasoning"
    store.close()


def test_rejected_packet_cannot_be_opened(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(tmp_path / "desk.sqlite")
    packet = make_packet(now_ms=NOW)
    packet["status"] = "rejected"
    packet["decision_path"] = "hard_gate_reject"
    result = _open(store, settings, packet, NOW)
    assert result["effective_decision"] == "skip"
    assert "not_eligible" in result["blocking"]
    store.close()


def test_timeout_is_skip_even_if_config_says_open(tmp_path):
    settings = make_settings(tmp_path, decision={"timeout_seconds": 60, "timeout_action": "open"})
    store = Store(tmp_path / "desk.sqlite")
    packet = make_packet(now_ms=NOW - 120_000)
    store.save_signal(packet)
    expired = expire_pending(store, settings, NOW)
    assert len(expired) == 1
    assert expired[0]["effective_decision"] == "skip"
    assert expired[0]["reason"] == "decision_timeout"
    review = store.latest_review(packet["signal_id"])
    assert review["effective_decision"] == "skip"
    store.close()


def test_second_review_is_refused(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(tmp_path / "desk.sqlite")
    packet = make_packet(now_ms=NOW)
    _open(store, settings, packet, NOW)
    with pytest.raises(ReviewError) as caught:
        submit_review(
            store,
            settings,
            packet["signal_id"],
            market_context(NOW),
            {"decision": "skip", "reasoning": "changed my mind", "ts": None},
            now_ms=NOW + 1,
            account=account_view(),
        )
    assert caught.value.status == 409
    assert store.latest_review(packet["signal_id"])["effective_decision"] == "open"
    store.close()


def test_http_review_requires_a_valid_signature(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEW_SECRET", "review-secret")
    settings = make_settings(tmp_path)
    store = Store(tmp_path / "desk.sqlite")
    packet = make_packet(now_ms=NOW)
    store.save_signal(packet)
    DecisionJournal(store, settings).record_signal(packet, NOW)
    body = (
        '{"market_context":{"summary":"ok","strengtheners":[],"weakeners":[],'
        '"onchain_checks":{},"sources":["bot"],"ts":null},'
        '"master_decision":{"decision":"open","reasoning":"closed bar held","ts":null}}'
    ).encode()
    missing = apply_http_review(store, settings, packet["signal_id"], body, {}, now_ms=NOW, account=account_view())
    assert missing[0] == 401
    assert store.latest_review(packet["signal_id"]) is None
    signature = sign_payload(body.decode(), "review-secret", str(NOW))
    status, payload = apply_http_review(
        store,
        settings,
        packet["signal_id"],
        body,
        {"X-Desk-Timestamp": str(NOW), "X-Desk-Signature": signature},
        now_ms=NOW,
        account=account_view(),
    )
    assert status == 200
    assert payload["effective_decision"] == "open"
    assert payload["override_refused"] is False
    again = apply_http_review(
        store,
        settings,
        packet["signal_id"],
        body,
        {"X-Desk-Timestamp": str(NOW), "X-Desk-Signature": signature},
        now_ms=NOW,
        account=account_view(),
    )
    assert again[0] == 409
    store.close()


def test_http_review_fails_closed_without_a_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("REVIEW_SECRET", raising=False)
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    settings = make_settings(tmp_path)
    store = Store(tmp_path / "desk.sqlite")
    packet = make_packet(now_ms=NOW)
    store.save_signal(packet)
    status, payload = apply_http_review(
        store,
        settings,
        packet["signal_id"],
        b"{}",
        {},
        now_ms=NOW,
    )
    assert status == 503
    assert payload["error"] == "review_secret_missing"
    assert store.latest_review(packet["signal_id"]) is None
    store.close()


def test_plumbing_does_not_name_the_current_setups():
    for relative in (
        "src/desk/delivery/eligibility.py",
        "src/desk/delivery/outbox.py",
        "src/desk/delivery/signing.py",
        "src/desk/journal/decision.py",
        "src/desk/paper/engine.py",
        "src/desk/paper/scorecard.py",
    ):
        text = (ROOT / relative).read_text()
        assert "squeeze_breakout" not in text
        assert "meme_momentum" not in text
        assert "/fapi/v1/order" not in text
        assert "apiKey" not in text
        assert "api_key" not in text
