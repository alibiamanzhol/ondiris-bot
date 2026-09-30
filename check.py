import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent
BINS_FILE = ROOT / "bins.txt"
STATE_FILE = ROOT / "state.json"
DEBUG_DIR = ROOT / "debug"

REGISTRY_URL = os.getenv("REGISTRY_URL") or "https://e-ondiris.gov.kz"
SEARCH_SELECTOR = os.getenv("SEARCH_SELECTOR") or (
    "input[type=search], input[placeholder*='БИН' i], input[placeholder*='поиск' i], input[type=text]"
)
RESULT_SELECTOR = os.getenv("RESULT_SELECTOR") or "table tbody tr, [role=row], .ant-table-row"
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DEBUG = os.getenv("DEBUG", "") == "1"

ASTANA = timezone(timedelta(hours=5))


def load_bins() -> dict[str, str]:
    bins = {}
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


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"found": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def send_telegram(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[telegram не настроен]\n" + text)
        return
    data = urllib.parse.urlencode(
        {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    ).encode()
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    with urllib.request.urlopen(url, data=data, timeout=30) as resp:
        resp.read()


def check_bin(page, bin_: str) -> tuple[bool, str]:
    page.goto(REGISTRY_URL, wait_until="networkidle", timeout=60_000)
    search = page.locator(SEARCH_SELECTOR).first
    search.wait_for(state="visible", timeout=30_000)
    search.fill(bin_)
    search.press("Enter")
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except PWTimeout:
        pass
    page.wait_for_timeout(2_000)

    if DEBUG:
        DEBUG_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / f"{bin_}.png"), full_page=True)
        (DEBUG_DIR / f"{bin_}.html").write_text(page.content(), encoding="utf-8")

    for row in page.locator(RESULT_SELECTOR).all():
        text = row.inner_text()
        if bin_ in re.sub(r"\s", "", text):
            return True, " ".join(text.split())[:300]
    return False, ""


def main() -> int:
    bins = load_bins()
    state = load_state()
    now = datetime.now(ASTANA).strftime("%d.%m.%Y %H:%M")
    newly_found, errors = [], []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="ru-RU")
        for bin_, name in bins.items():
            if bin_ in state["found"]:
                continue
            try:
                found, row_text = check_bin(page, bin_)
            except Exception as e:
                errors.append(f"{bin_}: {type(e).__name__}: {str(e).splitlines()[0][:200]}")
                continue
            print(f"{bin_} {name}: {'НАЙДЕН' if found else 'нет'}")
            if found:
                state["found"][bin_] = {"name": name, "date": now, "row": row_text}
                newly_found.append((bin_, name, row_text))
        browser.close()

    save_state(state)

    if newly_found:
        lines = [f"✅ <b>Появились в реестре товаропроизводителей</b> ({now}):", ""]
        for bin_, name, row_text in newly_found:
            lines.append(f"• <b>{bin_}</b> {html.escape(name)}".rstrip())
            if row_text:
                lines.append(f"  <i>{html.escape(row_text)}</i>")
        lines += ["", REGISTRY_URL]
        send_telegram("\n".join(lines))

    if errors:
        send_telegram("⚠️ Ошибки при проверке реестра:\n" + html.escape("\n".join(errors[:20])))

    remaining = [b for b in bins if b not in state["found"]]
    print(f"Итог: новых {len(newly_found)}, ещё не в реестре {len(remaining)}, ошибок {len(errors)}")
    return 1 if errors and len(errors) == len(remaining) else 0


if __name__ == "__main__":
    sys.exit(main())
