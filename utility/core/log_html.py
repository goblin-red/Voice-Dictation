"""Журнал в HTML: строки файла -> страница для браузера (время, цветная метка события, текст).

Оформление — как журнал на пульте окна Voice.
"""

import html
import json
import re
from datetime import datetime

STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})(?:,\d+)? (\w+) (.*)$")
SOURCE = re.compile(r"^\[([^\]]*)\] ")      # подробный журнал: [логгер · поток] перед сообщением

# начало сообщения -> вид события (цвет метки)
KINDS = [
    (re.compile(r"^((?:fallback|страховка) [\w-]+) (?:in|за) [0-9.]+ (?:s|с):\s*"), "heard"),
    (re.compile(r"^(услышано и напечатано|heard and typed)\s*"), "both"),
    (re.compile(r"^(услышано \(ввод\)|heard \(typing\)|OpenAI \(ввод\)|OpenAI \(typing\)|услышано|heard)[:\s]*"), "heard"),
    (re.compile(r"^(напечатано|typed)\s*"), "typed"),
    (re.compile(r"^(отправлено|sent)\s*"), "sent"),
    (re.compile(r"^(читаю ответ|reading the)\s*"), "voice"),
    (re.compile(r"^(мусор Whisper пропущен|Whisper noise skipped)[:\s]*"), ""),
    (re.compile(r"^(нечёткий слух|fuzzy ear)[:\s]*"), ""),
]
TARGET = re.compile(r" → ([^→]*)$")

# где произошло событие: значок слева от метки (первое совпадение по порядку)
WHERE = [
    ("📱", "phone", re.compile(r"планшет|телефон|android|phone|tablet", re.I)),
    ("🌐", "server (Goblin site)", re.compile(r"goblin|мост|/api/remote|bridge|сервер", re.I)),
    ("☁️", "cloud (OpenAI, Grok)", re.compile(r"openai|grok|xai|live:|aioice|звонок|облак|cloud", re.I)),
    ("💻", "Mac (terminal, microphone, speakers)", re.compile(
        r"напечатан|отправлено|терминал|terminal|applescript|клавиш|микрофон|динамик|детектор речи|"
        r"typed|sent |microphone|speaker|keys|afplay|say |macos|Apple:", re.I)),
]
APP = ("🧩", "Voice app")
WHO = {"me": ("🗣", "my phrases"), "agent": ("🤖", "agent answers")}   # значок, RU, EN


def where(msg, src=""):
    text = (src or "") + " " + msg
    for icon, title, rx in WHERE:
        if rx.search(text):
            return icon, title
    return APP
QUOTED = re.compile(r"(&quot;.*?&quot;|&#x27;.*?&#x27;|«[^»]*»)")

CSS = """
:root { --bg:#f4f4f6; --card:#fff; --text:#1c1c1f; --muted:#6b6b73; --line:#e2e2e7;
        --accent:#3a6df0; --accent-soft:#e6edff; --ok:#1f9d55; --err:#d64545; --warn:#c98f00; --chip:#eef0f4; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#17171a; --card:#222226; --text:#ececf0; --muted:#9a9aa3; --line:#34343a;
          --accent:#6b93ff; --accent-soft:#2a3350; --chip:#303036; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
header { position: sticky; top: 0; z-index: 1; display: flex; gap: 12px; align-items: center;
         padding: 12px 16px; background: var(--card); border-bottom: 1px solid var(--line); }
h1 { font-size: 16px; margin: 0; white-space: nowrap; }
button { font: inherit; font-size: 13px; padding: 6px 12px; border-radius: 8px; cursor: pointer;
         border: 1px solid var(--line); background: var(--bg); color: var(--text); white-space: nowrap; }
input { flex: 1; min-width: 0; font: inherit; padding: 6px 10px; border-radius: 8px;
        border: 1px solid var(--line); background: var(--bg); color: var(--text); }
main { max-width: 1100px; margin: 0 auto; padding: 12px 16px 40px; }
.day { margin: 18px 0 6px; font-size: 13px; font-weight: 600; color: var(--muted); }
.box { background: var(--card); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.row { display: grid; grid-template-columns: 70px 26px 170px 1fr; gap: 10px; align-items: baseline;
       padding: 6px 12px; border-bottom: 1px solid var(--line); word-break: break-word; }
.row:last-child { border-bottom: 0; }
.where { text-align: center; font-size: 15px; line-height: 1.2; cursor: default; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 14px; padding: 8px 16px; font-size: 12px; color: var(--muted);
          background: var(--card); border-bottom: 1px solid var(--line); }
.time { color: var(--muted); font: 12px ui-monospace, "SF Mono", Menlo, monospace; }
.kind { justify-self: start; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
        font-size: 11px; font-weight: 600; padding: 1px 8px; border-radius: 999px; background: var(--chip); color: var(--muted); }
.kind:empty { visibility: hidden; padding: 0; }
.msg { color: var(--muted); white-space: pre-wrap; }
.q { color: var(--text); font-weight: 500; }
.more summary { cursor: pointer; }
.more summary:hover { color: var(--text); }
.full { margin-top: 6px; padding: 8px 10px; border-left: 3px solid #0ea5a4; border-radius: 4px;
        background: var(--bg); color: var(--text); white-space: pre-wrap; }
.to { color: var(--accent); font-weight: 600; white-space: nowrap; }
.k-heard { background: var(--accent-soft); color: var(--accent); }
.k-both  { background: linear-gradient(90deg, var(--accent-soft), color-mix(in srgb, var(--ok) 22%, transparent)); color: var(--text); }
.k-typed { background: color-mix(in srgb, var(--ok) 16%, transparent); color: var(--ok); }
.k-sent  { background: color-mix(in srgb, #8b5cf6 18%, transparent); color: #8b5cf6; }
.k-voice { background: color-mix(in srgb, #0ea5a4 18%, transparent); color: #0ea5a4; }
.k-warn  { background: color-mix(in srgb, var(--warn) 20%, transparent); color: var(--warn); }
.k-err   { background: color-mix(in srgb, var(--err) 18%, transparent); color: var(--err); }
.row.warn .msg { color: var(--warn); }
.row.err .msg { color: var(--err); }
.src { font: 11px ui-monospace, "SF Mono", Menlo, monospace; color: var(--muted); opacity: .75; }
.row.dbg .msg { opacity: .7; }
.hide, .cut, .dayblock.empty { display: none; }
.legend .chip { font-size: 12px; padding: 2px 10px; border-radius: 999px; color: var(--text); background: var(--chip); }
.legend .chip.off { opacity: .45; text-decoration: line-through; }
.loadmore { display: block; margin: 16px auto 0; }
@media (max-width: 560px) { .row { grid-template-columns: 62px 26px 1fr; } .msg { grid-column: 1 / -1; } }
"""

