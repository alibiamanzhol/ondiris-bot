#!/usr/bin/env bash
# Запуск бота в Linux / Ubuntu (WSL): bash start.sh
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null; then
  echo "Python не найден. Установите: sudo apt update && sudo apt install -y python3 python3-venv"
  exit 1
fi

if [ ! -f .env ]; then
  read -r -p "Вставьте токен бота от @BotFather и нажмите Enter: " TOKEN
  TOKEN="$(echo "$TOKEN" | tr -d '[:space:]\"')"
  if [ -z "$TOKEN" ]; then
    echo "Токен пустой. Запустите ещё раз и вставьте токен (в терминале Ubuntu вставка — правой кнопкой мыши или Ctrl+Shift+V)."
    exit 1
  fi
  echo "TELEGRAM_BOT_TOKEN=$TOKEN" > .env
  echo "Токен сохранён в файл .env"
fi

if [ ! -x .venv/bin/python ]; then
  echo "Первый запуск: устанавливаю библиотеки, это займёт 1-2 минуты..."
  if ! python3 -m venv .venv; then
    rm -rf .venv
    echo "Не хватает модуля venv. Выполните: sudo apt update && sudo apt install -y python3-venv  и запустите снова."
    exit 1
  fi
fi
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt

echo
echo "Бот запущен. Не закрывайте это окно — пока оно открыто, бот работает. Остановить: Ctrl+C"
echo
exec .venv/bin/python -m ondiris_bot
