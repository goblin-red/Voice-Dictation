"""Render a manually reviewed English dialogue and check source-row coverage."""

import argparse
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import re


class Underlines(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag != 'u' or attrs or self.depth:
            raise ValueError('Only balanced, unnested <u> tags are allowed')
        self.depth = 1
        self.output.append('<u>')

    def handle_endtag(self, tag):
        if tag != 'u' or not self.depth:
            raise ValueError('Unbalanced underline')
        self.depth = 0
        self.output.append('</u>')

    def handle_data(self, data):
        self.output.append(escape(data))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=Path, required=True)
    parser.add_argument('--dialogue', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--speaker-1', default='Speaker 1')
    parser.add_argument('--speaker-2', default='Speaker 2')
    args = parser.parse_args()
    source = json.loads(args.rows.read_text())
    turns = []
    expected = 1
    names = {1: args.speaker_1, 2: args.speaker_2}
    for line in args.dialogue.read_text().splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r'(\d+)-(\d+)\|([12])\|(.+)', line)
        if not match:
            raise ValueError('Expected: first-last|speaker|text')
        first, last, speaker = map(int, match.group(1, 2, 3))
        if first != expected or last < first:
            raise ValueError(f'Source coverage mismatch at {first}-{last}; expected {expected}')
        markup = Underlines()
        markup.feed(match[4])
        markup.close()
        if markup.depth:
            raise ValueError('Unclosed underline')
        turns.append({'first': first, 'last': last, 'speaker': speaker,
                      'html': ''.join(markup.output)})
        expected = last + 1
    if expected != len(source['rows']) + 1:
        raise ValueError('The dialogue does not cover the complete source transcript')
    body = '\n'.join(
        f'<tr class="speaker-{turn["speaker"]}"><th scope="row">{escape(names[turn["speaker"]])}</th>'
        f'<td>{turn["html"]}</td></tr>' for turn in turns)
    template = Path(__file__).resolve().parent / 'templates/dialogue.html'
    page = template.read_text().replace('{{ROWS}}', body)
    args.output.write_text(page)
    provenance = {'source': source['source'], 'source_rows': len(source['rows']),
                  'speaker_assignment': 'inferred from conversational context, not verified speaker recognition',
                  'names': names, 'turns': turns}
    args.dialogue.with_name('dialogue-reviewed.json').write_text(json.dumps(provenance, ensure_ascii=False, indent=2))
    print(json.dumps({'output': str(args.output), 'turns': len(turns), 'source_rows': len(source['rows']),
                      'uncertain_spans': page.count('<u>')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
