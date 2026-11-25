"""Migrate retired model commands and the two speech switches."""
import copy
import re

REMOVED_MODEL_COMMANDS = (
    'dict_whisper', 'dict_gigaam', 'dict_openai', 'dict_apple',
    'read_system', 'read_openai', 'read_silero',
)


def migrate_commands(cfg, defaults):
    before = copy.deepcopy(cfg)
    commands = cfg.setdefault('commands', {})
    retired_words = set()
    for name in REMOVED_MODEL_COMMANDS:
        old = commands.pop(name, {})
        for lang in ('ru', 'en'):
            retired_words.update(str(w).casefold() for w in old.get('words_' + lang, []))
        cfg.get('zipa_templates', {}).pop(name, None)
    if cfg.get('speaker_commands_revision', 0) < 1:
        old = commands.get('read_off', {})
        retired_words.update(str(w).casefold() for w in old.get('words_ru', []))
        replacement = copy.deepcopy(defaults['commands']['read_off'])
        replacement.update({k: v for k, v in old.items() if k not in ('parts_ru', 'words_ru', 'words')})
        commands['read_off'] = replacement
        commands.setdefault('read_on', copy.deepcopy(defaults['commands']['read_on']))
        # The old IPA samples describe “выключи озвучку”, not the renamed command.
        cfg.get('zipa_templates', {}).pop('read_off', None)
        cfg['speaker_commands_revision'] = 1
    if retired_words:
        cfg['hint_extra'] = ', '.join(w.strip() for w in re.split(r'[,;\n]+', cfg.get('hint_extra', ''))
                                     if w.strip() and w.strip().casefold() not in retired_words)
    return cfg != before
