"""Мини-сервер на 127.0.0.1 для окна (ui.html): пульт, журнал и настройки."""

import json
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from pathlib import Path

import code_editor
import short_log
import audio_log_playback
import audio_out
import voice_tests
import i18n
from i18n import t
from libdir import lib_dir

HERE = Path(__file__).resolve().parent
from paths import ROOT, DATA, LOGS   # раскладка папок: core/paths.py
MODELS = ROOT / "models"          # модели рядом с программой (так их кладёт install.sh); основные — lib/models

# файлы окна: адрес -> (файл, тип)
PAGES = {
    "/": ("ui/ui.html", "text/html; charset=utf-8"),
    "/badge": ("ui/badge.html", "text/html; charset=utf-8"),               # плашка «слух» поверх всех окон
    "/ui_texts.js": ("ui/ui_texts.js", "application/javascript; charset=utf-8"),
    "/button_editor.js": ("ui/button_editor.js", "application/javascript; charset=utf-8"),
    "/devices.js": ("ui/devices.js", "application/javascript; charset=utf-8"),
    "/ui_ids.js": ("ui/ui_ids.js", "application/javascript; charset=utf-8"),
    "/ui_ids.json": ("ui/ui_ids.json", "application/json; charset=utf-8"),
    "/ui_short.js": ("ui/ui_short.js", "application/javascript; charset=utf-8"),   # короткие пояснения (полные — за «?»)
    "/goblin-ui.css": ("ui/goblin-ui.css", "text/css; charset=utf-8"),     # общие токены и компоненты Goblin UI (копия из goblin-ui/sync.sh)
    "/logo.svg": ("ui/logo.svg", "image/svg+xml"),
    "/brand-logo.svg": ("ui/brand-logo.svg", "image/svg+xml"),              # лого goblin dot red для шапки (светлая тема)
    "/brand-logo-light.svg": ("ui/brand-logo-light.svg", "image/svg+xml"),  # то же для тёмной темы
    "/jonny_actions.json": ("data/jonny_actions.json", "application/json; charset=utf-8"),   # вкладка «Действия»
    "/transcription/INSTRUCTIONS.md": ("transcription/INSTRUCTIONS.md", "text/plain; charset=utf-8"),
    "/transcription/BENCHMARKS.md": ("transcription/BENCHMARKS.md", "text/plain; charset=utf-8"),
}

MAX_BODY = 10 * 1024 * 1024     # тело POST больше — отказ (звук телефона идёт кусками по ~0,1–2 с)
KEY_MASK = "********"           # ключи API окну не отдаём: заданный ключ — маской, пустой — пустой строкой


def masked_config(cfg):
    """Настройки для окна: ключи API (…api_key) — маской. {настройки, какие ключи заданы}."""
    out, keys = dict(cfg), {}
    for k, v in cfg.items():
        if k.endswith("api_key"):
            keys[k] = bool((v or "").strip()) if isinstance(v, str) else bool(v)
            out[k] = KEY_MASK if keys[k] else ""
    return out, keys


def _json_type(value):
    return (value or "").split(";")[0].strip().lower() == "application/json"


