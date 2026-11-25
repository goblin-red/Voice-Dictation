"""Работа с Orca через его CLI: какая вкладка в фокусе, переключение вкладок, отправка клавиш, статусы агентов."""

import json
import shutil
import subprocess

from i18n import t

# команда orca: из PATH, а если её там нет — прямо из приложения Orca
ORCA = shutil.which("orca") or "/Applications/Orca.app/Contents/Resources/bin/orca"
ORCA_BUNDLE = "com.stablyai.orca"     # приложение Orca: выводим вперёд при переключении вкладки


def _run(args, timeout=10):
    """Запускает `orca ... --json` и возвращает поле result."""
    proc = subprocess.run(
        [ORCA, *args, "--json"],
        capture_output=True, text=True, timeout=timeout,
    )
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        raise RuntimeError(f"orca {' '.join(args)}: {t('err.orca_not_json')}: {proc.stdout[:200]} {proc.stderr[:200]}")
    if not data.get("ok"):
        raise RuntimeError(f"orca {' '.join(args)}: {data.get('error') or proc.stderr[:300]}")
    return data["result"]


def worktrees():
    """Все рабочие области Orca вместе с агентами (состояние, последний ответ)."""
    return _run(["worktree", "ps"]).get("worktrees", [])


# ---------- вкладка в фокусе

def _terminal_leaves(node, active_leaf=None):
    """Обходит дерево раскладки и отдаёт терминалы только из активных вкладок."""
    if isinstance(node, list):
        for item in node:
            yield from _terminal_leaves(item, active_leaf)
        return
    if not isinstance(node, dict):
        return

    if node.get("type") == "terminal":
        yield node, active_leaf
        return

    # группа вкладок: берём только активную вкладку
    if "tabs" in node:
        tab = next((t for t in node["tabs"] if t.get("tabId") == node.get("activeTabId")), None)
        if tab:
            yield from _terminal_leaves(tab.get("panes"), tab.get("activeLeafId"))
        return

    # сплит или другой контейнер: идём во все вложенные узлы
    for value in node.values():
        if isinstance(value, (dict, list)):
            yield from _terminal_leaves(value, active_leaf)


def _tabs(node):
    """Все вкладки раскладки по порядку (и активные, и нет)."""
    if isinstance(node, list):
        for item in node:
            yield from _tabs(item)
        return
    if not isinstance(node, dict):
        return

    if "tabs" in node:
        yield from node["tabs"]
        return

    for value in node.values():
        if isinstance(value, (dict, list)):
            yield from _tabs(value)


def _active_layout():
    """Активная рабочая область, её терминалы и раскладка: (active, listing, layout) или None."""
    active = next((w for w in worktrees() if w.get("isActive")), None)
    if not active:
        return None

    wt_id = active["worktreeId"]
    listing = _run(["terminal", "list", "--worktree", f"id:{wt_id}", "--include-visual-layouts"])
    layout = next((l for l in listing.get("visualLayouts", []) if l.get("worktreeId") == wt_id), None)
    if not layout:
        return None
    return active, listing, layout


def _focused_leaf(layout):
    """Панель терминала в фокусе или None."""
    leaves = list(_terminal_leaves(layout.get("root")))
    # активная панель вкладки важнее остальных панелей той же вкладки
    best = next((leaf for leaf, act in leaves if leaf.get("leafId") == act), None)
    if best is None and leaves:
        best = leaves[0][0]
    return best


def _describe(leaf, active, listing):
    """Панель -> {handle, paneKey, title, worktree, agent, cwd}."""
    info = next((t for t in listing.get("terminals", []) if t.get("handle") == leaf["handle"]), {})
    return {
        "handle": leaf["handle"],
        "paneKey": f"{leaf['tabId']}:{leaf['leafId']}",
        "title": leaf.get("title", ""),
        "worktree": active.get("displayName", ""),
        "worktreeId": active.get("worktreeId"),                 # для команды «Активный терминал»
        "agent": info.get("agentIdentity"),                     # claude / codex / opencode
        "cwd": info.get("worktreePath") or active.get("path"),  # папка проекта
        "ptyId": info.get("ptyId"),
        "incarnationId": info.get("incarnationId"),
        "executionHostId": info.get("executionHostId"),
    }


