"""Ищет в распознанной фразе команды:

«дамбл-бамбл»   -> space  (пробел)
«румба»         -> double (два пробела подряд, пауза 1 с)
«рекс»          -> enter
«выключи динамик» / «включи озвучку» -> read_off / read_on (модель выбирается в настройках)
«переключи терминал» -> switch (следующая вкладка с агентом)
«переключи проект» -> project (следующий проект Orca, только Orca)
«активный терминал» -> active (вернуться в последний терминал и его вкладку)
«заткнись»      -> stop   (прервать голос)

Сравниваем по звучанию, а не по написанию (идея из jauvex): Whisper путает гласные,
поэтому сверяем «скелет» из согласных и допускаем небольшую разницу в буквах.

У каждой команды два списка: words_ru и words_en. Работает список языка окна (ui_language).
Списки собираются из двух столбиков parts_ru / parts_en: [первая часть и её варианты, вторая часть и её варианты] —
командой считается любое сочетание, в любом порядке (expand_parts). «-» в столбике — эту часть можно не говорить.
"""

import re
from functools import lru_cache
from command_pauses import prefix as pause_prefix

# латиница -> кириллица, если Whisper написал слово латиницей ("rex", "dumble bumble")
LATIN = {
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х",
    "i": "и", "j": "дж", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п",
    "q": "к", "r": "р", "s": "с", "t": "т", "u": "а", "v": "в", "w": "в", "x": "кс",
    "y": "й", "z": "з",
}
VOWELS = re.compile(r"[аеёиоуыэюяйьъ]+")
CYRILLIC = re.compile(r"[а-яё]", re.I)


def word_lang(word):
    """Язык варианта команды: ru — есть кириллица, иначе en."""
    return "ru" if CYRILLIC.search(word) else "en"


@lru_cache(maxsize=8192)
def squash(text):
    """Нижний регистр, латиница в кириллицу, только буквы."""
    text = text.lower().replace("ё", "е")
    text = "".join(LATIN.get(ch, ch) for ch in text)
    return re.sub(r"[^а-я]+", "", text)


def exact(text):
    """Для номерных команд: нижний регистр, буквы и ЦИФРЫ («3-й» ≠ «1-й»)."""
    return re.sub(r"[^a-zа-я0-9]+", "", text.lower().replace("ё", "е"))


@lru_cache(maxsize=8192)
def skeleton(text):
    """Только согласные: «дамблбамбл» -> «дмблбмбл»."""
    return VOWELS.sub("", squash(text))


DEVOICE = str.maketrans("бвгджз", "пфктшс")


def devoice(skel):
    """Звонкие = глухие (з/с, д/т, б/п…): по-русски на конце слова их не различить («заткниз» = «заткнис»)."""
    return skel.translate(DEVOICE)


