"""Код выполнения голосовых команд: показать и изменить настоящие функции Voice прямо из окна настроек.

Каждой команде сопоставлены функции, которые она выполняет (COMMAND_CODE). Правка пишется прямо в .py-файл:
  — меняется только эта функция, остальной файл (и чужие правки в нём) не трогаем;
  — новый код должен быть одной функцией с тем же именем, а весь файл после замены — компилироваться;
  — если функция на диске изменилась с момента открытия (hash), запись отклоняется;
  — прежняя версия файла сохраняется в logs/code-backups.
Изменения вступают в силу после перезапуска Voice.
"""

import ast
import hashlib
import os
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent      # папка модулей utility/core (тесты подменяют на копию)

# команда -> функции, которые она выполняет: (файл, имя функции или Класс.метод)
_KEYS = [("app.py", "OrcaVoice.fire"), ("app.py", "OrcaVoice._send_key"), ("app.py", "OrcaVoice._press")]
_SWITCH = [("app.py", "OrcaVoice.fire"), ("app.py", "OrcaVoice._switch_tab"), ("app.py", "OrcaVoice._switch_direct")]
_TAB = _SWITCH + [("orca_cli.py", "terminal_at"), ("orca_cli.py", "switch_terminal"), ("direct.py", "Direct.switch_to")]

COMMAND_CODE = {
    "space": _KEYS + [("orca_cli.py", "send_space"), ("direct.py", "Direct.send_key")],
    "double": _KEYS + [("orca_cli.py", "send_space"), ("direct.py", "Direct.send_key")],
    "enter": _KEYS + [("orca_cli.py", "send_enter"), ("direct.py", "Direct.send_key")],
    "enter2": _KEYS + [("orca_cli.py", "send_enter"), ("direct.py", "Direct.send_key")],
    "clear": _KEYS + [("orca_cli.py", "send_clear"), ("direct.py", "Direct.send_key")],
    "switch": _SWITCH + [("orca_cli.py", "next_agent_terminal"), ("orca_cli.py", "switch_terminal"),
                         ("direct.py", "Direct.switch")],
    "project": [("app.py", "OrcaVoice.fire"), ("app.py", "OrcaVoice._switch_project"),
                ("orca_cli.py", "next_project_terminal"), ("orca_cli.py", "switch_terminal")],
    "active": [("app.py", "OrcaVoice.fire"), ("app.py", "OrcaVoice._return_terminal"),
               ("last_terminal.py", "LastTerminal.restore"), ("last_terminal.py", "LastTerminal._restore_orca")],
    "stop": [("app.py", "OrcaVoice.fire"), ("speaker.py", "Speaker.stop")],
    **{f"tab{n}": _TAB for n in range(1, 6)},
    **{f"dict_{v}": [("app.py", "OrcaVoice.fire"), ("app.py", "OrcaVoice.mode_command"), ("app.py", "OrcaVoice.set_dictation")]
       for v in ("whisper", "gigaam", "openai", "apple", "off")},
    **{f"read_{v}": [("app.py", "OrcaVoice.fire"), ("app.py", "OrcaVoice.mode_command"), ("app.py", "OrcaVoice.set_reading")]
       for v in ("system", "openai", "silero", "off")},
    "mic_off": [("app.py", "OrcaVoice.fire"), ("app.py", "OrcaVoice.mode_command"), ("app.py", "OrcaVoice.set_mic")],
}
ALLOWED = {pair for pairs in COMMAND_CODE.values() for pair in pairs}     # писать можно только сюда


class CodeError(ValueError):
    """Понятная ошибка для окна (синтаксис, конфликт, не та функция)."""


def _digest(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]


def _locate(text, name):
    """Функция name (или Класс.метод) в тексте модуля → (первая строка, последняя строка) с 1, или None."""
    tree = ast.parse(text)
    *classes, func = name.split(".")
    body = tree.body
    for cls in classes:
        node = next((n for n in body if isinstance(n, ast.ClassDef) and n.name == cls), None)
        if node is None:
            return None
        body = node.body
    node = next((n for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func), None)
    if node is None:
        return None
    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
    return start, node.end_lineno


def _read(file, name):
    """(строки файла, начало, конец, отступ, код функции без отступа)."""
    path = ROOT / file
    text = path.read_text(encoding="utf-8")
    where = _locate(text, name)
    if where is None:
        raise CodeError(f"{file}: function {name} not found")
    start, end = where
    lines = text.splitlines(keepends=True)
    chunk = "".join(lines[start - 1:end])
    indent = chunk[:len(chunk) - len(chunk.lstrip(" \t"))]
    return lines, start, end, indent, textwrap.dedent(chunk).rstrip("\n") + "\n"


def functions(cmd):
    """Код выполнения команды: [{file, name, line, code, hash}]."""
    if cmd not in COMMAND_CODE:
        raise CodeError(f"No code for command {cmd}")
    out = []
    for file, name in COMMAND_CODE[cmd]:
        _, start, _, _, code = _read(file, name)
        out.append({"file": file, "name": name, "line": start, "code": code, "hash": _digest(code)})
    return out


def save(file, name, code, expected_hash):
    """Заменяет функцию в файле новым кодом. → {code, hash, line, backup}. Ошибки — CodeError."""
    if (file, name) not in ALLOWED:
        raise CodeError(f"{file}: {name} cannot be changed here")
    lines, start, end, indent, current = _read(file, name)
    if _digest(current) != expected_hash:
        raise CodeError("This function changed on disk. Close and reopen it")

    # новый код: ровно одна функция с тем же именем
    new = textwrap.dedent(code.replace("\r\n", "\n").replace("\t", "    ")).strip("\n") + "\n"
    try:
        tree = ast.parse(new)
    except SyntaxError as e:
        raise CodeError(f"Syntax error on line {e.lineno}: {e.msg}")
    func = name.split(".")[-1]
    if (len(tree.body) != 1 or not isinstance(tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef))
            or tree.body[0].name != func):
        raise CodeError(f"Expected one function: def {func}(…)")

    # вставляем с прежним отступом и проверяем, что весь файл компилируется
    body = "".join(indent + line if line.strip() else line for line in new.splitlines(keepends=True))
    text = "".join(lines[:start - 1]) + body + "".join(lines[end:])
    try:
        compile(text, file, "exec")
    except SyntaxError as e:
        raise CodeError(f"{file}: Replacement error on line {e.lineno}: {e.msg}")

    # копия прежнего файла, затем запись целиком (сначала во временный файл)
    path = ROOT / file
    backups = ROOT.parent / "logs" / "code-backups"
    backups.mkdir(parents=True, exist_ok=True)
    backup = backups / f"{path.stem}-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1000000:06d}{path.suffix}"
    backup.write_text("".join(lines), encoding="utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

    _, start, _, _, saved = _read(file, name)
    return {"code": saved, "hash": _digest(saved), "line": start, "backup": str(backup.relative_to(ROOT))}
