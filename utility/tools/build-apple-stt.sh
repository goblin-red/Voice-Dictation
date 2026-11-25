#!/bin/sh
# Собирает распознаватель Apple (диктовка «Apple»): tools/apple_stt.swift -> lib/bin/apple-stt
# Нужен swiftc (Xcode или Command Line Tools). Описание разрешения «Распознавание речи» вшито в программу.
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
# lib — как в core/pyenv.sh (настройка lib_dir; пусто — utility/lib, а если её нет — goblin-voice/lib)
. "$DIR/core/pyenv.sh"
OUT="${1:-$LIB/bin/apple-stt}"
PLIST="$(mktemp -t apple-stt).plist"
trap 'rm -f "$PLIST"' EXIT

cat > "$PLIST" <<'PL'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>local.orca-voice.apple-stt</string>
  <key>NSSpeechRecognitionUsageDescription</key><string>GOBL(in) Voice recognizes speech on this Mac for dictation.</string>
</dict></plist>
PL

mkdir -p "$(dirname "$OUT")"
swiftc -O "$DIR/tools/apple_stt.swift" -o "$OUT" \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$PLIST"
echo "apple-stt: $OUT"
