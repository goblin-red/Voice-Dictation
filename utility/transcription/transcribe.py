from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import argparse
import hashlib
import io
import json
import mimetypes
import re
import subprocess
import sys
import time
import wave

parser = argparse.ArgumentParser(description='ZIPA + Grok: audio file to a simple colour-coded HTML dialogue.')
parser.add_argument('audio', type=Path)
parser.add_argument('--output-dir', type=Path, default=Path.cwd())
parser.add_argument('--parts', type=int, default=1)
parser.add_argument('--start', type=float, default=0.0, help='Sample start in seconds; default: whole recording')
parser.add_argument('--duration', type=float, help='Sample duration in seconds')
parser.add_argument('--prefix', default='transcription')
parser.add_argument('--cache-dir', type=Path)
parser.add_argument('--expected-speakers', type=int, default=2)
parser.add_argument('--workers', type=int, default=2)
args = parser.parse_args()
if args.parts < 1 or args.workers < 1 or args.start < 0 or (args.duration is not None and args.duration <= 0):
    parser.error('parts/workers/duration must be positive, start must be nonnegative')
if not re.fullmatch(r'[\w-]+', args.prefix):
    parser.error('prefix must contain only letters, digits, underscores or hyphens')
HERE = Path(__file__).resolve().parent
ROOT = args.output_dir.expanduser().resolve()
ROOT.mkdir(parents=True, exist_ok=True)
VOICE = HERE.parent
SOURCE = args.audio.expanduser().resolve()
if not SOURCE.is_file():
    parser.error('audio file does not exist')
sys.path.insert(0, str(VOICE / 'core'))
import grok_voice

config = json.loads((VOICE / 'data' / 'config.json').read_text())
source_hash = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
rate = 16000
source_duration = float(subprocess.check_output([
    'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
    '-of', 'default=noprint_wrappers=1:nokey=1', str(SOURCE)], text=True))
if args.start >= source_duration:
    parser.error('sample starts after the end of the recording')
offset = args.start
duration = min(args.duration if args.duration is not None else source_duration, source_duration - offset)
windowed = offset > 0 or args.duration is not None
cache_id = source_hash[:16]
if windowed:
    cache_id += '-' + hashlib.sha256(f'{offset:.6f}-{duration:.6f}'.encode()).hexdigest()[:12]
WORK = (args.cache_dir.expanduser().resolve() if args.cache_dir else HERE / 'runs' / cache_id)
WORK.mkdir(parents=True, exist_ok=True)

def report(stage, **values):
    print(json.dumps({'stage': stage, **values}, ensure_ascii=False), flush=True)

def save_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)

def grok_full():
    cache = WORK / ('grok-full-' + cache_id + '.json')
    if cache.exists():
        result = json.loads(cache.read_text())
        report('grok_cached', words=len(result.get('words', [])))
        return result
    fields = [('model', 'grok-voice-transcribe-2.0'), ('language', 'en'), ('format', 'false'),
              ('diarize', 'true'), ('filler_words', 'true'), ('vad_threshold', '0.15')]
    boundary = 'transcript_full_' + str(time.time_ns())
    body = bytearray()
    for name, value in fields:
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
    if windowed:
        audio = subprocess.check_output(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-ss', str(offset),
                                         '-i', str(SOURCE), '-t', str(duration), '-vn', '-ac', '1',
                                         '-ar', str(rate), '-f', 'wav', '-'])
        filename, content_type = 'sample.wav', 'audio/wav'
    else:
        audio = SOURCE.read_bytes()
        filename = 'audio' + SOURCE.suffix
        content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
             f'Content-Type: {content_type}\r\n\r\n').encode()
    body += audio + f'\r\n--{boundary}--\r\n'.encode()
    request = Request(grok_voice.API + '/stt', data=bytes(body), method='POST', headers={
        'Authorization': 'Bearer ' + grok_voice.api_key(config),
        'Content-Type': 'multipart/form-data; boundary=' + boundary})
    report('grok_started', seconds=duration)
    started = time.monotonic()
    with urlopen(request, timeout=240) as response:
        result = json.load(response)
    if not result.get('words') or any('speaker' not in word for word in result['words']):
        raise RuntimeError('Grok returned no speaker timestamps')
    if abs(float(result.get('duration', duration)) - duration) > 1.0:
        raise RuntimeError('Grok response duration differs from the selected recording')
    save_json(cache, result)
    report('grok_done', elapsed=round(time.monotonic() - started, 1),
           words=len(result['words']), speakers=sorted({word['speaker'] for word in result['words']}))
    return result

def decode_audio():
    target = WORK / ('audio-' + cache_id + '.pcm')
    if not target.exists():
        temporary = target.with_suffix('.pcm.tmp')
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-ss', str(offset), '-i', str(SOURCE),
                        '-t', str(duration), '-vn', '-ac', '1', '-ar', str(rate), '-f', 's16le', str(temporary)], check=True)
        temporary.replace(target)
    raw = target.read_bytes()
    report('audio_decoded', seconds=round(len(raw) / (rate * 2), 3))
    return raw

with ThreadPoolExecutor(max_workers=2) as pool:
    grok_job = pool.submit(grok_full)
    audio_job = pool.submit(decode_audio)
    grok = grok_job.result()
    raw = audio_job.result()

words = grok['words']
speaker_map = {}
for word in words:
    if word['speaker'] not in speaker_map:
        speaker_map[word['speaker']] = len(speaker_map) + 1
if len(speaker_map) != args.expected_speakers:
    report('speaker_review_required', speakers=speaker_map)
    raise RuntimeError('Speaker count differs from expected; review cached Grok response before continuing')

