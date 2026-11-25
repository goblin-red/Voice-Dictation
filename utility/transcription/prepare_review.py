"""Prepare the complete ZIPA/Grok source for an agent's English reconstruction."""

import argparse
import hashlib
import json
from pathlib import Path
import shlex


def stamp(seconds):
    minute, second = divmod(float(seconds), 60)
    return f'{int(minute):02d}:{second:05.2f}'


def prepare(rows_path, output_dir, chunk_size=170):
    rows_path, output_dir = Path(rows_path).resolve(), Path(output_dir).resolve()
    if chunk_size < 1:
        raise ValueError('chunk_size must be positive')
    source = json.loads(rows_path.read_text())
    rows = source['rows']
    if not rows or [row['index'] for row in rows] != list(range(1, len(rows) + 1)):
        raise ValueError('Source rows must be numbered consecutively from 1')
    output_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for start in range(0, len(rows), chunk_size):
        selected = rows[start:start + chunk_size]
        name = f'sources-{len(files) + 1:03d}.txt'
        lines = [f'Source rows {selected[0]["index"]}–{selected[-1]["index"]} of {len(rows)}.',
                 'Use ZIPA as the primary wording and Grok as a second reading.',
                 'Use context to verify Grok speaker labels.', '']
        for row in selected:
            lines.extend([
                f'{row["index"]:03d} | Grok speaker {row["speaker"]} | {stamp(row["start"])}–{stamp(row["end"])}',
                'ZIPA: ' + (row['zipa'].strip() or '[not recognized]'),
                'Grok: ' + row['grok'], ''])
        (output_dir / name).write_text('\n'.join(lines) + '\n')
        files.append({'file': name, 'first': selected[0]['index'], 'last': selected[-1]['index'],
                      'rows': len(selected)})
    summary = {'status': 'agent_review_required', 'source': str(rows_path),
               'source_sha256': hashlib.sha256(rows_path.read_bytes()).hexdigest(),
               'source_rows': len(rows), 'files_to_read_completely': files,
               'editable_dialogue': str(output_dir / 'dialogue.txt')}
    (output_dir / 'review-input.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    renderer = Path(__file__).resolve().with_name('render_review.py')
    command = ' '.join(shlex.quote(str(item)) for item in [
        'python3', renderer, '--rows', rows_path, '--dialogue', output_dir / 'dialogue.txt',
        '--output', output_dir / 'dialogue-reviewed.html'])
    note = f'''# Review the English dialogue

The recording has been transcribed and is ready for review.
Read every sources-*.txt file listed in review-input.json. The files contain {len(rows)} source rows.


Use ZIPA as the primary wording and Grok as a second reading. Identify speakers from context,
including sentence continuations, questions, and replies. Verify each Grok speaker label.
Keep the entire conversation. Mark uncertain words with <u>...</u>.

Write dialogue.txt with one English turn per line in the form `first-last|speaker|text`. Use 1 or 2 for the speaker.
Cover all source rows exactly once and keep them in order.
Keep your edits to dialogue.txt when preparing the source again.

Render the reviewed text:

```sh
{command}
```

Use --speaker-1 and --speaker-2 to set speaker names.
The HTML shows the full dialogue, one color per speaker, with uncertain words underlined.
'''
    (output_dir / 'REVIEW.md').write_text(note)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--chunk-size', type=int, default=170)
    args = parser.parse_args()
    output_dir = args.output_dir or args.rows.parent / 'review'
    print(json.dumps(prepare(args.rows, output_dir, args.chunk_size), ensure_ascii=False))


if __name__ == '__main__':
    main()
