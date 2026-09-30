import asyncio
import io
import json
from datetime import time
from pathlib import Path

from telegram import Update
from telegram.request import BaseRequest

from conftest import TZ, FakePortal, org, run
from ondiris_bot.bot import BTN_ADD, BTN_CHECK, BTN_LIST, BTN_REMOVE, BTN_RUN, BTN_SETTINGS, build_application
from ondiris_bot.config import Config

BIN1, BIN2, BIN3 = "181240006529", "971240001315", "940140000385"


class FakeTelegram(BaseRequest):
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.files: dict[str, bytes] = {}

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    @property
    def read_timeout(self):
        return 5

    async def do_request(self, url, method, request_data=None, **kwargs):
        if "/file/bot" in url:
            return 200, self.files[url.rsplit("/", 1)[-1]]
        name = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((name, params))
        if name == "getMe":
            result = {"id": 1, "is_bot": True, "first_name": "bot", "username": "ondiris95bot"}
        elif name in ("sendMessage", "editMessageText"):
            result = {"message_id": len(self.calls), "date": 0,
                      "chat": {"id": params.get("chat_id", 0), "type": "private"}, "text": params.get("text", "")}
        elif name == "getFile":
            result = {"file_id": params["file_id"], "file_unique_id": "u", "file_path": params["file_id"]}
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def texts(self, chat_id=None) -> list[str]:
        return [p["text"] for n, p in self.calls
                if n in ("sendMessage", "editMessageText") and (chat_id is None or p.get("chat_id") == chat_id)]


class Harness:
    def __init__(self, tmp_path: Path, allowed=frozenset()):
        self.tg = FakeTelegram()
        self.portal = FakePortal()
        cfg = Config(token="123:TEST", db_path=tmp_path / "bot.db", monitor_time=time(18, 0), tz=TZ,
                     allowed_user_ids=frozenset(allowed), portal_url="", portal_concurrency=1,
                     legacy_state_file=tmp_path / "none.json")
        self.app = build_application(cfg, portal=self.portal, request=self.tg)
        self.store = self.app.bot_data["store"]
        self.uid = 0

    async def __aenter__(self):
        await self.app.initialize()
        return self

    async def __aexit__(self, *exc):
        await asyncio.sleep(0.05)
        await self.app.shutdown()

    def _next(self):
        self.uid += 1
        return self.uid

    async def _process(self, payload):
        await self.app.process_update(Update.de_json(payload, self.app.bot))
        await asyncio.sleep(0.05)

    async def text(self, user_id: int, text: str):
        entities = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else []
        await self._process({"update_id": self._next(), "message": {
            "message_id": self._next(), "date": 0, "text": text, "entities": entities,
            "chat": {"id": user_id, "type": "private"}, "from": {"id": user_id, "is_bot": False, "first_name": "U"}}})

    async def document(self, user_id: int, name: str, data: bytes):
        self.tg.files[name] = data
        await self._process({"update_id": self._next(), "message": {
            "message_id": self._next(), "date": 0,
            "document": {"file_id": name, "file_unique_id": name, "file_name": name, "file_size": len(data)},
            "chat": {"id": user_id, "type": "private"}, "from": {"id": user_id, "is_bot": False, "first_name": "U"}}})

    async def press(self, user_id: int, data: str):
        await self._process({"update_id": self._next(), "callback_query": {
            "id": str(self._next()), "chat_instance": "x", "data": data,
            "from": {"id": user_id, "is_bot": False, "first_name": "U"},
            "message": {"message_id": 1, "date": 0, "text": "old", "chat": {"id": user_id, "type": "private"}}}})

    def last(self, chat_id) -> str:
        return self.tg.texts(chat_id)[-1]


