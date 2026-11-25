# Находит Python для GOBL(in) Voice. Подключается из start.sh и из «GOBL(in) Voice.app»:  . "$DIR/core/pyenv.sh"
# Нужна переменная DIR (папка программы utility/, а не core/). Ставит LIB (папка lib) и PY (python из lib/venv).
#
# Путь к lib — настройка "lib_dir" в data/config.json (если его ещё нет — в data/config.default.json):
#   пусто       — utility/lib, а если её нет — на уровень выше (goblin-voice/lib)
#   /полный/путь или ~/путь — общая lib для нескольких копий Voice; другой путь — от utility/

CFG="$DIR/data/config.json"
[ -f "$CFG" ] || CFG="$DIR/data/config.default.json"

LIB=$(sed -n 's/^ *"lib_dir": *"\(.*\)", *$/\1/p; s/^ *"lib_dir": *"\(.*\)" *$/\1/p' "$CFG" 2>/dev/null | head -1)
case "$LIB" in
  "")   if [ -d "$DIR/lib" ]; then LIB="$DIR/lib"; else LIB="$(cd "$DIR/.." && pwd)/lib"; fi ;;
  "~"/*) LIB="$HOME/${LIB#\~/}" ;;
  /*)   ;;
  *)    LIB="$DIR/$LIB" ;;
esac

PY="$LIB/venv/bin/python"
# запасной вариант — окружение рядом с программой (так ставит install.sh)
[ -x "$PY" ] || PY="$DIR/.venv/bin/python"
