import re
from html import escape

from .snapshot import APPEARED, DISAPPEARED, Change, OrgState

TG_LIMIT = 3800
CARD_PRODUCTS = 30
CHANGE_PRODUCTS = 25

_FORMS = [
    (re.compile(r"^товарищество с ограниченной ответственностью\s*", re.I), "ТОО "),
    (re.compile(r"^акционерное общество\s*", re.I), "АО "),
    (re.compile(r"^индивидуальный предприниматель\s*", re.I), "ИП "),
    (re.compile(r"^крестьянское хозяйство\s*", re.I), "КХ "),
    (re.compile(r"^крестьянское \(фермерское\) хозяйство\s*", re.I), "КФХ "),
    (re.compile(r"^производственный кооператив\s*", re.I), "ПК "),
]


def short_company(name: str) -> str:
    name = " ".join((name or "").split())
    for pattern, repl in _FORMS:
        if pattern.match(name):
            name = pattern.sub(repl, name, count=1)
            break
    return re.sub(r'"([^"]*)"', r"«\1»", name)


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


def _products_block(names: list[str], limit: int, title_one="📦 Товар", title_many="📦 Товары") -> list[str]:
    if not names:
        return []
    if len(names) == 1:
        return [f"{title_one}: {escape(names[0])}"]
    return [f"{title_many} ({len(names)}):", *_bullets(names, limit)]


def format_card(state: OrgState) -> str:
    if not state.found:
        return (f"🔎 БИН/ИИН <code>{state.bin}</code>\n\n"
                "❌ В реестре казахстанских товаропроизводителей не найден.")
    lines = ["🔎 <b>Найден в реестре</b>", "", *_header(state.bin, state.company), ""]
    lines += _products_block(state.product_names(), CARD_PRODUCTS)
    return "\n".join(lines).rstrip()


def format_change(change: Change) -> str:
    lines = _header(change.bin, change.company)
    if change.kind == APPEARED:
        lines += ["", "✅ <b>Появилась в реестре</b>"]
        lines += _products_block(list(change.added), CHANGE_PRODUCTS)
    elif change.kind == DISAPPEARED:
        lines += ["", "⚠️ <b>Больше не найдена в реестре</b>"]
    else:
        if change.old_company:
            lines += ["", f"✏️ Изменилось наименование, было: {escape(short_company(change.old_company))}"]
        if change.added:
            lines += ["", "🆕 <b>Новый товар:</b>" if len(change.added) == 1 else f"🆕 <b>Новые товары ({len(change.added)}):</b>"]
            lines += _bullets(change.added, CHANGE_PRODUCTS)
        if change.removed:
            lines += ["", "➖ <b>Исключён товар:</b>" if len(change.removed) == 1 else f"➖ <b>Исключены товары ({len(change.removed)}):</b>"]
            lines += _bullets(change.removed, CHANGE_PRODUCTS)
    return "\n".join(lines)


def format_changes(changes: list[Change], checked_at: str) -> list[str]:
    """Группирует изменения в минимальное число сообщений в пределах лимита Telegram."""
    head = "🔔 <b>ОБНАРУЖЕНО ИЗМЕНЕНИЕ</b>" if len(changes) == 1 else f"🔔 <b>ОБНАРУЖЕНЫ ИЗМЕНЕНИЯ ({len(changes)})</b>"
    foot = f"Проверка: {checked_at}"
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
