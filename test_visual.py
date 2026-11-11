"""python test_visual.py — dibuja la isla en todos sus estados (sin mostrarla) y comprueba que nada falle y que
cada dibujo tenga contenido. Guarda las imágenes en test_visual/ para mirarlas si algo se ve raro."""
import io
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["ISLA_PORT"] = "47870"  # no choca con la isla abierta
import island  # noqa: E402

OUT = Path(__file__).parent / "test_visual"
OUT.mkdir(exist_ok=True)
island.CONFIG = Path(tempfile.gettempdir()) / "isla-test-visual.json"
island.CONFIG.unlink(missing_ok=True)
LANG = os.environ.get("ISLA_LANG", "es")  # ISLA_LANG=en python test_visual.py: todo en inglés
island.CONFIG.write_text('{"language": "%s"}' % LANG, encoding="utf-8")
OUT = OUT / LANG
OUT.mkdir(exist_ok=True)

isl = island.Island()
isl.root.withdraw()
isl.check_privacy = lambda: None  # sin leer el registro real
isl.check_clipboard = lambda: None
isl.cfg["hide_fullscreen"] = False  # que un juego o video abierto no oculte la isla durante el test
isl.hidden = False
isl.bat, isl.cpu_pct, isl.ram_pct = None, 14, 62
m = isl.media
m.title, m.artist, m.playing, m.end, m.pos, m.stamp = "Canción de prueba", "Artista", True, 200, 64, time.time()
isl.weather.temp, isl.weather.icon = 24, "☁"
cc = isl.claude
cc.handle({"session_id": "a", "cwd": "C:/p/webshop", "hook_event_name": "PreToolUse", "tool_name": "Edit",
           "tool_input": {"file_path": "x.ts", "old_string": "a = 0", "new_string": "a = 1"}})
failures, count = [], 0


def check(name, mode, size, setup=None):
    """Dibuja un estado; falla si tira error o si la isla quedó vacía."""
    global count
    count += 1
    try:
        if setup:
            setup()
        isl.w, isl.h = size
        isl.draw(isl.w, isl.h, time.time(), mode, size)
        surf = isl.win.surface
        surf.flush()
        data = bytes(surf.get_data())
        painted = sum(1 for i in range(3, len(data), 4 * 7) if data[i])  # alfa ≠ 0, muestreando
        surf.write_to_png(str(OUT / f"{name}.png"))
        if painted < 50:
            failures.append(f"{name}: dibujo vacío ({painted} px)")
    except Exception as e:  # el test reporta cualquier error de dibujo, sea cual sea
        failures.append(f"{name}: {type(e).__name__}: {e}")


def card(**kw):
    def setup():
        isl.card = {"until": time.time() + 9, **kw}
    return setup


big = island.BIG
check("cerrada", "small", island.SMALL)
check("abierta", "big", big)
for combo in (["claude", "music", "today", "system"], ["today", "music"], ["system"], ["music", "claude", "today"]):
    isl.cfg["widgets"] = combo
    check("widgets-" + "-".join(combo), "big", isl.big_size())
isl.cfg["widgets"] = ["claude", "music"]
buf_art = io.BytesIO()
from PIL import Image as _Image  # noqa: E402
_Image.new("RGB", (64, 64), (230, 120, 40)).save(buf_art, "PNG")  # carátula naranja: el fondo "Carátula" la usa
m.art = buf_art.getvalue()
for theme in [k for _label, k in island.THEMES]:
    isl.cfg["theme"] = theme
    isl.album_art, isl.album = None, list(island.album_color(m.art))
    check(f"fondo-{theme}", "big", big)
    check(f"fondo-{theme}-cerrada", "small", island.SMALL)
isl.cfg["theme"] = "black"
m.art = None
check("permiso", "perm", island.PERM,
      lambda: cc.perms.append({"sid": "a", "text": "git push", "done": None, "allow": None}))
cc.perms.clear()
check("pregunta", "question", (600, 92), lambda: cc.questions.append(
    {"sid": "a", "text": "¿Qué base de datos usamos?", "options": ["SQLite", "Postgres", "MySQL"], "done": None}))
