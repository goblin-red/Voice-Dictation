"""Диктовка «ZIPA»: вместо слов — транскрипция, фонемы IPA через пробел.

Модель держит отдельный процесс (zipa_srv.py в окружении ZIPA). Команды («рекс», «удали фразу»)
ищутся по звучанию: фонемы переводятся в русские буквы и сравниваются со словами команд (matcher.Variant),
только в конце фразы — найденные фонемы в печать не идут.
"""

import json
import unicodedata
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
import command_pauses as pauses
import libdir

HERE = Path(__file__).resolve().parent

# IPA -> русские буквы (для поиска команд по звучанию; скелет согласных прощает неточности)
IPA_RU = {
    "p": "п", "b": "б", "t": "т", "d": "д", "k": "к", "g": "г", "ɡ": "г", "f": "ф", "v": "в", "s": "с", "z": "з",
    "ʂ": "ш", "ʃ": "ш", "ʐ": "ж", "ʒ": "ж", "x": "х", "ɣ": "г", "h": "х", "m": "м", "n": "н", "l": "л", "ɫ": "л",
    "r": "р", "ɾ": "р", "ɹ": "р", "j": "й", "ɕ": "щ", "tɕ": "ч", "ʨ": "ч", "ts": "ц", "ʦ": "ц", "tʃ": "ч",
    "a": "а", "ɑ": "а", "ʌ": "а", "ɐ": "а", "ə": "а", "æ": "э", "o": "о", "ɔ": "о", "u": "у", "ʊ": "у",
    "i": "и", "ɪ": "и", "ɨ": "ы", "e": "э", "ɛ": "е", "ɵ": "о", "y": "ю", "w": "у",
    # редкие и чужие звуки — к ближайшему русскому
    "ɭ": "л", "ʎ": "л", "ʋ": "в", "β": "в", "ɲ": "н", "ŋ": "н", "c": "к", "q": "к", "ɟ": "г", "ʁ": "р", "ʀ": "р",
    "ð": "з", "θ": "с", "ç": "х", "ɯ": "ы", "ɤ": "ы", "ø": "о", "œ": "о", "ɒ": "о", "ɜ": "э", "ɘ": "э", "ʉ": "у",
}


def sample_tokens(text):
    """Accept spaced or compact IPA samples; retain pause groups and diacritics."""
    result = []
    for chunk in str(text).lower().split():
        i = 0
        while i < len(chunk):
            ch = chunk[i]
            if ch in ",.":
                j = i + 1
                while j < len(chunk) and chunk[j] in ",.":
                    j += 1
                result.append(chunk[i:j]); i = j
                continue
            if ch in "ʲːˑ˞ʰʷˠˤ" or unicodedata.combining(ch):
                if result and not pauses.is_pause(result[-1]):
                    result[-1] += ch
                else:
                    result.append(ch)
                i += 1
                continue
            if result and result[-1].endswith(("͡", "͜")):
                result[-1] += ch
                i += 1
                continue
            unit = next((p for p in ("tɕ", "tʃ", "ts") if chunk.startswith(p, i)), ch)
            result.append(unit); i += len(unit)
    return result


def normalize_sample(text):
    return " ".join(sample_tokens(text))


def to_ru(phone):
    """Одна фонема IPA -> русские буквы (мягкость и долгота отбрасываются)."""
    base = phone.replace("ʲ", "").replace("ː", "").replace("ˑ", "").replace("˞", "").replace("͡", "")
    if base in IPA_RU:
        return IPA_RU[base]
    return "".join(IPA_RU.get(ch, "") for ch in base)