def test_full_user_flow(tmp_path):
    async def scenario():
        async with Harness(tmp_path) as h:
            h.portal.set(org(BIN1, "ТОО \"Пример\"", {"code:1": "Трубы стальные"}))
            await h.text(10, "/start")
            assert "Как это работает" in h.last(10)
            start_markup = [p for n, p in h.tg.calls if n == "sendMessage"][-1]["reply_markup"]
            assert BTN_ADD in str(start_markup) and BTN_RUN in str(start_markup)

            await h.text(10, BTN_ADD)
            assert "Отправьте БИН/ИИН" in h.last(10)
            await h.text(10, f"{BIN1}, {BIN2}; 123456789012")
            texts = h.tg.texts(10)
            assert any("Добавлено в мониторинг: <b>2</b>" in t and "Некорректных значений: 1" in t for t in texts)
            report = h.last(10)
            assert "Результат проверки" in report and "В реестре: <b>1</b>" in report and "Нет в реестре: <b>1</b>" in report
            assert f"✅ <code>{BIN1}</code>" in report and f"❌ <code>{BIN2}</code>" in report

            await h.text(10, BTN_CHECK)
            await h.text(10, BIN1)
            assert "ТОО «Пример»" in h.last(10) and "уже в вашем мониторинге" in h.last(10)

            await h.text(10, BIN3)  # один БИН без режима — быстрая проверка
            assert "НЕТ В РЕЕСТРЕ" in h.last(10)
            await h.press(10, f"add:{BIN3}")
            assert "Добавлено в мониторинг" in h.last(10) and f"❌ <code>{BIN3}</code>" in h.last(10)

            await h.text(10, BTN_LIST)
            listing = h.last(10)
            assert "3 организации" in listing and "ТОО «Пример»" in listing
            assert f"✅ <code>{BIN1}</code>" in listing and f"❌ <code>{BIN3}</code>" in listing

            await h.text(10, BTN_REMOVE)
            await h.text(10, BIN2)
            assert f"{BIN2}</code> удалён" in h.last(10)
            await h.text(10, f"/remove {BIN2}")
            assert "отсутствует" in h.last(10)

            await h.text(10, BTN_RUN)
            assert "Проверка завершена" in h.last(10) and "Проверено: 2 из 2" in h.last(10)
            assert "Изменений с прошлой проверки нет" in h.last(10) and f"✅ <code>{BIN1}</code>" in h.last(10)

            await h.text(10, BTN_SETTINGS)
            assert "Автопроверка: ежедневно в <b>18:00</b>" in h.last(10) and "Организаций в мониторинге: <b>2</b>" in h.last(10)
            await h.press(10, "settings:time")
            await h.text(10, "9:30")
            assert "09:30" in h.last(10)
            await h.press(10, "settings:notify")
            assert "выключены" in h.last(10)

            # второй пользователь не видит список первого
            await h.text(20, "/list")
            assert "пуст" in h.last(20)
            await h.text(20, f"{BIN1} {BIN2}")  # несколько БИН без режима — добавление
            assert any("Добавлено в мониторинг: <b>2</b>" in t for t in h.tg.texts(20))
            assert [s.bin for s in h.store.list_subscriptions(10)] == [BIN1, BIN3]
    run(scenario())


def test_excel_upload(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["Компания", "БИН"])
    wb.active.append(["А", BIN1])
    wb.active.append(["Б", int(BIN2)])
    buf = io.BytesIO()
    wb.save(buf)

    async def scenario():
        async with Harness(tmp_path) as h:
            await h.document(10, "list.xlsx", buf.getvalue())
            assert any("Добавлено в мониторинг: <b>2</b>" in t for t in h.tg.texts(10))
            await h.document(10, "virus.exe", b"MZ")
            assert "Поддерживаются файлы Excel" in h.last(10)
    run(scenario())


def test_whitelist(tmp_path):
    async def scenario():
        async with Harness(tmp_path, allowed={10}) as h:
            await h.text(99, "/start")
            assert "нет доступа" in h.last(99) and "99" in h.last(99)
            await h.text(99, BIN1)
            assert h.store.get_user(99) is None
            await h.text(10, "/start")
            assert "Как это работает" in h.last(10)
    run(scenario())


def test_commands_with_args(tmp_path):
    async def scenario():
        async with Harness(tmp_path) as h:
            h.portal.set(org(BIN1))
            await h.text(10, f"/add {BIN1} {BIN2}")
            assert any("Добавлено в мониторинг: <b>2</b>" in t for t in h.tg.texts(10))
            await h.text(10, f"/check {BIN1}")
            assert "ЕСТЬ В РЕЕСТРЕ" in h.last(10)
            await h.text(10, "/check")
            assert "Проверка завершена" in h.last(10)
            await h.text(10, f"/remove {BIN1} {BIN2}")
            assert "Удалено из мониторинга: <b>2</b>" in h.last(10)
            await h.text(10, "привет")
            assert "Не нашёл в сообщении БИН" in h.last(10)
    run(scenario())