cc.questions.clear()
cc.sessions["a"]["todos"] = [("Leer", "completed", ""), ("Arreglar", "in_progress", ""), ("Probar", "pending", "")]
check("abierta-tareas", "big", big)
cc.sessions["a"].pop("todos")
check("volumen", "level", island.LEVEL, lambda: isl.levels.show("vol", 40))
check("brillo", "level", island.LEVEL, lambda: isl.levels.show("sun", 70))
check("notificacion", "note", island.NOTIF, lambda: setattr(isl.notes, "current", {
    "app": "Discord", "title": "Ana", "body": "hola", "icon": None, "until": time.time() + 9}))
isl.notes.current = None
isl.updater.latest = ("9.9.9", "x", "y")
check("tarjeta-actualizacion", "card", island.NOTIF, card(kind="update"))
for icon in ("plug", "battery", "headphones", "speaker", "usb", "mic", "cam", "focus", "clip"):
    check(f"tarjeta-{icon}", "card", island.NOTIF,
          card(kind="device", icon=icon, title="Título", body="Texto", buttons=[("Botón", lambda: None, True)]))
from datetime import datetime, timedelta, timezone  # noqa: E402
check("tarjeta-cita", "card", island.NOTIF,
      card(kind="cal", title="Reunión", start=(datetime.now(timezone.utc) + timedelta(minutes=5)).astimezone()))
from PIL import Image  # noqa: E402
buf = io.BytesIO()
Image.new("RGB", (64, 36), (200, 80, 60)).save(buf, "PNG")
check("tarjeta-captura", "card", island.NOTIF, lambda: (setattr(isl, "card", None), isl.shot_card(
    Image.open(io.BytesIO(buf.getvalue())))))
isl.card = None
class FakeChat:  # la cabecera del chat, sin abrir el panel ni llamar a Claude
    busy = True
    close = new = copy_last = staticmethod(lambda: None)
isl.chat = FakeChat()
check("chat-pensando", "chat", island.PERM)
FakeChat.busy = False
check("chat", "chat", island.PERM)
isl.chat = None
check("tarjeta-recordatorio", "card", island.NOTIF, card(kind="reminder", icon="bell", title="Sacar la ropa",
                                                       body="Recordatorio · 18:30",
                                                       buttons=[("+10 min", lambda: None, False), ("Listo", lambda: None, True)]))
for step in range(len(island.TOUR)):
    check(f"tour-{step + 1}", "card", (520, 92), lambda step=step: (setattr(isl, "tour", step), isl.show_tour_step(),
                                                                    setattr(isl, "tour", None)))
isl.card = None
isl.focus.start()
check("cerrada-enfoque", "small", island.SMALL)
isl.focus.stop()
for kind in [k for _, k in island.CHARACTERS]:
    for mood in island.MOODS:
        check(f"personaje-{kind}-{mood}", "small", island.SMALL,
              lambda kind=kind, mood=mood: (isl.cfg.update(character=kind), setattr(cc, "mood_override", (mood, time.time() + 9))))
cc.mood_override = ("", 0)
isl.cfg.update(mode="companion", translucent=True, accent="#a78bfa")
isl.apply_config()
check("compañero-abierta", "big", isl.big_size())
check("compañero-cerrada", "small", island.SMALL)

try:  # la ventana de ajustes se arma sin errores
    isl.open_settings()
    isl.root.update()
    isl.settings.close()
except Exception as e:  # noqa: BLE001
    failures.append(f"ajustes: {type(e).__name__}: {e}")
count += 1

import textos  # noqa: E402
if LANG == "en" and textos.MISSING:
    failures.append("sin traducir: " + " | ".join(sorted(textos.MISSING)))
if isl.tray:
    isl.tray.stop()
print("\n".join(failures) if failures else f"ok ({count} dibujos, imágenes en {OUT.name}/)", flush=True)
os._exit(1 if failures else 0)
