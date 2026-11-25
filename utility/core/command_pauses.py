"""Общие обозначения пауз в вариантах команд и фонетических образцах."""

import re

COMMA_S = 0.22
DOT_S = 0.45


def duration(marks):
    return marks.count(",") * COMMA_S + marks.count(".") * DOT_S


def is_pause(token):
    return bool(token) and all(ch in ",." for ch in token)


def prefix(text):
    match = re.match(r"^[\s,.]*", text)
    return duration(match[0]), text[match.end():]


def before(phones, start, edge=float("inf")):
    while start < len(phones) and is_pause(phones[start]):
        start += 1
    marks = []
    i = start - 1
    while i >= 0 and is_pause(phones[i]):
        marks.append(phones[i])
        i -= 1
    if i < 0:
        return max(duration("".join(marks)), edge)
    return duration("".join(marks))


def marker(seconds):
    ticks = max(0, int(seconds * 100 + 1e-6))
    _, _, dots, commas = max(
        (d * 45 + ((ticks - d * 45) // 22) * 22,
         -d - (ticks - d * 45) // 22, d, (ticks - d * 45) // 22)
        for d in range(ticks // 45 + 1)
    )
    return "." * dots + "," * commas


def permits(required, available):
    return available + 1e-8 >= required
