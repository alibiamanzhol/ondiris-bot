import logging

from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                      ReplyKeyboardMarkup, Update)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from .bins import SUPPORTED_EXTENSIONS, UnsupportedFile, parse_document, parse_text
from .config import Config
from .messages import split_long
from .monitor import Monitor, PermanentSendError
from .portal import PortalClient
from .service import BotService, migrate_legacy_state
from .storage import Storage

log = logging.getLogger(__name__)

BTN_ADD = "➕ Добавить БИН"
BTN_CHECK = "🔎 Проверить БИН"
BTN_LIST = "📋 Мой список"
BTN_REMOVE = "➖ Удалить БИН"
BTN_RUN = "🔄 Проверить сейчас"
BTN_SETTINGS = "⚙️ Настройки"

MAIN_MENU = ReplyKeyboardMarkup(
    [[KeyboardButton(BTN_ADD), KeyboardButton(BTN_CHECK)],
     [KeyboardButton(BTN_LIST), KeyboardButton(BTN_REMOVE)],
     [KeyboardButton(BTN_RUN), KeyboardButton(BTN_SETTINGS)]],
    resize_keyboard=True,
    is_persistent=True,
)

WELCOME = (
    "👋 Я слежу за организациями в <b>Реестре казахстанских товаропроизводителей</b> (e-ondiris.gov.kz).\n\n"
    "<b>Как это работает</b>\n"
    "1. Вы добавляете БИН — я сразу проверяю каждый и показываю статус:\n"
    "   ✅ есть в реестре · ❌ нет в реестре\n"
    "2. Каждый день в {time} я проверяю весь ваш список.\n"
    "3. Пишу только если что-то изменилось: организация появилась в реестре, "
    "появились новые записи о товарах или записи удалены. Нет изменений — не беспокою.\n\n"
    "<b>Кнопки</b>\n"
    f"{BTN_ADD} — добавить в мониторинг (текстом, списком, Excel, CSV или PDF)\n"
    f"{BTN_CHECK} — посмотреть данные по одному БИН прямо сейчас\n"
    f"{BTN_LIST} — ваш список со статусами ✅/❌\n"
    f"{BTN_REMOVE} — убрать из мониторинга\n"
    f"{BTN_RUN} — проверить весь список сейчас и показать статус каждого\n"
    f"{BTN_SETTINGS} — время автопроверки и уведомления\n\n"
    "💡 Можно просто отправить БИН в чат: один — покажу данные, несколько — добавлю в мониторинг."
)

ADD_PROMPT = (
    "Отправьте БИН/ИИН организаций (12 цифр).\n\n"
    "Подойдёт любой вариант:\n"
    "• один БИН;\n"
    "• несколько — через пробел, запятую, точку с запятой или с новой строки;\n"
    "• таблица, скопированная из Excel или письма (лишний текст я пропущу);\n"
    "• файл Excel (.xlsx, .xls), .csv или PDF — БИН могут быть в любом месте файла.\n\n"
    "После добавления я сразу проверю каждый БИН и покажу статус ✅/❌."
)
CHECK_PROMPT = "Отправьте один БИН/ИИН — покажу, есть ли организация в реестре и какие у неё товары."
REMOVE_PROMPT = "Отправьте БИН, который нужно убрать из мониторинга. Можно несколько сразу."
TIME_PROMPT = "Отправьте время ежедневной проверки в формате ЧЧ:ММ, например <code>09:30</code>."

MODE_ADD, MODE_CHECK, MODE_REMOVE, MODE_TIME = "add", "check", "remove", "time"


def _svc(context: ContextTypes.DEFAULT_TYPE) -> BotService:
    return context.application.bot_data["service"]


