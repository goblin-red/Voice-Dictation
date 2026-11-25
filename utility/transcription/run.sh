#!/bin/sh
# Python и настройки берём из установленного Voice.
DIR="$(cd "$(dirname "$0")/.." && pwd)"
. "$DIR/core/pyenv.sh"
exec "$PY" "$DIR/transcription/transcribe.py" "$@"
