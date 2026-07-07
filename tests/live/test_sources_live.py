"""Live source smoke tests. Run ON THE DEPLOYMENT MACHINE with:
    POLYWEATHER_LIVE=1 pytest tests/live -v
They hit the real endpoints and verify our parsing against real payloads.
(This repo's CI sandbox has no egress to these hosts — use `polyweather
doctor` for an equivalent, friendlier check.)
"""
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("POLYWEATHER_LIVE") != "1",
    reason="live tests only with POLYWEATHER_LIVE=1")


@pytest.fixture()
def cfg():
    from polyweather.config import get_config
    return get_config()


async def test_metar_live(cfg):
    from polyweather.ingest.metar import MetarIngestor
    ing = MetarIngestor(cfg)
    changed = await ing.poll()
    # at least one of the three cities must yield a report at any hour
    assert isinstance(changed, list)
    from polyweather.db import Observation, get_session
    with get_session() as s:
        assert s.query(Observation).filter_by(source="metar").count() > 0


async def test_amedas_live(cfg):
    from polyweather.ingest.amedas import AmedasIngestor
    ing = AmedasIngestor(cfg)
    ids = await ing.resolve_ids()
    assert "羽田" in ids
    changed = await ing.poll()
    assert "tokyo" in changed or changed == []  # dedupe on rerun is fine


async def test_openmeteo_live(cfg):
    from polyweather.ingest.openmeteo import OpenMeteoIngestor
    ing = OpenMeteoIngestor(cfg)
    ok = await ing.poll_city("tokyo")
    assert ok
    from polyweather.db import ModelForecast, get_session
    with get_session() as s:
        row = s.query(ModelForecast).filter_by(city="tokyo").first()
    assert row is not None and row.today_max is not None


async def test_polymarket_live(cfg):
    from polyweather.ingest.polymarket import PolymarketIngestor
    ing = PolymarketIngestor(cfg)
    ok = await ing.poll_city("london")
    # market may genuinely not exist some days; only assert no crash
    assert ok in (True, False)


async def test_knmi_live(cfg):
    if not os.environ.get("KNMI_API_KEY"):
        pytest.skip("KNMI_API_KEY not set")
    from polyweather.ingest.knmi import KnmiIngestor
    ing = KnmiIngestor(cfg)
    changed = await ing.poll()
    assert isinstance(changed, list)
