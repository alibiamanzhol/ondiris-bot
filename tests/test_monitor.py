from conftest import gen_bins, org, run
from ondiris_bot.bins import parse_text
from ondiris_bot.snapshot import OrgState
from ondiris_bot.storage import Storage

A, B = 111, 222
BIN1, BIN2, BIN3 = "181240006529", "971240001315", "940140000385"


def add(env, uid, *bins):
    env.user(uid)
    outcome = env.service.add(uid, parse_text(" ".join(bins)))
    run(env.service.baseline_report(uid, outcome.added))
    return outcome


def daily(env, *uids):
    return run(env.monitor.run([env.store.get_user(u) for u in uids]))


# 1, 8, 10 — добавление, изоляция пользователей, удаление
def test_add_single_and_isolation_and_remove(env):
    out = add(env, A, BIN1)
    assert out.added == [BIN1] and out.total == 1
    add(env, B, BIN2, BIN3)
    assert [s.bin for s in env.store.list_subscriptions(A)] == [BIN1]
    assert [s.bin for s in env.store.list_subscriptions(B)] == [BIN2, BIN3]

    assert "удалён" in env.service.remove(A, BIN2) or "отсутствует" in env.service.remove(A, BIN2)
    assert [s.bin for s in env.store.list_subscriptions(B)] == [BIN2, BIN3]
    assert "✅" in env.service.remove(A, f"/remove {BIN1}")
    assert env.store.count_bins(A) == 0
    assert "отсутствует" in env.service.remove(A, BIN1)
    msg = env.service.remove(B, f"{BIN2} {BIN3} {BIN1}")
    assert "Удалено: 2" in msg and BIN1 in msg


def test_add_reports_existing_and_invalid(env):
    add(env, A, BIN1)
    out = env.service.add(A, parse_text(f"{BIN1} {BIN2} 123456789012"))
    assert out.added == [BIN2] and out.existed == [BIN1] and out.invalid == ["123456789012"]
    text = out.text()
    assert "Добавлено: <b>1</b>" in text and "Уже были в списке: 1" in text and "Некорректных значений: 1" in text
    assert "Всего в мониторинге: <b>2</b>" in text


# 11 — baseline: уже существующие записи не приходят как «новые»
def test_baseline_on_add_no_false_news(env):
    env.portal.set(org(BIN1, products={"code:1": "Трубы", "code:2": "Арматура"}))
    add(env, A, BIN1)
    assert env.store.list_subscriptions(A)[0].snapshot.found
    stats = daily(env, A)
    assert stats.changed == 0 and env.sent.messages == []


# 12 — без изменений тишина
def test_no_changes_no_messages(env):
    env.portal.set(org(BIN1))
    add(env, A, BIN1, BIN2)
    for _ in range(3):
        daily(env, A)
    assert env.sent.messages == []


# 13 — новый товар
def test_new_product_notified_once(env):
    env.portal.set(org(BIN1, products={"code:1": "Трубы стальные"}))
    add(env, A, BIN1)
    env.portal.set(org(BIN1, products={"code:1": "Трубы стальные", "code:2": "Кабель силовой"}))
    stats = daily(env, A)
    assert stats.changed == 1 and stats.notifications == 1
    msg = env.sent.to(A)[0]
    assert "ОБНАРУЖЕНО ИЗМЕНЕНИЕ" in msg and "Новый товар" in msg and "Кабель силовой" in msg
    assert "Трубы стальные" not in msg and BIN1 in msg and "Проверка:" in msg
    # 16 — повторный запуск не дублирует уведомление
    daily(env, A)
    daily(env, A)
    assert len(env.sent.to(A)) == 1


def test_appearance_in_registry(env):
    add(env, A, BIN1)  # пока не в реестре
    env.portal.set(org(BIN1, "ТОО \"ABC\"", {"code:1": "Кабель", "code:2": "Провод"}))
    daily(env, A)
    msg = env.sent.to(A)[0]
    assert "Появилась в реестре" in msg and "ТОО «ABC»" in msg and "Кабель" in msg and "Провод" in msg


def test_removed_product_and_rename(env):
    env.portal.set(org(BIN1, "ТОО \"Старое\"", {"code:1": "Трубы", "code:2": "Арматура"}))
    add(env, A, BIN1)
    env.portal.set(org(BIN1, "ТОО \"Новое\"", {"code:1": "Трубы"}))
    daily(env, A)
    msg = env.sent.to(A)[0]
    assert "Исключён товар" in msg and "Арматура" in msg and "было: ТОО «Старое»" in msg