# поиск по журналу: прячем строки без совпадения
SCRIPT = """
// «Раскрыть все ответы» / «Свернуть все»
const all = document.getElementById("all");
let open = false;
all.addEventListener("click", () => {
  open = !open;
  document.querySelectorAll("details.more").forEach((d) => (d.open = open));
  all.textContent = open ? all.dataset.close : all.dataset.open;
});
let logAudio = null, logAudioUrl = null;
document.addEventListener("click", async e => {
  const button = e.target.closest(".audio-play");
  if (!button) return;
  const result = button.nextElementSibling;
  if (logAudio) logAudio.pause();
  if (logAudioUrl) URL.revokeObjectURL(logAudioUrl);
  button.disabled = true; result.textContent = "…";
  try {
    const base = document.body.dataset.search;
    if (!base) throw new Error("Open the full log from Voice");
    const url = new URL(base); url.pathname = "/api/log/audio";
    url.searchParams.set("start", button.dataset.start); url.searchParams.set("end", button.dataset.end);
    const response = await fetch(url);
    if (!response.ok) throw new Error(response.status === 404 ? "Audio is missing or was deleted" : "Audio is unavailable. Reopen the log");
    logAudioUrl = URL.createObjectURL(await response.blob());
    logAudio = new Audio(logAudioUrl); logAudio.controls = true;
    result.replaceChildren(logAudio); await logAudio.play();
  } catch (err) { result.textContent = err.message || "Voice is unavailable"; }
  finally { button.disabled = false; }
});
const box = document.getElementById("q");
// в странице — только последние записи; «Догрузить ещё» берёт следующие у Voice (и прошлые дни из архива)
const more = document.getElementById("loadmore");
let loaded = +document.body.dataset.page || 0;
function show() {                          // мгновенный фильтр по загруженному (пока не ответил поиск по всему)
  const q = box.value.trim().toLowerCase();
  document.querySelectorAll("#local .row").forEach((r) => r.classList.toggle("hide", !!q && !r.textContent.toLowerCase().includes(q)));
  cut();
}
// значки «где произошло» (телефон, сервер, облако, Мак, Voice) и «мои фразы» / «ответы агентов»:
// выключенный — его строки скрыты; помнится в браузере
// по умолчанию видны только мои фразы и ответы агентов (остальные значки выключены)
let OFF = new Set(["📱", "🌐", "☁️", "💻", "🧩"]);
try { const saved = localStorage.getItem("voice-log-off2"); if (saved) OFF = new Set(JSON.parse(saved)); } catch (e) {}
function cut() {
  document.querySelectorAll("main .row").forEach((r) => {
    const w = r.querySelector(".where");
    r.classList.toggle("cut", (!!w && OFF.has(w.textContent)) || OFF.has(r.dataset.who));
  });
  document.querySelectorAll("main .dayblock").forEach((b) => b.classList.toggle("empty", !b.querySelector(".row:not(.hide):not(.cut)")));
  document.querySelectorAll(".legend .chip").forEach((c) => c.classList.toggle("off", OFF.has(c.dataset.key)));
}
document.querySelectorAll(".legend .chip").forEach((c) => c.addEventListener("click", () => {
  OFF.has(c.dataset.key) ? OFF.delete(c.dataset.key) : OFF.add(c.dataset.key);
  try { localStorage.setItem("voice-log-off2", JSON.stringify([...OFF])); } catch (e) {}
  cut();
}));
cut();
// поиск по всему журналу (все дни архива) — через Voice; не ответил — остаётся поиск по загруженному
const SEARCH = document.body.dataset.search || "";
const found = document.getElementById("found"), local = document.getElementById("local");
let timer = null, seq = 0;
function searchAll(q) {
  clearTimeout(timer);
  if (!SEARCH || q.length < 2) { found.hidden = true; local.hidden = false; seq++; return; }
  timer = setTimeout(async () => {
    const my = ++seq;
    try {
      const d = await (await fetch(SEARCH + "&q=" + encodeURIComponent(q))).json();
      if (my !== seq) return;
      found.innerHTML = `<div class="day">${d.title}</div>` + d.html;
      cut();
      found.hidden = false; local.hidden = true;
    } catch (e) { if (my === seq) { found.hidden = true; local.hidden = false; } }
  }, 300);
}
box.addEventListener("input", () => { show(); searchAll(box.value.trim()); });
more.addEventListener("click", async () => {
  more.disabled = true;
  try {
    const d = await (await fetch(SEARCH + "&offset=" + loaded)).json();
    more.insertAdjacentHTML("beforebegin", d.html);
    loaded += +document.body.dataset.page;
    more.hidden = !d.more;
    show();
  } catch (e) { more.textContent = more.dataset.off; }
  more.disabled = false;
});
"""

