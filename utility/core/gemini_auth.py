"""Read-only agy account availability; never starts a voice or agent session."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

_lock = threading.Lock()
_busy = False
_checked = 0.0
_state = {"ok": None, "mode": "subscription", "detail": "Checking agy access…"}


def auth_info():
    global _busy
    with _lock:
        if not _busy and (not _checked or time.monotonic() - _checked > 300):
            _busy = True
            threading.Thread(target=_refresh, daemon=True).start()
        return dict(_state)


def _refresh():
    global _busy, _checked, _state
    result = {"ok": None, "mode": "subscription", "effective_mode": None}
    try:
        executable = shutil.which('agy') or str(Path.home() / '.local/bin/agy')
        if not Path(executable).is_file():
            result.update(ok=False, detail="Install agy and sign in with Google")
        else:
            settings_path = Path.home() / '.gemini/antigravity-cli/settings.json'
            settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
            if settings.get('modelProvider') not in (None, 'antigravity'):
                result.update(ok=False, detail="Configure agy to use a signed-in Google subscription for Voice")
            else:
                env = dict(os.environ)
                env['BROWSER'] = '/usr/bin/false'
                for key in ('GEMINI_API_KEY', 'GOOGLE_API_KEY', 'AGY_ADC_AUTH'):
                    env.pop(key, None)
                proc = subprocess.run([executable, 'models'], stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, timeout=20, env=env, cwd=Path.home())
                if proc.returncode == 0 and any(line.startswith('gemini-') for line in proc.stdout.splitlines()):
                    result.update(ok=True, effective_mode="subscription",
                                  detail="Signed in through agy. Gemini models are available. Voice is checked when you connect")
                else:
                    message = (proc.stdout + proc.stderr).lower()
                    login = any(word in message for word in ('unauthenticated', 'not logged in', 'login required', 'sign in'))
                    result.update(ok=False if login else None,
                                  detail="Sign in to Google through agy with an active subscription" if login
                                  else "Could not confirm agy access. Check your account and network")
    except Exception:
        result.update(detail="Could not check agy. Check your account and network")
    with _lock:
        _state, _checked, _busy = result, time.monotonic(), False