def request_guard(method, path, headers, port):
    """Сервер окна только для своих (защита от чужих страниц в браузере: CSRF и DNS-rebinding).
    None — пускаем, иначе — причина отказа.

    Host — только 127.0.0.1 / localhost с нашим портом (чужое имя = страница с подменённым DNS).
    Origin, если он есть, — только наш адрес (браузер ставит его сам, страница подделать не может).
    Без Origin — не браузер (мост Goblin Remote, Сборщик ленты, curl): пускаем.
    POST — только JSON (простую форму или text/plain чужая страница шлёт без спроса) или со своим
    заголовком X-Voice-Code (его браузер без разрешения не пошлёт); звук телефона — application/octet-stream."""
    own = {f"127.0.0.1:{port}", f"localhost:{port}"}
    host = (headers.get("Host") or "").strip().lower()
    if host and host not in own:
        return "host " + host[:60]
    origin = (headers.get("Origin") or "").strip().rstrip("/").lower()
    if path.startswith(("/api/log/search?", "/api/log/audio?")):
        return None                  # страница журнала из файла (Origin: null) — вход по метке log_token
    mine = origin in {"http://" + h for h in own}
    if origin and not mine:
        return "origin " + origin[:60]
    if method == "POST" and not mine:
        ctype = headers.get("Content-Type")
        if path == "/api/remote/audio":
            if (ctype or "").split(";")[0].strip().lower() != "application/octet-stream":
                return "content-type " + (ctype or "—")[:40]
        elif not _json_type(ctype) and headers.get("X-Voice-Code") != "1":
            return "content-type " + (ctype or "—")[:40]
    return None


def list_models(cfg):
    """Все модели whisper.cpp (ggml-*.bin): lib/models, models рядом с программой и папки model_dirs."""
    # модели из lib и из своей папки — коротким путём models/…, как в настройках по умолчанию
    found = []
    for folder in (lib_dir(cfg) / "models", MODELS):
        found += [f"models/{p.name}" for p in sorted(folder.glob("ggml-*.bin")) if f"models/{p.name}" not in found]
    for folder in cfg.get("model_dirs", []):
        folder = Path(folder).expanduser()
        if folder.is_dir():
            found += [str(p) for p in sorted(folder.glob("ggml-*.bin"))]
    return found


def list_voices():
    """Голоса macOS: [{name, lang}]."""
    out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    voices = []
    for line in out.splitlines():
        m = re.match(r"^(.+?)\s{2,}([a-z]{2}_[A-Z]{2})", line)
        if m:
            voices.append({"name": m[1].strip(), "lang": m[2]})
    return voices


