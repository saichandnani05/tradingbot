@echo off
REM Launcher for Windows.  First run creates a venv and installs deps.
cd /d "%~dp0"

if not exist ".venv" (
  echo ==^> First run: creating virtualenv .venv
  python -m venv .venv
)

call .venv\Scripts\activate.bat

echo ==^> Installing / updating dependencies
pip install --quiet -r requirements.txt
pip install --quiet --upgrade yfinance

if not exist ".env" if exist ".env.example" (
  copy .env.example .env >nul
  echo ==^> Created .env (paper mode by default^)
)

echo ==^> Launching dashboard - open the URL shown below in Chrome
python app.py
pause
