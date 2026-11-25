"""Чтение HTML-файла вслух (вкладка «Чтение»): весь видимый текст по фрагментам, подряд — до конца или до «Стоп».

Фрагмент — текст одного блока страницы (абзац, заголовок, ячейка таблицы, пункт списка).
Читает тот голос, что выбран в настройках: Reader зовёт speaker.say и ждёт, пока голос замолчит.
"""

import re
import threading
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path


# блоки: на их границах фрагмент заканчивается
BLOCK_TAGS = {
    "p", "div", "li", "td", "th", "tr", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "blockquote", "pre", "dd", "dt", "caption", "figcaption",
    "table", "ul", "ol", "header", "footer", "nav", "main", "aside", "form", "label", "button",
}
# невидимое содержимое — не читаем
SKIP_TAGS = {"script", "style", "head", "title", "noscript", "template", "svg", "iframe", "object", "select", "textarea"}
# теги без закрывающего — для них глубину пропуска не считаем
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

PIECE_CHARS = 800      # длинный фрагмент режем по предложениям — чтобы «Стоп» срабатывал быстро
START_WAIT_S = 20      # сколько ждать, пока голос начнёт говорить (облачному голосу нужно время)
GAP_S = 0.3            # тишина, после которой фрагмент считаем дочитанным
ANSWER_GAP_S = 1.0     # тишина после ответа агента, после которой возвращаемся к чтению


class _VisibleText(HTMLParser):
    """Собирает видимый текст страницы, разбивая его на фрагменты по блокам."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fragments = []
        self._buf = []
        self._skip = 0     # глубина внутри невидимого элемента

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf = []
        if text and re.search(r"\w", text):     # одни знаки препинания не читаем
            self.fragments.append(text)

    @staticmethod
    def _hidden(attrs):
        attrs = dict(attrs)
        style = (attrs.get("style") or "").replace(" ", "").lower()
        return "hidden" in attrs or attrs.get("aria-hidden") == "true" or "display:none" in style

    def handle_starttag(self, tag, attrs):
        if tag in VOID_TAGS:
            if tag in BLOCK_TAGS and not self._skip:
                self._flush()
            return
        if self._skip or tag in SKIP_TAGS or self._hidden(attrs):
            self._skip += 1
            return
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        if self._skip:
            self._skip -= 1
            return
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def fragments(html_text):
    """Видимый текст HTML списком фрагментов."""
    parser = _VisibleText()
    parser.feed(html_text)
    parser.close()
    return parser.fragments


def normalize_source(source):
    """Путь или ссылка, как её вставили: кавычки, «\\ » из терминала, ~, file://."""
    src = (source or "").strip().strip("'\"").strip()
    if src.startswith("file://"):
        return Path(urllib.parse.unquote(urllib.parse.urlparse(src).path))
    if re.match(r"https?://", src, re.I):
        return src
    src = re.sub(r"\\(.)", r"\1", src)          # «Псевдоним\ GOBL\(in\)» → «Псевдоним GOBL(in)»
    return Path(src).expanduser()