class SettingsServer:
    def __init__(self, app, port, log):
        self.app = app
        self.port = port
        self.log = log
        self._voices = None
        self._refused = {}       # причина отказа -> когда писали в журнал (не чаще раза в минуту)

    def log_tail(self, lines=60):
        """Свежие строки журнала (в файле они сверху) без шума — фраз без команд."""
        path = LOGS / "orca-voice.log"
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                head = [next(f, "") for _ in range(400)]
        except FileNotFoundError:
            return []
        rows = [r.rstrip("\n") for r in head if r.strip() and not r.rstrip().endswith("-> —")]
        return short_log.clean(rows)[:lines]

    def url(self):
        return f"http://127.0.0.1:{self.port}/"

    def refused(self, method, path, why):
        now = time.time()
        if now - self._refused.get(why, 0) >= 60:
            self._refused[why] = now
            self.log.warning("settings server: denied %s %s — %s (different page?)", method, path.split("?")[0][:60], why)

    def start(self):
        # порт может ещё держать только что убитая прошлая копия — пробуем до 10 с
        for attempt in range(40):
            try:
                server = ThreadingHTTPServer(("127.0.0.1", self.port), self._handler())
                break
            except OSError:
                if attempt == 39:
                    raise
                time.sleep(0.25)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.log.info(t("log.settings_url"), self.url())

    def _handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, body, content_type, code=200, cors=False):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                if cors:        # страница журнала открыта из файла (file://) — ей можно, вход по метке log_token
                    self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, data, code=200):
                self._send(json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", code)

            def _size(self):
                size = int(self.headers.get("Content-Length") or 0)
                if size < 0 or size > MAX_BODY:
                    raise ValueError(f"request body {size} bytes — more than {MAX_BODY}")
                return size

            def _body(self):
                return json.loads(self.rfile.read(self._size()) or b"{}")

            def _guard(self, method):
                """Чужой запрос — 403 и True (дальше не обрабатываем)."""
                why = request_guard(method, self.path, self.headers, owner.port)
                if why:
                    owner.refused(method, self.path, why)
                    self.close_connection = True       # тело не читали — соединение не переиспользуем
                    self._json({"ok": False, "error": "forbidden"}, 403)
                return bool(why)

            def _touch_goblin(self):
                # любой запрос моста Goblin Remote — признак, что утилита на Маке запущена и сцеплена с нами
                if self.path.startswith("/api/remote/"):
                    owner.app.goblin_seen = time.time()

            def do_GET(self):
                if self._guard("GET"):
                    return
                self._touch_goblin()
                try:
                    self._get()
                except (BrokenPipeError, ConnectionResetError):
                    pass                                 # окно / мост ушли, не дождавшись ответа
                except Exception as e:
                    owner.log.warning(t("log.settings_error"), e)
                    try:
                        self._json({"ok": False, "error": str(e)}, 400)
                    except Exception:
                        pass

            def _get(self):
                if self.path in PAGES:
                    name, content_type = PAGES[self.path]
                    body = (ROOT / name).read_text(encoding="utf-8")
                    if name == "ui/ui.html":
                        # язык — сразу в странице: окно открывается на нужном языке без мигания
                        body = body.replace('<html lang="en">', f'<html lang="{i18n.get_lang()}">', 1)
                    self._send(body.encode(), content_type)
                elif self.path.startswith("/api/log/audio?"):
                    qs = parse_qs(urlsplit(self.path).query)
                    if (qs.get("t") or [""])[0] != owner.app.log_token:
                        self._send(b"Forbidden", "text/plain", 403, cors=True)
                        return
                    try:
                        audio = audio_log_playback.clip(LOGS / "audio",
                            float(qs.get("start", [""])[0]), float(qs.get("end", [""])[0]))
                    except FileNotFoundError:
                        self._send("The audio was removed or is unavailable".encode(), "text/plain; charset=utf-8", 404, cors=True)
                    except ValueError:
                        self._send(b"Invalid interval", "text/plain", 400, cors=True)
                    else:
                        self._send(audio, "audio/wav", cors=True)
                elif self.path.startswith("/api/log/search?"):
                    # поиск по всему подробному журналу (сегодня + archive/) для страницы orca-voice-full.html
                    qs = parse_qs(urlsplit(self.path).query)
                    if (qs.get("t") or [""])[0] != owner.app.log_token:
                        self._send(b"{}", "application/json", 403, cors=True)
                    elif "offset" in qs:          # «Догрузить ещё»: следующая порция записей
                        data = owner.app.log_more(int((qs.get("offset") or ["0"])[0] or 0))
                    else:
                        data = owner.app.log_search((qs.get("q") or [""])[0])
                    if (qs.get("t") or [""])[0] == owner.app.log_token:
                        self._send(json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", cors=True)
                elif self.path == "/api/config":
                    if owner._voices is None:
                        owner._voices = list_voices()
                    config, keys = masked_config(owner.app.cfg)
                    self._json({
                        "config": config,                  # ключи API — маской (пустая / маска при сохранении = не менять)
                        "keys_set": keys,
                        "models": list_models(owner.app.cfg),
                        "voices": owner._voices,
                        "status": owner.app.status,
                        **owner.app.hints_info(),      # подсказки распознавателям (вкладка «Связка»)
                    })
                elif self.path.startswith("/api/code?"):
                    # код выполнения команды (функции из исходников)
                    cmd = (parse_qs(urlsplit(self.path).query).get("cmd") or [""])[0]
                    owner.log.debug("request: action code %s", cmd)
                    try:
                        self._json({"ok": True, "functions": code_editor.functions(cmd)})
                    except code_editor.CodeError as e:
                        self._json({"ok": False, "error": str(e)}, 400)
                elif self.path == "/api/sounds":
                    # «Сигналы»: библиотека звуков (свои + системные) и звуки по умолчанию
                    self._json(owner.app.signal_library())
                elif self.path == "/api/devices":
                    # «Звуковые устройства»: что есть сейчас (микрофоны и выходы)
                    self._json(audio_out.devices())
                elif self.path == "/api/ear":
                    # плашка «слух»: только то, что ей нужно (опрос 4 раза в секунду)
                    app = owner.app
                    lis = app.listener
                    self._json({"ear": app.ear_state(), "lang": i18n.get_lang(),
                                "level": round(lis.level, 4) if lis else 0,
                                "threshold": round(lis.threshold, 4) if lis else 0,
                                "label": bool(app.cfg.get("badge_label", True)),
                                # второй кружок: озвучка — говорит ли сейчас, чем и где звучит
                                "speaking": bool(app.speaker.busy()),
                                "reading": app.reading(),          # off — Озвучка «выкл» (блок «Звук»)
                                "sound_on": "phone" if app.phone.plays_here() else "mac",
                                "zipa": app.zipa.state if app.dictation() == "zipa" else "",
                                # мостик между кружками: защита от эха (commands) или перебивать можно (dictation)
                                "mic_speaking": app.mic_while_speaking(),
                                # микрофон глохнет прямо сейчас: play — играет любой звук, voice — говорит голос
                                "deaf": (lis.mute_reason or "voice") if lis and lis._muted else "",
                                # «Пауза голоса» (55-voice-pause): плашка и окно Goblin Remote (опрос раз в 2 с)
                                "paused": app.paused(), "paused_at": app.cfg.get("voice_paused_at") if app.paused() else None})
                elif self.path == "/api/read/state":
                    # чтение файла: что и где читаем (для страницы Сборщика)
                    self._json(owner.app.reader.info())
                elif self.path == "/api/read/items":
                    # список фрагментов для вкладки «Чтение» (окно берёт его, когда меняется items_ver)
                    reader = owner.app.reader
                    self._json({"ver": reader.items_ver, "items": reader.items})
                elif self.path == "/api/state":
                    self._json({**owner.app.snapshot(), "log": owner.log_tail()})
                elif self.path == "/api/input-focus":
                    self._json({"ok": True, **owner.app.input_focus_state()})
                elif self.path == "/api/orch/state":
                    # «Шлюз»: вкладки с агентами, диалоги, лента (окно и gbus list)
                    self._json(owner.app.terminal_voice_state())
                elif self.path == "/api/tests":
                    # вкладка «Тесты»: база текстов и что уже записано
                    playing = owner.app._test_play
                    proc = owner.app._test_run
                    self._json(voice_tests.info(owner.app.recorder, list(playing[:2]) if playing else None, owner.app.synth,
                                                bool(proc and proc.poll() is None)))
                elif self.path.startswith("/api/remote/out"):
                    # мост Goblin Remote забирает, что проиграть на tabletе (ждёт до wait секунд)
                    q = parse_qs(urlsplit(self.path).query)
                    after = int((q.get("after") or ["0"])[0])
                    wait = float((q.get("wait") or ["0"])[0])
                    self._json(owner.app.phone.take(after, wait))
                elif self.path == "/api/remote/info":
                    # для страницы tabletа: слова команд и язык (режим «Текст» распознаёт на tabletе)
                    cfg = owner.app.cfg
                    lang = cfg.get("ui_language") or i18n.get_lang()     # слова команд — языка окна
                    if owner._voices is None:
                        owner._voices = list_voices()
                    self._json({"commands": {k: {"enabled": v.get("enabled", True), "words": v.get("words_" + lang, [])}
                                             for k, v in cfg.get("commands", {}).items()},
                                "language": cfg.get("language", lang), "ui_language": lang,
                                # для настроек на tabletе: голос, скорость, сигналы, куда печатать, голоса языка окна
                                "voice": cfg.get("voice"), "rate": cfg.get("rate"), "sounds": cfg.get("sounds", True),
                                # режим «по слову-ключу» («дружище»): переключаем и с телефона
                                "keyword_mode": bool(cfg.get("keyword_mode", False)),
                                "phone_sounds": {k: owner.app.phone_sound(k) for k in ("sent", "text", "enter", "clear", "beep")},
                                "phone_sounds_set": cfg.get("phone_sounds") or {},
                                # факт с телефона: гарнитура подключена (по нему — эхо «авто» и «Streaming» без эхоподавления)
                                "phone_headphones": bool(cfg.get("phone_headphones", False)),
                                # блок «Звук» (sound, sound_now) — в snapshot ниже; озвучка телефона: авто / вручную
                                "phone_output": owner.app.sound_info()["phone_output"],
                                # микрофон телефона: авто (гарнитура, если подключена) или вручную
                                "phone_input": cfg.get("phone_input") or {"mode": "auto", "name": "headset"},
                                "phone_gain": cfg.get("phone_gain") or {"max": 12, "ratio": 4},
                                "voices": [v["name"] for v in owner._voices if v["lang"].startswith(lang)],
                                **owner.app.snapshot(),
                                # после snapshot: там «target» — подпись для пульта («Терминал / AppleScript»),
                                # а телефону нужно значение настройки (smart | orca | keys | applescript)
                                "target": cfg.get("target", "orca"),
                                # кнопки 1–5 на tabletе: номер терминала → имя агента в шлюзе (своё или выданное)
                                "terminals": {str(x["terminal_index"]): x.get("nick", "")
                                              for x in owner.app.orch.sessions()
                                              if x.get("terminal_index") and x.get("nick")}})
                else:
                    self._json({"error": t("err.not_found")}, 404)

            def _save_code(self):
                """Запись кода функции. Только из окна Voice: свой заголовок (с чужого сайта браузер его
                без разрешения не пошлёт) и Origin — наш адрес, если он есть."""
                origin = self.headers.get("Origin")
                if self.headers.get("X-Voice-Code") != "1" or (origin and origin.rstrip("/") != owner.url().rstrip("/")):
                    self._json({"ok": False, "error": "forbidden"}, 403)
                    return
                data = self._body()
                try:
                    result = code_editor.save(data.get("file", ""), data.get("name", ""),
                                              data.get("code", ""), data.get("hash", ""))
                except code_editor.CodeError as e:
                    self._json({"ok": False, "error": str(e)}, 400)
                    return
                owner.log.info("action code changed: %s %s (copy %s)", data.get("file"), data.get("name"), result["backup"])
                self._json({"ok": True, **result})

            def do_POST(self):
                if self._guard("POST"):
                    return
                self._touch_goblin()
                try:
                    if self.path == "/api/remote/audio":
                        # звук tabletа: PCM 16 бит, 16 кГц, моно
                        owner.app.phone.audio(self.rfile.read(self._size()))
                        self._json({"ok": True})
                        return
                    if self.path == "/api/code":
                        self._save_code()
                        return
                    data = self._body()
                    if self.path not in ("/api/remote/state", "/api/config", "/api/read/texts", "/api/read/update", "/api/read/jump"):   # тексты чтения не пишем в журнал
                        body = json.dumps(data, ensure_ascii=False)
                        owner.log.debug("request: POST %s %s", self.path, body if len(body) <= 300 else body[:300] + "…")
                    if self.path == "/api/remote/state":
                        owner.app.phone.update_channels(data.get("channels"))
                        # Браузер на Android не равен нативному Android-приложению.
                        diag = str(data.get("diag") or "")
                        owner.app.phone.input_diag = diag[:2000]
                        owner.app.phone.android_input = bool(re.search(r"(?:^| · )app=\d+\.", diag))
                        # proto ≥ 2 — страница знает блок «Звук» (её mic / reply — факт); мост может передать, если узнает
                        owner.app.phone.set_state(bool(data.get("active")), data.get("reply"), data.get("mode"), data.get("mic"),
                                                  data.get("proto"), data.get("mic_dev"), data.get("out_dev"))
                        self._json({"ok": True, "phone": owner.app.phone.active(),
                                    "channel_requests": owner.app.phone.pending_channels()})
                    elif self.path == "/api/remote/channel":
                        kind = owner.app.phone.finish_channel(data.get("id"), data.get("ok") is True)
                        if kind in ("mic", "speech"):
                            # Даже при phone → phone меняется физический источник/выход.
                            if kind == "mic" and owner.app.listener:
                                owner.app.listener.reset()
                            if kind == "speech":
                                owner.app.speaker.stop()
                                owner.app.watcher.clear_held()
                            owner.app.set_sound({kind: "phone"}, source="remote_device")
                        elif data.get("ok") is not True:
                            owner.log.warning("Could not switch the device: %s", data.get("error", "not connected"))
                        self._json({"ok": True})
                    elif self.path == "/api/remote/text":
                        owner.app.phone_text(data.get("text", ""))
                        self._json({"ok": True})
                    elif self.path == "/api/remote/control":
                        owner.app.phone_control(data)
                        self._json({"ok": True})
                    elif self.path == "/api/config":
                        owner.app.apply_config(data)
                        self._json({"ok": True})
                    elif self.path == "/api/answer":
                        # ответ своего агента (вкладка «Связка CLI»): curl -d '{"agent":"…","text":"…"}'
                        self._json({"ok": True, "read": owner.app.custom_answer(data)})
                    elif self.path in ("/api/read", "/api/read/texts", "/api/read/jump") and owner.app.paused():
                        # «Пауза голоса»: чтение вслух не начинаем (иначе оно шло бы молча по фрагментам)
                        self._json({"ok": False, "error": t("err.voice_paused")}, 409)
                    elif self.path == "/api/read":
                        # вкладка «Чтение»: {"source": путь или ссылка на HTML}
                        owner.app.reader.start(data.get("source", ""))
                        self._json({"ok": True})
                    elif self.path == "/api/read/texts":
                        # готовые тексты по порядку: {"items": [...], "title": "Лента"} (кнопка «Читать все» в Сборщике)
                        owner.app.reader.start_items(data.get("items") or [], data.get("title", ""), data.get("client_id", ""), data.get("index", 1))
                        self._json({"ok": True})
                    elif self.path == "/api/read/jump":
                        # клик по фрагменту в списке: {"index": номер с 1}
                        owner.app.reader.jump(data.get("index", 1), data.get("items"), data.get("client_id"))
                        self._json({"ok": True})
                    elif self.path == "/api/read/update":
                        changed = owner.app.reader.update_items(data.get("items"), data.get("client_id", ""))
                        self._json({"ok": True, "changed": changed})
                    elif self.path == "/api/preview_sound":
                        # прослушать звук сигнала на телефоне: {"name": "Purr", "volume": 0.3}
                        self._json(owner.app.preview_phone_sound(data.get("name") or "", data.get("volume", 0.3)))
                    elif self.path == "/api/test-voice":
                        self._json(owner.app.preview_voice(data))
                    elif self.path == "/api/tests/run":
                        # вкладка «Тесты»: запустить прогон по отмеченным галочкам
                        self._json(owner.app.test_run())
                    elif self.path == "/api/tests/report":
                        self._json(owner.app.test_report())
                    elif self.path == "/api/tests/synth":
                        # вкладка «Тесты»: {"voices": ["silero", …], "redo": false} — озвучить тексты базы
                        self._json(owner.app.synth.run(list(data.get("voices") or []), bool(data.get("redo"))))
                    elif self.path == "/api/tests/play":
                        # вкладка «Тесты»: {"voice": "me", "id": "p1"} — прослушать (повторно — остановить)
                        self._json(owner.app.test_play(str(data.get("voice") or ""), str(data.get("id") or "")))
                    elif self.path == "/api/tests/record":
                        # вкладка «Тесты»: {"id": "p01"} — начать запись, {"id": ""} — остановить и сохранить
                        self._json(owner.app.test_record(str(data.get("id") or ""), str(data.get("slot") or "me1")))
                    elif self.path == "/api/sounds/play":
                        # «Сигналы» → ▶: {"sound": "enter", "file": "<путь>", "volume": 0.8}
                        self._json(owner.app.signal_preview(str(data.get("sound") or ""), str(data.get("file") or ""),
                                                            data.get("volume", 1.0), str(data.get("where") or "")))
                    elif self.path == "/api/uilog":
                        # окно само пишет в журнал: ошибки JS и этапы отрисовки (пустое окно при старте)
                        owner.app.ui_log(str(data.get("kind") or ""), str(data.get("text") or "")[:300])
                        self._json({"ok": True})
                    elif self.path == "/api/sounds/open":
                        self._json(owner.app.signal_folder())
                    elif self.path == "/api/action":
                        owner.app.action(data.get("name", ""), data.get("value"))
                        self._json({"ok": True})
                    elif self.path == "/api/switch-app":
                        # переключатель «Voice | Remote | Сборщик» в шапке: вывести вперёд окно запущенной утилиты;
                        # не запущена — running: false, окно покажет её титульную страницу с «Запустить» (/api/launch)
                        if data.get("app") == "lenta":
                            up = owner.app.lenta_link()
                            self._json({"ok": up and owner.app.show_lenta(), "running": up})
                        else:
                            shown = owner.app.show_goblin()
                            self._json({"ok": shown, "running": shown})
                    elif self.path == "/api/launch":
                        # Шапка и карточки запуска передают идентификатор приложения из меню.
                        self._json(owner.app.launch_app(str(data.get("app") or "")))
                    elif self.path == "/api/lang":
                        owner.app.set_language(data.get("lang", ""))
                        self._json({"ok": True})
                    elif self.path == "/api/folds":
                        owner.app.set_folds(data.get("folds"))
                        self._json({"ok": True})
                    elif self.path == "/api/input-focus":
                        if "id" not in data:
                            raise ValueError("Choose a terminal or clear the input lock")
                        state = (owner.app.set_input_focus_name(data["id"], data["name"]) if "name" in data
                                 else owner.app.set_input_focus(data["id"]))
                        self._json({"ok": True, **state})
                    elif self.path.startswith("/api/orch/"):
                        # «Шлюз»: send / end — от gbus из вкладки агента; start / stop / pause — из окна
                        orch, what = owner.app.orch, self.path[len("/api/orch/"):]
                        if what == "voice":
                            self._json(owner.app.set_terminal_voice(data))
                        elif what == "conversation":
                            self._json(orch.conversation(str(data.get("channel", "")), int(data.get("after", 0)), data.get("limit", 10)))
                        elif what == "send":
                            self._json(orch.public_send(data))
                        elif what == "read":
                            self._json(orch.read(data))
                        elif what == "reply":
                            self._json(orch.reply(data))
                        elif what == "whoami":
                            self._json(orch.whoami(data))
                        elif what == "bind":
                            self._json(orch.bind(data))
                        elif what == "end":
                            self._json(orch.end(data))
                        elif what == "describe":
                            self._json(orch.describe(data))
                        elif what == "rename":
                            self._json(orch.rename(data))
                        elif what == "start":
                            self._json(orch.start_from_ui(str(data.get("a") or ""), str(data.get("b") or ""),
                                                          str(data.get("topic") or "")))
                        elif what == "stop":
                            self._json(orch.stop_thread(data.get("thread") or None))
                        elif what == "pause":
                            self._json(orch.set_paused(data.get("value")))
                        else:
                            self._json({"error": t("err.not_found")}, 404)
                    else:
                        self._json({"error": t("err.not_found")}, 404)
                except Exception as e:
                    owner.log.warning(t("log.settings_error"), e)
                    self._json({"ok": False, "error": str(e)}, 400)

        return Handler