class Zipa:
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.proc = None
        self.state = "off"               # off / loading / ready / error — для окна Voice и плашки

    @property
    def port(self):
        return int(self.cfg.get("zipa_port", 8796))

    def _alive(self):
        try:
            with urlopen(f"http://127.0.0.1:{self.port}/health", timeout=1) as r:
                return r.status == 200
        except Exception:
            return False

    def load(self):
        """Поднять сервер ZIPA (модель ~1 ГБ грузится несколько секунд) и дождаться готовности."""
        if self._alive():
            self.state = "ready"
            return
        self.state = "loading"
        try:
            self._start()
            self.state = "ready"
        except Exception:
            self.state = "error"
            raise

    def _start(self):
        # ZIPA — в lib/zipa (модель и свой venv); настройка zipa_dir — другая папка
        zdir = Path(self.cfg["zipa_dir"]).expanduser() if self.cfg.get("zipa_dir") else libdir.lib_dir(self.cfg) / "zipa"
        model = self.cfg.get("zipa_model") or str(zdir / "models/zipa-large-ns-800k/model.onnx")
        tokens = str(Path(model).with_name("tokens.txt"))
        python = zdir / ".venv/bin/python"
        if not python.exists():
            raise RuntimeError(f"ZIPA не установлена: нет {python}")
        if self.proc is None or self.proc.poll() is not None:
            self.log.info("ZIPA: запускаю сервер модели (%s)", Path(model).parent.name)
            self.proc = subprocess.Popen([str(python), "-I", str(HERE / "zipa_srv.py"), str(zdir), model, tokens, str(self.port)],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(120):
            if self._alive():
                self.log.info("ZIPA: модель готова")
                return
            if self.proc.poll() is not None:
                raise RuntimeError("ZIPA: сервер модели не запустился")
            time.sleep(0.5)
        raise RuntimeError("ZIPA: модель не загрузилась за 60 с")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.proc = None
        self.state = "off"

    def transcribe(self, wav_bytes):
        """WAV -> фонемы IPA через пробел (пусто — не речь)."""
        self.load()
        req = Request(f"http://127.0.0.1:{self.port}/ipa", data=wav_bytes, method="POST",
                      headers={"Content-Type": "audio/wav"})
        with urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
        if "error" in data:
            raise RuntimeError(data["error"])
        self.log.debug("ZIPA: %d мс, %s", data.get("ms", 0), data.get("ipa", "")[:120])
        return data.get("ipa", "")


def has_command(ipa, matcher, name, templates=None, pause_before_s=float("inf")):
    """Есть ли команда где угодно в куске (для «заткнись» поверх голоса: кусок речи по 3 с, слово может быть в середине)."""
    phones = ipa.split()
    indexes = [i for i, p in enumerate(phones) if not pauses.is_pause(p) and to_ru(p)]
    ru = [to_ru(phones[i]) for i in indexes]
    units = list(ru)
    for s in (templates or {}).get(name, []):    # записанные образцы — по звукам подряд
        required, sound = pauses.prefix(s)
        L = len(_units(sample_tokens(sound)))
        for i in range(len(units)):
            available = pauses.before(phones, indexes[i], pause_before_s)
            if not pauses.permits(required, available):
                continue
            for n in range(max(1, L - 2), L + 3):
                if i + n <= len(units) and template_match(units[i:i + n], [s], available) is not None:
                    return True
    for v in matcher.variants.get(name, []):
        for i in range(len(ru)):
            if not pauses.permits(v.pause_before_s, pauses.before(phones, indexes[i], pause_before_s)):
                continue
            tail = ""
            for j in range(i, min(len(ru), i + 24)):
                tail += ru[j]
                if len(_forms(tail)[-1]) > len(v.sq) + 1:
                    break
                if variant_gap(v, tail) is not None:
                    return True
    return False


def _units(phones):
    """Фонемы -> звуки для сравнения (русские буквы: близкие звуки IPA совпадают, паузы не в счёт)."""
    return [u for u in (to_ru(p) for p in phones if not pauses.is_pause(p)) if u]


def _dist(a, b):
    """Расстояние Левенштейна по звукам (спискам)."""
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


DEVOICE = str.maketrans("бвгджз", "пфктшс")       # «зборщик» и «сборщик» — одно и то же на слух
RU_VOWELS = set("аеёиоуыэюяйь")


def _cons(units):
    """Только согласные, без звонкости: «р а т с» -> [р, т, с]."""
    return [u.translate(DEVOICE) for u in units if u and u not in RU_VOWELS]


def template_match(tail_units, samples, pause_before_s=0):
    """Хвост похож на записанный образец команды: доля отличий (0 — совпал) или None.
    Короткий образец (до 5 звуков): согласные — точно, гласные могут отличаться («r ɪ k s» = «r e k s», но не «r ɑ t s»)."""
    best = None
    for s in samples or []:
        required, sound = pauses.prefix(s)
        if not pauses.permits(required, pause_before_s):
            continue
        ref = _units(sample_tokens(sound))
        if not ref or abs(len(tail_units) - len(ref)) > 2:
            continue
        # согласные: у короткого образца — точно, у длинного — не больше одного отличия («с т в о т р и ч» ≠ «с т о п р и ч»)
        if _dist(_cons(tail_units), _cons(ref)) > (0 if len(ref) <= 5 else 1):
            continue
        d = _dist(tail_units, ref)
        if d <= max(1, len(ref) // 4):
            r = d / len(ref)
            best = r if best is None else min(best, r)
    return best


TAIL_CMDS = ("enter", "enter2", "clear", "space", "double", "stop")   # в конце любой фразы



# ZIPA пишет «ч» и «ц» двумя звуками («t ʂ», «t ɕ», «t s»), а слово команды — одной буквой:
# «p ʲ ɛ r ɪ k ɭ ʲ u t ʂ ɨ j t e r m i n a l» -> «периклутшыйтерминал» ≠ «переключи терминал» по длине.
AFFRICATES = (("тш", "ч"), ("тщ", "ч"), ("тс", "ц"))


def _forms(tail):
    """Хвост как есть и (если отличается) со слитыми «т ш» / «т щ» -> «ч», «т с» -> «ц». Последний — самый короткий."""
    norm = tail
    for a, b in AFFRICATES:
        norm = norm.replace(a, b)
    return (tail,) if norm == tail else (tail, norm)


def variant_fit(v, tail):
    """Хвост похож на слово команды v: (разница длины в буквах — 0 или 1, расстояние по буквам), иначе None."""
    from matcher import distance
    best = None
    for form in _forms(tail):
        gap = abs(len(form) - len(v.sq))
        if gap <= 1 and sounds_like(v, form):
            fit = (gap, distance(form, v.sq))
            best = fit if best is None else min(best, fit)
    return best


def variant_gap(v, tail):
    """Хвост похож на слово команды v: разница длины в буквах (0 или 1), иначе None."""
    fit = variant_fit(v, tail)
    return fit[0] if fit else None


def sounds_like(v, tail):
    """Слово команды похоже на хвост. Номерные команды («сборщик первое», «терминал два») Voice сравнивает строго —
    для фонем ZIPA ещё и по согласным без учёта звонкости (если в слове нет цифр)."""
    if v.match(tail):
        return True
    if not v.strict or any(c.isdigit() for c in v.ex) or len(v.skel) < 5:
        return False
    from matcher import skeleton
    return skeleton(tail).translate(DEVOICE) == v.skel.translate(DEVOICE)


def split_commands(ipa, matcher, templates=None, pause_before_s=float("inf")):
    """Команды по звучанию: [фонемы для печати], [команды по порядку].

    Фонемы переводим в русские буквы и с конца примеряем слова команд: «r ʲɛ k s» -> «рекс» -> enter.
    Enter, «удали фразу», пробел, «заткнись» — в конце любой фразы (можно подряд: «… удали фразу рекс»).
    Остальные (переключение терминала / проекта, режимы, «сборщик первое…») — только если вся фраза и есть команда:
    у ZIPA нет границ слов, и короткие команды иначе срабатывали бы внутри обычной речи."""
    phones = ipa.split()
    found = []
    while phones:
        # хвост примеряем к каждому слову команды: длина хвоста в буквах — как у слова (±1),
        # из совпавших берём самое близкое по длине — «рекс» не съест «к с», «удали фразу» не съест слово перед ней
        best = None
        tail = ""
        for n in range(1, min(24, len(phones)) + 1):
            tail = to_ru(phones[-n]) + tail
            units = _units(phones[-n:])
            available = pauses.before(phones, len(phones) - n, pause_before_s)
            for name, variants in matcher.variants.items():
                if name not in TAIL_CMDS and (found or n != len(phones)):
                    continue                     # «целая фраза»: команда должна занять всё сказанное
                # записанный голосом образец ZIPA — главнее слов (ранг 0)
                r = template_match(units, (templates or {}).get(name), available)
                if r is not None:
                    score = (0, r, -n)
                    if best is None or score < best[0]:
                        best = (score, n, matcher.ALIASES.get(name, name))
                for v in variants:
                    gap = variant_gap(v, tail) if pauses.permits(v.pause_before_s, available) else None
                    if gap is not None:
                        score = (1, gap, -n)
                        if best is None or score < best[0]:
                            best = (score, n, matcher.ALIASES.get(name, name))
        if not best:
            break
        found.insert(0, best[2])
        phones = phones[:-best[1]]
        while phones and pauses.is_pause(phones[-1]):
            phones.pop()                         # пауза перед командой — не текст
    # конец фразы — точка (паузы внутри уже расставил сервер)
    if phones and not pauses.is_pause(phones[-1]):
        phones.append(".")
    elif phones and phones[-1] == ",":
        phones[-1] = "."
    return " ".join(phones), found


ANYWHERE_CMDS = ("clear", "stop", "enter", "enter2")   # в любом месте фразы (как в обычной диктовке)
PREFIX_CMDS = ("jonny", "amigo", "mic_on", "wake")   # только в начале фразы («дружище …» будит диктовку)
SAMPLES_ONLY = ("enter", "enter2")   # в середине фразы — только по записанным образцам: короткие слова («экс», «скс») ловятся ложно


def _best_at(phones, i, matcher, templates, names, pause_before_s=float("inf")):
    """Команда, начинающаяся с фонемы i: (сколько фонем, имя) или None — самая близкая по длине.
    Кусок, который кончается на паузе (или в конце фразы), главнее: «d ʐ o n i t e r m i n a l , p r o …» —
    команда «джони терминал», а не «джони терминал п» (иначе первый звук следующего слова пропадал)."""
    best, tail = None, ""
    for n in range(1, min(24, len(phones) - i) + 1):
        if pauses.is_pause(phones[i + n - 1]):
            continue
        tail += to_ru(phones[i + n - 1])
        edge = 0 if _pause(phones, i + n) else 1
        units = _units(phones[i:i + n])
        for name in names:
            samples = (templates or {}).get(name)
            if name in SAMPLES_ONLY:             # Enter в середине: только точное совпадение с образцом от 4 звуков
                samples = [x for x in samples or [] if len(_units(sample_tokens(x))) >= 4 and len(_units(sample_tokens(x))) == len(units)
                           and _cons(_units(sample_tokens(x))) == _cons(units)]   # гласные — любые («r ɛ k s» = «r e k s»)
            available = pauses.before(phones, i, pause_before_s)
            r = template_match(units, samples, available)
            if r is not None:
                score = (edge, 0, r, -n)
                if best is None or score < best[0]:
                    best = (score, n, name)
            if name in SAMPLES_ONLY:
                continue
            for v in matcher.variants.get(name, []):
                fit = variant_fit(v, tail) if pauses.permits(v.pause_before_s, available) else None
                if fit is not None:
                    # из похожих — ближе по буквам: «джонитерминал» (= «джони терминал»),
                    # а не «джонитерминалп» (похоже на «джонни терминал», но съедает звук следующего слова)
                    score = (edge, 1, fit[1], -n)
                    if best is None or score < best[0]:
                        best = (score, n, name)
        if len(tail) > 24:
            break
    return (best[1], best[2]) if best else None


def _pause(phones, i):
    """Пауза (или край фразы) на месте i."""
    return i < 0 or i >= len(phones) or pauses.is_pause(phones[i])


def _wake_units(units):
    """Близкие звуки слова-ключа; общий разбор остальных команд не меняется."""
    table = str.maketrans({"д": "т", "б": "п", "в": "ф", "г": "к", "з": "ж", "щ": "ш",
                          "ы": "и", "э": "и", "е": "и", "й": None, "ь": None, "ъ": None})
    result = []
    for sound in "".join(units).translate(table):
        if result and sound == result[-1] and sound not in RU_VOWELS:
            continue                         # ZIPA может разбить долгую «щ» на два звука
        result.append(sound)
    return result


def _wake_distance(heard, reference):
    """Вставленная гласная дешевле неверной согласной; первая согласная иногда искажена."""
    def gap(sound):
        return 0.35 if sound in RU_VOWELS else 2.0

    prev = [0.0]
    for sound in reference:
        prev.append(prev[-1] + gap(sound))
    for i, sound in enumerate(heard, 1):
        cur = [prev[0] + gap(sound)]
        for j, ref in enumerate(reference, 1):
            if sound == ref:
                cost = 0.0
            elif sound in RU_VOWELS and ref in RU_VOWELS:
                cost = 0.25
            elif i == j == 1 and sound not in RU_VOWELS and ref not in RU_VOWELS:
                cost = 0.75
            else:
                cost = 2.0
            cur.append(min(prev[j] + gap(sound), cur[-1] + gap(ref), prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def _wake_prefix(phones, matcher, templates, pause_before_s):
    """Запасное сравнение начала ZIPA с образцами wake, без второго распознавателя.

    «д у р ж и й ш и» и «л у р у з ы й щ и» из журнала 03.11: лишние гласные,
    близкие шипящие, иногда первая согласная. Остальные согласные и конец слова обязательны.
    """
    start = 0
    while start < len(phones) and pauses.is_pause(phones[start]):
        start += 1
    available = pauses.before(phones, start, pause_before_s)
    refs = []
    for sample in (templates or {}).get("wake", []):
        required, sound = pauses.prefix(sample)
        if pauses.permits(required, available):
            refs.append(_wake_units(_units(sample_tokens(sound))))
    for variant in matcher.variants.get("wake", []):
        if pauses.permits(variant.pause_before_s, available):
            refs.append(_wake_units(variant.sq))
    refs = [ref for ref in refs if len(_cons(ref)) >= 4]
    if not refs:
        return None                          # короткие слова остаются со строгим сравнением
    best = None
    for end in range(start + 1, min(len(phones), start + 24) + 1):
        if pauses.is_pause(phones[end - 1]):
            break                            # не собирать слово-ключ из разных слов через паузу
        heard = _wake_units(_units(phones[start:end]))
        if not heard or heard[0] in RU_VOWELS:
            continue
        cons = _cons(heard)
        for ref in refs:
            ref_cons = _cons(ref)
            if abs(len(heard) - len(ref)) > 2 or heard[-1] != ref[-1] \
                    or len(cons) != len(ref_cons) or cons[1:] != ref_cons[1:]:
                continue
            cost = _wake_distance(heard, ref)
            if cost <= 1.2:
                # Сначала близость, затем длина: пауза дальше не должна «съесть» начало следующего слова.
                score = (cost, abs(len(heard) - len(ref)), end)
                if best is None or score < best:
                    best = score
    return (best[2], "wake") if best else None


def segments(ipa, matcher, templates=None, pause_before_s=float("inf")):
    """Фраза ZIPA -> [("text", фонемы), (команда, None), …] по порядку.

    «Удали фразу» и «заткнись» — где угодно (текст до неё напечатается, команда сотрёт строку, дальше — новый текст);
    Enter, пробел и остальные — в конце фразы (split_commands)."""
    body, tail = split_commands(ipa, matcher, templates, pause_before_s)
    phones = body.split()
    out, start, i = [], 0, 0

    def text(a, b):
        piece = " ".join(phones[a:b]).strip(" ,")
        if piece and any(not pauses.is_pause(p) for p in piece.split()):
            out.append(("text", piece))

    # «Джони, …» — только в самом начале фразы: дальше текст идёт в его терминал
    head = _best_at(phones, 0, matcher, templates, PREFIX_CMDS, pause_before_s) if phones else None
    if not head:
        head = _wake_prefix(phones, matcher, templates, pause_before_s)
    if head:
        out.append((matcher.ALIASES.get(head[1], head[1]), None))
        i = start = head[0]
        while i < len(phones) and pauses.is_pause(phones[i]):
            i = start = i + 1                    # пауза после «Джони терминал» — не текст
    while i < len(phones):
        hit = _best_at(phones, i, matcher, templates, ANYWHERE_CMDS, pause_before_s) if not pauses.is_pause(phones[i]) else None
        # в середине фразы — только отдельным словом между паузами: у ZIPA нет границ слов,
        # и «если причина» звучит как «стоп речь»; конец фразы проверяет split_commands
        if hit and not (_pause(phones, i - 1) and _pause(phones, i + hit[0])):
            hit = None
        if hit:
            text(start, i)
            out.append((hit[1], None))
            i += hit[0]
            start = i
        else:
            i += 1
    text(start, len(phones))
    return out + [(c, None) for c in tail]


def is_noise(ipa):
    """Не речь: один звук или одни гласные («a», «a a», «k») — так ZIPA записывает вздох, «а», щелчок.
    «да», «нет» и слова команд сюда не попадают (в них есть согласная и больше одного звука)."""
    units = _units(ipa.split())
    return len(units) < 2 or not _cons(units)


def is_echo(ipa, said, min_cons=3):
    """Фраза ZIPA — это хвост того, что сейчас читал голос (эхо из колонок)?
    Согласные фразы (без звонкости) ищем внутри согласных прочитанного текста с допуском ~30 %.
    ZIPA пишет «ц» / «ч» двумя звуками («t s», «t ɕ»), текст — одной буквой: сводим к одной."""
    from matcher import squash

    def one(s):
        return list(s.replace("тщ", "ч").replace("тш", "ч").replace("тс", "ц"))

    mine = one("".join(_cons(_units(ipa.split()))))
    ref = one("".join(c.translate(DEVOICE) for c in squash(said or "") if c not in RU_VOWELS))
    if len(mine) < min_cons or not ref:
        return False
    # расстояние до лучшего кусочка ref (начало и конец кусочка — любые)
    prev = [0] * (len(ref) + 1)
    for i, x in enumerate(mine, 1):
        cur = [i]
        for j, y in enumerate(ref, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return min(prev) <= max(1, int(len(mine) * 0.3))
