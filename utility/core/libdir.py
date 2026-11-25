"""Папка lib: свой Python (lib/venv), whisper-server (lib/bin) и модели Whisper (lib/models).

Путь — настройка lib_dir в data/config.json. Пусто — папка lib рядом с программой, а если её нет —
на уровень выше (раскладка репозитория: goblin-voice/utility + goblin-voice/lib).
Так несколько копий Voice в разных папках могут пользоваться одной lib без установки.
Тот же путь читают запускалки (start.sh, «GOBL(in) Voice.app») — см. pyenv.sh.
"""

from pathlib import Path

HERE = Path(__file__).resolve().parent     # utility/core
ROOT = HERE.parent                          # utility/ — от неё считаются lib и относительный lib_dir


def default_lib():
    """lib по умолчанию: utility/lib, если есть (установка install.sh), иначе utility/../lib (репозиторий)."""
    own = ROOT / "lib"
    return own if own.is_dir() else (ROOT.parent / "lib").resolve()


def lib_dir(cfg):
    """Папка lib по настройке lib_dir (абсолютный путь, ~/… или путь от папки программы utility/)."""
    value = str(cfg.get("lib_dir") or "").strip()
    if not value:
        return default_lib()
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def find_model(cfg, value):
    """Путь к модели: абсолютный — как есть; models/… — сначала в lib, потом рядом с программой (utility/)."""
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    for base in (lib_dir(cfg), ROOT):
        if (base / path).exists():
            return base / path
    return lib_dir(cfg) / path
