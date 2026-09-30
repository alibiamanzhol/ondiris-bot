import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
BINS_FILE = ROOT / "bins.txt"
STATE_FILE = ROOT / "state.json"

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

BIN_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
NAME_STRIP = " \t-–—:;,.|\"'«»()[]"

HELP = (
    "Отправьте список БИН в любом виде: столбиком, через запятую, скопированным из Excel, "
    "с названиями или без. Можно прислать .txt или .csv файлом.\n\n"
    "/list — текущий список\n"
    "/remove БИН БИН … — удалить из списка"
)


def load_state() -> dict:
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    else:
        state = {}
    state.setdefault("found", {})
    state.setdefault("tg_offset", 0)
    return state


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_bins() -> dict[str, str]:
    bins = {}
    if not BINS_FILE.exists():
        return bins
    for line in BINS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        bin_ = re.sub(r"\D", "", parts[0])
        if len(bin_) != 12:
            print(f"Пропускаю некорректный БИН: {parts[0]}", file=sys.stderr)
            continue
        bins[bin_] = parts[1] if len(parts) > 1 else ""
    return bins


def save_bins(bins: dict[str, str]) -> None:
    lines = ["# Один БИН на строку, после БИН можно указать название."]
    lines += [f"{b} {n}".rstrip() for b, n in bins.items()]
    BINS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def tg(method: str, **params) -> dict:
    data = urllib.parse.urlencode(params).encode()
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    with urllib.request.urlopen(url, data=data, timeout=60) as resp:
        return json.loads(resp.read())


def send_telegram(text: str, chat_id: str | None = None) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[telegram не настроен]\n" + text)
        return
    for i in range(0, len(text), 4000):
        tg("sendMessage", chat_id=chat_id or TG_CHAT_ID, text=text[i : i + 4000],
           parse_mode="HTML", disable_web_page_preview="true")


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


def handle_add(text: str, bins: dict[str, str], state: dict) -> str:
    incoming = extract_bins(text)
    if not incoming:
        return "Не нашёл в сообщении ни одного БИН (12 цифр).\n\n" + HELP
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
    out = [f"➕ Добавлено: <b>{len(added)}</b>"]
    if existed:
        out.append(f"Уже были в списке: {len(existed)}")
    already_found = [b for b in added if b in state["found"]]
    if already_found:
        out.append("Уже в реестре: " + ", ".join(already_found))
    if suspicious:
        out.append("⚠️ Не сходится контрольная цифра, проверьте: " + ", ".join(suspicious))
    out.append(f"Всего в списке: {len(bins)}")
    return "\n".join(out)


def handle_remove(text: str, bins: dict[str, str], state: dict) -> str:
    targets = BIN_RE.findall(text)
    removed = [b for b in targets if bins.pop(b, None) is not None]
    for b in removed:
        state["found"].pop(b, None)
    missing = [b for b in targets if b not in removed]
    out = [f"➖ Удалено: {len(removed)}"]
    if missing:
        out.append("Не было в списке: " + ", ".join(missing))
    out.append(f"Всего в списке: {len(bins)}")
    return "\n".join(out)


def handle_list(bins: dict[str, str], state: dict) -> str:
    if not bins:
        return "Список пуст."
    lines = [f"<b>БИН в списке: {len(bins)}</b>"]
    for b, name in bins.items():
        mark = "✅" if b in state["found"] else "⏳"
        lines.append(f"{mark} {b} {html.escape(name)}".rstrip())
    return "\n".join(lines)


def process_inbox() -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    state = load_state()
    bins = load_bins()
    updates = tg("getUpdates", offset=state["tg_offset"], timeout=0,
                 allowed_updates='["message"]')["result"]
    if not updates:
        return
    for upd in updates:
        state["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != TG_CHAT_ID:
            continue
        text = (msg.get("text") or msg.get("caption") or "").strip()
        doc = msg.get("document")
        cmd = text.split(maxsplit=1)[0].split("@")[0].lower() if text.startswith("/") else ""
        try:
            if cmd in ("/start", "/help"):
                reply = HELP
            elif cmd == "/list":
                reply = handle_list(bins, state)
            elif cmd == "/remove":
                reply = handle_remove(text, bins, state)
            elif doc:
                reply = handle_add(text + "\n" + download_document(doc), bins, state)
            else:
                reply = handle_add(text, bins, state)
        except Exception as e:
            reply = f"Ошибка обработки: {html.escape(str(e))}"
        send_telegram(reply, chat_id)
    save_bins(bins)
    save_state(state)


if __name__ == "__main__":
    process_inbox()
