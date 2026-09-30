import os

import pytest

from conftest import run
from ondiris_bot.portal import PortalClient

pytestmark = pytest.mark.skipif(os.getenv("LIVE_PORTAL") != "1", reason="LIVE_PORTAL=1 для проверки на реальном портале")

URL = "https://e-ondiris.gov.kz/awp-api/registry-front"


def test_real_portal_known_company_all_pages():
    async def scenario():
        c = PortalClient(URL, concurrency=2)
        try:
            found = await c.get_state("181240006529")
            missing = await c.get_state("971240001315")
            return found, missing
        finally:
            await c.close()

    found, missing = run(scenario())
    stats = (f"{found.company}: строк {found.total}, уникальных записей {len(found.records)}, "
             f"активных {found.active}, наименований {len(found.product_names())}; "
             f"второй БИН найден={missing.found} строк={missing.total}")
    print(f"\n::notice title=LIVE portal::{stats}")
    assert found.found and "Торг-Партнер" in found.company
    assert found.total > 100  # собраны все страницы, а не только первая
    assert 0 < len(found.records) <= found.total
    assert 0 < found.active <= len(found.records)
