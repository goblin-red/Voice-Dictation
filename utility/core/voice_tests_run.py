"""Прогон базы тестов: запись -> детектор речи Voice -> распознаватель -> текст и команды (тем же кодом, что Voice)
-> сравнение с ожидаемым -> HTML-отчёт.

Запускает Voice (вкладка «Тесты» → «Запустить прогон»), чтобы у распознавателя Apple было разрешение приложения:
    python voice_tests_run.py '{"stt": ["whisper", …], "sources": ["me1", "silero", …], "kinds": ["phrase", …]}'
Прогресс — tests/corpus/results/progress.json, отчёт — tests/corpus/results/report.html (+ копия с датой).
"""

import difflib
import html
import json
import logging
import os
import re
import sys
import threading
import time
import types
from pathlib import Path

import numpy as np
import soundfile as sf

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))
os.chdir(APP)

import listener as L                      # noqa: E402
import app as core                        # noqa: E402
import voice_tests as VT                  # noqa: E402
import zipa_ipa                           # noqa: E402
from matcher import Matcher               # noqa: E402

RESULTS = VT.CORPUS / "results"
RESULTS.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=RESULTS / "run.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tests")

STT_NAMES = {"whisper": "Whisper", "gigaam": "GigaAM", "zipa": "ZIPA (phonetics)", "apple": "Apple",
             "grok": "Grok", "openai": "OpenAI (API key)", "live": "OpenAI (subscription)"}
KIND_NAMES = {"phrase": "Phrase", "phrase+command": "Phrase with command", "command": "Command",
              "paragraph": "Paragraph", "paragraph+command": "Paragraph with command"}


# ---------- настройки Voice (только читаем: config.json правит сам Voice)

