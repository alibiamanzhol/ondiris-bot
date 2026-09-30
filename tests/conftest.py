import asyncio
import sys
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ondiris_bot.bins import checksum_ok  # noqa: E402
from ondiris_bot.monitor import Monitor  # noqa: E402
from ondiris_bot.portal import PortalError  # noqa: E402
from ondiris_bot.service import BotService  # noqa: E402
from ondiris_bot.snapshot import OrgState  # noqa: E402
from ondiris_bot.storage import Storage  # noqa: E402

TZ = ZoneInfo("Asia/Almaty")


def gen_bins(n: int, start: int = 1) -> list[str]:
    out, i = [], start
    while len(out) < n:
        base = f"2{i % 10}{(i % 12) + 1:02d}40{i:05d}"[:11]
        for d in range(10):
            if checksum_ok(base + str(d)):
                out.append(base + str(d))
                break
        i += 1
    return out


def org(bin_: str, company: str = "", products: dict | None = None) -> OrgState:
    return OrgState(bin=bin_, found=True, company=company or f"ТОО \"Компания {bin_[-4:]}\"",
                    products=products if products is not None else {"code:1": "Товар 1"})


class FakePortal:
    def __init__(self):
        self.states: dict[str, OrgState] = {}
        self.fail: set[str] = set()
        self.calls: list[str] = []

    def set(self, state: OrgState) -> None:
        self.states[state.bin] = state

    async def get_state(self, bin_: str, max_age=None) -> OrgState:
        self.calls.append(bin_)
        if bin_ in self.fail:
            raise PortalError("timeout")
        return self.states.get(bin_, OrgState(bin=bin_, found=False))

    async def get_many(self, bins, max_age=None):
        out = {}
        for b in dict.fromkeys(bins):
            try:
                out[b] = await self.get_state(b)
            except PortalError as e:
                out[b] = e
        return out


class Sent:
    def __init__(self):
        self.messages: list[tuple[int, str]] = []

    async def __call__(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))

    def to(self, chat_id: int) -> list[str]:
        return [t for c, t in self.messages if c == chat_id]


@pytest.fixture
def env(tmp_path, monkeypatch):
    import ondiris_bot.monitor as monitor_mod

    async def no_sleep(_):
        return None

    monkeypatch.setattr(monitor_mod.asyncio, "sleep", no_sleep)
    db = tmp_path / "bot.db"
    store = Storage(db)
    portal = FakePortal()
    sent = Sent()
    mon = Monitor(store, portal, TZ, sent)
    svc = BotService(store, portal, mon, TZ, time(18, 0))

    class Env:
        pass

    e = Env()
    e.db, e.store, e.portal, e.sent, e.monitor, e.service = db, store, portal, sent, mon, svc

    def user(uid: int):
        store.upsert_user(uid, uid, f"user{uid}")
        return store.get_user(uid)

    e.user = user
    yield e
    store.close()


def run(coro):
    return asyncio.run(coro)
