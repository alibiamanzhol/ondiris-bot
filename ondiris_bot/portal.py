import asyncio
import logging
import time

import httpx

from .snapshot import OrgState, state_from_rows

log = logging.getLogger(__name__)

SITE_URL = "https://e-ondiris.gov.kz"
PAGE_LIMIT = 100
MAX_PAGES = 100


class PortalError(Exception):
    """Данные получить не удалось. Это НЕ означает, что записей нет."""


class PortalClient:
    def __init__(self, api_url: str, concurrency: int = 3, cache_ttl: float = 300,
                 transport: httpx.AsyncBaseTransport | None = None, retry_delays=(2, 5, 10)):
        self.api_url = api_url
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Referer": SITE_URL + "/"},
            transport=transport,
        )
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._cache: dict[str, tuple[float, OrgState]] = {}
        self._inflight: dict[str, asyncio.Future] = {}
        self._cache_ttl = cache_ttl
        self._retry_delays = retry_delays
        self.requests_made = 0

    async def close(self) -> None:
        await self._client.aclose()

    async def _get_page(self, bin_: str, page: int) -> dict:
        params = {"page": page, "limit": PAGE_LIMIT, "bin_iin": bin_}
        last: Exception | None = None
        for attempt, delay in enumerate((0, *self._retry_delays)):
            if delay:
                await asyncio.sleep(delay)
            try:
                async with self._sem:
                    self.requests_made += 1
                    resp = await self._client.get(self.api_url, params=params)
                if resp.status_code != 200:
                    raise PortalError(f"HTTP {resp.status_code}")
                try:
                    data = resp.json()
                except ValueError as e:
                    raise PortalError("ответ не JSON (возможна заглушка/Cloudflare)") from e
                if not isinstance(data, dict) or data.get("success") is False or not isinstance(data.get("data"), list):
                    raise PortalError(f"неожиданный формат ответа: {str(data)[:120]}")
                return data
            except (httpx.HTTPError, PortalError) as e:
                last = e
                log.warning("[PORTAL] %s page %s attempt %s failed: %s", bin_, page, attempt + 1, e)
        raise PortalError(str(last))

    async def _fetch_rows(self, bin_: str) -> list[dict]:
        rows: list[dict] = []
        page = 1
        while True:
            data = await self._get_page(bin_, page)
            rows.extend(data["data"])
            meta = data.get("meta") or {}
            if not meta.get("hasNextPage"):
                total = meta.get("total")
                if isinstance(total, int) and total != len(rows):
                    raise PortalError(f"получено {len(rows)} записей из {total}")
                break
            page += 1
            if page > MAX_PAGES:
                raise PortalError("слишком много страниц")
        return [
            r for r in rows
            if str(r.get("bin_iin") or "").strip() == bin_ and r.get("is_active") is not False
        ]

    async def get_state(self, bin_: str, max_age: float | None = None) -> OrgState:
        max_age = self._cache_ttl if max_age is None else max_age
        cached = self._cache.get(bin_)
        if cached and time.monotonic() - cached[0] <= max_age:
            return cached[1]
        if bin_ in self._inflight:
            return await asyncio.shield(self._inflight[bin_])
        fut = asyncio.get_running_loop().create_future()
        self._inflight[bin_] = fut
        try:
            state = state_from_rows(bin_, await self._fetch_rows(bin_))
            self._cache[bin_] = (time.monotonic(), state)
            fut.set_result(state)
            return state
        except Exception as e:
            fut.set_exception(e)
            fut.exception()
            raise
        finally:
            del self._inflight[bin_]

    async def get_many(self, bins, max_age: float | None = None) -> dict[str, OrgState | PortalError]:
        async def one(b):
            try:
                return b, await self.get_state(b, max_age)
            except PortalError as e:
                return b, e
            except Exception as e:
                log.exception("[PORTAL] unexpected error for %s", b)
                return b, PortalError(str(e))

        return dict(await asyncio.gather(*(one(b) for b in dict.fromkeys(bins))))
