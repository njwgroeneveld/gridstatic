import pytest
from unittest.mock import AsyncMock, MagicMock

import src.main as m


@pytest.mark.asyncio
async def test_wait_for_returns_as_soon_as_the_probe_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(m.asyncio, "sleep", AsyncMock(side_effect=lambda d: sleeps.append(d)))

    attempts = {"n": 0}

    async def probe():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("not yet")

    await m._wait_for("dal", probe)

    assert attempts["n"] == 3
    assert sleeps == [2.0, 2.0]


@pytest.mark.asyncio
async def test_wait_for_gives_up_loudly_after_all_attempts(monkeypatch):
    monkeypatch.setattr(m.asyncio, "sleep", AsyncMock())

    async def probe():
        raise ConnectionError("still down")

    with pytest.raises(RuntimeError, match="dal not reachable after 60s"):
        await m._wait_for("dal", probe, attempts=30, delay=2.0)


@pytest.mark.asyncio
async def test_lifespan_waits_for_the_connector_and_the_dal(monkeypatch, tmp_path):
    """Regression test. On a fresh install the dal is ready later than grid-static:
    its initContainer applies the schema first. When startup only waited for the
    connector, it failed on the first get_active_configs and the pod restarted."""
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        "connector_url: http://c:8080\n"
        "dal_url: http://d:8080\n"
        "alerter_url: http://a:8080\n"
        "coins:\n"
        "  BTC-10:\n"
        "    coin: BTC\n"
        "    active: true\n"
        "    shadow: true\n"
        "    allocation_pct: 100\n"
        "    upper: 68000\n"
        "    lower: 57000\n"
        "    num_lines: 10\n"
        "    leverage: 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SETTINGS_FILE", str(settings))
    m._grids.clear()

    waited_for = []

    async def fake_wait(name, probe, **kwargs):
        waited_for.append(name)

    monkeypatch.setattr(m, "_wait_for", fake_wait)

    grid = AsyncMock()
    grid.dal.get_active_configs.return_value = []
    monkeypatch.setattr(m, "StaticGrid", MagicMock(return_value=grid))

    try:
        async with m.lifespan(None):
            pass
    finally:
        m._grids.clear()

    assert waited_for == ["connector", "dal"]
    grid.initialize.assert_awaited_once()
