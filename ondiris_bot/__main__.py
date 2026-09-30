import argparse
import logging
import sys
from pathlib import Path

from .bins import parse_document
from .config import load_config


class _RedactToken(logging.Filter):
    def __init__(self, token: str):
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if self.token and self.token in record.getMessage():
            record.msg = record.getMessage().replace(self.token, "***")
            record.args = ()
        return True


def _setup_logging(token: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RedactToken(token))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[handler])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def cmd_import(args) -> None:
    from .storage import Storage

    config = load_config()
    path = Path(args.file)
    data = path.read_bytes()
    parsed = parse_document(data, path.name)
    store = Storage(config.db_path)
    store.upsert_user(args.user_id, args.user_id)
    added, existed = store.add_bins(args.user_id, parsed.valid, silent_baseline=not args.notify_existing)
    print(f"Пользователь {args.user_id}: добавлено {len(added)}, уже были {len(existed)}, "
          f"некорректных {len(parsed.invalid)}")
    store.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m ondiris_bot")
    sub = parser.add_subparsers(dest="command")
    imp = sub.add_parser("import", help="Импортировать список БИН из файла в список пользователя")
    imp.add_argument("user_id", type=int, help="Telegram user_id")
    imp.add_argument("file", help=".xlsx/.xls/.csv/.txt")
    imp.add_argument("--notify-existing", action="store_true",
                     help="сообщить об организациях, которые уже есть в реестре (по умолчанию — молча)")
    args = parser.parse_args()

    if args.command == "import":
        cmd_import(args)
        return

    config = load_config()
    if not config.token:
        sys.exit("Не задан TELEGRAM_BOT_TOKEN (см. .env.example)")
    _setup_logging(config.token)
    from .bot import build_application

    build_application(config).run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
