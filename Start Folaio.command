#!/bin/bash
# Folaio for Mac: double-click to start. The first time, it sets itself up.
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
  echo "Setting up Folaio for the first time. This takes a minute or two..."
  if ! command -v python3 >/dev/null 2>&1; then
    echo
    echo "Python 3 is needed. Download it from https://www.python.org/downloads/ , install it,"
    echo "then double-click 'Start Folaio' again."
    read -r -p "Press Enter to close."
    exit 1
  fi
  python3 -m venv .venv &&
    .venv/bin/python -m pip install --quiet --upgrade pip &&
    .venv/bin/python -m pip install --quiet -r requirements.txt || {
      rm -rf .venv
      echo
      echo "Setup didn't finish. Check your internet connection and try again."
      read -r -p "Press Enter to close."
      exit 1
    }
  echo "Done."
fi

echo "Starting Folaio... (keep this window open; close it to stop Folaio)"
.venv/bin/python run.py
