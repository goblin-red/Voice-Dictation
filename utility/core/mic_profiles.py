"""Persistent manual VAD levels, independent of automatic level estimation."""
import math


def valid(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0.002 <= value <= 0.1


def selected(cfg, default_name):
    source = cfg.get('audio_in') or {}
    return (source.get('name') if source.get('mode') == 'manual' else default_name) or 'system-default'


def apply(old, new, name, old_name=None):
    """Mutate merged configuration; never accept a stale client profile dictionary."""
    profiles = {k: v for k, v in (old.get('mic_manual_levels') or {}).items() if valid(v)}
    previous = old.get('mic_manual_active') or old_name or name
    level = old['vad']['min_level']
    if previous not in profiles and valid(level):
        profiles[previous] = level
    if not old['vad'].get('auto_level') and valid(level):
        profiles[previous] = level
    switched = name != previous
    manual_edit = not switched and not old['vad'].get('auto_level') and not new['vad'].get('auto_level')
    if manual_edit and valid(new['vad']['min_level']):
        profiles[name] = new['vad']['min_level']
    # An unseen microphone inherits the current manual level until adjusted.
    profiles.setdefault(name, profiles.get(previous, level))
    new['vad']['min_level'] = profiles[name]
    new['mic_manual_levels'] = profiles
    new['mic_manual_active'] = name