# 14 — порядок товаров / перерегистрация не считаются изменением
def test_order_change_is_not_a_change(env):
    env.portal.set(org(BIN1, products={"code:1": "A", "code:2": "B", "code:3": "C"}))
    add(env, A, BIN1)
    env.portal.set(org(BIN1, products={"code:3": "C", "code:1": "A", "code:2": "B"}))
    assert daily(env, A).changed == 0
    env.portal.set(org(BIN1, products={"code:3": "C", "code:1": "A", "code:99": "B"}))
    assert daily(env, A).changed == 0
    assert env.sent.messages == []


# 15, 18 — ошибка портала не равна исчезновению записей
def test_portal_timeout_keeps_snapshot(env):
    env.portal.set(org(BIN1, products={"code:1": "Трубы"}))
    add(env, A, BIN1)
    before = env.store.list_subscriptions(A)[0].snapshot
    env.portal.fail.add(BIN1)
    stats = daily(env, A)
    assert stats.errors == 1 and stats.changed == 0 and env.sent.messages == []
    assert env.store.list_subscriptions(A)[0].snapshot == before
    env.portal.fail.clear()
    assert daily(env, A).changed == 0


def test_manual_check_reports_portal_failure(env):
    add(env, A, BIN1)
    env.portal.fail.add(BIN1)
    assert "Не удалось получить данные" in run(env.service.run_manual(A))


def test_disappearance_requires_confirmation(env):
    env.portal.set(org(BIN1))
    add(env, A, BIN1)
    del env.portal.states[BIN1]
    stats = daily(env, A)
    assert env.portal.calls.count(BIN1) >= 3  # baseline + запрос + подтверждение
    assert stats.changed == 1 and "Больше не найдена" in env.sent.to(A)[0]


# 9, 19 — один БИН у двух пользователей: один запрос, независимые снимки
def test_shared_bin_single_request_independent_snapshots(env):
    env.portal.set(org(BIN1, products={"code:1": "Трубы"}))
    add(env, A, BIN1)
    env.portal.set(org(BIN1, products={"code:1": "Трубы", "code:2": "Кабель"}))
    add(env, B, BIN1)  # у B базовое состояние уже с кабелем
    env.portal.calls.clear()
    stats = daily(env, A, B)
    assert env.portal.calls == [BIN1]
    assert stats.unique_bins == 1
    assert len(env.sent.to(A)) == 1 and "Кабель" in env.sent.to(A)[0]
    assert env.sent.to(B) == []


def test_changes_grouped_into_one_message(env):
    bins = gen_bins(5, 100)
    add(env, A, *bins)
    for b in bins:
        env.portal.set(org(b))
    stats = daily(env, A)
    assert stats.changed == 5 and len(env.sent.to(A)) == 1
    assert "ОБНАРУЖЕНЫ ИЗМЕНЕНИЯ (5)" in env.sent.to(A)[0]


def test_many_changes_split_by_telegram_limit(env):
    bins = gen_bins(40, 200)
    add(env, A, *bins)
    for b in bins:
        env.portal.set(org(b, products={f"code:{i}": f"Очень длинное наименование товара номер {i}" for i in range(20)}))
    daily(env, A)
    msgs = env.sent.to(A)
    assert 1 < len(msgs) < 40 and all(len(m) <= 4096 for m in msgs)


# 17 — перезапуск: данные и снимки сохраняются
def test_restart_persistence(env):
    env.portal.set(org(BIN1, products={"code:1": "Трубы"}))
    add(env, A, BIN1)
    add(env, B, BIN2)
    env.store.set_monitor_time(A, "09:30")
    env.store.close()

    store = Storage(env.db)
    assert [s.bin for s in store.list_subscriptions(A)] == [BIN1]
    assert [s.bin for s in store.list_subscriptions(B)] == [BIN2]
    assert store.list_subscriptions(A)[0].snapshot.products == {"code:1": "Трубы"}
    assert store.get_user(A).monitor_time == "09:30"
    store.close()
    env.store = Storage(env.db)


def test_outbox_survives_send_failure(env):
    env.portal.set(org(BIN1, products={"code:1": "Трубы"}))
    add(env, A, BIN1)
    env.portal.set(org(BIN1, products={"code:1": "Трубы", "code:2": "Кабель"}))

    async def broken(chat_id, text):
        raise RuntimeError("network down")

    good = env.monitor.send
    env.monitor.send = broken
    daily(env, A)
    assert env.sent.messages == []
    env.monitor.send = good
    daily(env, A)  # новое состояние уже сохранено → повторного diff нет, но письмо из outbox доставляется
    assert len(env.sent.to(A)) == 1 and "Кабель" in env.sent.to(A)[0]


