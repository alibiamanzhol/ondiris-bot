@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python не найден. Установите его с https://www.python.org/downloads/
  echo При установке обязательно отметьте галочку "Add python.exe to PATH".
  pause
  exit /b 1
)

if exist .env goto install
echo.
set /p TOKEN=Вставьте токен бота от @BotFather и нажмите Enter: 
>.env echo TELEGRAM_BOT_TOKEN=%TOKEN%
echo Токен сохранён в файл .env

:install
if not exist .venv\Scripts\python.exe (
  echo Первый запуск: устанавливаю библиотеки, это займёт 1-2 минуты...
  python -m venv .venv
)
.venv\Scripts\python -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo Не удалось установить библиотеки. Проверьте интернет и запустите ещё раз.
  pause
  exit /b 1
)

echo.
echo Бот запущен. Не закрывайте это окно - пока оно открыто, бот работает.
echo.
.venv\Scripts\python -m ondiris_bot
pause
