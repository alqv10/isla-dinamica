"""Recordatorios en lenguaje natural: "recuérdame en 20 min sacar la ropa", "avísame a las 18:30 que llame a
mamá", "recuérdame mañana a las 9 pagar la luz", "remind me in 1 hour to stretch". Se escriben en el chat
de la isla (o desde el menú) y la isla los resuelve sola, sin preguntarle a Claude."""
import re
from datetime import datetime, timedelta

START = re.compile(r"^\s*(recu[eé]rdame|av[ií]same|remind me)\b[\s,:]*(?:que\s+|to\s+|de\s+)?", re.I)
WORD_NUMBERS = {"un": 1, "una": 1, "uno": 1, "a": 1, "an": 1, "one": 1, "dos": 2, "two": 2, "tres": 3, "three": 3,
                "cinco": 5, "five": 5, "diez": 10, "ten": 10, "quince": 15, "veinte": 20, "treinta": 30, "media": 0.5,
                "half an": 0.5}
UNITS = {"s": 1 / 60, "seg": 1 / 60, "segundo": 1 / 60, "segundos": 1 / 60, "second": 1 / 60, "seconds": 1 / 60,
         "m": 1, "min": 1, "mins": 1, "minuto": 1, "minutos": 1, "minute": 1, "minutes": 1,
         "h": 60, "hr": 60, "hrs": 60, "hora": 60, "horas": 60, "hour": 60, "hours": 60}
IN = re.compile(r"\b(?:en|in|dentro de)\s+(\d+(?:[.,]\d+)?|un|una|uno|a|an|one|dos|two|tres|three|cinco|five|diez|ten|"
                r"quince|veinte|treinta|media|half an)\s*(segundos?|seg|s|minutos?|mins?|m|horas?|hrs?|h|seconds?|"
                r"minutes?|hours?)\b", re.I)
AT = re.compile(r"\b(?:a\s+las|a\s+la|at)\s+(\d{1,2})(?:[:.h](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.|de la mañana|"
                r"de la tarde|de la noche)?", re.I)
TOMORROW = re.compile(r"\b(mañana|tomorrow)\b", re.I)


def parse(text, now=None):
    """(cuándo, qué) si el texto es un recordatorio, o None. "qué" queda sin la parte del tiempo."""
    now = now or datetime.now()
    start = START.match(text)
    if not start:
        return None
    rest = text[start.end():]
    when, spans = None, []  # partes del texto que dicen "cuándo": se quitan al final, de atrás hacia adelante
    m = IN.search(rest)
    if m:
        amount = m.group(1).lower().replace(",", ".")
        n = float(amount) if amount[0].isdigit() else WORD_NUMBERS[amount]
        when = now + timedelta(minutes=n * UNITS[m.group(2).lower()])
        spans.append(m.span())
    else:
        m = AT.search(rest)
        if m:
            hour, minute, suffix = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
            if suffix.startswith(("pm", "p.m", "de la tarde", "de la noche")) and hour < 12:
                hour += 12
            if suffix.startswith(("am", "a.m", "de la mañana")) and hour == 12:
                hour = 0
            if hour > 23 or minute > 59:
                return None
            when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            spans.append(m.span())
            tomorrow = TOMORROW.search(rest)
            if tomorrow or when <= now:  # "a las 9" ya pasó hoy: mañana
                when += timedelta(days=1)
            if tomorrow:
                spans.append(tomorrow.span())
        else:
            tomorrow = TOMORROW.search(rest)
            if tomorrow:  # "mañana" sin hora: a las 9
                when = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
                spans.append(tomorrow.span())
    if when is None:
        return None
    for a, b in sorted(spans, reverse=True):
        rest = rest[:a] + " " + rest[b:]
    what = re.sub(r"^\s*(?:que|de|to|a)\s+", "", re.sub(r"\s+", " ", rest)).strip(" ,.:;")
    return when, what or "…"
