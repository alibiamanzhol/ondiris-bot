import re
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from .snapshot import APPEARED, DISAPPEARED, Change, OrgState

TG_LIMIT = 3800
CARD_PRODUCTS = 30
CHANGE_PRODUCTS = 25

IN_REGISTRY = "✅"
NOT_IN_REGISTRY = "❌"
UNKNOWN = "⚠️"
NOT_CHECKED = "⏳"

LEGEND = f"{IN_REGISTRY} есть в реестре · {NOT_IN_REGISTRY} нет в реестре"

_FORMS = [
    (re.compile(r"^товарищество с ограниченной ответственностью\s*", re.I), "ТОО "),
    (re.compile(r"^акционерное общество\s*", re.I), "АО "),
    (re.compile(r"^индивидуальный предприниматель\s*", re.I), "ИП "),
    (re.compile(r"^крестьянское \(фермерское\) хозяйство\s*", re.I), "КФХ "),
    (re.compile(r"^крестьянское хозяйство\s*", re.I), "КХ "),
    (re.compile(r"^производственный кооператив\s*", re.I), "ПК "),
]


def short_company(name: str) -> str:
    name = " ".join((name or "").split())
    for pattern, repl in _FORMS:
        if pattern.match(name):
            name = pattern.sub(repl, name, count=1)
            break
    return re.sub(r'"([^"]*)"', r"«\1»", name)


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def records_phrase(total: int, active: int) -> str:
    """Как на сайте: число строк реестра по БИН; отдельно — сколько из них активных."""
    word = plural(total, "запись", "записи", "записей")
    if active == total:
        return f"{total} {word}"
    return f"{total} {word} (активных: {active})"


def local_time(iso_utc: str | None, tz: ZoneInfo) -> str:
    if not iso_utc:
        return ""
    try:
        return datetime.fromisoformat(iso_utc).astimezone(tz).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return ""


def status_line(bin_: str, state: OrgState | None, label: str = "", failed: bool = False,
                number: int | None = None) -> str:
    """Одна строка списка: значок статуса, БИН, название, число записей."""
    prefix = f"{number}. " if number is not None else ""
    title = (state.company if state and state.company else "") or label
    name = f" — {escape(short_company(title))}" if title else ""
    if failed:
        known = ""
        if state:
            known = f" (по прошлой проверке: {'в реестре' if state.found else 'нет в реестре'})"
        return f"{prefix}{UNKNOWN} <code>{bin_}</code>{name} — не удалось проверить{known}"
    if state is None:
        return f"{prefix}{NOT_CHECKED} <code>{bin_}</code>{name} — ещё не проверялся"
    if state.found:
        return f"{prefix}{IN_REGISTRY} <code>{bin_}</code>{name} · {records_phrase(state.total, state.active)}"
    return f"{prefix}{NOT_IN_REGISTRY} <code>{bin_}</code>{name} · нет в реестре"


def counts_line(states: list[OrgState | None], failed: int = 0) -> str:
    found = sum(1 for s in states if s and s.found)
    missing = sum(1 for s in states if s and not s.found)
    parts = [f"{IN_REGISTRY} В реестре: <b>{found}</b>", f"{NOT_IN_REGISTRY} Нет в реестре: <b>{missing}</b>"]
    unchecked = sum(1 for s in states if s is None)
    if unchecked:
        parts.append(f"{NOT_CHECKED} Не проверялись: {unchecked}")
    if failed:
        parts.append(f"{UNKNOWN} Не удалось проверить: {failed}")
    return "\n".join(parts)


def _bullets(items, limit: int) -> list[str]:
    items = list(items)
    lines = [f"• {escape(x)}" for x in items[:limit]]
    if len(items) > limit:
        lines.append(f"…и ещё {len(items) - limit}")
    return lines


def _header(bin_: str, company: str) -> list[str]:
    lines = []
    if company:
        lines.append(f"🏢 <b>{escape(short_company(company))}</b>")
    lines.append(f"БИН/ИИН: <code>{bin_}</code>")
    return lines


