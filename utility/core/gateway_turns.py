"""Exact Codex receipt -> turn -> completion tracking; never use the latest answer."""
import datetime
import json
import re
from pathlib import Path


class TurnTracker:
    def __init__(self, paths, expected, since):
        self.paths = paths
        self.expected = expected
        self.since = since
        self.offsets = {}
        self.baselines = {}
        self.audited = set()
        self.turns = {}
        self.matches = set()
        for path in paths():
            try:
                self.offsets[str(path)] = Path(path).stat().st_size
                self.baselines[str(path)] = self.offsets[str(path)]
            except OSError:
                pass

    def poll(self):
        for path in self.paths():
            path = str(path)
            try:
                if Path(path).stat().st_size == self.offsets.get(path, -1):
                    continue
                with open(path, 'rb') as stream:
                    offset = self.offsets.get(path, 0)
                    stream.seek(0, 2)
                    if stream.tell() < offset:
                        return {'stage': 'error', 'reason': 'The session log was truncated'}
                    stream.seek(offset)
                    data = stream.read(4 * 1024 * 1024)
                end = data.rfind(b'\n')
                if end < 0:
                    continue
                self.offsets[path] = offset + end + 1
                for raw in data[:end].split(b'\n'):
                    try:
                        event = json.loads(raw)
                        ts = datetime.datetime.fromisoformat(event.get('timestamp', '').replace('Z', '+00:00')).timestamp()
                    except (ValueError, TypeError):
                        continue
                    if ts < self.since - 2:
                        continue
                    self._event(path, event)
            except OSError:
                continue
        if len(self.matches) > 1:
            return {'stage': 'error', 'reason': 'The same message was found in multiple turns or sessions'}
        if not self.matches:
            return {'stage': 'delivered'}
        path, turn_id = next(iter(self.matches))
        if (path, turn_id) not in self.audited:
            try:
                with open(path, 'rb') as stream:
                    boundary = self.baselines.get(path, 0)
                    while stream.tell() < boundary:
                        raw = stream.readline()
                        if not raw:
                            break
                        try:
                            event = json.loads(raw)
                        except ValueError:
                            continue
                        payload = event.get('payload') or {}
                        identity = payload.get('turn_id') or (payload.get('internal_chat_message_metadata_passthrough') or {}).get('turn_id')
                        identity = identity or (((event.get('metadata') or {}).get('retained_source') or {}).get('id') or {}).get('turn_id')
                        if identity == turn_id:
                            self._event(path, event)
                self.audited.add((path, turn_id))
            except OSError:
                return {'stage':'error','reason':'Could not verify the turn history'}
        turn = self.turns[(path, turn_id)]
        result = {'stage': 'accepted', 'path': path, 'turn_id': turn_id}
        sid = re.search(r'([0-9a-f-]{36})\.jsonl$', path)
        result['session'] = sid.group(1) if sid else ''
        if turn.get('aborted'):
            return {**result, 'stage': 'error', 'reason': 'Agent turn interrupted'}
        if turn['inputs'] != {self.expected}:
            return {**result, 'stage': 'error', 'reason': 'A gateway message and another message appear in the same turn'}
        if turn.get('complete'):
            answer = turn.get('answer', '').strip()
            if not answer:
                return {**result, 'stage': 'error', 'reason': 'The turn ended without a text reply'}
            return {**result, 'stage': 'completed', 'answer': answer}
        return result

    def _event(self, path, event):
        payload = event.get('payload') or {}
        kind = event.get('type')
        turn_id, text = None, None
        if kind == 'response_item' and payload.get('type') == 'message' and payload.get('role') == 'user':
            meta = payload.get('internal_chat_message_metadata_passthrough') or {}
            kinds = meta.get('content_item_kinds') or []
            origins = meta.get('content_item_metadata') or []
            # Codex injects changed AGENTS.md into the same turn. Only explicit harness
            # provenance is context, never a user message merely starting with that text.
            if ((event.get('metadata') or {}).get('client_authored') is False and kinds
                    and all(k in ('agents_md.instructions', 'environments.environment_context') for k in kinds)
                    and len(origins) == len(kinds)
                    and all((x.get('provenance') or {}).get('type') in ('agents_md', 'harness') for x in origins)):
                return
            turn_id = (payload.get('internal_chat_message_metadata_passthrough') or {}).get('turn_id')
            turn_id = turn_id or (((event.get('metadata') or {}).get('retained_source') or {}).get('id') or {}).get('turn_id')
            text = ' '.join(b.get('text', '') for b in payload.get('content', []) if isinstance(b, dict))
        elif kind == 'event_msg' and payload.get('type') == 'item_completed' and (payload.get('item') or {}).get('type') == 'UserMessage':
            turn_id = payload.get('turn_id')
            text = ' '.join(b.get('text', '') for b in payload['item'].get('content', []) if isinstance(b, dict))
        elif kind == 'event_msg' and payload.get('type') in ('task_complete', 'turn_aborted', 'task_aborted', 'task_failed'):
            turn_id = payload.get('turn_id')
        if not turn_id:
            return
        key = (path, turn_id)
        turn = self.turns.setdefault(key, {'inputs': set()})
        if text is not None:
            turn['inputs'].add(text)
            if text == self.expected:
                self.matches.add(key)
        elif payload.get('type') == 'task_complete':
            turn['complete'] = True
            turn['answer'] = payload.get('last_agent_message') or ''
        else:
            turn['aborted'] = True
