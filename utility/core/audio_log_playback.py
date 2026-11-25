"""Read time-selected audio across live WAVs and trimmed FLAC archives."""
import gzip
import io
import json
import math
from pathlib import Path
import numpy as np
import soundfile as sf
from audio_resample import ArchiveResampler
from audio_log_paths import locate


def clip(folder, start, end):
    if not all(math.isfinite(x) for x in (start,end)) or not 0 < end-start <= 120:
        raise ValueError('invalid audio interval')
    chunks=[]
    folder=Path(folder)
    # Prefer original while archive conversion is in progress; no duplicate playback.
    stems=sorted({p.name.split('.json')[0] for directory in (folder/'json', folder) for p in directory.glob('raw-*.json*') if not p.name.endswith('.tmp')})
    for stem in stems:
        try:
            raw=locate(folder, stem+'.wav', 'wav'); index=locate(folder, stem+'.jsonl', 'json')
            if raw.exists() and index.exists():
                rows=[]
                for line in index.read_text().splitlines():
                    try: rows.append(json.loads(line))
                    except json.JSONDecodeError: pass  # active last index line
                path=raw; segments=None
            else:
                path=locate(folder, stem+'.flac', 'flac')
                with gzip.open(locate(folder, stem+'.json.gz', 'json'),'rt') as f: mapping=json.load(f)
                rows=mapping['original_clock'];segments=mapping['segments']
            selected=[r for r in rows if 'received' in r and start <= r['received'] <= end]
            if not selected: continue
            with sf.SoundFile(path) as f:
                ranges=[]
                for row in selected:
                    a=round(row['offset']*f.samplerate);b=a+row['samples']
                    if segments is None: spans=[(a,b)]
                    else:
                        spans=[]
                        for seg in segments:
                            left=max(a,seg['source_start_sample']);right=min(b,seg['source_start_sample']+seg['samples'])
                            if right>left:
                                x=seg['output_start_sample']+left-seg['source_start_sample'];spans.append((x,x+right-left))
                    for a,b in spans:
                        if ranges and ranges[-1][1]==a: ranges[-1]=(ranges[-1][0],b)
                        else: ranges.append((a,b))
                for a,b in ranges:
                    f.seek(a);chunks.append((f.read(b-a,dtype='int16'), f.samplerate))
        except FileNotFoundError:
            continue  # expired or conversion race; caller can retry
    if not chunks or not any(len(x) for x, rate in chunks):
        raise FileNotFoundError('Audio expired, unavailable, or interval removed as silence')
    rate = max(r for _, r in chunks)
    samples = [x if r == rate else np.clip(ArchiveResampler().feed(x.astype(float)), -32768, 32767).astype('int16') for x, r in chunks]
    out=io.BytesIO();sf.write(out,np.concatenate(samples),rate,format='WAV',subtype='PCM_16')
    return out.getvalue()