def focused_terminal():
    """Терминал в фокусе окна Orca: {handle, paneKey, title, worktree} или None."""
    found = _active_layout()
    if not found:
        return None
    active, listing, layout = found

    best = _focused_leaf(layout)
    if best is None:
        return None
    return _describe(best, active, listing)


# ---------- переключение вкладок

def next_agent_terminal(direction=1):
    """Следующая по кругу вкладка с агентом в активном проекте или None, если другой такой нет."""
    found = _active_layout()
    if not found:
        return None
    active, listing, layout = found

    agents = {t.get("handle") for t in listing.get("terminals", []) if t.get("agentIdentity")}
    current = _focused_leaf(layout)
    current_tab = current.get("tabId") if current else None

    tabs = list(_tabs(layout.get("root")))
    start = next((i for i, tab in enumerate(tabs) if tab.get("tabId") == current_tab), -1)

    # идём по кругу от текущей вкладки, обычные терминалы пропускаем
    for step in range(1, len(tabs) + 1):
        tab = tabs[(start + direction * step) % len(tabs)]
        if tab.get("tabId") == current_tab:
            continue
        leaves = [leaf for leaf, _ in _terminal_leaves(tab.get("panes")) if leaf.get("handle") in agents]
        if not leaves:
            continue
        # активная панель вкладки важнее остальных
        leaf = next((l for l in leaves if l.get("leafId") == tab.get("activeLeafId")), leaves[0])
        return _describe(leaf, active, listing)
    return None


def next_project_terminal():
    """Следующий по кругу проект (worktree) Orca — его активный терминал — или None, если другого нет.

    Порядок — как у списка проектов Orca (sortOrder). Проекты без живых терминалов пропускаем:
    переключиться можно только на терминал.
    """
    projects = [w for w in worktrees() if not w.get("isArchived")]
    projects.sort(key=lambda w: w.get("sortOrder") or 0, reverse=True)
    start = next((i for i, w in enumerate(projects) if w.get("isActive")), -1)
    for step in range(1, len(projects) + 1):
        wt = projects[(start + step) % len(projects)]
        if wt.get("isActive") or not wt.get("liveTerminalCount"):
            continue
        wt_id = wt["worktreeId"]
        listing = _run(["terminal", "list", "--worktree", f"id:{wt_id}", "--include-visual-layouts"])
        layout = next((l for l in listing.get("visualLayouts", []) if l.get("worktreeId") == wt_id), None)
        leaf = _focused_leaf(layout) if layout else None
        if leaf is None:
            continue
        return _describe(leaf, wt, listing)
    return None


def terminal_at(n):
    """Вкладка №n (с 1) активного проекта — по порядку вкладок раскладки — или None, если такой нет."""
    found = _active_layout()
    if not found:
        return None
    active, listing, layout = found
    tabs = list(_tabs(layout.get("root")))
    if not 1 <= n <= len(tabs):
        return None
    tab = tabs[n - 1]
    leaves = [leaf for leaf, _ in _terminal_leaves(tab.get("panes"))]
    if not leaves:
        return None
    leaf = next((l for l in leaves if l.get("leafId") == tab.get("activeLeafId")), leaves[0])
    return _describe(leaf, active, listing)


def find_terminal(handle, pane_key=None, worktree_id=None):
    """Живой терминал Orca по адресу (handle) — для команды «Активный терминал». Или None, если он закрыт.

    Адрес устарел (Orca перевыдала handle) — ищем ту же панель: та же рабочая область, вкладка и панель.
    Другую вкладку взамен не подбираем.
    """
    listing = _run(["terminal", "list", "--limit", "1000"])
    live = [t for t in listing.get("terminals", []) if not t.get("orphaned")]
    term = next((t for t in live if t.get("handle") == handle), None)
    if term is None and pane_key:
        tab, _, leaf = pane_key.partition(":")
        term = next((t for t in live if t.get("tabId") == tab and t.get("leafId") == leaf
                     and (not worktree_id or t.get("worktreeId") == worktree_id)), None)
    return term


