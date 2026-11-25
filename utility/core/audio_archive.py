"""Compact completed diagnostic WAVs; retain exact source/time mapping."""
import gzip
import json
import os
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from audio_log_paths import locate

THRESHOLD = 0.0001
MIN_QUIET_S = 2
PADDING_S = .5


def compact(path):
    path = Path(path)
    folder = path.parent.parent if path.parent.name == 'wav' else path.parent
    clock = locate(folder, path.stem + '.jsonl', 'json')
    if not clock.exists():
        return None
    data, rate = sf.read(path, dtype='int16')
    if rate not in (16000, 24000) or data.ndim != 1:
        raise ValueError('expected mono PCM16 at 16000 or 24000 Hz')
    rows = [json.loads(line) for line in clock.read_text().splitlines()]
    if sum(r.get('samples', 0) for r in rows) != len(data):
        raise ValueError('audio/time index mismatch; leaving original intact')
    block = rate // 50
    frames = data[:len(data)//block*block].astype(float).reshape(-1, block) / 32768
    rms = np.sqrt(np.mean(frames**2, axis=1))
    peak = np.max(np.abs(frames), axis=1)
    quiet = (rms <= THRESHOLD) & (peak <= THRESHOLD * 4)
    edges = np.diff(np.r_[False, quiet, False].astype(int))
    cuts = [(int(a*block+rate*PADDING_S), int(b*block-rate*PADDING_S))
            for a,b in zip(np.flatnonzero(edges == 1),np.flatnonzero(edges == -1))
            if (b-a)*block >= rate*MIN_QUIET_S]
    ranges = []; cursor = 0
    for a,b in cuts:
        ranges.append((cursor,a)); cursor=b
    ranges.append((cursor,len(data)))
    segments=[]; offset=0
    for a,b in ranges:
        if b>a:
            segments.append({'output_start_sample':offset,'source_start_sample':a,'samples':b-a})
            offset += b-a
    kept = np.concatenate([data[a:b] for a,b in ranges])
    mapping = {'version':1,'source_wav':path.name,'sample_rate':rate,'source_samples':len(data),
               'output_samples':len(kept),'threshold_rms':THRESHOLD,'peak_limit':THRESHOLD*4,
               'minimum_quiet_seconds':MIN_QUIET_S,'edge_padding_seconds':PADDING_S,
               'removed_source_samples':[list(pair) for pair in cuts],'segments':segments,'original_clock':rows,
               'note':'Low-level intervals removed by amplitude, not guaranteed speech-free. received is frame reception time, not ADC time.'}
    target = folder / 'flac' / (path.stem + '.flac')
    index = folder / 'json' / (path.stem + '.json.gz')
    target.parent.mkdir(parents=True, exist_ok=True)
    index.parent.mkdir(parents=True, exist_ok=True)
    tmp_audio = Path(str(target)+'.tmp')
    tmp_index = Path(str(index)+'.tmp')
    original_mtime = path.stat().st_mtime
    original_size = path.stat().st_size + clock.stat().st_size
    try:
        sf.write(tmp_audio, kept, rate, format='FLAC', subtype='PCM_16')
        decoded, _ = sf.read(tmp_audio, dtype='int16')
        if not np.array_equal(decoded, kept):
            raise ValueError('FLAC verification failed')
        with gzip.open(tmp_index, 'wt', encoding='utf-8') as f:
            json.dump(mapping, f, ensure_ascii=False, separators=(',', ':'))
        with gzip.open(tmp_index, 'rt', encoding='utf-8') as f:
            if json.load(f) != mapping:
                raise ValueError('time mapping verification failed')
        # Both verified before originals removed; crash before completion leaves originals recoverable.
        os.utime(tmp_audio, (original_mtime, original_mtime))
        os.utime(tmp_index, (original_mtime, original_mtime))
        os.replace(tmp_audio, target)
        os.replace(tmp_index, index)
        path.unlink()
        clock.unlink()
        return {'file':target.name,'removed_seconds':(len(data)-len(kept))/rate,
                'before_bytes':original_size,'after_bytes':target.stat().st_size+index.stat().st_size}
    finally:
        tmp_audio.unlink(missing_ok=True)
        tmp_index.unlink(missing_ok=True)


def maintain(folder, log, stop, retention=86400, interval=1800):
    """Separate worker: slow compression never blocks microphone or WAV writer."""
    folder = Path(folder)
    while not stop.is_set():
        try:
            now = time.time()
            if folder.exists():
                for path in sorted(list((folder/'wav').glob('raw-*.wav')) + list(folder.glob('raw-*.wav'))):
                    if stop.is_set():
                        break
                    age = now-path.stat().st_mtime
                    if 120 < age < retention:  # closed files only; active writer refreshes mtime every frame
                        try:
                            result = compact(path)
                            if result:
                                log.info('Audio archive: %s', result)
                        except Exception:
                            log.exception('Audio archive: kept the original WAV %s', path)
        except Exception:
            log.exception('Audio archive: background processing failed')
        stop.wait(interval)
