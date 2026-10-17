"""Timing and classifying requests to Hyperliquid. The metrics are global, so
every test measures the difference it makes."""

import pytest
import requests
from hyperliquid.utils.error import ClientError, ServerError
from prometheus_client import REGISTRY

from src import metrics


def _count(api, request, outcome):
    return REGISTRY.get_sample_value(
        "connector_hl_requests_total",
        {"api": api, "request": request, "outcome": outcome}) or 0.0


def _timed(api, request):
    return REGISTRY.get_sample_value(
        "connector_hl_request_duration_seconds_count",
        {"api": api, "request": request}) or 0.0


class FakeApi:
    def __init__(self, answer=None, raises=None):
        self.answer, self.raises, self.calls = answer, raises, []

    def post(self, url_path, payload=None):
        self.calls.append((url_path, payload))
        if self.raises:
            raise self.raises
        return self.answer


def test_the_request_type_comes_from_the_payload():
    assert metrics.request_type("/info", {"type": "userFills", "user": "0x"}) == "userFills"
    assert metrics.request_type("/exchange", {"action": {"type": "order"}}) == "order"
    assert metrics.request_type("/info", {}) == "unknown"


def test_an_instrumented_post_still_returns_the_answer_and_is_counted():
    api = FakeApi(answer=[{"oid": 1}])
    metrics.instrument(api)
    before, timed = _count("info", "frontendOpenOrders", "ok"), _timed("info", "frontendOpenOrders")
    assert api.post("/info", {"type": "frontendOpenOrders"}) == [{"oid": 1}]
    assert _count("info", "frontendOpenOrders", "ok") == before + 1
    assert _timed("info", "frontendOpenOrders") == timed + 1


@pytest.mark.parametrize("answer", [
    {"status": "err", "response": "Insufficient margin"},
    {"status": "ok", "response": {"type": "order", "data": {"statuses": [
        {"error": "Order must have minimum value of $10."}]}}},
])
def test_an_exchange_refusal_is_rejected_not_ok(answer):
    api = FakeApi(answer=answer)
    metrics.instrument(api)
    before = _count("exchange", "order", "rejected")
    api.post("/exchange", {"action": {"type": "order"}})
    assert _count("exchange", "order", "rejected") == before + 1


@pytest.mark.parametrize("exc, outcome", [
    (ClientError(429, None, "too many requests", {}), "rate_limited"),
    (ClientError(422, None, "bad", {}), "http_4xx"),
    (ServerError(502, "bad gateway"), "http_5xx"),
    (requests.Timeout(), "timeout"),
    (requests.ConnectionError(), "connection"),
    (ValueError("?"), "error"),
])
def test_a_failed_request_is_counted_by_outcome_and_still_raises(exc, outcome):
    api = FakeApi(raises=exc)
    metrics.instrument(api)
    before, timed = _count("info", "userFills", outcome), _timed("info", "userFills")
    with pytest.raises(type(exc)):
        api.post("/info", {"type": "userFills"})
    assert _count("info", "userFills", outcome) == before + 1
    assert _timed("info", "userFills") == timed + 1    # a failure takes time too


def test_the_info_client_times_its_own_requests(monkeypatch):
    from src.info_client import InfoClient
    client = InfoClient()
    monkeypatch.setattr(client, "_send", lambda payload: {"BTC": "81000"})
    before = _count("info", "allMids", "ok")
    assert client.get_all_mids() == {"BTC": 81000.0}
    assert _count("info", "allMids", "ok") == before + 1


def test_the_connector_serves_its_metrics():
    from fastapi.testclient import TestClient
    from src.main import app
    TestClient(app).get("/health")
    body = TestClient(app).get("/metrics/").text
    assert "connector_hl_requests_total" in body
    assert 'route="/health"' in body