LABELS = {"warn": "warning", "err": "error"}


TYPED = re.compile(r"^(\S+ \S+ \w+ )(напечатано|typed) (.*)$")
HEARD = re.compile(r"^\S+ \S+ \w+ (?:услышано \(ввод\)|heard \(typing\)|OpenAI \(ввод\)|OpenAI \(typing\)): (.*)$")


def records(lines):
    """Строки журнала -> записи (многострочная запись, например traceback, — одним списком строк)."""
    out = []
    for line in lines:
        if STAMP.match(line) or not out:
            out.append([line])
        else:
            out[-1].append(line)
    return out


def newest_first(lines):
    """Журнал по порядку (свежие внизу) -> свежие сверху; многострочные записи (traceback) не разрываем."""
    return [line for rec in reversed(records(lines)) for line in rec]


def merge(lines):
    """«Услышано» и следом (строкой выше — свежие сверху) «напечатано» с тем же текстом — одной строкой."""
    out, i = [], 0
    while i < len(lines):
        typed = TYPED.match(lines[i])
        heard = typed and i + 1 < len(lines) and HEARD.match(lines[i + 1])
        if heard and (typed.group(3) == heard.group(1) or typed.group(3).startswith(heard.group(1) + " → ")):
            both = "heard and typed "
            out.append(typed.group(1) + both + typed.group(3))
            i += 2
        else:
            out.append(lines[i])
            i += 1
    return out


def _row(time_s, level, msg, en):
    msg = re.sub(r" \[zipa-source:.*\]$", "", msg)
    audio = re.search(r" \[audio:([0-9.]+):([0-9.]+)\]$", msg)
    if audio:
        msg = msg[:audio.start()]
    kind, label = "", ""
    src = SOURCE.match(msg)
    if src:                                         # подробный журнал: источник — мелко, перед текстом
        msg = msg[src.end():]
    icon, title = where(msg, src.group(1) if src else "")
    src_html = f'<span class="src">{html.escape(src.group(1))}</span> ' if src else ""
    for pattern, k in KINDS:
        hit = pattern.match(msg)
        if hit:
            kind, label, msg = k, hit.group(1), msg[hit.end():]
            break
    lvl = "err" if level in ("ERROR", "CRITICAL") else "warn" if level == "WARNING" else ""
    if lvl and not label:
        kind, label = lvl, LABELS[lvl]
    body = QUOTED.sub(r'<span class="q">\1</span>', html.escape(msg))
    cut = msg.find(" ¶ ") if kind == "voice" else -1
    if cut >= 0:                                    # ответ агента: раскрывается по клику
        try:
            full = json.loads(msg[cut + 3:])
        except ValueError:
            full = msg[cut + 3:]
        body = (f'<details class="more"><summary>{html.escape(msg[:cut])}</summary>'
                f'<div class="full">{html.escape(str(full))}</div></details>')
    if kind in ("typed", "both"):
        body = TARGET.sub(r' <span class="to">→ \1</span>', body)     # куда ушла фраза
    if audio:
        body += (f' <button class="audio-play" data-start="{audio.group(1)}" data-end="{audio.group(2)}">'
                 + "▶ Listen" + '</button><span class="audio-result"></span>')
    cls = f"kind k-{kind}" if kind else "kind"
    if level == "DEBUG":
        lvl = "dbg"
    who = "me" if kind in ("heard", "typed", "both") else "agent" if kind == "voice" else ""   # мои фразы / ответы агентов
    if who:                                         # у них свой значок вместо «где произошло»
        icon, title = WHO[who]
    return (f'<div class="row {lvl}" data-who="{who}"><span class="time">{time_s}</span>'
            f'<span class="where" title="{title}">{icon}</span>'
            f'<span class="{cls}">{html.escape(label)}</span><span class="msg">{src_html}{body}</span></div>')


