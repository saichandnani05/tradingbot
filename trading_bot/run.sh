#!/usr/bin/env bash
# Launcher for macOS / Linux.  First run creates a venv and installs deps.
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "==> First run: creating virtualenv .venv"
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing / updating dependencies"
pip install --quiet -r requirements.txt
# yfinance specifically needs regular bumps because Yahoo shifts its API.
pip install --quiet --upgrade yfinance

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
  cp .env.example .env
  echo "==> Created .env (paper mode by default)"
fi

echo "==> Launching dashboard — open the URL shown below in Chrome"
python app.py