def distance(a, b):
    """Расстояние Левенштейна."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def close(a, b, limit):
    """distance(a, b) <= limit, но с ранним выходом: строка таблицы целиком больше limit — дальше не считаем."""
    if abs(len(a) - len(b)) > limit:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return False
        prev = cur
    return prev[-1] <= limit


# «3-й терминал», «3-е терминал», «3 вкладку» -> словами: так Whisper large пишет порядковые числа
ORDINALS = {"1": ("первый", "первую"), "2": ("второй", "вторую"), "3": ("третий", "третью"),
            "4": ("четвёртый", "четвёртую"), "5": ("пятый", "пятую")}
ORDINAL_DIGIT = re.compile(r"^([1-5])(?:-?[а-яё]{1,3})?$", re.I)


def ordinal_words(words):
    """Цифру перед «терминал»/«вкладку» заменяет словом; длина списка не меняется."""
    out = list(words)
    for i in range(len(out) - 1):
        m = ORDINAL_DIGIT.match(out[i])
        nxt = out[i + 1].lower()
        if m and nxt in ("терминал", "вкладку", "вкладка"):      # «3 терминала открыто» — не команда
            out[i] = ORDINALS[m.group(1)][0 if nxt == "терминал" else 1]
    return out


OPTIONAL = "-"     # вариант «без этой части»: «-» + «первый терминал» = «первый терминал»


def expand_parts(parts):
    """[[«переключи», «переключить»], [«терминал»]] -> все сочетания: «переключи терминал», «переключить терминал»."""
    first = [w.strip() for w in (parts[0] if parts else []) if w.strip()]
    second = [w.strip() for w in (parts[1] if len(parts) > 1 else []) if w.strip()] or [""]
    out = []
    for a in first or [OPTIONAL]:
        for b in second:
            pair = [x for x in (a, b) if x and x != OPTIONAL]
            # обе части есть — годится и обратный порядок: «системная диктовка» = «диктовка системная»
            for phrase in {" ".join(pair), " ".join(reversed(pair))}:
                if phrase and phrase not in out:
                    out.append(phrase)
    return out


def split_words(text):
    return [w for w in re.split(r"[\s,.!?;:…]+", text) if w]


class Variant:
    """Один вариант слова-команды.

    Длинный («дамбл-бамбл», «заткнись») — нечёткое сравнение по буквам и согласным.
    Короткий («рекс») — точное совпадение или тот же скелет согласных.
    """

    LONG = 7

    def __init__(self, text, strict=False):
        self.pause_before_s, text = pause_prefix(text)
        self.sq = squash(text)
        self.skel = skeleton(text)
        self.dskel = devoice(self.skel)
        self.long = len(self.sq) >= self.LONG
        self.ex = exact(text)
        # номерные команды: почти точное совпадение, без скелета согласных.
        # Слово с цифрой — всегда так: squash выбрасывает цифры, и «1 терминал» иначе стал бы просто «терминал»
        self.strict = strict or any(c.isdigit() for c in self.ex)

    def same(self, chunk):
        """Точно это слово (без нечёткости): у номерных — с цифрами (exact), у остальных — по squash."""
        return exact(chunk) == self.ex if self.strict else squash(chunk) == self.sq

    def match(self, chunk):
        if self.strict:
            ex = exact(chunk)
            if not ex or not self.ex:
                return False
            if ex == self.ex:
                return True
            # с цифрой — только точно («3» и «1» отличаются одним знаком); словами — допуск в одну букву
            return (not any(c.isdigit() for c in self.ex) and len(self.ex) >= 12
                    and close(ex, self.ex, 1))
        sq, sk = squash(chunk), skeleton(chunk)
        dsk = devoice(sk)
        if not sq or not self.sq:
            return False
        if not self.long:
            return sq == self.sq or (len(sq) <= len(self.sq) + 1 and dsk == self.dskel)

        if self.sq in sq:
            return True
        if len(self.skel) <= 5:
            # мало согласных («мутабор», «shut up», «заткнись») — легко спутать с обычными словами:
            # скелет должен совпасть целиком, буквы — почти
            return dsk == self.dskel and close(devoice(sq), devoice(self.sq), 1)
        if self.dskel in dsk:
            return True
        if len(sq) > len(self.sq) * 1.6 or abs(len(sq) - len(self.sq)) > len(self.sq) // 4:
            return False
        # почти те же буквы — или чуть больше отличий, но согласные почти совпали
        if not close(sq, self.sq, len(self.sq) // 4):
            return False                  # даже с запасом не похоже — дальше не считаем
        if close(sq, self.sq, len(self.sq) // 5):
            return True
        return close(dsk, self.dskel, max(1, len(self.skel) // 4))


# голосовые команды смены режима: имя -> (настройка, значение)
MODE_COMMANDS = {
    "dict_off": ("mic", "mute"),  # синоним «приглуши микрофон», движок диктовки сохраняется
    "read_off": ("reading", "off"), "read_on": ("reading", "on"),
    "mic_off": ("mic", False),
    "mic_mute": ("mic", "mute"),     # «приглуши микрофон»: слышит только «Амиго», «Джони», «включи микрофон»
}


class Matcher:
    # порядок проверки; «терминал N» раньше «переключи терминал»
    ORDER = ("space", "double", *MODE_COMMANDS, "tab1", "tab2", "tab3", "tab4", "tab5",
             "lenta1", "lenta2", "lenta3", "lenta4", "lenta5",       # «сборщик первое … пятое» — прочитать сообщение ленты
             "active", "project", "previous", "switch", "clear", "stop", "enter", "enter2", "jonny", "amigo", "mic_on")
    PREFIX = ("jonny", "amigo", "mic_on")   # «Джони, …» / «Амиго, …» / «включи микрофон» — только первым словом фразы, дальше — обычные слова
    ALIASES = {"enter2": "enter"}   # «Enter 2» — свои слова, действие как у Enter
    SWITCHES = ("previous", "switch", "project", "tab1", "tab2", "tab3", "tab4", "tab5", "active")   # переключение вкладки / терминала
    # в длинной фразе — обычные слова: только переключения («удали фразу», как Enter, работает в любом месте)
    LONG_SKIP = SWITCHES

    def __init__(self, cfg):
        self.lang = cfg.get("ui_language", "en")
        self.variants = {}
        for name in self.ORDER:
            command = cfg["commands"].get(name) or {}
            if command.get("enabled", True):
                words = command.get("words_" + self.lang)
                if words is None:      # старый формат: один список, язык — по алфавиту слова
                    words = [w for w in command.get("words", []) if word_lang(w) == self.lang]
                self.variants[name] = [Variant(w, strict=name.startswith(("tab", "lenta"))) for w in words if w.strip()]
        self.max_words = cfg.get("max_words", 6)
        # результаты сравнений: длинная фраза проверяет одни и те же куски сотни раз
        # (без запоминания фраза в 80 слов разбиралась 10–90 с)
        self._memo = {}

    def match(self, name, chunk):
        key = (name, chunk)
        hit = self._memo.get(key)
        if hit is None:
            if len(self._memo) > 50000:
                self._memo.clear()
            hit = self._memo[key] = any(v.match(chunk) for v in self.variants.get(name, []))
        return hit

    def is_enter(self, word):
        return self.match("enter", word) or self.match("enter2", word)

    WINDOW = 4    # команду распознаватель может разбить до 4 слов: «du rikma du rik»

    def _single(self, chunk):
        """Какая команда в этом куске (слово или несколько слов) или None.
        Точное совпадение главнее похожего: «включи микрофон» не должно стать «выключи микрофон».
        Номерные команды сравниваются с цифрами (Variant.same): «терминал» — не «1 терминал»."""
        name = next((name for name, vs in self.variants.items() if any(v.same(chunk) for v in vs)), None) \
            or next((name for name in self.variants if self.match(name, chunk)), None)
        return self.ALIASES.get(name, name)

    def _at(self, words, i):
        """Команда, которая начинается со слова i: (имя, сколько слов) или (None, 0).

        Склеиваем 1, 2, 3, 4 слова подряд; дальше не тянем, если следующее слово — само команда.
        """
        if not words[i]:
            return None, 0
        # номерные команды — самым длинным куском: «switch to terminal two» не должно стать «switch terminal»
        chunks, chunk = [], ""
        for n in range(1, self.WINDOW + 1):
            if i + n > len(words) or not words[i + n - 1] or (n > 1 and self._single(words[i + n - 1])):
                break
            chunk += words[i + n - 1]
            chunks.append((n, chunk))
        for n, piece in reversed(chunks):
            name = next((k for k in self.SWITCHES if (k.startswith("tab") or k == "previous") and self.match(k, piece)), None)
            if name:
                return name, n
        name = self._single(words[i])
        if name:
            return name, 1
        chunk = words[i]
        for n in range(2, self.WINDOW + 1):
            if i + n > len(words) or not words[i + n - 1] or self._single(words[i + n - 1]):
                break
            chunk += words[i + n - 1]
            name = self._single(chunk)
            if name:
                return name, n
        return None, 0

    def segments(self, text, skip=()):
        """Фраза из режима OpenAI: текст и команды по порядку.

        «сделай аудит, рекс» -> [("text", "сделай аудит"), ("enter", None)]
        skip — команды, которые здесь считаются обычными словами (печатаются как текст).
        """
        tokens = self._unglue(re.findall(r"\S+", text), skip)
        clean = ordinal_words([re.sub(r"[^\w-]+", "", t) for t in tokens])
        out, buf = [], []

        def flush():
            if buf:
                piece = " ".join(buf).rstrip(" ,;:—-")
                if piece:
                    out.append(("text", piece))
                buf.clear()

        i = 0
        while i < len(tokens):
            name, size = self._at(clean, i)
            if name in self.PREFIX and (out or buf):
                name = None                      # «Джони» не в начале фразы — просто слово
            if name and name not in skip:
                flush()
                out.append((name, None))
                i += size
            else:
                buf.append(tokens[i])
                i += 1
        flush()
        return out

    GLUE_MIN = 3     # слово перед слипшимся Enter — не короче 3 букв («проекто|рекс»)

    def _unglue(self, tokens, skip=()):
        """Слово Enter приклеилось к последнему слову («проекторекс», «фрекс») — отделить.

        Перед ним 3 буквы и больше — это слово текста; 1 буква — лишний звук, прилипший к команде (выбрасываем);
        2 буквы — не угадать, не трогаем."""
        if not tokens:
            return tokens
        word = re.sub(r"[^\w-]+$", "", tokens[-1])
        low = word.lower().replace("ё", "е")
        if not re.fullmatch(r"[а-я]+", low) or self._single(low):
            return tokens
        for name in ("enter", "enter2"):
            if name in skip:
                continue
            for v in self.variants.get(name, []):
                if v.long or not low.endswith(v.sq):
                    continue
                rest = len(low) - len(v.sq)
                if rest >= self.GLUE_MIN:
                    return tokens[:-1] + [word[:rest], word[rest:]]
                if rest == 1:
                    return tokens[:-1] + [word[rest:]]
        return tokens

    MODE_MAX_WORDS = 3    # смена режима — только короткой фразой: «сделай системную диктовку текста» — не команда

    def mode_skip(self, text):
        """Команды смены режима, которые в этой фразе считаются обычными словами."""
        return tuple(MODE_COMMANDS) if len(split_words(text)) > self.MODE_MAX_WORDS else ()

    def short(self, text):
        """Фраза не длиннее лимита слов (max_words) — в ней можно узнавать «длинные» команды."""
        return len(split_words(text)) <= self.max_words

    def _has_stop(self, words):
        # слова и пары соседних слов: «shut up» — команда из двух слов
        chunks = words + [a + b for a, b in zip(words, words[1:])]
        return ["stop"] if any(self.match("stop", c) for c in chunks) else []

    def commands(self, text, long=False):
        """Список команд по порядку: ["space", "enter", ...].

        long=True — кусок длинной речи: в нём ищем только «заткнись»
        (чтобы не нажать Enter посреди диктовки).
        """
        words = ordinal_words(split_words(text))
        if long or not words or len(words) > self.max_words:
            return self._has_stop(words)

        # «дамбл бамбл» / «du rikma du rik» — команда, разбитая на несколько слов
        skip = self.mode_skip(text)
        result = []
        i = 0
        while i < len(words):
            name, size = self._at(words, i)
            if name in self.PREFIX and i > 0:
                name = None                      # «Джони» — только первым словом
            if name:
                if name not in skip:
                    result.append(name)
                i += size
            else:
                i += 1
        return result
