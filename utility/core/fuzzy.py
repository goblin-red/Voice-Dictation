"""«Нечёткий слух»: ловит слово-команду в конце фразы по звуку, когда текст GigaAM его потерял.

Как работает: берём хвост фразы (последние ~1.5 с речи), прогоняем через уже загруженную модель
GigaAM (RNNT) и считаем, с какой вероятностью этот хвост — именно слово команды.
Слово может начинаться где угодно в хвосте, но обязано быть последним (после него — только тишина).
Отдельной модели не нужно: работает та же GigaAM, что печатает текст.
"""

import re

import numpy as np

# Команды, для которых работает нечёткий слух. Добавить команду — дописать её имя сюда.
FUZZY_COMMANDS = ("enter", "enter2")

TAIL_S = 1.5                       # сколько секунд речи с конца фразы проверяем
FRAME = 480                        # 30 мс при 16 кГц
ENDINGS = ("", ".", "!", "?")      # GigaAM пишет слово с заглавной и со знаком в конце — проверяем все варианты
PUNCT = re.compile(r"[^\w\s-]+")


def tail(pcm, sec=TAIL_S):
    """Последние sec секунд речи: тишину в конце отрезаем, оставляем 180 мс запаса."""
    n = len(pcm) // FRAME
    if n == 0:
        return pcm
    rms = np.sqrt((pcm[: n * FRAME].reshape(n, FRAME) ** 2).mean(1))
    loud = np.nonzero(rms > rms.max() * 0.1)[0]
    end = min(n, loud[-1] + 6) * FRAME if len(loud) else len(pcm)
    return pcm[max(0, end - int(sec * 16000)): end]


def command_words(cfg, names=FUZZY_COMMANDS):
    """{команда: [слова]} из настроек команд — список языка окна, без знаков и повторов."""
    lang = cfg.get("ui_language", "en")
    out = {}
    for name in names:
        command = cfg.get("commands", {}).get(name) or {}
        if not command.get("enabled", True):
            continue
        words = []
        for w in command.get("words_" + lang) or []:
            w = " ".join(PUNCT.sub(" ", w).lower().split())
            if w and w not in words:
                words.append(w)
        if words:
            out[name] = words
    return out


class FuzzyEar:
    """Оценка «хвост фразы = слово команды» по модели GigaAM (RNNT)."""

    def __init__(self, gigaam):
        self.gigaam = gigaam          # обёртка из local_voice: модель грузит она
        self._ids = {}                # слово -> варианты токенов (кэш)

    def _variants(self, word):
        if word not in self._ids:
            spm = self.gigaam.model.decoding.tokenizer.model
            seqs = set()
            for w in (word, word[:1].upper() + word[1:]):
                for end in ENDINGS:
                    seqs.add(tuple(spm.encode(w + end)))
            self._ids[word] = [list(s) for s in seqs if s]
        return self._ids[word]

    def best(self, pcm, words):
        """pcm — вся фраза (float32, 16 кГц); words — {команда: [слова]}.

        Возвращает (команда, слово, вероятность) с наибольшей вероятностью или None.
        """
        import torch

        if not words:
            return None
        self.gigaam.load()
        model = self.gigaam.model
        piece = tail(pcm)
        if len(piece) < 0.2 * 16000:
            return None

        with torch.inference_mode():
            # энкодер — на устройстве модели (MPS), остальное — на CPU: там мелкие матрицы
            wav = torch.tensor(piece, dtype=model._dtype, device=model._device).unsqueeze(0)
            length = torch.full([1], wav.shape[-1], device=model._device)
            enc, enc_len = model.forward(wav, length)
            x = enc.transpose(1, 2)[0, : int(enc_len[0])].float().cpu()      # [T, D]
            head = model.head
            if next(head.parameters()).device.type != "cpu":
                head = self._cpu_head(head)
            enc_proj = head.joint.enc(x)                                      # [T, H]
            blank = model.decoding.blank_id

            result = None
            for name, ws in words.items():
                for word in ws:
                    for ids in self._variants(word):
                        p = self._score(head, enc_proj, ids, blank)
                        if result is None or p > result[2]:
                            result = (name, word, p)
        return result

    def _cpu_head(self, head):
        """Копия головы RNNT на CPU (один раз): декодер и joint маленькие, на CPU быстрее."""
        if getattr(self, "_head", None) is None:
            import copy
            self._head = copy.deepcopy(head).float().cpu().eval()
        return self._head

    @staticmethod
    def _score(head, enc_proj, ids, blank):
        """Вероятность, что хвост заканчивается словом ids (сумма по всем выравниваниям RNNT)."""
        import torch

        y = torch.tensor([ids])
        g0, state = head.decoder.predict(None, None, batch_size=1)
        g, _ = head.decoder.predict(y, state)
        pred = head.joint.pred(torch.cat([g0, g], dim=1)[0])              # [U+1, H]
        lp = head.joint.joint_net(enc_proj[:, None, :] + pred[None, :, :]).log_softmax(-1)
        T, U = lp.shape[0], len(ids)
        emit = lp[:, torch.arange(U), torch.tensor(ids)].numpy()           # [T, U]
        stay = lp[:, :, blank].numpy()                                     # [T, U+1]

        # alpha[t, u]: съедено u токенов к кадру t; начало слова — в любом кадре (alpha[:, 0] = 0)
        alpha = np.full((T, U + 1), -np.inf)
        alpha[:, 0] = 0.0
        for t in range(T):
            for u in range(1, U + 1):
                v = alpha[t, u - 1] + emit[t, u - 1]
                if t > 0:
                    v = np.logaddexp(v, alpha[t - 1, u] + stay[t - 1, u])
                alpha[t, u] = v
        return float(min(1.0, np.exp(alpha[T - 1, U] + stay[T - 1, U])))
