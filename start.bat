@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist .venv\Scripts\python.exe (
  echo Первый запуск: ставлю нужные библиотеки, это минута-две...
  py -m venv .venv 2>nul || python -m venv .venv
  .venv\Scripts\python.exe -m pip install -q -r requirements.txt
)
if not exist token.txt (
  echo Вставьте в Блокнот токен бота от @BotFather, сохраните и закройте Блокнот.
  type nul > token.txt
  notepad token.txt
)
.venv\Scripts\python.exe bot.py
pause
