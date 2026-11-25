#!/bin/sh
# Скрипт запуска: его стартует запускатель Contents/MacOS/orca-voice (tools/launcher.c). PATH из Finder пустой, поэтому задаём его сами (в том числе путь к команде orca)
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/Orca.app/Contents/Resources/bin:$PATH"
DIR="$(cd "$(dirname "$0")/../../.." && pwd)"   # utility/ (папка рядом с бандлом)
. "$DIR/core/pyenv.sh"     # PY — python из lib (настройка lib_dir) или из .venv рядом
mkdir -p "$DIR/logs"
cd "$DIR"
exec "$PY" "$DIR/core/app.py" >> "$DIR/logs/stdout.log" 2>&1
