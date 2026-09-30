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
    print(f"\nLIVE: {found.company}: {len(found.products)} записей, {len(found.product_names())} наименований")
    assert found.found and "Торг-Партнер" in found.company
    assert len(found.products) > 100  # проверка, что собраны все страницы, а не только первая
    assert missing.bin == "971240001315"
