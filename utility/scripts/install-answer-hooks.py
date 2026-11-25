#!/usr/bin/env python3
"""Install only Voice's passive hooks, preserving every other CLI setting."""
import json
from pathlib import Path
import shlex
import shutil
import time

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'utility/core/answer_hook.py'


def install():
    paths = {'codex': Path.home() / '.codex/hooks.json',
             'claude': Path.home() / '.claude/settings.json',
             'grok': Path.home() / '.grok/hooks/goblin-voice.json',
             'agy': Path.home() / '.gemini/config/hooks.json'}
    for agent, path in paths.items():
        data = json.loads(path.read_text()) if path.exists() else {}
        key = 'goblin-voice' if agent == 'agy' else 'hooks'
        groups = data.setdefault(key, {}).setdefault('Stop', [])
        handlers = groups if agent == 'agy' else [h for g in groups for h in g.get('hooks', [])]
        script = SCRIPT.with_name('agy_answer.py') if agent == 'agy' else SCRIPT
        command = '/usr/bin/python3 ' + shlex.quote(str(script))
        if agent != 'agy':
            command += ' ' + agent
        if any(h.get('command') == command for h in handlers):
            print(agent + ': already installed')
            continue
        if path.exists():
            backup = ROOT / '_backups/external-hooks' / agent / (
                path.name + '.bak-' + time.strftime('%Y%m%d-%H%M%S'))
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
        handler = {'type': 'command', 'command': command, 'timeout': 3}
        groups.append(handler if agent == 'agy' else {'hooks': [handler]})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        path.chmod(0o600)
        print(agent + ': installed; reload CLI and approve its hook/project trust if prompted')


if __name__ == '__main__':
    install()
