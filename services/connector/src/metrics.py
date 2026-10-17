"""What the connector sees at its boundary with Hyperliquid.

grid-static can only time a whole round or a whole order, through the
connector. These are timed where the connector itself goes over the network,
per request type, so a slow round can be pinned on one kind of call -- and on
Hyperliquid rather than on this service, or the other way round.
"""

import time

import requests
from prometheus_client import Counter, Histogram

_BUCKETS = [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10]

# "api" (info or exchange), not "endpoint": the Prometheus Operator puts an
# `endpoint` label on every series it scrapes, the name of the Service port,
# and that one wins -- ours would arrive as exported_endpoint.

hl_request_duration = Histogram(
    "connector_hl_request_duration_seconds",
    "One request to Hyperliquid, retries included", ["api", "request"], buckets=_BUCKETS
)
hl_requests = Counter(
    "connector_hl_requests",
    "Requests to Hyperliquid by outcome: ok, rejected (answered, but the exchange "
    "refused), rate_limited, http_4xx, http_5xx, timeout, connection or error",
    ["api", "request", "outcome"]
)
http_request_duration = Histogram(
    "connector_http_request_duration_seconds",
    "Requests the connector answers, by route", ["method", "route", "status"], buckets=_BUCKETS
)


def request_type(url_path: str, payload: dict) -> str:
    """"userFills", "frontendOpenOrders", "order", "cancel", ... -- a small, fixed
    set, so it is safe as a label."""
    if url_path.strip("/") == "exchange":
        return str((payload.get("action") or {}).get("type", "unknown"))
    return str(payload.get("type", "unknown"))


def _outcome_of(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    if status is None and isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
    if status == 429:
        return "rate_limited"
    if isinstance(status, int):
        return f"http_{status // 100}xx"
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.ConnectionError):
        return "connection"
    return "error"


def _rejected(result) -> bool:
    """An /exchange answer can be HTTP 200 and still a refusal: status "err", or
    an error per order inside an "ok"."""
    if not isinstance(result, dict):
        return False
    if result.get("status") != "ok":
        return True
    response = result.get("response")
    data = response.get("data") if isinstance(response, dict) else None
    statuses = data.get("statuses", []) if isinstance(data, dict) else []
    return any(isinstance(s, dict) and "error" in s for s in statuses)


def timed(api: str, request: str, call):
    t0 = time.perf_counter()
    try:
        result = call()
    except Exception as e:
        hl_requests.labels(api, request, _outcome_of(e)).inc()
        raise
    finally:
        hl_request_duration.labels(api, request).observe(time.perf_counter() - t0)
    outcome = "rejected" if api == "exchange" and _rejected(result) else "ok"
    hl_requests.labels(api, request, outcome).inc()
    return result


def instrument(api) -> None:
    """Time every request an SDK Info or Exchange object makes. They all go
    through its post(); wrapping that one method covers every call."""
    original = api.post

    def post(url_path, payload=None):
        payload = payload or {}
        return timed(url_path.strip("/"), request_type(url_path, payload),
                     lambda: original(url_path, payload))

    api.post = post