def _cfg(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.application.bot_data["config"]


async def _allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user is None or update.effective_chat is None or update.effective_chat.type != ChatType.PRIVATE:
        return False
    allowed = _cfg(context).allowed_user_ids
    if allowed and user.id not in allowed:
        target = update.effective_message
        if target:
            await target.reply_text(
                f"⛔ У вас нет доступа к этому боту.\nВаш Telegram ID: <code>{user.id}</code> — "
                "передайте его администратору.",
                parse_mode=ParseMode.HTML,
            )
        elif update.callback_query:
            await update.callback_query.answer("Нет доступа", show_alert=True)
        return False
    _svc(context).store.upsert_user(user.id, update.effective_chat.id, user.username or user.full_name or "")
    return True


async def _reply(update: Update, text: str, markup=None) -> None:
    parts = split_long(text)
    for i, part in enumerate(parts):
        await update.effective_message.reply_text(
            part, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            reply_markup=markup if i == len(parts) - 1 else None,
        )


def _time_str(context) -> str:
    return _cfg(context).monitor_time.strftime("%H:%M")


# ---------- команды и кнопки ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    context.user_data.pop("mode", None)
    await _reply(update, WELCOME.format(time=_time_str(context)), MAIN_MENU)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    if context.args:
        await do_add(update, context, " ".join(context.args))
        return
    context.user_data["mode"] = MODE_ADD
    await _reply(update, ADD_PROMPT, MAIN_MENU)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/check — проверить весь список, /check БИН — показать данные по БИН."""
    if not await _allowed(update, context):
        return
    if context.args:
        await do_check(update, context, " ".join(context.args))
    else:
        await do_run(update, context)


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    if context.args:
        await _reply(update, _svc(context).remove(update.effective_user.id, " ".join(context.args)), MAIN_MENU)
        return
    context.user_data["mode"] = MODE_REMOVE
    await _reply(update, REMOVE_PROMPT, MAIN_MENU)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    context.user_data.pop("mode", None)
    await send_list(update, context, 0)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    context.user_data.pop("mode", None)
    await send_settings(update, context)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    context.user_data.pop("mode", None)
    await _reply(update, "Ок, отменено.", MAIN_MENU)


# ---------- действия ----------

async def do_add(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    parsed = parse_text(text)
    labels = _labels_from_text(text, parsed.valid)
    await _finish_add(update, context, parsed, labels)


async def _finish_add(update, context, parsed, labels=None) -> None:
    svc = _svc(context)
    user_id = update.effective_user.id
    outcome = svc.add(user_id, parsed, labels)
    text = outcome.text()
    if outcome.added or outcome.existed:
        text += "\n\n⏳ Проверяю статус в реестре…"
    await _reply(update, text, MAIN_MENU)
    if outcome.added or outcome.existed:
        chat_id = update.effective_chat.id

        async def baseline():
            try:
                report = await svc.baseline_report(user_id, outcome.added, outcome.existed)
                if report:
                    for part in split_long(report):
                        await context.bot.send_message(chat_id, part, parse_mode=ParseMode.HTML)
            except Exception:
                log.exception("[BASELINE] failed for user %s", user_id)

        context.application.create_task(baseline())


async def do_check(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    parsed = parse_text(text)
    if not parsed.valid:
        bad = f"\nНе распознано: <code>{', '.join(parsed.invalid[:5])}</code>" if parsed.invalid else ""
        await _reply(update, "❌ Не вижу корректного БИН/ИИН (12 цифр)." + bad, MAIN_MENU)
        return
    if len(parsed.valid) > 1:
        await _reply(update, "Для проверки отправьте один БИН. Несколько БИН можно добавить в мониторинг — "
                             f"нажмите «{BTN_ADD}».", MAIN_MENU)
        return
    await show_bin(update, context, parsed.valid[0])


async def show_bin(update: Update, context: ContextTypes.DEFAULT_TYPE, bin_: str) -> None:
    svc = _svc(context)
    await update.effective_chat.send_action("typing")
    text, ok = await svc.check_one(bin_)
    markup = MAIN_MENU
    if ok:
        if svc.store.has_bin(update.effective_user.id, bin_):
            text += "\n\n📌 Этот БИН уже в вашем мониторинге."
        else:
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Добавить этот БИН в мониторинг",
                                                                 callback_data=f"add:{bin_}")]])
    await _reply(update, text, markup)


async def do_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = _svc(context)
    user_id = update.effective_user.id
    if svc.store.count_bins(user_id):
        await _reply(update, f"🔄 Проверяю ваш список — {svc.store.count_bins(user_id)} БИН… Обычно это занимает до пары минут.")
    await _reply(update, await svc.run_manual(user_id), MAIN_MENU)


async def send_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int, edit: bool = False) -> None:
    text, page, pages = _svc(context).list_page(update.effective_user.id, page)
    markup = None
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Назад", callback_data=f"list:{page - 1}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"list:{page + 1}"))
        markup = InlineKeyboardMarkup([nav])
    if edit:
        try:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except BadRequest:
            pass
    else:
        await _reply(update, text, markup or MAIN_MENU)


def _settings_markup(context, user_id: int) -> InlineKeyboardMarkup:
    user = _svc(context).store.get_user(user_id)
    notify = user.notify if user else True
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🕒 Изменить время", callback_data="settings:time")],
        [InlineKeyboardButton("🔕 Выключить уведомления" if notify else "🔔 Включить уведомления",
                              callback_data="settings:notify")],
    ])


async def send_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False) -> None:
    user_id = update.effective_user.id
    text = _svc(context).settings_text(user_id)
    markup = _settings_markup(context, user_id)
    if edit:
        try:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except BadRequest:
            pass
    else:
        await _reply(update, text, markup)


# ---------- входящие сообщения ----------

MENU_ACTIONS = {
    BTN_ADD: (MODE_ADD, ADD_PROMPT),
    BTN_CHECK: (MODE_CHECK, CHECK_PROMPT),
    BTN_REMOVE: (MODE_REMOVE, REMOVE_PROMPT),
}


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    text = update.effective_message.text or ""
    if text in MENU_ACTIONS:
        mode, prompt = MENU_ACTIONS[text]
        context.user_data["mode"] = mode
        await _reply(update, prompt, MAIN_MENU)
        return
    if text == BTN_LIST:
        context.user_data.pop("mode", None)
        await send_list(update, context, 0)
        return
    if text == BTN_RUN:
        context.user_data.pop("mode", None)
        await do_run(update, context)
        return
    if text == BTN_SETTINGS:
        context.user_data.pop("mode", None)
        await send_settings(update, context)
        return

    mode = context.user_data.pop("mode", None)
    svc = _svc(context)
    user_id = update.effective_user.id
    if mode == MODE_ADD:
        await do_add(update, context, text)
    elif mode == MODE_CHECK:
        await do_check(update, context, text)
    elif mode == MODE_REMOVE:
        await _reply(update, svc.remove(user_id, text), MAIN_MENU)
    elif mode == MODE_TIME:
        await _reply(update, svc.set_time(user_id, text), MAIN_MENU)
    else:
        parsed = parse_text(text)
        if len(parsed.valid) == 1:
            await show_bin(update, context, parsed.valid[0])
        elif len(parsed.valid) > 1:
            await do_add(update, context, text)
        else:
            await _reply(update, "Не нашёл в сообщении БИН (12 цифр). Выберите действие на кнопках ниже 👇", MAIN_MENU)


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    doc = update.effective_message.document
    name = doc.file_name or ""
    if not name.lower().endswith(SUPPORTED_EXTENSIONS):
        await _reply(update, "Поддерживаются файлы Excel (.xlsx, .xls), .csv, .txt и PDF.", MAIN_MENU)
        return
    mode = context.user_data.pop("mode", None)
    await update.effective_chat.send_action("typing")
    try:
        data = bytes(await (await doc.get_file()).download_as_bytearray())
        parsed = parse_document(data, name)
    except UnsupportedFile as e:
        await _reply(update, f"❌ {e}", MAIN_MENU)
        return
    except Exception:
        log.exception("[FILE] cannot parse %s", name)
        await _reply(update, "❌ Не удалось прочитать файл. Проверьте, что он открывается и не защищён паролем.",
                     MAIN_MENU)
        return
    if mode == MODE_REMOVE:
        await _reply(update, _svc(context).remove(update.effective_user.id, " ".join(parsed.valid)), MAIN_MENU)
    else:
        await _finish_add(update, context, parsed)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _allowed(update, context):
        return
    data = query.data or ""
    svc = _svc(context)
    user_id = update.effective_user.id
    if data.startswith("add:"):
        await query.answer()
        text = await svc.add_checked(user_id, data[4:])
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        await _reply(update, text, MAIN_MENU)
    elif data.startswith("list:"):
        await query.answer()
        await send_list(update, context, int(data[5:]), edit=True)
    elif data == "settings:notify":
        svc.toggle_notify(user_id)
        await query.answer("Готово")
        await send_settings(update, context, edit=True)
    elif data == "settings:time":
        await query.answer()
        context.user_data["mode"] = MODE_TIME
        await _reply(update, TIME_PROMPT, MAIN_MENU)
    else:
        await query.answer()


def _labels_from_text(text: str, bins: list[str]) -> dict[str, str]:
    """Если в строке один БИН и есть текст — считаем текст названием (для таблиц «Компания | БИН»)."""
    labels = {}
    for line in text.splitlines():
        found = [b for b in bins if b in line or b.lstrip("0") in line]
        if len(found) != 1:
            continue
        rest = line.replace(found[0], " ").replace(found[0].lstrip("0"), " ")
        rest = " ".join(ch if not ch.isdigit() else " " for ch in rest)
        rest = " ".join(rest.split()).strip(" \t-–—:;,.|\"'«»()[]")
        if len(rest) >= 3:
            labels.setdefault(found[0], rest[:120])
    return labels


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Что-то пошло не так. Попробуйте ещё раз.")
        except Exception:
            pass


# ---------- сборка приложения ----------

async def _tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _svc(context).scheduled_tick()


def build_application(config: Config, portal: PortalClient | None = None, request=None) -> Application:
    store = Storage(config.db_path)
    migrate_legacy_state(store, config.legacy_state_file)
    portal = portal or PortalClient(config.portal_url, concurrency=config.portal_concurrency)

    builder = (ApplicationBuilder().token(config.token).concurrent_updates(True)
               .post_init(_post_init).post_shutdown(_post_shutdown))
    if request is not None:
        builder = builder.request(request).get_updates_request(request)
    app = builder.build()

    async def send(chat_id: int, text: str) -> None:
        try:
            await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Forbidden as e:
            raise PermanentSendError(str(e)) from e
        except BadRequest as e:
            if "chat not found" in str(e).lower():
                raise PermanentSendError(str(e)) from e
            raise

    monitor = Monitor(store, portal, config.tz, send)
    service = BotService(store, portal, monitor, config.tz, config.monitor_time)
    app.bot_data.update(config=config, service=service, store=store, portal=portal)

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(_tick, interval=60, first=15, name="monitor-tick",
                                job_kwargs={"max_instances": 1, "coalesce": True})
    return app


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
        BotCommand("add", "Добавить БИН в мониторинг"),
        BotCommand("check", "Проверить список сейчас / /check БИН — данные по БИН"),
        BotCommand("list", "Мой список"),
        BotCommand("remove", "Удалить БИН"),
        BotCommand("settings", "Настройки"),
        BotCommand("help", "Помощь"),
    ])
    cfg: Config = app.bot_data["config"]
    log.info("[BOT] Started. Daily check at %s %s, access: %s", cfg.monitor_time.strftime("%H:%M"), cfg.tz.key,
             f"{len(cfg.allowed_user_ids)} user(s)" if cfg.allowed_user_ids else "open")


async def _post_shutdown(app: Application) -> None:
    await app.bot_data["portal"].close()
    app.bot_data["store"].close()