groups = []
for word in words:
    if not groups:
        groups.append([])
    elif (word['speaker'] != groups[-1][-1]['speaker']
          or float(word['start']) - float(groups[-1][-1]['end']) >= 0.8
          or re.search(r'[.!?…][»”")\]]?$', groups[-1][-1]['text'])
          or float(word['end']) - float(groups[-1][0]['start']) > 16):
        groups.append([])
    groups[-1].append(word)

rows = []
for index, group in enumerate(groups):
    start = offset + max(0.0, min(float(word['start']) for word in group))
    end = offset + min(duration, max(float(word['end']) for word in group))
    left, right = max(offset, start - 0.12), min(offset + duration, end + 0.12)
    if index:
        previous_end = offset + max(float(word['end']) for word in groups[index - 1])
        if previous_end <= start:
            left = max(left, (previous_end + start) / 2)
    if index + 1 < len(groups):
        next_start = offset + min(float(word['start']) for word in groups[index + 1])
        if next_start >= end:
            right = min(right, (end + next_start) / 2)
    rows.append({'index': index + 1, 'speaker': speaker_map[group[0]['speaker']],
                 'grok_speaker_id': group[0]['speaker'], 'start': start, 'end': end,
                 'audio_start': left, 'audio_end': right, 'word_count': len(group),
                 'grok': ' '.join(word['text'] for word in group)})

if sum(row['word_count'] for row in rows) != len(words):
    raise RuntimeError('Word count mismatch')
save_json(WORK / 'rows-before-zipa.json', rows)
report('utterances_ready', utterances=len(rows), speakers=speaker_map)
zipa_cache = WORK / 'zipa'
zipa_cache.mkdir(exist_ok=True)

def zipa_row(row):
    pcm = raw[round((row['audio_start'] - offset) * rate) * 2:round((row['audio_end'] - offset) * rate) * 2]
    if len(pcm) < 3200:
        # ZIPA needs at least 0.1 seconds for short utterances.
        extra = 3200 - len(pcm)
        pcm += b'\0' * extra
    signature = hashlib.sha256(pcm).hexdigest()[:16]
    cache = zipa_cache / (f'{row["index"]:04d}-' + signature + '.json')
    if cache.exists():
        result = json.loads(cache.read_text())
    else:
        target = io.BytesIO()
        with wave.open(target, 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(rate)
            stream.writeframes(pcm)
        for attempt in range(3):
            try:
                request = Request('http://127.0.0.1:%d/ipa' % config.get('zipa_port', 8796),
                                  data=target.getvalue(), method='POST', headers={'Content-Type': 'audio/wav'})
                with urlopen(request, timeout=60) as response:
                    result = json.load(response)
                if 'error' in result:
                    raise RuntimeError(result['error'])
                save_json(cache, result)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(0.5)
    row['zipa'] = result.get('ipa', '')
    return row

started = time.monotonic()
with ThreadPoolExecutor(max_workers=args.workers) as pool:
    futures = {pool.submit(zipa_row, row): row['index'] for row in rows}
    completed = 0
    for future in as_completed(futures):
        row = future.result()
        rows[row['index'] - 1] = row
        completed += 1
        if completed % 20 == 0 or completed == len(rows):
            report('zipa_progress', completed=completed, total=len(rows),
                   elapsed=round(time.monotonic() - started, 1))

save_json(WORK / 'transcript-full.json', {
    'source': str(SOURCE), 'source_sha256': source_hash, 'duration': duration,
    'sample_start': offset, 'sample_end': offset + duration,
    'grok_model': 'grok-voice-transcribe-2.0', 'speaker_map': speaker_map,
    'word_count': len(words), 'rows': rows})

# Split into similarly sized parts at turn boundaries.
if len(rows) < args.parts:
    raise RuntimeError('Not enough utterances for the requested number of HTML files')
cuts = [0]
for part in range(1, args.parts):
    target = offset + duration * part / args.parts
    lower = cuts[-1] + 1
    upper = len(rows) - (args.parts - part)
    cut = min(range(lower, upper + 1), key=lambda index: abs(rows[index]['start'] - target))
    cuts.append(cut)
cuts.append(len(rows))

template = (HERE / 'templates/simple.html').read_text()
parts = []
for part in range(args.parts):
    selected = rows[cuts[part]:cuts[part + 1]]
    table_rows = []
    for row in selected:
        phonetics = escape(row['zipa']) if row['zipa'].strip() else 'not recognized'
        table_rows.append(f'<tr class="speaker-{row["speaker"]}"><td>{row["speaker"]}</td>'
                          f'<td>{escape(row["grok"])}</td><td class="zipa" lang="und-fonipa">{phonetics}</td></tr>')
    title = f'Transcript · {part + 1} of {args.parts}' if args.parts > 1 else 'Transcript'
    page = template.replace('{{TITLE}}', escape(title)).replace('{{ROWS}}', '\n'.join(table_rows))
    filename = f'{args.prefix}-{part + 1}.html' if args.parts > 1 else f'{args.prefix}.html'
    output = ROOT / filename
    temporary = output.with_suffix('.html.tmp')
    temporary.write_text(page)
    temporary.replace(output)
    parts.append({'file': str(output), 'utterances': len(selected), 'start': selected[0]['start'],
                  'end': selected[-1]['end'], 'bytes': output.stat().st_size})

manifest = {'parts': parts, 'utterances': len(rows), 'words': len(words),
            'speakers': len(speaker_map), 'empty_zipa_rows': [row['index'] for row in rows if not row['zipa'].strip()]}
from prepare_review import prepare

review_materials = prepare(WORK / 'transcript-full.json', WORK / 'review')
manifest['workflow_stage'] = 'recognition_done_agent_review_required'
manifest['review_materials'] = review_materials
save_json(WORK / 'manifest.json', manifest)
report('recognition_complete', **manifest)
