#!/bin/sh
# Остановка GOBL(in) Voice и его whisper-server
DIR="$(cd "$(dirname "$0")/.." && pwd)"   # папка программы (utility/), скрипт лежит в scripts/
cd "$DIR"

# только процессы Python, у которых скрипт — наш core/app.py
our_pids() {
  ps -axo pid=,command= | awk -v app="$DIR/core/app.py" 'tolower($2) ~ /python/ && $3 == app {print $1}'
}

PIDS=$(our_pids)
if [ -n "$PIDS" ]; then
  kill $PIDS 2>/dev/null

  # пока открыто окно, утилита может не заметить сигнал: ждём до 2 с и добиваем
  for i in 1 2 3 4 5 6 7 8; do
    [ -z "$(our_pids)" ] && break
    sleep 0.25
  done
  LEFT=$(our_pids)
  [ -n "$LEFT" ] && kill -9 $LEFT 2>/dev/null
fi
rm -f logs/app.pid

# порт Whisper — из своих настроек, а если их ещё нет — из настроек по умолчанию
CONFIG=data/config.json
[ -f "$CONFIG" ] || CONFIG=data/config.default.json
PORT=$(sed -n 's/^ *"whisper_port": *\([0-9]*\).*/\1/p' "$CONFIG" | head -1)   # без Python: его может не быть рядом
pkill -f "whisper-server.*--port $PORT" 2>/dev/null
echo "GOBL(in) Voice stopped"
