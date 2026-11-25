"""Audio journal layout: WAV, FLAC and time indexes in separate directories."""
from pathlib import Path


def prepare(folder):
    """Migrate legacy files before recording and archive workers start."""
    folder = Path(folder)
    for name in ("wav", "flac", "json"):
        (folder / name).mkdir(parents=True, exist_ok=True)
    for path in folder.glob("raw-*"):
        if not path.is_file():
            continue
        kind = ("wav" if path.name.endswith(".wav") else
                "flac" if path.name.endswith(".flac") else
                "json" if path.name.endswith((".jsonl", ".json.gz", ".json")) else None)
        if kind:
            target = folder / kind / path.name
            if target.exists():
                raise FileExistsError(f"Audio migration would overwrite {target}")
            path.rename(target)


def locate(folder, name, kind):
    """Read both the new layout and older saved diagnostic folders."""
    folder = Path(folder)
    path = folder / kind / name
    return path if path.exists() else folder / name