def test_silent_baseline_for_migrated(env):
    env.user(A)
    env.store.add_bins(A, [BIN1], silent_baseline=True)
    env.portal.set(org(BIN1))
    assert daily(env, A).changed == 0
    assert env.store.list_subscriptions(A)[0].snapshot is not None


def test_failed_baseline_then_found_is_reported(env):
    env.portal.fail.add(BIN1)
    add(env, A, BIN1)
    assert env.store.list_subscriptions(A)[0].snapshot is None
    env.portal.fail.clear()
    env.portal.set(org(BIN1))
    daily(env, A)
    assert "Появилась в реестре" in env.sent.to(A)[0]


def test_manual_run_summary(env):
    env.portal.set(org(BIN1))
    add(env, A, BIN1, BIN2)
    text = run(env.service.run_manual(A))
    assert "Проверка завершена" in text and "Проверено: 2" in text and "Изменений: 0" in text


def test_check_one_card(env):
    env.portal.set(org(BIN1, "Товарищество с ограниченной ответственностью \"Пример\"",
                       {"code:1": "Трубы стальные", "code:2": "Арматура"}))
    text, ok = run(env.service.check_one(BIN1))
    assert ok and "ТОО «Пример»" in text and BIN1 in text and "Трубы стальные" in text
    assert "product_code" not in text
    text, ok = run(env.service.check_one(BIN2))
    assert ok and "не найден" in text


def test_add_checked_uses_baseline(env):
    env.user(A)
    env.portal.set(org(BIN1))
    assert "добавлен" in run(env.service.add_checked(A, BIN1))
    assert env.store.list_subscriptions(A)[0].snapshot is not None
    assert "уже есть" in run(env.service.add_checked(A, BIN1))


def test_list_pagination(env):
    add(env, A, *gen_bins(65, 300))
    text, page, pages = env.service.list_page(A, 0)
    assert pages == 3 and "Всего: 65" in text and "1. " in text and "31. " not in text
    text, page, _ = env.service.list_page(A, 2)
    assert "61. " in text and page == 2


def test_notifications_disabled_skips_schedule(env):
    env.user(A)
    env.store.set_notify(A, False)
    assert env.store.users_due("2026-09-30", "18:05", "18:00") == []
    env.store.set_notify(A, True)
    assert [u.user_id for u in env.store.users_due("2026-09-30", "18:05", "18:00")] == [A]
    assert env.store.users_due("2026-09-30", "17:59", "18:00") == []
    env.store.set_monitor_time(A, "09:00")
    assert [u.user_id for u in env.store.users_due("2026-09-30", "09:00", "18:00")] == [A]
    env.store.set_last_run([A], "2026-09-30")
    assert env.store.users_due("2026-09-30", "23:59", "18:00") == []
    assert [u.user_id for u in env.store.users_due("2026-10-01", "09:01", "18:00")] == [A]


def test_scheduled_tick_runs_once_per_day(env, monkeypatch):
    from datetime import datetime

    import ondiris_bot.service as service_mod
    from conftest import TZ

    env.portal.set(org(BIN1))
    add(env, A, BIN1)

    class FakeDT(datetime):
        current = datetime(2026, 9, 30, 18, 1, tzinfo=TZ)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current

    monkeypatch.setattr(service_mod, "datetime", FakeDT)
    env.portal.calls.clear()
    run(env.service.scheduled_tick())
    run(env.service.scheduled_tick())
    assert env.portal.calls == [BIN1]
    assert env.store.get_user(A).last_run_date == "2026-09-30"


def test_legacy_state_migration(env, tmp_path):
    import json

    from ondiris_bot.service import migrate_legacy_state

    legacy = tmp_path / "state.json"
    legacy.write_text(json.dumps({
        "tg_offset": 5,
        "users": {"555": {"bins": {BIN1: "Альфа", BIN2: ""}, "name": "old"}},
        "found": {BIN1: {"company": "X"}},
    }), encoding="utf-8")
    assert migrate_legacy_state(env.store, legacy) == 2
    assert migrate_legacy_state(env.store, legacy) == 0
    subs = {s.bin: s for s in env.store.list_subscriptions(555)}
    assert subs[BIN1].silent_baseline and subs[BIN1].label == "Альфа"
    assert not subs[BIN2].silent_baseline


def test_state_json_roundtrip():
    s = OrgState("181240006529", True, "ТОО", {"code:1": "A"})
    assert OrgState.from_json(s.to_json()) == s
