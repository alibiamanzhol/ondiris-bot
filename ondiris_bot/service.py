import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from .bins import ParseResult, parse_text
from .config import parse_hhmm
from .messages import (UNKNOWN, counts_line, format_card, local_time, plural, status_line)
from .monitor import Monitor
from .portal import PortalClient, PortalError
from .storage import Storage

log = logging.getLogger(__name__)

LIST_PAGE_SIZE = 30
INVALID_SHOWN = 20
REPORT_LIMIT = 200


@dataclass
class AddOutcome:
    added: list[str] = field(default_factory=list)
    existed: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    total: int = 0

    def text(self) -> str:
        if not self.added and not self.existed:
            lines = ["❌ Не нашёл ни одного корректного БИН/ИИН.",
                     "БИН/ИИН — это 12 цифр. Проверьте номер и отправьте ещё раз."]
            if self.invalid:
                lines.append("Не подошли: <code>" + ", ".join(self.invalid[:INVALID_SHOWN]) + "</code>")
            return "\n".join(lines)
        lines = [f"➕ Добавлено в мониторинг: <b>{len(self.added)}</b>"]
        if self.existed:
            lines.append(f"♻️ Уже были в вашем списке: {len(self.existed)}")
        if self.invalid:
            shown = ", ".join(self.invalid[:INVALID_SHOWN])
            more = f" и ещё {len(self.invalid) - INVALID_SHOWN}" if len(self.invalid) > INVALID_SHOWN else ""
            lines.append(f"🚫 Некорректных значений: {len(self.invalid)} (не добавлены)\n<code>{shown}</code>{more}")
        lines.append(f"📋 Всего в вашем мониторинге: <b>{self.total}</b>")
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

    def _user_time(self, user_id: int) -> str:
        user = self.store.get_user(user_id)
        return user.monitor_time if user and user.monitor_time else self.default_time.strftime("%H:%M")

    def _now(self) -> str:
        return datetime.now(self.tz).strftime("%d.%m.%Y %H:%M")

    # --- добавление ---
    def add(self, user_id: int, parsed: ParseResult, labels: dict[str, str] | None = None) -> AddOutcome:
        added, existed = self.store.add_bins(user_id, parsed.valid, labels)
        return AddOutcome(added, existed, parsed.invalid, self.store.count_bins(user_id))

    async def baseline_report(self, user_id: int, bins: list[str], existed: list[str] | None = None) -> str | None:
        """Проверяет только что добавленные БИН, фиксирует их состояние и показывает статус каждого."""
        existed = existed or []
        if not bins and not existed:
            return None
        states, failed = await self.monitor.capture_baseline(user_id, bins) if bins else ({}, [])
        subs = {s.bin: s for s in self.store.list_subscriptions(user_id)}
        lines = [f"📌 <b>Результат проверки</b> · {self._now()}", ""]
        rows, shown_states = [], []
        for i, b in enumerate(bins + existed, 1):
            sub = subs.get(b)
            label = sub.label if sub else ""
            if b in states:
                state = states[b]
            else:
                state = sub.snapshot if sub else None
            shown_states.append(None if b in failed and state is None else state)
            rows.append(status_line(b, state, label, failed=b in failed, number=i))
        lines.append(counts_line([s for b, s in zip(bins + existed, shown_states) if b not in failed],
                                 failed=len(failed)))
        lines.append("")
        lines += rows[:REPORT_LIMIT]
        if len(rows) > REPORT_LIMIT:
            lines.append(f"…и ещё {len(rows) - REPORT_LIMIT} — полный список в «📋 Мой список».")
        if failed:
            lines += ["", f"{UNKNOWN} Портал не ответил по {len(failed)} БИН — они уже в списке, "
                          "проверю при следующей автопроверке."]
        lines += ["", f"🔔 Дальше пишу только когда что-то меняется. Автопроверка — ежедневно в {self._user_time(user_id)}."]
        return "\n".join(lines)

    # --- проверка одного БИН ---
    async def check_one(self, bin_: str) -> tuple[str, bool]:
        try:
            state = await self.portal.get_state(bin_, max_age=60)
        except PortalError:
            return (f"{UNKNOWN} Не удалось получить данные с портала e-ondiris.gov.kz.\n"
                    "Попробуйте ещё раз через несколько минут."), False
        return format_card(state), True

    async def add_checked(self, user_id: int, bin_: str) -> str:
        added, _ = self.store.add_bins(user_id, [bin_])
        if not added:
            return f"♻️ <code>{bin_}</code> уже есть в вашем списке мониторинга."
        state = None
        try:
            state = await self.portal.get_state(bin_)
            self.store.save_baseline(user_id, state)
        except PortalError:
            pass
        return (f"➕ Добавлено в мониторинг:\n{status_line(bin_, state, failed=state is None)}\n\n"
                f"📋 Всего в вашем мониторинге: <b>{self.store.count_bins(user_id)}</b>\n"
                f"🔔 Сообщу, когда по нему что-то изменится.")

    # --- удаление ---
    def remove(self, user_id: int, text: str) -> str:
        parsed = parse_text(text)
        targets = parsed.valid + [x for x in parsed.invalid if len(x) == 12]
        if not targets:
            return "Отправьте БИН, который нужно удалить, например: <code>123456789012</code>"
        removed, missing = self.store.remove_bins(user_id, targets)
        if len(targets) == 1:
            if removed:
                return (f"🗑 <code>{removed[0]}</code> удалён из вашего списка мониторинга.\n"
                        f"📋 Осталось в мониторинге: <b>{self.store.count_bins(user_id)}</b>")
            return f"ℹ️ БИН <code>{missing[0]}</code> отсутствует в вашем списке."
        lines = [f"🗑 Удалено из мониторинга: <b>{len(removed)}</b>"]
        if missing:
            lines.append("ℹ️ Не было в списке: " + ", ".join(f"<code>{b}</code>" for b in missing))
        lines.append(f"📋 Осталось в мониторинге: <b>{self.store.count_bins(user_id)}</b>")
        return "\n".join(lines)

    # --- список ---
    def list_page(self, user_id: int, page: int) -> tuple[str, int, int]:
        subs = self.store.list_subscriptions(user_id)
        if not subs:
            return "📋 Ваш список мониторинга пуст.\n\nНажмите «➕ Добавить БИН».", 0, 1
        pages = (len(subs) + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE
        page = max(0, min(page, pages - 1))
        start = page * LIST_PAGE_SIZE
        last = max((s.snapshot_at for s in subs if s.snapshot_at), default=None)
        lines = [f"📋 <b>Ваш список мониторинга</b> — {len(subs)} {plural(len(subs), 'организация', 'организации', 'организаций')}",
                 counts_line([s.snapshot for s in subs]), ""]
        for i, s in enumerate(subs[start:start + LIST_PAGE_SIZE], start + 1):
            lines.append(status_line(s.bin, s.snapshot, s.label, number=i))
        if pages > 1:
            lines += ["", f"Страница {page + 1} из {pages}"]
        if last:
            lines += ["", f"🕒 Статусы по последней проверке: {local_time(last, self.tz)}",
                      "Проверить сейчас — кнопка «🔄 Проверить сейчас»."]
        return "\n".join(lines), page, pages

    # --- настройки ---
    def settings_text(self, user_id: int) -> str:
        user = self.store.get_user(user_id)
        t = user.monitor_time if user and user.monitor_time else self.default_time.strftime("%H:%M")
        notify = user.notify if user else True
        state = ("🔔 Уведомления: <b>включены</b> — каждый день проверяю ваш список и пишу, если что-то изменилось."
                 if notify else
                 "🔕 Уведомления: <b>выключены</b> — автопроверки нет. «🔄 Проверить сейчас» работает как обычно.")
        return (
            "⚙️ <b>Настройки</b>\n\n"
            f"🕒 Автопроверка: ежедневно в <b>{t}</b> (время Казахстана)\n"
            f"📋 Организаций в мониторинге: <b>{self.store.count_bins(user_id)}</b>\n"
            f"{state}"
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
            return "⏳ Проверка уже идёт — дождитесь результата."
        user = self.store.get_user(user_id)
        if not user or self.store.count_bins(user_id) == 0:
            return "📋 Ваш список мониторинга пуст. Сначала добавьте БИН — кнопка «➕ Добавить БИН»."
        self._manual_running.add(user_id)
        try:
            stats = await self.monitor.run([user], label=f"MANUAL {user_id}")
        finally:
            self._manual_running.discard(user_id)
        res = stats.per_user.get(user_id)
        if res is None or (res.checked == 0 and res.errors):
            return (f"{UNKNOWN} Не удалось получить данные с портала e-ondiris.gov.kz.\n"
                    "Список не изменён. Попробуйте ещё раз через несколько минут.")
        subs = self.store.list_subscriptions(user_id)
        n = len(res.changes)
        lines = [f"🔄 <b>Проверка завершена</b> · {self._now()}",
                 f"Проверено: {res.checked} из {len(subs)}", "",
                 counts_line([res.states[s.bin] for s in subs if s.bin in res.states], failed=res.errors), ""]
        if n:
            lines.append(f"🔔 Изменений с прошлой проверки: <b>{n}</b> — подробности в сообщении выше.")
        else:
            lines.append("🔕 Изменений с прошлой проверки нет.")
        lines.append("")
        rows = [status_line(s.bin, res.states.get(s.bin, s.snapshot), s.label,
                            failed=s.bin in res.failed, number=i) for i, s in enumerate(subs, 1)]
        lines += rows[:REPORT_LIMIT]
        if len(rows) > REPORT_LIMIT:
            lines.append(f"…и ещё {len(rows) - REPORT_LIMIT} — полный список в «📋 Мой список».")
        if res.errors:
            lines += ["", f"{UNKNOWN} По {res.errors} БИН портал не ответил — попробуйте позже."]
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