def legend(en=False):
    """Подпись к значкам «где произошло» — она же переключатели: нажатый значок скрывает свои строки в ленте."""
    items = [(i, t) for i, t, _ in WHERE] + [APP]
    tip = "Show or hide in the feed"
    chips = [(i, i, t) for i, t in items]
    chips += [(k, i, title) for k, (i, title) in WHO.items()]
    return "".join(f'<button class="chip" data-key="{k}" title="{tip}">{i} {html.escape(t)}</button>' for k, i, t in chips)


def body(lines, en=False):
    """Строки журнала (свежие сверху) -> HTML записей, сгруппированных по дням."""
    parts, day, rows = [], None, []

    def flush():
        if rows:
            parts.append(f'<section class="dayblock"><div class="day">{day or ""}</div><div class="box">{"".join(rows)}</div></section>')
            rows.clear()

    # Keep the audio-enabled row when the same phrase also has a typing-stage row.
    audio_phrases = []
    for entry in lines:
        parsed = STAMP.match(entry)
        if parsed and "[audio:" in entry:
            message = SOURCE.sub("", parsed.group(4))
            hit = re.match(r"(?:heard|услышано): (.*) \[audio:", message)
            if hit:
                audio_phrases.append((parsed.group(1), parsed.group(2), hit.group(1)))
    filtered = []
    for entry in lines:
        parsed = STAMP.match(entry)
        if parsed:
            message = SOURCE.sub("", parsed.group(4))
            hit = re.match(r"(?:heard \(typing\)|услышано \(ввод\)): (.*)$", message)
            if hit and (parsed.group(1), parsed.group(2), hit.group(1)) in audio_phrases:
                continue
        filtered.append(entry)
    for line in merge(filtered):
        m = STAMP.match(line)
        if not m:
            if line.strip():
                rows.append(_row("", "", line, en))     # продолжение многострочной записи
            continue
        if m.group(1) != day:
            flush()
            day = m.group(1)
        message = m.group(4)
        # Older recordings already have phrase-duration events, but no explicit audio marker.
        old_audio = re.search(r"аудиодиагностика: фраза в распознавание ([0-9.]+) с", message)
        if old_audio and "[audio:" not in message:
            stamp = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d+)?)", line).group(1)
            end = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S,%f" if "," in stamp else "%Y-%m-%d %H:%M:%S").timestamp()
            message += f" [audio:{end-float(old_audio.group(1))-.4:.6f}:{end+.2:.6f}]"
        rows.append(_row(m.group(2), m.group(3), message, en))
    flush()
    return "".join(parts)


def render(lines, title, en=False, search_url="", page=0):
    """Строки журнала (свежие сверху) -> HTML-страница.
    search_url — поиск по всему журналу через Voice (все дни архива); Voice не запущен — ищем по загруженному.
    page — сколько записей в странице: есть search_url и page — внизу «Догрузить ещё» (следующие page записей у Voice)."""

    search = "Search…"
    expand, collapse = "Expand all answers", "Collapse all answers"
    more = "Load more"
    more_off = "Voice is not running"
    paged = bool(search_url and page)
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{CSS}</style></head><body data-search="{html.escape(search_url)}" data-page="{page}">'
            f'<header><h1>{html.escape(title)}</h1><input id="q" type="search" placeholder="{search}">'
            f'<button id="all" data-open="{expand}" data-close="{collapse}">{expand}</button></header>'
            f'<div class="legend">{legend(en)}</div>'
            f'<main><div id="found" hidden></div><div id="local">{body(lines, en)}'
            f'<button id="loadmore" class="loadmore" data-off="{more_off}"{"" if paged else " hidden"}>{more}</button></div></main>'
            f'<script>{SCRIPT}</script></body></html>\n')
