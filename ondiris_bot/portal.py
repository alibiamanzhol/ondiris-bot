import asyncio
import logging
import time

import httpx

from .snapshot import OrgState, row_identity, state_from_rows

log = logging.getLogger(__name__)

SITE_URL = "https://e-ondiris.gov.kz"
PAGE_LIMIT = 100
MAX_PAGES = 100
MAX_PASSES = 25
STALE_PASSES = 8
PASS_LIMITS = (100, 50)


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

    async def _get_page(self, bin_: str, page: int, limit: int = PAGE_LIMIT) -> dict:
        params = {"page": page, "limit": limit, "bin_iin": bin_}
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

    async def _pass(self, bin_: str, first: dict | None = None, limit: int = PAGE_LIMIT) -> tuple[list[dict], int]:
        """Один проход по всем страницам. Возвращает строки и общее число записей по данным портала."""
        rows: list[dict] = []
        page = 1
        while True:
            data = first if page == 1 and first is not None else await self._get_page(bin_, page, limit)
            rows.extend(data["data"])
            meta = data.get("meta") or {}
            if not meta.get("hasNextPage"):
                total = meta.get("total")
                return rows, total if isinstance(total, int) else len(rows)
            page += 1
            if page > MAX_PAGES:
                raise PortalError("слишком много страниц")

    async def _fetch_rows(self, bin_: str) -> tuple[list[dict], int]:
        """Все строки реестра по БИН.

        Портал отдаёт максимум 100 строк за запрос и не держит порядок строк между страницами:
        при каждом проходе часть строк повторяется, а часть не попадает ни на одну страницу.
        Поэтому для больших компаний делаем несколько проходов и объединяем уникальные строки,
        пока не наберём столько, сколько портал называет в meta.total.
        """
        first = await self._get_page(bin_, 1)
        meta = first.get("meta") or {}
        if not meta.get("hasNextPage"):
            rows = first["data"]
            total = meta.get("total")
            if isinstance(total, int) and total != len(rows):
                raise PortalError(f"получено {len(rows)} записей из {total}")
            return self._own(bin_, rows), len(rows)

        union: dict[str, dict] = {}
        total, stale = 0, 0
        for attempt in range(MAX_PASSES):
            # Чередуем размер страницы: границы страниц сдвигаются, и «редкие» строки попадаются быстрее.
            limit = PASS_LIMITS[attempt % len(PASS_LIMITS)]
            rows, total = await self._pass(bin_, first if attempt == 0 else None, limit)
            before = len(union)
            for r in rows:
                union.setdefault(row_identity(r), r)
            if len(union) >= total:
                break
            stale = stale + 1 if len(union) == before else 0
            if stale >= STALE_PASSES:
                break  # много проходов подряд без новых строк — остальное, видимо, полные дубли на стороне портала
        else:
            raise PortalError(f"собрано {len(union)} записей из {total} за {MAX_PASSES} проходов")
        log.info("[PORTAL] %s: %s записей собрано за %s прох.", bin_, len(union), attempt + 1)
        return self._own(bin_, list(union.values())), total

    @staticmethod
    def _own(bin_: str, rows: list[dict]) -> list[dict]:
        # Как на сайте: все строки по БИН, включая неактивные (признак активности хранится в снимке).
        return [r for r in rows if str(r.get("bin_iin") or "").strip() == bin_]

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
            rows, total = await self._fetch_rows(bin_)
            state = state_from_rows(bin_, rows, total)
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
