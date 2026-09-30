import html
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from inbox import load_bins, load_state, process_inbox, save_state, send_telegram

ROOT = Path(__file__).parent
DEBUG_DIR = ROOT / "debug"

REGISTRY_URL = os.getenv("REGISTRY_URL") or "https://e-ondiris.gov.kz"
SEARCH_SELECTOR = os.getenv("SEARCH_SELECTOR") or (
    "input[type=search], input[placeholder*='БИН' i], input[placeholder*='поиск' i], input[type=text]"
)
RESULT_SELECTOR = os.getenv("RESULT_SELECTOR") or "table tbody tr, [role=row], .ant-table-row"
DEBUG = os.getenv("DEBUG", "") == "1"

ASTANA = timezone(timedelta(hours=5))


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
    try:
        process_inbox()
    except Exception as e:
        print(f"Не удалось обработать сообщения Telegram: {e}", file=sys.stderr)
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
