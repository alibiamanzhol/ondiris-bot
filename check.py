import html
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from inbox import load_bins, load_state, process_inbox, save_state, send_telegram

ROOT = Path(__file__).parent
DEBUG_DIR = ROOT / "debug"

SITE_URL = "https://e-ondiris.gov.kz"
API_URL = os.getenv("REGISTRY_API_URL") or f"{SITE_URL}/awp-api/registry-front"
DEBUG = os.getenv("DEBUG", "") == "1"
SELFTEST_BIN = "181240006529"

ASTANA = timezone(timedelta(hours=5))


def fetch_rows(bin_: str) -> list[dict]:
    query = urllib.parse.urlencode({"page": 1, "limit": 100, "bin_iin": bin_})
    req = urllib.request.Request(
        f"{API_URL}?{query}",
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Referer": SITE_URL + "/"},
    )
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            break
        except Exception as e:
            last_error = e
            time.sleep(5 * (attempt + 1))
    else:
        raise last_error
    if DEBUG:
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / f"{bin_}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"Неожиданный ответ API: {str(data)[:200]}")
    return [
        r for r in rows
        if str(r.get("bin_iin") or r.get("bin") or "").strip() == bin_ and r.get("is_active") is not False
    ]


def describe(rows: list[dict]) -> tuple[str, list[str]]:
    company = next((r.get("company_name") for r in rows if r.get("company_name")), "")
    products = []
    for r in rows:
        p = r.get("product_name") or r.get("name")
        if p and p != "—" and p not in products:
            products.append(p)
    return company, products


def main() -> int:
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        print("Telegram: секрет TELEGRAM_BOT_TOKEN не задан")
    try:
        process_inbox()
    except Exception as e:
        print(f"Telegram: не удалось обработать сообщения: {e}")
    print(f"Telegram: владелец {load_state().get('owner_chat_id') or 'не назначен'}")

    if DEBUG:
        rows = fetch_rows(SELFTEST_BIN)
        print(f"Самопроверка {SELFTEST_BIN}: найдено строк {len(rows)}, {describe(rows)[0]}")

    bins = load_bins()
    state = load_state()
    now = datetime.now(ASTANA).strftime("%d.%m.%Y %H:%M")
    newly_found, errors = [], []

    for bin_, name in bins.items():
        if bin_ in state["found"]:
            continue
        try:
            rows = fetch_rows(bin_)
        except Exception as e:
            errors.append(f"{bin_}: {type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}")
            continue
        print(f"{bin_} {name}: {'НАЙДЕН' if rows else 'нет'}")
        if rows:
            company, products = describe(rows)
            state["found"][bin_] = {"name": name or company, "company": company, "date": now, "products": products}
            newly_found.append((bin_, name or company, products))
        time.sleep(1)

    save_state(state)

    if newly_found:
        lines = [f"✅ <b>Появились в реестре казахстанских товаропроизводителей</b> ({now}):", ""]
        for bin_, name, products in newly_found:
            lines.append(f"• <b>{bin_}</b> {html.escape(name)}".rstrip())
            if products:
                shown = ", ".join(products[:5]) + (f" и ещё {len(products) - 5}" if len(products) > 5 else "")
                lines.append(f"  Товары: <i>{html.escape(shown)}</i>")
        lines += ["", SITE_URL]
        send_telegram("\n".join(lines))

    if errors:
        send_telegram("⚠️ Ошибки при проверке реестра:\n" + html.escape("\n".join(errors[:20])))

    remaining = [b for b in bins if b not in state["found"]]
    print(f"Итог: новых {len(newly_found)}, ещё не в реестре {len(remaining)}, ошибок {len(errors)}")
    return 1 if errors and len(errors) == len(remaining) else 0


if __name__ == "__main__":
    sys.exit(main())