def load(source):
    """Текст HTML по пути или ссылке."""
    src = normalize_source(source)
    if isinstance(src, str):
        req = urllib.request.Request(src, headers={"User-Agent": "GOBL(in) Voice"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    if not src.is_file():
        raise FileNotFoundError(f"File not found: {src}")
    return src.read_text(encoding="utf-8", errors="replace")


def pieces(text, limit=PIECE_CHARS):
    """Длинный фрагмент — кусками по предложениям, не длиннее limit."""
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for sentence in re.split(r"(?<=[.!?;…])\s+", text):
        while len(sentence) > limit:                     # предложение без точек — режем по словам
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if cur:
                out.append(cur)
                cur = ""
            out.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if cur and len(cur) + 1 + len(sentence) > limit:
            out.append(cur)
            cur = sentence
        else:
            cur = f"{cur} {sentence}".strip()
    if cur:
        out.append(cur)
    return out


class Reader:
    """Читает фрагменты подряд в своём потоке.

    start() — загрузить файл и читать с начала, pause() — пауза / продолжить с начала прерванного фрагмента,
    jump(n) — читать с фрагмента n, stop() — остановить. Список фрагментов остаётся — по нему можно перейти.
    """

    def __init__(self, speaker, log):
        self.speaker = speaker
        self.log = log
        self._run_id = 0
        self._lock = threading.Lock()
        self.items = []
        self.items_ver = 0     # меняется при загрузке нового файла — окно перечитывает список
        self.active = False    # поток чтения идёт (в том числе загрузка файла)
        self.paused = False
        self.source = ""
        self.title = ""        # чтение готовых текстов: что читаем (например, «Лента»)
        self.index = 0         # номер текущего фрагмента (с 1)
        self.current = ""
        self.error = ""
        self.completed = set()
        self.client_id = ""

    def info(self):
        """Для окна: что читаем и где мы (сам список — отдельно, по items_ver)."""
        with self._lock:
            return {
                "active": self.active, "paused": self.paused, "source": self.source, "title": self.title, "index": self.index,
                "total": len(self.items), "current": self.current[:200], "error": self.error, "items_ver": self.items_ver,
                "completed": sorted(self.completed), "client_id": self.client_id,
            }

    def start(self, source):
        source = (source or "").strip()
        if not source:
            raise ValueError("Enter a path or URL for an HTML file")
        self.stop()
        with self._lock:
            run_id = self._new_run()
            self.completed = set()
            self.client_id = ""
            self.active, self.paused, self.source, self.title, self.index, self.current, self.error = \
                True, False, source, "", 0, "", ""
        threading.Thread(target=self._load_and_read, args=(run_id, source), daemon=True).start()

    def start_items(self, items, title="", client_id="", index=1):
        """Читать готовые тексты (например, сообщения ленты Сборщика) — так же, как фрагменты файла."""
        items = [re.sub(r"\s+", " ", str(x)).strip() for x in items or []]
        items = [x for x in items if x]
        if not items:
            raise ValueError("No text to read")
        if type(index) is not int or not 1 <= index <= len(items):
            raise ValueError("Invalid starting section")
        self.stop()
        with self._lock:
            run_id = self._new_run()
            self.items = items
            self.completed = set()
            self.client_id = str(client_id)[:100]
            self.items_ver += 1
            self.active, self.paused, self.source, self.title, self.index, self.current, self.error = \
                True, False, "", title, index, items[index - 1], ""
        self.log.info("Reading text: %s, %d sections", title or "Untitled", len(items))
        threading.Thread(target=self._read, args=(run_id, index), daemon=True).start()

    def pause(self):
        """Кнопка «Пауза / Продолжить»."""
        if self.paused:
            self.jump(self.index or 1)
            return
        with self._lock:
            if not self.active or not self.items:
                return
            self._new_run()
            self.active, self.paused = False, True
        self.speaker.stop()
        self.log.info("Reading paused at section %d of %d", self.index, len(self.items))

    def update_items(self, items, client_id):
        """Обновить тексты той же очереди; изменившийся текущий фрагмент перечитать сразу."""
        if not isinstance(items, list) or any(not isinstance(item, str) or not item.strip() for item in items):
            raise ValueError("Invalid text for the reading queue")
        items = [re.sub(r"\s+", " ", item).strip() for item in items]
        with self._lock:
            if not client_id or client_id != self.client_id:
                raise ValueError("The reading queue has changed")
            if len(items) != len(self.items):
                raise ValueError("The number of messages in the queue has changed")
            changed = items != self.items
            current_changed = 1 <= self.index <= len(items) and items[self.index - 1] != self.items[self.index - 1]
            self.items = items
            if changed:
                self.items_ver += 1
            if 1 <= self.index <= len(items):
                self.current = items[self.index - 1]
            restart = current_changed and self.active and not self.paused and self.index not in self.completed
            if restart:
                run_id = self._new_run()
                index = self.index
        if restart:
            self.speaker.stop()
            self.log.info("Reading display changed for section %d", index)
            threading.Thread(target=self._read, args=(run_id, index), daemon=True).start()
        return changed

    def jump(self, index, items=None, client_id=None):
        """Читать с фрагмента index (с 1)."""
        with self._lock:
            if not self.items:
                return
            if items is not None:
                if not client_id or client_id != self.client_id:
                    raise ValueError("The reading queue has changed")
                if not isinstance(items, list) or len(items) != len(self.items) or any(not isinstance(item, str) or not item.strip() for item in items):
                    raise ValueError("Invalid text for the reading queue")
                values = [re.sub(r"\s+", " ", item).strip() for item in items]
                if values != self.items:
                    self.items = values
                    self.items_ver += 1
            index = max(1, min(int(index), len(self.items)))
            run_id = self._new_run()
            self.active, self.paused, self.error = True, False, ""
            self.index, self.current = index, self.items[index - 1]
        self.speaker.stop()
        self.log.info("Reading from section %d of %d", index, len(self.items))
        threading.Thread(target=self._read, args=(run_id, index), daemon=True).start()

    def interrupt(self):
        """Ответ агента перебивает чтение: дослушиваем его и читаем прерванный фрагмент заново."""
        with self._lock:
            if not self.active or not self.items:
                return
            run_id = self._new_run()
            index = self.index or 1
        self.speaker.stop()
        self.log.info("Reading interrupted by a reply. Resuming at section %d", index)
        threading.Thread(target=self._resume_after_answer, args=(run_id, index), daemon=True).start()

    def output_changed(self):
        """Смена выхода: прервать старый звук и перечитать текущий фрагмент на новом устройстве."""
        with self._lock:
            if not self.active or self.paused or not self.items or not self.index:
                return
            run_id = self._new_run()
            index = self.index
        self.speaker.stop()
        self.log.info("Audio output changed. Resuming at section %d", index)
        threading.Thread(target=self._read, args=(run_id, index), daemon=True).start()

    def _resume_after_answer(self, run_id, index):
        self._wait_spoken(run_id, ANSWER_GAP_S)     # ответ зазвучал и закончился
        if self._alive(run_id):
            self._read(run_id, index)

    def stop(self):
        with self._lock:
            was = self.active or self.paused
            self._new_run()            # поток чтения увидит новый номер и выйдет
            self.active = self.paused = False
        if was:
            self.speaker.stop()
            self.log.info("Reading stopped at section %d of %d", self.index, len(self.items))

    def _new_run(self):
        self._run_id += 1
        return self._run_id

    def _alive(self, run_id):
        return self._run_id == run_id

    def _load_and_read(self, run_id, source):
        try:
            items = fragments(load(source))
        except Exception as e:
            self.log.warning("Reading file: %s", e)
            with self._lock:
                if self._alive(run_id):
                    self.error, self.active = str(e), False
            return
        with self._lock:
            if not self._alive(run_id):
                return
            self.items = items
            self.items_ver += 1
        self.log.info("Reading file: %s, %d sections", source, len(items))
        self._read(run_id, 1)

    def _read(self, run_id, first):
        for i in range(first, len(self.items) + 1):
            with self._lock:
                if not self._alive(run_id):
                    return
                frag = self.items[i - 1]
                self.index, self.current = i, frag
            for piece in pieces(frag):
                with self._lock:
                    if not self._alive(run_id):
                        return
                    self.speaker.say(piece)
                if not self._wait_spoken(run_id):
                    with self._lock:
                        if self._alive(run_id):
                            self.active = False
                            self.error = "Speech did not start. The message is still unread"
                    return
            with self._lock:
                if not self._alive(run_id):
                    return
                self.completed.add(i)

        with self._lock:
            if self._alive(run_id):
                self.active = False
                self.log.info("Finished reading")

    def _wait_spoken(self, run_id, gap=GAP_S):
        """Ждём, пока голос начнёт говорить и потом замолчит на gap секунд."""
        deadline = time.time() + START_WAIT_S
        while not self.speaker.busy():
            if not self._alive(run_id) or time.time() > deadline:
                return
            time.sleep(0.05)
        quiet_since = None
        while self._alive(run_id):
            if self.speaker.busy():
                quiet_since = None
            elif quiet_since is None:
                quiet_since = time.time()
            elif time.time() - quiet_since >= gap:
                return True
            time.sleep(0.05)
