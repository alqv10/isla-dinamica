"""Estadísticas del día: cuánto trabajó Claude, rondas de enfoque y canciones escuchadas. Se guardan por día en
%LOCALAPPDATA%\\IslaDinamica\\estadisticas.json (los últimos 30 días) y solo quedan en tu PC."""
import json
import os
import threading
from datetime import date
from pathlib import Path

PATH = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "IslaDinamica" / "estadisticas.json"
EMPTY = {"claude_secs": 0, "claude_turns": 0, "focus": 0, "songs": [], "summary_shown": False}


class Stats:
    def __init__(self, path=None):
        self.path, self.lock = path or PATH, threading.Lock()  # PATH se lee acá: los tests lo cambian
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def today(self):
        day = self.data.setdefault(date.today().isoformat(), {})
        for k, v in EMPTY.items():
            day.setdefault(k, list(v) if isinstance(v, list) else v)
        return day

    def add(self, field, amount=1):
        with self.lock:
            self.today()[field] += amount
            self.save()

    def song(self, key):
        """Cuenta una canción (título · artista) una sola vez por día."""
        with self.lock:
            songs = self.today()["songs"]
            if key not in songs:
                songs.append(key)
                self.save()

    def mark(self, field):
        with self.lock:
            self.today()[field] = True
            self.save()

    def save(self):
        for old in sorted(self.data)[:-30]:  # solo los últimos 30 días
            del self.data[old]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
