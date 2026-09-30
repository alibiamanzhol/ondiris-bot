import asyncio

import httpx
import pytest

from conftest import run
from ondiris_bot.portal import PortalClient, PortalError

BIN = "181240006529"
URL = "https://e-ondiris.gov.kz/awp-api/registry-front"


def row(i, bin_=BIN, active=True):
    return {
        "product_code": f"{i:013d}", "registration_number": "20049447", "bin_iin": bin_,
        "company_name": "Товарищество с ограниченной ответственностью \"Торг-Партнер\"",
        "product_name": f"Товар {i}", "production_capacity": "100.00 шт/год", "dvc_percent": "50.00",
        "registry_inclusion_date": "2026-09-29", "is_active": active,
    }


def paged(rows, limit=100):
    def handler(request: httpx.Request):
        assert request.url.params["bin_iin"] == BIN
        page = int(request.url.params["page"])
        chunk = rows[(page - 1) * limit: page * limit]
        pages = max(1, (len(rows) + limit - 1) // limit)
        return httpx.Response(200, json={
            "success": True, "data": chunk,
            "meta": {"total": len(rows), "page": page, "limit": limit, "totalPages": pages,
                     "hasNextPage": page < pages, "hasPrevPage": page > 1},
        })
    return handler


def client(handler):
    return PortalClient(URL, transport=httpx.MockTransport(handler), retry_delays=(0, 0))


def test_all_pages_collected_and_normalized():
    rows = [row(i) for i in range(250)] + [row(999, active=False)]
    state = run(client(paged(rows)).get_state(BIN))
    assert state.found and len(state.products) == 250
    assert state.company.endswith("\"Торг-Партнер\"")
    assert "Товар 999" not in state.products.values()


def test_not_found():
    state = run(client(paged([])).get_state(BIN))
    assert not state.found and state.products == {}


def test_other_bins_filtered_out():
    rows = [row(1), {**row(2), "bin_iin": "971240001315"}]
    state = run(client(paged(rows)).get_state(BIN))
    assert list(state.products.values()) == ["Товар 1"]


@pytest.mark.parametrize("response", [
    httpx.Response(500, text="Internal error"),
    httpx.Response(503, text="<html>Cloudflare</html>"),
    httpx.Response(200, text="<html>Just a moment...</html>"),
    httpx.Response(200, json={"success": False, "message": "error"}),
    httpx.Response(200, json={"foo": "bar"}),
])
def test_errors_raise_not_empty(response):
    with pytest.raises(PortalError):
        run(client(lambda r: response).get_state(BIN))


def test_timeout_raises_portal_error():
    def handler(request):
        raise httpx.ReadTimeout("timeout", request=request)
    with pytest.raises(PortalError):
        run(client(handler).get_state(BIN))


def test_incomplete_pagination_is_error():
    def handler(request):
        return httpx.Response(200, json={"success": True, "data": [row(1)],
                                         "meta": {"total": 5, "hasNextPage": False}})
    with pytest.raises(PortalError):
        run(client(handler).get_state(BIN))


def test_retry_then_success():
    calls = {"n": 0}
    ok = paged([row(1)])

    def handler(request):
        calls["n"] += 1
        return httpx.Response(502) if calls["n"] == 1 else ok(request)
    state = run(client(handler).get_state(BIN))
    assert state.found and calls["n"] == 2


def test_concurrent_requests_deduplicated_and_cached():
    calls = {"n": 0}
    ok = paged([row(1)])

    async def handler(request):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return ok(request)

    async def scenario():
        c = client(handler)
        await asyncio.gather(*(c.get_state(BIN) for _ in range(5)))
        await c.get_state(BIN)
        results = await c.get_many([BIN, BIN])
        await c.close()
        return results

    results = run(scenario())
    assert calls["n"] == 1 and list(results) == [BIN]