def _input_terminal(row):
    """Адрес конкретной панели и её текущего PTY, без фрагментов вывода."""
    if (row.get("orphaned") or not row.get("connected", True) or not row.get("writable", True)
            or not all(row.get(key) for key in ("handle", "worktreeId", "tabId", "leafId"))):
        return None
    return {
        "handle": row["handle"],
        "paneKey": f"{row['tabId']}:{row['leafId']}",
        "worktreeId": row["worktreeId"],
        "title": row.get("title") or "",
        "worktree": row.get("branch") or row.get("worktreePath") or "",
        "cwd": row.get("worktreePath"),
        "agent": row.get("agentIdentity"),
        "ptyId": row.get("ptyId"),
        "incarnationId": row.get("incarnationId"),
        "executionHostId": row.get("executionHostId"),
    }


def list_input_terminals():
    """Доступные для печати панели Orca, включая обычные терминалы и сплиты."""
    listing = _run(["terminal", "list", "--limit", "1000"], timeout=3)
    terminals = [term for row in listing.get("terminals", []) if (term := _input_terminal(row))]
    if not terminals:
        return []
    try:
        snapshot = _run(["worktree", "ps", "--limit", "1000"], timeout=3)
        names = {worktree["worktreeId"]: worktree.get("displayName")
                 for worktree in snapshot.get("worktrees", []) if worktree.get("worktreeId")}
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return terminals
    for term in terminals:
        term["worktree"] = names.get(term["worktreeId"]) or term["worktree"]
    return terminals


def resolve_input_terminal(target):
    """Проверить закреплённую панель, сохраняя привязку к тому же PTY."""
    handle = target.get("handle")
    if not handle or not target.get("worktreeId") or not target.get("paneKey"):
        return None

    def matches(term):
        return term and all(
            term.get(key) == target[key]
            for key in ("worktreeId", "paneKey", "ptyId", "incarnationId", "executionHostId")
            if target.get(key)
        )

    try:
        result = _run(["terminal", "show", "--terminal", handle], timeout=2)
        term = _input_terminal(result.get("terminal") or {})
        if matches(term) and (term["handle"] == handle or target.get("incarnationId")):
            term["worktree"] = target.get("worktree") or term["worktree"]
            return term
    except (OSError, RuntimeError, subprocess.SubprocessError):
        pass

    # Старые версии CLI без идентификатора процесса позволяют проверить только исходный handle.
    if not target.get("incarnationId"):
        return None
    try:
        listing = _run(["terminal", "list", "--limit", "1000"], timeout=2)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None
    candidates = [term for row in listing.get("terminals", []) if matches(term := _input_terminal(row))]
    if len(candidates) != 1:
        return None
    term = candidates[0]
    term["worktree"] = target.get("worktree") or term["worktree"]
    return term


def switch_terminal(handle):
    """Выводит вкладку терминала на передний план — и само окно Orca, если впереди другое приложение."""
    result = _run(["terminal", "switch", "--terminal", handle])
    subprocess.run(["open", "-b", ORCA_BUNDLE], capture_output=True, timeout=5)
    return result


# ---------- клавиши

def send_space(handle):
    return _run(["terminal", "send", "--terminal", handle, "--text", " "])


def send_clear(handle):
    """Ctrl+U — стереть строку ввода (терминал, Claude Code, Codex)."""
    return _run(["terminal", "send", "--terminal", handle, "--text", "\x15"])


def send_enter(handle):
    return _run(["terminal", "send", "--terminal", handle, "--enter"])


def send_edit(handle, erase, text):
    """Диктовка «Apple»: стереть erase символов (Backspace) и допечатать text как есть (пробелы не трогаем)."""
    if erase or text:
        return _run(["terminal", "send", "--terminal", handle, "--text", "\x7f" * erase + text])


def send_text(handle, text, *, preserve_whitespace=False):
    """Печатает текст во ввод терминала без Enter."""
    if not preserve_whitespace:
        tail = " " if text[-1:].isspace() else ""   # пробел в конце нужен, чтобы фразы не слипались
        text = " ".join(text.split()) + tail        # без переводов строк — иначе агент отправит раньше времени
    return _run(["terminal", "send", "--terminal", handle, "--text", text])
