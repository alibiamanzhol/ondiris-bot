import html
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
SITE_URL = "https://e-ondiris.gov.kz"

BIN_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
NAME_STRIP = " \t-–—:;,.|\"'«»()[]"
MAX_PRODUCTS = 10

HELP = (
    "Бот следит за <a href=\"https://e-ondiris.gov.kz\">реестром казахстанских товаропроизводителей</a> "
    "и сообщает, когда организация из вашего списка в нём появится. Проверка — в 09:00 и 18:00 по Астане.\n\n"
    "Отправьте список БИН в любом виде: столбиком, через запятую, скопированным из Excel, "
    "с названиями или без. Можно прислать .txt или .csv файлом.\n\n"
    "/list — ваш список\n"
    "/remove БИН БИН … — удалить из списка\n\n"
    "Бот отвечает на сообщения в течение часа."
)


def load_state() -> dict:
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    state.setdefault("tg_offset", 0)
    state.setdefault("users", {})
    state.setdefault("found", {})
    return state


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def watchers(state: dict, bin_: str) -> list[str]:
    return [cid for cid, u in state["users"].items() if bin_ in u["bins"]]


def tg(method: str, **params) -> dict:
    data = urllib.parse.urlencode(params).encode()
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    with urllib.request.urlopen(url, data=data, timeout=60) as resp:
        return json.loads(resp.read())


def send_telegram(chat_id: str, text: str) -> None:
    if not TG_TOKEN:
        print(f"[telegram не настроен] -> {chat_id}\n{text}")
        return
    for i in range(0, len(text), 4000):
        tg("sendMessage", chat_id=chat_id, text=text[i : i + 4000],
           parse_mode="HTML", disable_web_page_preview="true")


def format_card(bin_: str, info: dict, user_name: str = "") -> str:
    e = html.escape
    title = info.get("company") or user_name
    lines = [f"✅ <b>{bin_}</b> {e(title)}".rstrip()]
    meta = []
    if info.get("reg_number"):
        meta.append(f"рег. № {e(info['reg_number'])}")
    if info.get("inclusion_date"):
        meta.append(f"включён {e(info['inclusion_date'])}")
    if info.get("region"):
        meta.append(e(info["region"]))
    if meta:
        lines.append(" · ".join(meta))
    products = info.get("products", [])
    total = info.get("total") or len(products)
    if products:
        lines.append(f"Товары ({total}):")
        for p in products[:MAX_PRODUCTS]:
            extra = [x for x in (f"ДВЦ {p['dvc']}%" if p.get("dvc") else "", p.get("capacity", "")) if x]
            lines.append(f"• {e(p['name'])}" + (f" — {e(', '.join(extra))}" if extra else ""))
        if total > MAX_PRODUCTS:
            lines.append(f"…и ещё {total - MAX_PRODUCTS}")
    lines.append(f"{SITE_URL}")
    return "\n".join(lines)


def checksum_ok(bin_: str) -> bool:
    d = [int(c) for c in bin_]
    for weights in (range(1, 12), [3, 4, 5, 6, 7, 8, 9, 10, 11, 1, 2]):
        s = sum(a * w for a, w in zip(d, weights)) % 11
        if s != 10:
            return s == d[11]
    return False


def extract_bins(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        found = BIN_RE.findall(line)
        if len(found) == 1:
            name = " ".join(BIN_RE.sub(" ", line).split()).strip(NAME_STRIP)
            result.setdefault(found[0], name)
        else:
            for b in found:
                result.setdefault(b, "")
    return result


def download_document(doc: dict) -> str:
    info = tg("getFile", file_id=doc["file_id"])["result"]
    url = f"https://api.telegram.org/file/bot{TG_TOKEN}/{info['file_path']}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        raw = resp.read()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def handle_add(text: str, user: dict, state: dict) -> list[str]:
    bins = user["bins"]
    incoming = extract_bins(text)
    if not incoming:
        return ["Не нашёл в сообщении ни одного БИН (12 цифр).\n\n" + HELP]
    added, existed, suspicious = [], [], []
    for b, name in incoming.items():
        if b in bins:
            if name and not bins[b]:
                bins[b] = name
            existed.append(b)
            continue
        bins[b] = name
        added.append(b)
        if not checksum_ok(b):
            suspicious.append(b)
    summary = [f"➕ Добавлено: <b>{len(added)}</b>"]
    if existed:
        summary.append(f"Уже были в списке: {len(existed)}")
    if suspicious:
        summary.append("⚠️ Не сходится контрольная цифра, проверьте: " + ", ".join(suspicious))
    already = [b for b in added if b in state["found"]]
    if already:
        summary.append(f"Уже в реестре: {len(already)} — подробности ниже.")
    summary.append(f"Всего в вашем списке: {len(bins)}")
    return ["\n".join(summary)] + [format_card(b, state["found"][b], bins[b]) for b in already]


def handle_remove(text: str, user: dict) -> str:
    bins = user["bins"]
    targets = BIN_RE.findall(text)
    if not targets:
        return "Укажите БИН после команды, например: /remove 123456789012"
    removed = [b for b in targets if bins.pop(b, None) is not None]
    missing = [b for b in targets if b not in removed]
    out = [f"➖ Удалено: {len(removed)}"]
    if missing:
        out.append("Не было в списке: " + ", ".join(missing))
    out.append(f"Всего в вашем списке: {len(bins)}")
    return "\n".join(out)


def handle_list(user: dict, state: dict) -> str:
    bins = user["bins"]
    if not bins:
        return "Ваш список пуст. Отправьте БИН сообщением."
    in_reg = sum(1 for b in bins if b in state["found"])
    lines = [f"<b>Ваш список: {len(bins)}</b> (в реестре: {in_reg})"]
    for b, name in bins.items():
        info = state["found"].get(b)
        title = name or (info or {}).get("company", "")
        lines.append(f"{'✅' if info else '⏳'} {b} {html.escape(title)}".rstrip())
    return "\n".join(lines)


def process_inbox() -> None:
    if not TG_TOKEN:
        return
    state = load_state()
    updates = tg("getUpdates", offset=state["tg_offset"], timeout=0,
                 allowed_updates='["message"]')["result"]
    if not updates:
        return
    for upd in updates:
        state["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        chat = msg.get("chat", {})
        if chat.get("type") not in ("private", "group", "supergroup"):
            continue
        chat_id = str(chat["id"])
        user = state["users"].setdefault(chat_id, {"bins": {}})
        sender = msg.get("from", {})
        name = chat.get("title") or sender.get("username") or sender.get("first_name")
        if name:
            user["name"] = name
        text = (msg.get("text") or msg.get("caption") or "").strip()
        doc = msg.get("document")
        cmd = text.split(maxsplit=1)[0].split("@")[0].lower() if text.startswith("/") else ""
        try:
            if cmd in ("/start", "/help"):
                replies = [HELP]
            elif cmd == "/list":
                replies = [handle_list(user, state)]
            elif cmd == "/remove":
                replies = [handle_remove(text, user)]
            elif cmd:
                replies = ["Неизвестная команда.\n\n" + HELP]
            elif doc:
                replies = handle_add(text + "\n" + download_document(doc), user, state)
            else:
                replies = handle_add(text, user, state)
        except Exception as e:
            replies = [f"Ошибка обработки: {html.escape(str(e))}"]
        for r in replies:
            try:
                send_telegram(chat_id, r)
            except Exception as e:
                print(f"Не удалось ответить {chat_id}: {e}")
    save_state(state)


if __name__ == "__main__":
    process_inbox()
