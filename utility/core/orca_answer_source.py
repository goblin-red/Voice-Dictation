"""Resolve a provider Stop event to the live Orca pane owning its session."""

from contextlib import closing
from pathlib import Path
import re
import sqlite3
import subprocess

import orca_cli


IDENTITIES = Path.home() / "Library/Application Support/Orca/agent-hooks/agent-identities.db"
PROVIDER_AGENTS = {
    "codex": "codex",
    "claude": "claude",
    "grok": "grok",
    "agy": "antigravity",
    "antigravity": "antigravity",
}
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)


def _session_ids(agent, session, path):
    # Native UUIDs are case-insensitive. Leave any other provider key untouched.
    collation = " COLLATE NOCASE" if UUID.fullmatch(session) else ""
    query = (
        "SELECT orca_session_id, locator FROM provider_links "
        "WHERE execution_host = 'local' AND wsl_distro = '' "
        "AND agent = ? AND provider_session_id" + collation + " = ?"
    )
    with closing(sqlite3.connect(IDENTITIES.as_uri() + "?mode=ro", uri=True, timeout=.2)) as db:
        rows = db.execute(query, (agent, session)).fetchall()
    if isinstance(path, str) and path:
        exact = [session_id for session_id, locator in rows if locator == path]
        return set(exact or [session_id for session_id, locator in rows if not locator])
    return {session_id for session_id, _ in rows}


def resolve_orca_answer(agent, session, path=""):
    """Return the pane of an unambiguous native session on the local Orca host."""
    if not isinstance(agent, str) or not isinstance(session, str) or not session:
        return None
    provider = PROVIDER_AGENTS.get(agent.lower())
    if not provider:
        return None
    try:
        ids = _session_ids(provider, session, path)
    except (OSError, sqlite3.Error):
        return None
    if not ids:
        return None
    try:
        result = orca_cli._run(["worktree", "ps", "--environment", "local", "--limit", "1000"], timeout=2)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(result, dict) or result.get("truncated"):
        return None
    matches = {}
    for worktree in result.get("worktrees", ()):
        if worktree.get("hostId") != "local" or worktree.get("isArchived"):
            continue
        for entry in worktree.get("agents", ()):
            orca_id = entry.get("orcaSessionId")
            pane = entry.get("paneKey")
            if (orca_id in ids and isinstance(pane, str) and pane
                    and PROVIDER_AGENTS.get(entry.get("agentType")) == provider
                    and entry.get("hasLivePty") and not entry.get("parentPaneKey")):
                matches[(orca_id, pane)] = {
                    "paneKey": pane,
                    "worktreeId": worktree.get("worktreeId"),
                    "orcaSessionId": orca_id,
                }
    return next(iter(matches.values())) if len(matches) == 1 else None