def load_cfg():
    cfg = json.loads((APP.parent / "data" / "config.default.json").read_text(encoding="utf-8"))
    try:
        cfg.update(json.loads((APP.parent / "data" / "config.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


CFG = load_cfg()
MATCHER = Matcher(CFG)


def progress(**kw):
    (RESULTS / "progress.json").write_text(json.dumps(kw, ensure_ascii=False), encoding="utf-8")


# ---------- детектор речи: запись режется на фразы так же, как вживую

def split_phrases(audio):
    """Звук (float32, 16 кГц) -> фразы [(wav, samples)] — настоящий Listener на подменённом микрофоне."""
    phrases = []
    lis = L.Listener(dict(CFG["vad"]), lambda wav, long, spoken, final=True, sid=None:
                     phrases.append((wav, long)), log)
    lis.stream = None
    lis.dictation = True
    lis.mute_on_speech = False
    tail = np.zeros(int(2.0 * L.RATE), dtype=np.float32)          # тишина в конце — фраза закроется
    data = np.concatenate([audio.astype(np.float32), tail])

    class FakeInput:
        def __init__(self, *a, callback=None, **k):
            self.cb, self.active, self.stopped, self.fed = callback, True, False, False

        def start(self):
            if self.fed:
                return
            self.fed = True

            def feed():
                for i in range(0, len(data) - L.FRAME + 1, L.FRAME):
                    self.cb(data[i:i + L.FRAME].reshape(-1, 1), L.FRAME, None, None)
                while not lis._frames.empty():
                    time.sleep(0.01)
                time.sleep(0.2)
                lis.stop()
            threading.Thread(target=feed, daemon=True).start()

        def stop(self):
            pass

        def close(self):
            pass

    L.sd.InputStream = FakeInput
    L.audio_dev.defaults = lambda: ("in", "out")
    lis.run()
    return [(wav, np.frombuffer(wav[44:], dtype="<i2").astype(np.float32) / 32767) for wav, long in phrases if not long]


# ---------- распознаватели

def hints():
    ns = types.SimpleNamespace(cfg=CFG, HINT_WORDS=core.OrcaVoice.HINT_WORDS)
    ns.extra_hints = lambda: core.OrcaVoice.extra_hints(ns)
    return core.OrcaVoice.hint_words(ns, "words_ru")


def make_engines(names):
    """{имя: функция(wav, samples) -> текст}; не запустился — {имя: текст ошибки}."""
    engines, broken = {}, {}
    words = hints()
    for name in names:
        try:
            if name == "whisper":
                eng = core.WhisperServer(CFG, log)
                engines[name] = lambda wav, s, e=eng: e.transcribe(wav)
            elif name == "gigaam":
                eng = core.GigaAM(CFG, log)
                eng.load()
                engines[name] = lambda wav, s, e=eng: e.transcribe(wav)
            elif name == "zipa":
                eng = zipa_ipa.Zipa(CFG, log)
                eng.load()
                engines[name] = lambda wav, s, e=eng: e.transcribe(wav)
            elif name == "apple":
                eng = core.AppleSTT(CFG, log)
                eng.ensure("ru", CFG.get("apple_punctuation", True))
                eng.set_hints(words)

                def apple(wav, s, e=eng):
                    sid = e.start(s)
                    e.end()
                    return e.finish(sid, 8)
                engines[name] = apple
            elif name in ("grok", "openai"):
                eng = core.GrokSTT(CFG, log) if name == "grok" else core.OpenAISTT(CFG, log)
                eng._lang, eng._hints = "ru", words
                engines[name] = lambda wav, s, e=eng: e.transcribe(wav)
            else:
                broken[name] = "Not supported for recognition tests yet (live WebRTC session)"
        except Exception as e:
            broken[name] = str(e)
            log.warning("%s: %s", name, e)
    return engines, broken


# ---------- разбор фразы: как Voice при диктовке (мусор, текст, команды)

def parse(stt, text):
    """Распознанный текст фразы -> (напечатанный текст, [команды], мусор?)."""
    ns = types.SimpleNamespace(cfg=CFG, dictation=lambda: stt)
    if not text.strip():
        return "", [], False
    if core.OrcaVoice.is_junk(ns, text):
        return "", [], True
    if stt == "zipa":
        segs = zipa_ipa.segments(text, MATCHER, CFG.get("zipa_templates"))
    else:
        skip = (() if MATCHER.short(text) else MATCHER.LONG_SKIP) + MATCHER.mode_skip(text)
        segs = MATCHER.segments(text, skip=skip)
    typed = " ".join(v for k, v in segs if k == "text" and v)
    return typed, [k for k, v in segs if k != "text"], False


# ---------- сравнение

def words(text):
    text = text.lower().replace("ё", "е")
    return re.findall(r"[a-zа-я0-9]+", text)


def letters(text):
    return "".join(words(text))


def zipa_letters(ipa):
    return "".join(zipa_ipa._units(ipa.split()))


def accuracy(a, b):
    """Доля совпадения по Левенштейну: 1 — одинаково, 0 — ничего общего."""
    if not a and not b:
        return 1.0
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return max(0.0, 1 - prev[-1] / max(len(a), 1))


def score(stt, expect, typed):
    """Точность: по словам (ZIPA — по звукам, слов у неё нет)."""
    if stt == "zipa":
        return accuracy(list(letters(expect)), list(zipa_letters(typed)))
    return accuracy(words(expect), words(typed))


# ---------- прогон

def attempt(stt, fn, phrases, item):
    """Один прогон записи через распознаватель: что услышано, напечатано, какие команды, точность."""
    out = {"heard": [], "typed": "", "commands": [], "junk": 0, "error": "", "time": 0.0}
    typed = []
    for wav, samples in phrases:
        t0 = time.time()
        try:
            text = fn(wav, samples) or ""
        except Exception as e:
            out["error"] = str(e)
            text = ""
        out["time"] += time.time() - t0
        out["heard"].append(text)
        t, cmds, junk = parse(stt, text)
        out["junk"] += junk
        if t:
            typed.append(t)
        out["commands"] += cmds
    out["typed"] = " ".join(typed)
    out["acc"] = score(stt, item["expect"], out["typed"])
    out["cmd_ok"] = out["commands"] == item["commands"]
    return out


def variants(tries):
    """Разные итоги repeats: [{typed, commands, count}] — больше одного, значит распознаватель нестабилен."""
    seen = {}
    for t in tries:
        key = (t["typed"], tuple(t["commands"]))
        seen[key] = seen.get(key, 0) + 1
    return [{"typed": k[0], "commands": list(k[1]), "count": c} for k, c in seen.items()]


def run(opts):
    (RESULTS / "notes.html").unlink(missing_ok=True)    # выводы прошлого прогона к новому не относятся
    items = [i for i in VT.texts() if i["kind"] in opts["kinds"]]
    sources = [s for s in opts["sources"] if s in VT.sources()]
    engines, broken = make_engines(opts["stt"])
    jobs = [(it, src) for it in items for src in sources if (VT.AUDIO / src / f"{it['id']}.wav").is_file()]
    repeats = max(1, min(5, int(opts.get("repeats", 1))))
    total = len(jobs) * max(1, len(engines)) * repeats
    started = time.time()

    # 1. детектор речи: каждую запись режем на фразы один раз (общий для всех распознавателей)
    cut = {}
    for k, (it, src) in enumerate(jobs, 1):
        progress(done=0, total=total, now=f"Splitting into phrases {k} / {len(jobs)}")
        audio, _ = sf.read(VT.AUDIO / src / f"{it['id']}.wav", dtype="float32")
        cut[(it["id"], src)] = split_phrases(audio)
    mics = VT.recorded().get("mics", {})

    # 2. распознаватели — параллельно, каждый в своём потоке (внутри потока записи идут по очереди)
    rows, lock = [], threading.Lock()
    state = {"done": 0, "now": {}}

    def tick(stt, label):
        with lock:
            state["done"] += 1 if label is None else 0
            if label is not None:
                state["now"][stt] = label
            progress(done=state["done"], total=total, now=" | ".join(state["now"].values()))

    def worker(stt, fn):
        for it, src in jobs:
            phrases = cut[(it["id"], src)]
            tries = []
            for n in range(repeats):
                tick(stt, f"{stt}: {it['id']} · {src}" + (f" · {n + 1}" if repeats > 1 else ""))
                tries.append(attempt(stt, fn, phrases, it))
                tick(stt, None)
            # итог проверки: первый прогон + средняя точность и устойчивость по всем повторам
            row = {"id": it["id"], "source": src, "mic": mics.get(f"{src}/{it['id']}", ""), "stt": stt,
                   "phrases": len(phrases), **tries[0],
                   "acc": avg(t["acc"] for t in tries), "cmd_ok": all(t["cmd_ok"] for t in tries),
                   "cmd_rate": avg(t["cmd_ok"] for t in tries), "time": avg(t["time"] for t in tries),
                   "error": next((t["error"] for t in tries if t["error"]), ""), "repeats": repeats,
                   "variants": variants(tries)}
            with lock:
                rows.append(row)
            log.info("%s %s %s: %.0f%% %s", it["id"], src, stt, row["acc"] * 100, row["commands"])
        with lock:
            state["now"].pop(stt, None)

    threads = [threading.Thread(target=worker, args=(stt, fn), daemon=True) for stt, fn in engines.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # порядок строк — как в базе (тексты, голоса, распознаватели), а не как закончили потоки
    order = {name: i for i, name in enumerate(engines)}
    ids = {it["id"]: i for i, it in enumerate(items)}
    rows.sort(key=lambda r: (ids[r["id"]], sources.index(r["source"]), order[r["stt"]]))
    result = {"at": time.strftime("%Y-%m-%d %H:%M"), "seconds": round(time.time() - started), "repeats": repeats,
              "opts": opts, "items": items, "rows": rows, "broken": broken}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (RESULTS / f"run-{stamp}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    page = report(result)
    (RESULTS / "report.html").write_text(page, encoding="utf-8")
    (RESULTS / f"report-{stamp}.html").write_text(page, encoding="utf-8")
    progress(done=total, total=total, now="", finished=True, report=str(RESULTS / "report.html"))
    return result


# ---------- отчёт

def src_label(src, mic=""):
    if src.startswith("me"):
        return f"My voice, slot {src[2:]}" + (f" · {mic}" if mic else "")
    return {"silero": "Silero", "system": "macOS voice", "grok": "Grok", "openai": "OpenAI"}.get(src, src)


def pct(x):
    # почти идеально — с десятыми, чтобы 99,6 % не выглядело как 100 %
    return f"{x * 100:.1f}%" if 0.99 <= x < 1 else f"{x * 100:.0f}%"


def color(x):
    return "good" if x >= 0.95 else "mid" if x >= 0.8 else "bad"


def diff_html(expect, typed, stt):
    """Слова: пропущенные — красным зачёркнутым, лишние/неверные — жёлтым."""
    if stt == "zipa":
        return f"<span class=ipa>{html.escape(typed) or '—'}</span>"
    a, b = re.findall(r"\S+", expect), re.findall(r"\S+", typed)
    na = [w.lower().strip(".,!?:;«»\"").replace("ё", "е") for w in a]
    nb = [w.lower().strip(".,!?:;«»\"").replace("ё", "е") for w in b]
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, na, nb).get_opcodes():
        if op == "equal":
            out.append(html.escape(" ".join(b[j1:j2])))
        else:
            if i2 > i1:
                out.append(f"<del>{html.escape(' '.join(a[i1:i2]))}</del>")
            if j2 > j1:
                out.append(f"<ins>{html.escape(' '.join(b[j1:j2]))}</ins>")
    return " ".join(out) or "—"


def unstable_html(it, r):
    """Повторы дали разный итог — показать все варианты (сколько раз каждый)."""
    vs = r.get("variants") or []
    if len(vs) < 2:
        return ""
    lines = "".join(f"<li>{v['count']}×: {diff_html(it['expect'], v['typed'], r['stt'])} "
                    f"<span class=muted>({', '.join(v['commands']) or 'no commands'})</span></li>" for v in vs)
    return f"<div class=mid>Results varied between runs:</div><ul class=vars>{lines}</ul>"


def avg(vals):
    vals = list(vals)
    return sum(vals) / len(vals) if vals else 0.0


def report(res):
    rows, items = res["rows"], {i["id"]: i for i in res["items"]}
    stts = list(dict.fromkeys(r["stt"] for r in rows))
    srcs = list(dict.fromkeys(r["source"] for r in rows))
    mics = {r["source"]: r["mic"] for r in rows if r["mic"]}

    # сводка по распознавателям
    summary = []
    for stt in stts:
        rs = [r for r in rows if r["stt"] == stt]
        cmd_rs = [r for r in rs if items[r["id"]]["commands"]]
        n_phr = sum(r["phrases"] for r in rs)
        summary.append({
            "stt": stt, "acc": avg(r["acc"] for r in rs),
            "cmd": avg(r["cmd_ok"] for r in cmd_rs) if cmd_rs else None,
            "false_cmd": sum(1 for r in rs if not items[r["id"]]["commands"] and r["commands"]),
            "perfect": sum(1 for r in rs if r["acc"] >= 0.999), "n": len(rs),
            "time": sum(r["time"] for r in rs) / max(n_phr, 1), "errors": sum(1 for r in rs if r["error"]),
            "unstable": sum(1 for r in rs if len(r.get("variants") or []) > 1),
            "junk": sum(r["junk"] for r in rs)})
    summary.sort(key=lambda s: -s["acc"])

    def bar(x):
        return f"<div class=bar><i class={color(x)} style='width:{x * 100:.0f}%'></i><b>{pct(x)}</b></div>"

    h = []
    h.append(f"<h1>Recognition test report — {res['at']}</h1>")
    h.append(f"<p class=muted>Recordings: {len(set((r['id'], r['source']) for r in rows))} · engines: {len(stts)} · "
             f"checks: {len(rows)} · repeats: {res.get('repeats', 1)} · elapsed: {res['seconds']} s. Accuracy measures the share of correct words "
             f"(phonetic similarity for ZIPA). Commands count as correct when they match the expected commands.</p>")
    # кратко: самое главное — в начале
    text_stts = [s for s in summary if s["stt"] != "zipa"] or summary
    best, worst = text_stts[0], text_stts[-1]
    fast = min(summary, key=lambda s: s["time"])
    cmd_rows = [r for r in rows if items[r["id"]]["commands"]]
    cmd_ok = sum(r["cmd_ok"] for r in cmd_rows)
    cmd_bad = max(summary, key=lambda s: (1 - (s["cmd"] or 0)) if s["cmd"] is not None else -1)
    false = [s for s in summary if s["false_cmd"]]
    bad_n = sum(1 for r in rows if r["error"] or not r["cmd_ok"] or r["acc"] < 0.9)
    brief = [
        f"<b>Most accurate:</b> {STT_NAMES.get(best['stt'])} — {pct(best['acc'])}; least accurate: "
        f"{STT_NAMES.get(worst['stt'])} — {pct(worst['acc'])}.",
        f"<b>Fastest:</b> {STT_NAMES.get(fast['stt'])} — {fast['time']:.2f} s per phrase.",
        f"<b>Commands:</b> correct in {cmd_ok} of {len(cmd_rows)} cases"
        + (f"; most errors: {STT_NAMES.get(cmd_bad['stt'])} ({pct(cmd_bad['cmd'])} correct)." if cmd_bad["cmd"] is not None else "."),
        "<b>False commands:</b> " + (", ".join(f"{STT_NAMES.get(s['stt'])} — {s['false_cmd']}" for s in false) if false else "none") + ".",
        f"<b>Checks with issues:</b> {bad_n} of {len(rows)}. See Errors for details.",
    ]
    if res.get("repeats", 1) > 1:
        uns = [s for s in summary if s["unstable"]]
        brief.append(f"<b>Repeats ({res['repeats']}×):</b> " + (
            "varied: " + ", ".join(f"{STT_NAMES.get(s['stt'])} — {s['unstable']}" for s in uns) if uns
            else "all engines returned consistent results") + ".")
    h.append("<div class=brief><h2>Summary</h2><ul>" + "".join(f"<li>{b}</li>" for b in brief) + "</ul></div>")

    if res["broken"]:
        h.append("<div class=warn><b>Skipped:</b> " + "; ".join(
            f"{STT_NAMES.get(k, k)} — {html.escape(v)}" for k, v in res["broken"].items()) + "</div>")

    h.append("<h2>Results by engine</h2><table class=sum><tr><th>Engine</th><th>Text accuracy</th>"
             "<th>Perfect results</th><th>Correct commands</th><th>False commands</th><th>Noise ignored</th>"
             "<th>Time per phrase</th><th>Failures</th>" + ("<th>Inconsistent</th>" if res.get("repeats", 1) > 1 else "") + "</tr>")
    for s in summary:
        cmd = bar(s["cmd"]) if s["cmd"] is not None else "—"
        h.append(f"<tr><td><b>{STT_NAMES.get(s['stt'], s['stt'])}</b></td><td>{bar(s['acc'])}</td>"
                 f"<td>{s['perfect']} of {s['n']}</td><td>{cmd}</td>"
                 f"<td class={'bad' if s['false_cmd'] else ''}>{s['false_cmd']}</td><td>{s['junk']}</td>"
                 f"<td>{s['time']:.2f} s</td><td class={'bad' if s['errors'] else ''}>{s['errors']}</td>"
                 + (f"<td class={'mid' if s['unstable'] else 'good'}>{s['unstable']} of {s['n']}</td>" if res.get("repeats", 1) > 1 else "")
                 + "</tr>")
    h.append("</table>")

    # матрица распознаватель × голос
    h.append("<h2>Accuracy by engine and voice</h2><table class=sum><tr><th></th>"
             + "".join(f"<th>{html.escape(src_label(s, mics.get(s, '')))}</th>" for s in srcs) + "</tr>")
    for stt in stts:
        cells = []
        for src in srcs:
            rs = [r for r in rows if r["stt"] == stt and r["source"] == src]
            cells.append(f"<td class={color(avg(r['acc'] for r in rs))}>{pct(avg(r['acc'] for r in rs))}</td>" if rs else "<td>—</td>")
        h.append(f"<tr><td><b>{STT_NAMES.get(stt, stt)}</b></td>{''.join(cells)}</tr>")
    h.append("</table>")

    # по видам текстов
    h.append("<h2>Accuracy by text type</h2><table class=sum><tr><th></th>"
             + "".join(f"<th>{STT_NAMES.get(s, s)}</th>" for s in stts) + "</tr>")
    for kind in dict.fromkeys(i["kind"] for i in res["items"]):
        cells = []
        for stt in stts:
            rs = [r for r in rows if r["stt"] == stt and items[r["id"]]["kind"] == kind]
            cells.append(f"<td class={color(avg(r['acc'] for r in rs))}>{pct(avg(r['acc'] for r in rs))}</td>" if rs else "<td>—</td>")
        h.append(f"<tr><td>{KIND_NAMES.get(kind, kind)}</td>{''.join(cells)}</tr>")
    h.append("</table>")

    # ошибки
    bad = [r for r in rows if r["error"] or not r["cmd_ok"] or r["acc"] < 0.9]
    h.append(f"<h2>Errors ({len(bad)})</h2><p class=muted>Accuracy below 90%, an incorrect command, or a recognition failure.</p>"
             "<table class=det><tr><th>Text</th><th>Voice</th><th>Engine</th><th>Transcribed text</th>"
             "<th>Commands</th><th>Accuracy</th></tr>")
    for r in sorted(bad, key=lambda r: (r["stt"], r["acc"])):
        it = items[r["id"]]
        cmds = f"{', '.join(r['commands']) or '—'}" + ("" if r["cmd_ok"] else f" <span class=bad>(expected: {', '.join(it['commands']) or '—'})</span>")
        err = f"<div class=bad>failure: {html.escape(r['error'])}</div>" if r["error"] else ""
        h.append(f"<tr><td class=id>{r['id']}</td><td>{html.escape(src_label(r['source'], r['mic']))}</td>"
                 f"<td>{STT_NAMES.get(r['stt'], r['stt'])}</td><td>{diff_html(it['expect'], r['typed'], r['stt'])}{err}</td>"
                 f"<td>{cmds}</td><td class={color(r['acc'])}>{pct(r['acc'])}</td></tr>")
    h.append("</table>")

    # подробно по каждому тексту
    h.append("<h2>Results by text</h2><p class=muted><del>Struck out</del> words are missing. "
             "<ins>Underlined</ins> words are extra or incorrect. Phrases counts the segments detected in the recording.</p>")
    for iid, it in items.items():
        rs = [r for r in rows if r["id"] == iid]
        if not rs:
            continue
        h.append(f"<h3>{iid} · {KIND_NAMES.get(it['kind'], it['kind'])}</h3><p class=expect>Expected: {html.escape(it['expect']) or '<i>nothing</i>'}"
                 f" <span class=muted>· commands: {', '.join(it['commands']) or '—'}</span></p>")
        h.append("<table class=det><tr><th>Voice</th><th>Engine</th><th>Transcribed text</th><th>Commands</th>"
                 "<th>Phrases</th><th>Time</th><th>Accuracy</th></tr>")
        for r in sorted(rs, key=lambda r: (srcs.index(r["source"]), stts.index(r["stt"]))):
            ok = "✓" if r["cmd_ok"] else "✗"
            h.append(f"<tr><td>{html.escape(src_label(r['source'], r['mic']))}</td><td>{STT_NAMES.get(r['stt'], r['stt'])}</td>"
                     f"<td>{diff_html(it['expect'], r['typed'], r['stt'])}{unstable_html(it, r)}</td>"
                     f"<td class={'good' if r['cmd_ok'] else 'bad'}>{ok} {', '.join(r['commands']) or '—'}</td>"
                     f"<td>{r['phrases']}</td><td>{r['time']:.1f} s</td><td class={color(r['acc'])}>{pct(r['acc'])}</td></tr>")
        h.append("</table>")

    # выводы (пишутся после разбора результатов: notes.html рядом)
    notes = RESULTS / "notes.html"
    if notes.is_file():
        at = next(i for i, x in enumerate(h) if x.startswith("<h2>Results by engine"))
        h.insert(at, "<h2>Notes</h2><div class=notes>" + notes.read_text(encoding="utf-8") + "</div>")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Recognition test report</title>
<style>
  body {{ font: 14px/1.5 -apple-system, sans-serif; margin: 24px auto; max-width: 1200px; padding: 0 16px; background: #1b1c21; color: #e6e6e6; }}
  h1 {{ font-size: 22px; }} h2 {{ font-size: 18px; margin-top: 34px; border-bottom: 1px solid #33343c; padding-bottom: 6px; }}
  h3 {{ font-size: 15px; margin: 22px 0 4px; }}
  .muted {{ color: #9a9aa5; }} .expect {{ margin: 0 0 8px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 8px 0; }}
  th, td {{ border-bottom: 1px solid #2c2d34; padding: 6px 8px; text-align: left; vertical-align: top; }}
  th {{ color: #9a9aa5; font-weight: 500; }}
  .good {{ color: #5fcf98; }} .mid {{ color: #e8c25a; }} .bad {{ color: #ff7a7a; }}
  .bar {{ position: relative; background: #2a2b31; border-radius: 6px; height: 20px; min-width: 140px; }}
  .bar i {{ position: absolute; left: 0; top: 0; bottom: 0; border-radius: 6px; opacity: .35; }}
  .bar i.good {{ background: #5fcf98; }} .bar i.mid {{ background: #e8c25a; }} .bar i.bad {{ background: #ff7a7a; }}
  .bar b {{ position: relative; padding-left: 8px; font-weight: 600; }}
  del {{ color: #ff7a7a; }} ins {{ color: #e8c25a; text-decoration: underline; }}
  .ipa {{ font-family: ui-monospace, monospace; font-size: 12px; color: #c9a7ff; }}
  .id {{ color: #9a9aa5; }}
  .warn {{ background: #3a2f1c; border-radius: 8px; padding: 10px 14px; margin: 10px 0; }}
  .notes {{ background: #22303a; border-radius: 10px; padding: 6px 18px; }}
  .brief {{ background: #1f3a2c; border-radius: 10px; padding: 4px 18px 10px; font-size: 15px; }}
  .brief h2 {{ border: 0; margin-top: 12px; }} .brief li {{ margin: 5px 0; }}
  .notes li {{ margin: 6px 0; }}
  ul.vars {{ margin: 4px 0 0; padding-left: 18px; font-size: 13px; }}
</style></head><body>{''.join(h)}</body></html>"""


if __name__ == "__main__":
    opts = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    opts.setdefault("stt", [k for k, v in CFG.get("tests", {}).get("stt", {}).items() if v])
    opts.setdefault("sources", VT.sources())
    opts.setdefault("kinds", list(VT.KINDS))
    try:
        run(opts)
    except Exception as e:
        log.exception("Recognition test failed")
        progress(done=0, total=0, now="", finished=True, error=str(e))
        raise