def _names_block(names: list[str], limit: int) -> list[str]:
    if not names:
        return []
    if len(names) == 1:
        return [f"📦 Товар: {escape(names[0])}"]
    return [f"📦 Товары ({len(names)} {plural(len(names), 'наименование', 'наименования', 'наименований')}):",
            *_bullets(names, limit)]


def format_card(state: OrgState) -> str:
    """Ответ на «🔎 Проверить БИН»."""
    if not state.found:
        return (f"{NOT_IN_REGISTRY} <b>НЕТ В РЕЕСТРЕ</b>\n\n"
                f"БИН/ИИН: <code>{state.bin}</code>\n"
                "В Реестре казахстанских товаропроизводителей записей с этим БИН нет.")
    lines = [f"{IN_REGISTRY} <b>ЕСТЬ В РЕЕСТРЕ</b>", "", *_header(state.bin, state.company),
             f"📄 Записей в реестре: <b>{records_phrase(state.total, state.active)}</b>", ""]
    names = state.product_names()
    lines += _names_block(names, CARD_PRODUCTS)
    if len(names) < state.total:
        lines += ["", "<i>Запись = строка таблицы на сайте. Один товар может встречаться в нескольких записях "
                      "(разные заявки/сертификаты), поэтому записей больше, чем наименований.</i>"]
    return "\n".join(lines).rstrip()


def format_change(change: Change) -> str:
    lines = _header(change.bin, change.company)
    now = f"📄 Сейчас в реестре: {records_phrase(change.total, change.active)}"
    if change.kind == APPEARED:
        lines += ["", f"{IN_REGISTRY} <b>Появилась в реестре</b>", now]
        lines += _names_block(list(change.added), CHANGE_PRODUCTS)
    elif change.kind == DISAPPEARED:
        lines += ["", f"{NOT_IN_REGISTRY} <b>Исчезла из реестра</b> — записей с этим БИН больше нет"]
    else:
        lines += ["", f"{IN_REGISTRY} В реестре, есть изменения:"]
        if change.old_company:
            lines.append(f"✏️ Изменилось наименование. Было: {escape(short_company(change.old_company))}")
        if change.added_count:
            lines.append(f"🆕 <b>Новые записи: {change.added_count}</b>")
            lines += _bullets(change.added, CHANGE_PRODUCTS)
        if change.removed_count:
            lines.append(f"➖ <b>Удалены записи: {change.removed_count}</b>")
            lines += _bullets(change.removed, CHANGE_PRODUCTS)
        if change.became_inactive:
            lines.append(f"⏸ Стали неактивными: {change.became_inactive}")
        if change.became_active:
            lines.append(f"▶️ Стали активными: {change.became_active}")
        lines.append(now)
    return "\n".join(lines)


def format_changes(changes: list[Change], checked_at: str) -> list[str]:
    """Группирует изменения в минимальное число сообщений в пределах лимита Telegram."""
    head = ("🔔 <b>ОБНАРУЖЕНО ИЗМЕНЕНИЕ</b>" if len(changes) == 1
            else f"🔔 <b>ОБНАРУЖЕНЫ ИЗМЕНЕНИЯ</b> — организаций: {len(changes)}")
    foot = f"🕒 Проверка: {checked_at}"
    messages, blocks = [], []
    size = len(head) + len(foot) + 4
    for block in (format_change(c) for c in changes):
        if blocks and size + len(block) + 2 > TG_LIMIT:
            messages.append("\n\n".join([head, *blocks, foot]))
            blocks, size = [], len(head) + len(foot) + 4
        blocks.append(block)
        size += len(block) + 2
    if blocks:
        messages.append("\n\n".join([head, *blocks, foot]))
    return messages


def split_long(text: str, limit: int = TG_LIMIT) -> list[str]:
    parts, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts
