import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from .bins import ParseResult, parse_text
from .config import parse_hhmm
from .messages import format_card, short_company
from .monitor import Monitor
from .portal import PortalClient, PortalError
from .storage import Storage

log = logging.getLogger(__name__)

LIST_PAGE_SIZE = 30
INVALID_SHOWN = 20


@dataclass
class AddOutcome:
    added: list[str] = field(default_factory=list)
    existed: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    total: int = 0

    def text(self) -> str:
        if not self.added and not self.existed:
            lines = ["❌ Не нашёл ни одного корректного БИН/ИИН (12 цифр)."]
            if self.invalid:
                lines.append("Не распознано: " + ", ".join(self.invalid[:INVALID_SHOWN]))
            return "\n".join(lines)
        lines = [f"✅ Добавлено: <b>{len(self.added)}</b>"]
        if self.existed:
            lines.append(f"♻️ Уже были в списке: {len(self.existed)}")
        if self.invalid:
            shown = ", ".join(self.invalid[:INVALID_SHOWN])
            more = f" и ещё {len(self.invalid) - INVALID_SHOWN}" if len(self.invalid) > INVALID_SHOWN else ""
            lines.append(f"❌ Некорректных значений: {len(self.invalid)}\n<code>{shown}</code>{more}")
        lines += ["", f"Всего в мониторинге: <b>{self.total}</b>"]
        return "\n".join(lines)


class BotService:
    def __init__(self, store: Storage, portal: PortalClient, monitor: Monitor,
                 tz: ZoneInfo, default_time: time):
        self.store = store
        self.portal = portal
        self.monitor = monitor
        self.tz = tz
        self.default_time = default_time
        self._manual_running: set[int] = set()

    # --- добавление ---
    def add(self, user_id: int, parsed: ParseResult, labels: dict[str, str] | None = None) -> AddOutcome:
        added, existed = self.store.add_bins(user_id, parsed.valid, labels)
        return AddOutcome(added, existed, parsed.invalid, self.store.count_bins(user_id))

    async def baseline_report(self, user_id: int, bins: list[str]) -> str | None:
        if not bins:
            return None
        states, failed = await self.monitor.capture_baseline(user_id, bins)
        in_registry = [s for s in states.values() if s.found]
        lines = ["📌 Текущее состояние зафиксировано. Дальше пришлю только изменения."]
        if in_registry:
            lines.append(f"\nУже в реестре: <b>{len(in_registry)}</b> из {len(bins)}")
            for s in in_registry[:INVALID_SHOWN]:
                lines.append(f"• <code>{s.bin}</code> {escape(short_company(s.company))} — товаров: {len(s.product_names())}")
            if len(in_registry) > INVALID_SHOWN:
                lines.append(f"…и ещё {len(in_registry) - INVALID_SHOWN}")
        else:
            lines.append(f"\nПока ни одна из {len(bins)} организаций не найдена в реестре — сообщу, когда появится.")
        if failed:
            lines.append(f"\n⚠️ По {len(failed)} БИН портал не ответил — зафиксирую при следующей проверке.")
        return "\n".join(lines)

    # --- проверка одного БИН ---
    async def check_one(self, bin_: str) -> tuple[str, bool]:
        try:
            state = await self.portal.get_state(bin_, max_age=60)
        except PortalError:
            return "⚠️ Не удалось получить данные. Попробуйте позже.", False
        return format_card(state), True

    async def add_checked(self, user_id: int, bin_: str) -> str:
        added, _ = self.store.add_bins(user_id, [bin_])
        if not added:
            return f"♻️ <code>{bin_}</code> уже есть в вашем списке мониторинга."
        try:
            state = await self.portal.get_state(bin_)
            self.store.save_baseline(user_id, state)
        except PortalError:
            pass
        return (f"✅ <code>{bin_}</code> добавлен в мониторинг.\n"
                f"Всего в мониторинге: <b>{self.store.count_bins(user_id)}</b>")

    # --- удаление ---
    def remove(self, user_id: int, text: str) -> str:
        parsed = parse_text(text)
        targets = parsed.valid + [x for x in parsed.invalid if len(x) == 12]
        if not targets:
            return "Отправьте БИН, который нужно удалить, например: <code>123456789012</code>"
        removed, missing = self.store.remove_bins(user_id, targets)
        if len(targets) == 1:
            if removed:
                return f"✅ <code>{removed[0]}</code> удалён из вашего списка мониторинга."
            return f"ℹ️ БИН <code>{missing[0]}</code> отсутствует в вашем списке."
        lines = [f"✅ Удалено: {len(removed)}"]
        if missing:
            lines.append("ℹ️ Не было в списке: " + ", ".join(f"<code>{b}</code>" for b in missing))
        lines.append(f"\nВсего в мониторинге: <b>{self.store.count_bins(user_id)}</b>")
        return "\n".join(lines)

    # --- список ---
    def list_page(self, user_id: int, page: int) -> tuple[str, int, int]:
        subs = self.store.list_subscriptions(user_id)
        if not subs:
            return "📋 Ваш список мониторинга пуст.\n\nНажмите «➕ Добавить БИН».", 0, 1
        pages = (len(subs) + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE
        page = max(0, min(page, pages - 1))
        start = page * LIST_PAGE_SIZE
        in_reg = sum(1 for s in subs if s.snapshot and s.snapshot.found)
        lines = ["📋 <b>Ваш список мониторинга</b>", f"Всего: {len(subs)} · в реестре: {in_reg}", ""]
        for i, s in enumerate(subs[start:start + LIST_PAGE_SIZE], start + 1):
            mark = "✅" if s.snapshot and s.snapshot.found else "⏳"
            title = f" — {escape(short_company(s.title))}" if s.title else ""
            lines.append(f"{i}. {mark} <code>{s.bin}</code>{title}")
        if pages > 1:
            lines += ["", f"Страница {page + 1} из {pages}"]
        lines += ["", "✅ — есть в реестре, ⏳ — пока нет"]
        return "\n".join(lines), page, pages

    # --- настройки ---
    def settings_text(self, user_id: int) -> str:
        user = self.store.get_user(user_id)
        t = user.monitor_time if user and user.monitor_time else self.default_time.strftime("%H:%M")
        notify = user.notify if user else True
        return (
            "⚙️ <b>Настройки</b>\n\n"
            f"🕒 Автопроверка: ежедневно в {t} (время Казахстана)\n"
            f"📊 Организаций: {self.store.count_bins(user_id)}\n"
            f"🔔 Уведомления: {'включены' if notify else 'выключены'}"
        )

    def set_time(self, user_id: int, text: str) -> str:
        try:
            t = parse_hhmm(text)
        except (ValueError, TypeError):
            return "Не понял время. Отправьте в формате ЧЧ:ММ, например <code>18:00</code>."
        self.store.set_monitor_time(user_id, t.strftime("%H:%M"))
        return f"✅ Автопроверка будет выполняться ежедневно в {t.strftime('%H:%M')}."

    def toggle_notify(self, user_id: int) -> None:
        user = self.store.get_user(user_id)
        self.store.set_notify(user_id, not (user.notify if user else True))

    # --- ручная проверка всего списка ---
    async def run_manual(self, user_id: int) -> str:
        if user_id in self._manual_running:
            return "⏳ Проверка уже выполняется, дождитесь результата."
        user = self.store.get_user(user_id)
        count = self.store.count_bins(user_id)
        if not user or count == 0:
            return "📋 Ваш список мониторинга пуст. Сначала добавьте БИН."
        self._manual_running.add(user_id)
        try:
            stats = await self.monitor.run([user], label=f"MANUAL {user_id}")
        finally:
            self._manual_running.discard(user_id)
        res = stats.per_user.get(user_id)
        if res is None or (res.checked == 0 and res.errors):
            return "⚠️ Не удалось получить данные. Попробуйте позже."
        lines = ["✅ <b>Проверка завершена</b>", f"Проверено: {res.checked}", f"Изменений: {len(res.changes)}"]
        if res.errors:
            lines.append(f"⚠️ Не удалось проверить: {res.errors} — попробуйте позже.")
        return "\n".join(lines)

    # --- расписание ---
    async def scheduled_tick(self) -> None:
        now = datetime.now(self.tz)
        today = now.date().isoformat()
        due = self.store.users_due(today, now.strftime("%H:%M"), self.default_time.strftime("%H:%M"))
        if due:
            await self.monitor.run(due)
            self.store.set_last_run([u.user_id for u in due], today)
        await self.monitor.flush_outbox()


def migrate_legacy_state(store: Storage, path: Path) -> int:
    """Импорт списков из прежнего state.json (версия на GitHub Actions). Выполняется один раз."""
    if store.get_meta("legacy_state_imported") or not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    found = data.get("found", {})
    imported = 0
    for chat_id, user in (data.get("users") or {}).items():
        uid = int(chat_id)
        store.upsert_user(uid, uid, user.get("name", ""))
        bins = user.get("bins") or {}
        # О БИН, найденных прежней версией, пользователь уже знает — фиксируем их молча.
        known = [b for b in bins if b in found]
        fresh = [b for b in bins if b not in found]
        store.add_bins(uid, known, bins, silent_baseline=True)
        store.add_bins(uid, fresh, bins)
        imported += len(bins)
    store.set_meta("legacy_state_imported", datetime.now().isoformat(timespec="seconds"))
    log.info("[MIGRATION] Imported %s BINs for %s users from %s", imported, len(data.get("users") or {}), path)
    return imported
