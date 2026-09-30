import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from inbox import SITE_URL, format_card, load_state, process_inbox, save_state, send_telegram, watchers

ROOT = Path(__file__).parent
DEBUG_DIR = ROOT / "debug"

API_URL = os.getenv("REGISTRY_API_URL") or f"{SITE_URL}/awp-api/registry-front"
DEBUG = os.getenv("DEBUG", "") == "1"
SELFTEST_BIN = "181240006529"

ASTANA = timezone(timedelta(hours=5))


def fetch(bin_: str) -> tuple[list[dict], int]:
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
    rows = [
        r for r in rows
        if str(r.get("bin_iin") or r.get("bin") or "").strip() == bin_ and r.get("is_active") is not False
    ]
    total = (data.get("meta") or {}).get("total") if isinstance(data, dict) else None
    return rows, (total if rows and isinstance(total, int) else len(rows))


def fmt_date(s: str) -> str:
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return s or ""


def summarize(rows: list[dict], total: int) -> dict:
    def first(key):
        return next((str(r[key]) for r in rows if r.get(key) not in (None, "", "-", "—")), "")

    reg_numbers = list(dict.fromkeys(str(r["registration_number"]) for r in rows if r.get("registration_number")))
    dates = sorted(r["registry_inclusion_date"] for r in rows if r.get("registry_inclusion_date"))
    products = []
    for r in rows:
        name = r.get("product_name")
        if name and name != "—" and all(p["name"] != name for p in products):
            products.append({
                "name": name,
                "dvc": r.get("dvc_percent") or "",
                "capacity": r.get("production_capacity") or "",
            })
    return {
        "company": first("company_name"),
        "reg_number": ", ".join(reg_numbers[:3]),
        "inclusion_date": fmt_date(dates[0]) if dates else "",
        "region": first("region_kato"),
        "products": products,
        "total": max(total, len(products)),
    }


def main() -> int:
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        print("Telegram: секрет TELEGRAM_BOT_TOKEN не задан")
    try:
        process_inbox()
    except Exception as e:
        print(f"Telegram: не удалось обработать сообщения: {e}")

    if DEBUG:
        rows, total = fetch(SELFTEST_BIN)
        print(f"Самопроверка {SELFTEST_BIN}: строк {len(rows)}, всего {total}")
        print(format_card(SELFTEST_BIN, summarize(rows, total)))

    state = load_state()
    all_bins = sorted({b for u in state["users"].values() for b in u["bins"]})
    now = datetime.now(ASTANA).strftime("%d.%m.%Y %H:%M")
    print(f"Пользователей: {len(state['users'])}, БИН к проверке: {len(all_bins)}")
    newly_found, errors = [], []

    for bin_ in all_bins:
        if bin_ in state["found"]:
            continue
        try:
            rows, total = fetch(bin_)
        except Exception as e:
            errors.append(f"{bin_}: {type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}")
            continue
        if rows:
            state["found"][bin_] = summarize(rows, total) | {"found_at": now}
            newly_found.append(bin_)
        time.sleep(1)

    save_state(state)

    for bin_ in newly_found:
        for chat_id in watchers(state, bin_):
            name = state["users"][chat_id]["bins"].get(bin_, "")
            try:
                send_telegram(chat_id, "🆕 Появился в реестре:\n" + format_card(bin_, state["found"][bin_], name))
            except Exception as e:
                print(f"Не удалось отправить {chat_id}: {e}")

    if errors and len(errors) == len([b for b in all_bins if b not in state["found"]]):
        print("Все запросы к реестру завершились ошибкой:\n" + "\n".join(errors[:20]))
        return 1
    if errors:
        print("Ошибки:\n" + "\n".join(errors[:20]))
    print(f"Итог: новых {len(newly_found)}, ошибок {len(errors)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
