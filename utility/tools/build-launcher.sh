#!/bin/bash
# Собирает запускатель «GOBL(in) Voice.app» (tools/launcher.c) и подписывает приложение.
# Нужны инструменты командной строки Xcode (clang). Универсальный файл: Apple Silicon + Intel.
#
#   tools/build-launcher.sh
#
# После пересборки macOS считает приложение новым: разрешения «Микрофон» и
# «Универсальный доступ» нужно выдать заново.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/GOBL(in) Voice.app"
BUNDLE_ID="local.orca-voice"

clang -O2 -Wall -arch arm64 -arch x86_64 -mmacosx-version-min=13.0 \
  -o "$APP/Contents/MacOS/orca-voice" "$ROOT/tools/launcher.c"
chmod +x "$APP/Contents/Resources/orca-voice.sh"

# подпись «для себя» с нашим bundle id — по ней macOS и узнаёт приложение в списках разрешений
codesign --force --sign - --identifier "$BUNDLE_ID" "$APP"
touch "$APP"

echo "$APP"
echo "  $(lipo -archs "$APP/Contents/MacOS/orca-voice")"
codesign -dv "$APP" 2>&1 | grep -E "^Identifier"
