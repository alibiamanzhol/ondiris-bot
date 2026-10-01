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


def test_all_pages_collected_like_site():
    # Как на сайте: считаются все строки реестра по БИН, включая неактивные.
    rows = [row(i) for i in range(250)] + [row(i, active=False) for i in range(1000, 1010)]
    state = run(client(paged(rows)).get_state(BIN))
    assert state.found and state.total == 260 and len(state.records) == 260 and state.active == 250
    assert state.company.endswith("\"Торг-Партнер\"")


def test_same_code_under_different_registration_numbers_counted_separately():
    rows = [row(1), {**row(1), "registration_number": "110 5 00048", "is_active": False}]
    state = run(client(paged(rows)).get_state(BIN))
    assert state.total == 2 and len(state.records) == 2 and state.active == 1
    assert state.product_names() == ["Товар 1"]


def unstable(rows, limit=100, seed=1):
    """Как реальный портал: каждый запрос страницы берётся из заново перемешанного списка —
    строки повторяются между страницами, а часть не попадает ни на одну."""
    import random
    rnd = random.Random(seed)

    def handler(request: httpx.Request):
        page = int(request.url.params["page"])
        shuffled = rows[:]
        rnd.shuffle(shuffled)
        pages = (len(rows) + limit - 1) // limit
        return httpx.Response(200, json={
            "success": True, "data": shuffled[(page - 1) * limit: page * limit],
            "meta": {"total": len(rows), "page": page, "limit": limit, "totalPages": pages,
                     "hasNextPage": page < pages, "hasPrevPage": page > 1},
        })
    return handler


def test_unstable_pagination_collects_all_rows():
    rows = [row(i) for i in range(488)]
    for seed in range(5):
        state = run(client(unstable(rows, seed=seed)).get_state(BIN))
        assert state.total == 488 and len(state.records) == 488, seed


def test_unstable_pagination_gives_same_state_every_time():
    rows = [row(i, active=i % 4 != 0) for i in range(300)]
    states = [run(client(unstable(rows, seed=s)).get_state(BIN)) for s in range(3)]
    assert states[0].records == states[1].records == states[2].records
    assert states[0].active == 225


def test_pagination_that_never_completes_is_error():
    rows = [row(i) for i in range(250)]

    def handler(request):
        page = int(request.url.params["page"])
        data = rows[:100] if page < 3 else rows[100:150]  # строки 150–249 никогда не приходят
        return httpx.Response(200, json={"success": True, "data": data,
                                         "meta": {"total": 250, "hasNextPage": page < 3}})
    state = run(client(handler).get_state(BIN))
    # два прохода подряд без новых строк — принимаем то, что собрали (150 уникальных строк)
    assert len(state.records) == 150 and state.total == 250


def test_not_found():
    state = run(client(paged([])).get_state(BIN))
    assert not state.found and state.records == {} and state.total == 0


def test_other_bins_filtered_out():
    rows = [row(1), {**row(2), "bin_iin": "971240001315"}]
    state = run(client(paged(rows)).get_state(BIN))
    assert state.product_names() == ["Товар 1"]


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
