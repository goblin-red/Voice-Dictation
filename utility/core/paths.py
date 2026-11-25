"""Папки программы (раскладка с 2026-11-04): модули — utility/core/, всё остальное — от utility/.

CORE — эта папка (модули Python, pyenv.sh); ROOT — utility/ (рядом с «GOBL(in) Voice.app»).
"""
from pathlib import Path

CORE = Path(__file__).resolve().parent
ROOT = CORE.parent
DATA = ROOT / "data"                # config*.json, jonny_actions.json, state/, sounds/
LOGS = ROOT / "logs"                # журналы, stdout.log, app.pid, audio/, orch*, code-backups/
ARCHIVE = LOGS / "archive"          # журналы прошлых дней
UI = ROOT / "ui"
STATE = DATA / "state"
SOUNDS = DATA / "sounds"
TOOLS = ROOT / "tools"
TESTS = ROOT / "tests"
TRANSCRIPTION = ROOT / "transcription"
APP_BUNDLE = ROOT / "GOBL(in) Voice.app"
