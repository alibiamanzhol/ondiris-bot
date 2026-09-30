import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from .messages import format_changes
from .portal import PortalClient, PortalError
from .snapshot import Change, OrgState, diff
from .storage import Storage, User

log = logging.getLogger(__name__)

SendFunc = Callable[[int, str], Awaitable[None]]


class PermanentSendError(Exception):
    """Пользователь заблокировал бота или чат не существует — повторять отправку бессмысленно."""


@dataclass
class UserResult:
    checked: int = 0
    errors: int = 0
    changes: list[Change] = field(default_factory=list)


@dataclass
class CycleStats:
    users: int = 0
    unique_bins: int = 0
    success: int = 0
    errors: int = 0
    changed: int = 0
    notifications: int = 0
    per_user: dict[int, UserResult] = field(default_factory=dict)


class Monitor:
    def __init__(self, store: Storage, portal: PortalClient, tz: ZoneInfo, send: SendFunc):
        self.store = store
        self.portal = portal
        self.tz = tz
        self.send = send
        self.lock = asyncio.Lock()
        self._flush_lock = asyncio.Lock()

    def now_str(self) -> str:
        return datetime.now(self.tz).strftime("%d.%m.%Y %H:%M")

    async def capture_baseline(self, user_id: int, bins: list[str]) -> tuple[dict[str, OrgState], list[str]]:
        """Фиксирует текущее состояние только что добавленных БИН, чтобы старые записи не пришли как «новые»."""
        async with self.lock:
            results = await self.portal.get_many(bins)
        states, failed = {}, []
        for b, r in results.items():
            if isinstance(r, PortalError):
                failed.append(b)
            else:
                self.store.save_baseline(user_id, r)
                states[b] = r
        return states, failed

    async def run(self, users: list[User], label: str = "MONITOR") -> CycleStats:
        async with self.lock:
            return await self._run(users, label)

    async def _run(self, users: list[User], label: str) -> CycleStats:
        stats = CycleStats(users=len(users))
        log.info("[%s] Started", label)
        by_id = {u.user_id: u for u in users}
        subs = self.store.subscriptions_for(list(by_id))
        unique = list(dict.fromkeys(s.bin for s in subs))
        stats.unique_bins = len(unique)
        log.info("[%s] Users: %s", label, stats.users)
        log.info("[%s] Unique BINs: %s", label, stats.unique_bins)

        results = await self.portal.get_many(unique, max_age=0)

        # «Организация исчезла» подтверждаем повторным запросом, чтобы сбой портала не выдать за изменение.
        suspicious = [
            b for b, r in results.items()
            if isinstance(r, OrgState) and not r.found
            and any(s.bin == b and s.snapshot and s.snapshot.found for s in subs)
        ]
        if suspicious:
            await asyncio.sleep(3)
            recheck = await self.portal.get_many(suspicious, max_age=0)
            for b, r in recheck.items():
                results[b] = r if isinstance(r, PortalError) or not r.found else PortalError("нестабильный ответ")

        stats.success = sum(1 for r in results.values() if isinstance(r, OrgState))
        stats.errors = len(results) - stats.success
        for b, r in results.items():
            if isinstance(r, PortalError):
                log.warning("[%s] Error %s: %s", label, b, r)

        checked_at = self.now_str()
        for user_id, user in by_id.items():
            res = stats.per_user.setdefault(user_id, UserResult())
            new_states, changes, keys = [], [], []
            for sub in (s for s in subs if s.user_id == user_id):
                new = results.get(sub.bin)
                if not isinstance(new, OrgState):
                    res.errors += 1
                    continue
                res.checked += 1
                old = sub.snapshot
                if old is None:
                    change = None if sub.silent_baseline else diff(None, new)
                    new_states.append(new)
                else:
                    change = diff(old, new)
                    if old.to_json() != new.to_json():
                        new_states.append(new)
                if change:
                    changes.append(change)
                    keys.append(f"{sub.bin}:{change.old_digest}>{change.new_digest}")
            messages = []
            if changes:
                texts = format_changes(changes, checked_at)
                base = hashlib.sha256(f"{user_id}|{'|'.join(keys)}".encode()).hexdigest()[:24]
                messages = [(f"{base}:{i}", t) for i, t in enumerate(texts)]
            if new_states or messages:
                self.store.apply_changes(user_id, user.chat_id, new_states, messages)
            res.changes = changes
            stats.changed += len(changes)

        stats.notifications = await self.flush_outbox()
        log.info("[%s] Success: %s", label, stats.success)
        log.info("[%s] Errors: %s", label, stats.errors)
        log.info("[%s] Changed: %s", label, stats.changed)
        log.info("[%s] Notifications sent: %s", label, stats.notifications)
        log.info("[%s] Finished", label)
        return stats

    async def flush_outbox(self) -> int:
        async with self._flush_lock:
            return await self._flush()

    async def _flush(self) -> int:
        sent = 0
        for msg in self.store.pending_messages():
            try:
                await self.send(msg.chat_id, msg.text)
            except PermanentSendError as e:
                self.store.mark_failed(msg.id, str(e), permanent=True)
                log.warning("[OUTBOX] message %s to user %s dropped: %s", msg.id, msg.user_id, e)
                continue
            except Exception as e:
                self.store.mark_failed(msg.id, str(e))
                log.warning("[OUTBOX] message %s to user %s failed: %s", msg.id, msg.user_id, e)
                continue
            self.store.mark_sent(msg.id)
            sent += 1
        return sent
