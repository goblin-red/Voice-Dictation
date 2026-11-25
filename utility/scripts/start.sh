#!/bin/sh
# Запуск GOBL(in) Voice (откроется окно). Прошлые запуски утилита убивает сама.
# Python берётся из папки lib (настройка lib_dir в data/config.json), см. core/pyenv.sh.
DIR="$(cd "$(dirname "$0")/.." && pwd)"   # папка программы (utility/), скрипт лежит в scripts/
. "$DIR/core/pyenv.sh"

if [ ! -x "$PY" ]; then
  echo "No Python environment: neither $LIB/venv nor $DIR/.venv."
  echo "Set \"lib_dir\" in data/config.json to a folder with lib, or run:"
  echo "  python3 -m venv .venv && .venv/bin/pip install -r core/requirements.txt"
  exit 1
fi

mkdir -p "$DIR/logs"
cd "$DIR"
nohup "$PY" "$DIR/core/app.py" >> "$DIR/logs/stdout.log" 2>&1 &
echo "GOBL(in) Voice started (pid $!, python $PY)"
