import pytest
from unittest.mock import AsyncMock, MagicMock

import src.main as m


@pytest.mark.asyncio
async def test_wait_for_stopt_zodra_de_probe_lukt(monkeypatch):
    slapen = []
    monkeypatch.setattr(m.asyncio, "sleep", AsyncMock(side_effect=lambda d: slapen.append(d)))

    pogingen = {"n": 0}

    async def probe():
        pogingen["n"] += 1
        if pogingen["n"] < 3:
            raise ConnectionError("nog niet")

    await m._wait_for("dal", probe)

    assert pogingen["n"] == 3
    assert slapen == [2.0, 2.0]


@pytest.mark.asyncio
async def test_wait_for_geeft_luid_op_na_alle_pogingen(monkeypatch):
    monkeypatch.setattr(m.asyncio, "sleep", AsyncMock())

    async def probe():
        raise ConnectionError("blijft weg")

    with pytest.raises(RuntimeError, match="dal niet bereikbaar na 60s"):
        await m._wait_for("dal", probe, attempts=30, delay=2.0)


@pytest.mark.asyncio
async def test_lifespan_wacht_op_connector_en_op_de_dal(monkeypatch, tmp_path):
    """Regressietest. De dal is bij een verse installatie later klaar dan grid-static:
    zijn initContainer brengt eerst het schema aan. Wachtte de opstart alleen op de
    connector, dan viel hij om op de eerste get_active_configs en herstartte de pod."""
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

    gewacht = []

    async def nep_wait(naam, probe, **kwargs):
        gewacht.append(naam)

    monkeypatch.setattr(m, "_wait_for", nep_wait)

    grid = AsyncMock()
    grid.dal.get_active_configs.return_value = []
    monkeypatch.setattr(m, "StaticGrid", MagicMock(return_value=grid))

    try:
        async with m.lifespan(None):
            pass
    finally:
        m._grids.clear()

    assert gewacht == ["connector", "dal"]
    grid.initialize.assert_awaited_once()
