import os

import pytest

from conftest import run
from ondiris_bot.portal import PortalClient

pytestmark = pytest.mark.skipif(os.getenv("LIVE_PORTAL") != "1", reason="LIVE_PORTAL=1 для проверки на реальном портале")

URL = "https://e-ondiris.gov.kz/awp-api/registry-front"


def test_real_portal_stable_and_complete():
    async def fetch_three():
        out = []
        for _ in range(5):  # каждый раз новый клиент — без кэша
            c = PortalClient(URL, concurrency=2)
            try:
                out.append(await c.get_state("181240006529"))
            finally:
                await c.close()
        c = PortalClient(URL, concurrency=2)
        try:
            missing = await c.get_state("971240001315")
        finally:
            await c.close()
        return out, missing

    states, missing = run(fetch_three())
    s = states[0]
    print(f"\n::notice title=LIVE portal::{s.company}: на сайте {s.total}, собрано {[len(x.records) for x in states]}, "
          f"активных {s.active}, наименований {len(s.product_names())}; "
          f"одинаково все 5 раз: {all(x.records == s.records for x in states)}; "
          f"второй БИН найден={missing.found}")
    assert s.found and "Торг-Партнер" in s.company and s.total > 100
    assert all(x.records == s.records for x in states)  # нет ложных «изменений»
    assert len(s.records) == s.total
    assert 0 < s.active <= len(s.records) <= s.total
