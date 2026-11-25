"""Сервер ZIPA: WAV (16 кГц, моно, 16 бит) -> фонемы IPA через пробел.

Запускается окружением самой ZIPA (onnxruntime, lhotse, torch) — у Voice своё окружение,
поэтому модель живёт в отдельном процессе и держится в памяти: фраза — доли секунды.
    python zipa_srv.py <папка ZIPA> <модель .onnx> <tokens.txt> <порт>
POST /ipa (тело — WAV) -> {"ipa": "r ɨ vʲ ɛ t", "ms": 120};  GET /health -> {"ok": true}
"""

import json
import sys
import time
from pathlib import Path

# Python -I не добавляет папку скрипта: явно разрешаем локальные модули Voice.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from command_pauses import marker as pause_marker
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

ZIPA_DIR, MODEL, TOKENS, PORT = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
sys.path.insert(0, ZIPA_DIR + "/inference")

import onnxruntime as ort                      # noqa: E402
import torch                                   # noqa: E402
from utils import get_fbank_extractor, load_tokens   # noqa: E402

SESSION = ort.InferenceSession(MODEL)
VOCAB = load_tokens(TOKENS)
EXTRACTOR = get_fbank_extractor()


def ipa(wav_bytes):
    """WAV -> фонемы и знаки пауз. Заголовок WAV — 44 байта (так пишет Voice).

    CTC знает кадр каждого звука: промежуток между звуками — пауза; короткая — «,», длинная — «.»."""
    pcm = np.frombuffer(wav_bytes[44:], dtype=np.int16).astype(np.float32) / 32768
    if len(pcm) < 1600:                        # меньше 0,1 с — не речь
        return []
    feats = EXTRACTOR.extract_batch([torch.from_numpy(pcm).unsqueeze(0)], sampling_rate=16000)
    feat = feats[0].unsqueeze(0).numpy()
    out = SESSION.run(None, {"x": feat, "x_lens": np.array([feat.shape[1]], dtype=np.int64)})
    probs = out[0][0]
    frame = len(pcm) / 16000 / max(1, probs.shape[0])          # секунд на кадр модели
    preds = np.argmax(probs, axis=-1)
    phones, prev, last_t = [], 0, None
    for t, idx in enumerate(preds):
        if idx != 0 and idx != prev:                            # 0 — пустой символ CTC
            p = VOCAB.get(int(idx), "")
            if p and p != "▁":
                if last_t is not None:
                    gap = (t - last_t) * frame
                    mark = pause_marker(gap)
                    if mark:
                        phones.append(mark)
                phones.append(p)
        if idx != 0:
            last_t = t                                          # последний кадр звучания (звук тянется несколько кадров)
        prev = idx
    return phones


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._json({"ok": True} if self.path == "/health" else {"error": "not found"}, 200 if self.path == "/health" else 404)

    def do_POST(self):
        if self.path != "/ipa":
            return self._json({"error": "not found"}, 404)
        wav = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        t = time.time()
        try:
            phones = ipa(wav)
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        self._json({"ipa": " ".join(phones), "ms": int((time.time() - t) * 1000)})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
