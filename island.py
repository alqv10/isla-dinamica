"""Isla dinámica para Windows conectada a Claude Code: un robot muestra qué hace Claude (leyendo,
editando, ejecutando…), y los pedidos de permiso se aprueban desde la isla. Además: reloj, fecha,
batería (o CPU/RAM en PCs de escritorio), música con carátula, temporizador, volumen, brillo y
notificaciones de Windows.
Hover = expandir, rueda del mouse = minutos del temporizador, clic derecho = ajustes.
Conectar con Claude Code: python install_hooks.py"""
import asyncio
import ctypes
import io
import json
import math
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import winsound
from ctypes import wintypes
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cairo

try:
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as MediaManager
    from winrt.windows.storage.streams import Buffer, InputStreamOptions
except ImportError:
    MediaManager = None  # sin winrt: sin música
try:
    from winrt.windows.foundation import Size
    from winrt.windows.ui.notifications import KnownNotificationBindings, NotificationKinds
    from winrt.windows.ui.notifications.management import UserNotificationListener
except ImportError:
    UserNotificationListener = None  # sin notificaciones
try:
    from PIL import Image, ImageDraw, ImageTk
except ImportError:
    Image = None  # sin Pillow: sin carátula

PORT = int(os.environ.get("ISLA_PORT", 47823))  # el mismo en claude_hook.py
PERMISSION_WAIT = 100  # s esperando un clic antes de devolver el permiso a la terminal
SMALL, BIG, PERM = (230, 34), (480, 214), (520, 44)  # en px lógicos (a 100 % de escala)
LEVEL, NOTIF = (300, 40), (440, 64)  # barra de volumen/brillo y notificación
CONFIG = Path.home() / ".isla-dinamica.json"
DEFAULTS = {"size": 1.0, "sound": True, "hide_fullscreen": True, "notifications": True}
STARTUP = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup/Isla dinamica.lnk"
PLAYING = 4  # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING
GREEN, GREY, FIELD, RED, WHITE = "#3ddc84", "#9a9a9f", "#1c1c1e", "#ff5555", "#ffffff"
DIFF_BG = {RED: "#3a1c1f", GREEN: "#14301f"}  # fondo de las líneas del diff
MOODS = {"happy": GREEN, "focused": "#f5a524", "curious": "#5ab4ff", "love": "#ff5c8a", "sleep": GREY}
DAYS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


# ---------- sistema ----------

class PowerStatus(ctypes.Structure):
    _fields_ = [("ac", ctypes.c_ubyte), ("flag", ctypes.c_ubyte), ("percent", ctypes.c_ubyte),
                ("saver", ctypes.c_ubyte), ("life", ctypes.c_ulong), ("full", ctypes.c_ulong)]


class MemoryStatus(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + \
               [(f"x{i}", ctypes.c_ulonglong) for i in range(7)]


def battery():
    """(porcentaje, cargando) o None en PCs sin batería (escritorio)."""
    s = PowerStatus()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))
    return None if s.percent == 255 or s.flag & 128 else (s.percent, s.ac == 1)


def ram():
    m = MemoryStatus(length=ctypes.sizeof(MemoryStatus))
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.load


class Cpu:
    """% de CPU desde la última llamada (GetSystemTimes: el tiempo kernel ya incluye idle)."""
    def __init__(self):
        self.prev = self.times()

    @staticmethod
    def times():
        idle, kernel, user = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        return idle.value, kernel.value + user.value

    def percent(self):
        (i0, t0), (i1, t1) = self.prev, self.times()
        self.prev = (i1, t1)
        return 0 if t1 == t0 else round(100 * (1 - (i1 - i0) / (t1 - t0)))


def mmss(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


# ---------- música ----------

class Media:
    def __init__(self):
        self.title = self.artist = ""
        self.playing = False
        self.pos = self.end = 0.0  # segundos
        self.stamp = time.time()  # cuándo se leyó pos
        self.art, self.art_key = None, None  # bytes de la carátula y (título, artista) al que pertenece
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        if MediaManager:
            asyncio.run_coroutine_threadsafe(self.poll(), self.loop)

    async def poll(self):
        # ponytail: sondeo cada 1 s; usar los eventos *_changed de WinRT si hace falta menos latencia
        mgr = await MediaManager.request_async()
        while True:
            try:
                s = mgr.get_current_session()
                if s:
                    p = await s.try_get_media_properties_async()
                    self.title, self.artist = p.title, p.artist
                    self.playing = s.get_playback_info().playback_status == PLAYING
                    t = s.get_timeline_properties()
                    pos = t.position.total_seconds()
                    if self.playing:  # las apps reportan la posición de a ratos: sumar lo transcurrido
                        pos += (datetime.now(timezone.utc) - t.last_updated_time).total_seconds()
                    self.end = t.end_time.total_seconds()
                    self.pos, self.stamp = min(pos, self.end), time.time()
                    key = (p.title, p.artist)
                    if key != self.art_key or self.art is None:  # algunas apps mandan la carátula tarde
                        self.art, self.art_key = await self.thumbnail(p.thumbnail), key
                else:
                    self.title, self.artist, self.playing, self.pos, self.end = "", "", False, 0, 0
                    self.art = self.art_key = None
            except OSError:
                pass  # la sesión desapareció entre llamadas; el próximo ciclo lo corrige
            await asyncio.sleep(1)

    @staticmethod
    async def thumbnail(ref):
        if not ref:
            return None
        stream = await ref.open_read_async()
        buf = Buffer(stream.size)
        return bytes(await stream.read_async(buf, buf.capacity, InputStreamOptions.READ_AHEAD)) or None

    def send(self, cmd):
        async def go():
            s = (await MediaManager.request_async()).get_current_session()
            if s:
                await getattr(s, cmd)()
        if MediaManager:
            asyncio.run_coroutine_threadsafe(go(), self.loop)


class Timer:
    def __init__(self):
        self.minutes = 5
        self.ends = None  # time.time() en que termina, o None si está parado

    def left(self):
        return None if self.ends is None else self.ends - time.time()

    def toggle(self):
        self.ends = None if self.ends else time.time() + self.minutes * 60

    def scroll(self, up):
        if not self.ends:
            self.minutes = max(1, min(180, self.minutes + (1 if up else -1)))


# ---------- avisos de Windows ----------

class Levels:
    """Volumen y brillo: cuando cambian, la isla muestra una barra un par de segundos."""
    def __init__(self):
        self.kind, self.value, self.muted, self.until = "vol", 0, False, 0
        threading.Thread(target=self.volume, daemon=True).start()
        threading.Thread(target=self.brightness, daemon=True).start()

    def show(self, kind, value, muted=False):
        self.kind, self.value, self.muted, self.until = kind, value, muted, time.time() + 1.8

    def volume(self):
        try:
            import comtypes
            from pycaw.pycaw import AudioUtilities
            comtypes.CoInitialize()
        except ImportError:
            return
        last, ep, ep_at = None, None, 0
        while True:
            try:
                if time.time() - ep_at > 15:  # releer el dispositivo por si cambiaste de parlantes a auriculares
                    ep, ep_at = AudioUtilities.GetSpeakers().EndpointVolume, time.time()
                now = (round(ep.GetMasterVolumeLevelScalar() * 100), bool(ep.GetMute()))
                if last is not None and now != last:
                    self.show("vol", *now)
                last = now
            except (OSError, comtypes.COMError, AttributeError):
                ep_at = 0  # sin dispositivo de audio por ahora
            time.sleep(0.1)

    def brightness(self):
        try:
            import screen_brightness_control as sbc
        except ImportError:
            return
        last = None
        while True:
            try:
                now = sbc.get_brightness(display=0)[0]
            except Exception:  # la librería tira sus propios errores; monitor sin DDC/CI = sin brillo
                return
            if last is not None and now != last:
                self.show("sun", now)
            last = now
            time.sleep(1.5)  # cada lectura cuesta ~12 ms de CPU (DDC/WMI)


class Notifications:
    """Notificaciones nuevas de Windows (WhatsApp, Discord, correo…) para mostrarlas un momento."""
    def __init__(self, loop):
        self.current = None  # {"app", "title", "body", "icon", "until"}
        self.enabled = True
        if UserNotificationListener:
            asyncio.run_coroutine_threadsafe(self.poll(), loop)

    async def poll(self):
        listener = UserNotificationListener.current
        if await listener.request_access_async() != 1:  # UserNotificationListenerAccessStatus.ALLOWED
            return
        seen = None
        while True:
            try:
                found = list(await listener.get_notifications_async(NotificationKinds.TOAST))
                if seen is not None and self.enabled:
                    for n in found:
                        if n.id not in seen:
                            await self.show(n)
                seen = {n.id for n in found}
            except OSError:
                pass
            await asyncio.sleep(1)

    async def show(self, n):
        binding = n.notification.visual.get_binding(KnownNotificationBindings.toast_generic)
        # sin emojis: cairo no cambia de fuente solo y saldrían como cuadritos
        texts = ["".join(ch for ch in t.text if ord(ch) <= 0xFFFF and not 0xFE00 <= ord(ch) <= 0xFE0F
                         and not 0x2600 <= ord(ch) <= 0x27BF).strip()
                 for t in binding.get_text_elements()] if binding else []
        try:
            icon = await Media.thumbnail(n.app_info.display_info.get_logo(Size(64, 64)))
        except OSError:
            icon = None
        self.current = {"app": n.app_info.display_info.display_name, "title": first_line(texts[0] if texts else ""),
                        "body": first_line(" ".join(texts[1:]), 120), "icon": icon, "until": time.time() + 6}


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                ("ppid", wintypes.DWORD), ("prio", ctypes.c_long), ("flags", wintypes.DWORD),
                ("exe", ctypes.c_char * 260)]


def focus_terminal(pid):
    """Trae al frente la ventana de la terminal donde corre una sesión: sube por los procesos padre del
    hook hasta uno con ventana; si la cadena se corta (apps tipo Electron), busca por nombre del programa."""
    k, u = ctypes.windll.kernel32, ctypes.windll.user32
    k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = ctypes.c_void_p(k.CreateToolhelp32Snapshot(2, 0))  # TH32CS_SNAPPROCESS
    e, parent, exe = PROCESSENTRY32(), {}, {}
    e.size = ctypes.sizeof(e)
    ok = k.Process32First(snap, ctypes.byref(e))
    while ok:
        parent[e.pid], exe[e.pid] = e.ppid, e.exe.decode(errors="replace").lower()
        ok = k.Process32Next(snap, ctypes.byref(e))
    k.CloseHandle(snap)
    windows = {}  # pid -> ventana principal visible

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if u.IsWindowVisible(hwnd) and u.GetWindowTextLengthW(hwnd) and not u.GetWindow(hwnd, 4):  # sin dueño
            owner = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            windows.setdefault(owner.value, hwnd)
        return True
    u.EnumWindows(each, 0)
    chain = []
    while pid and pid not in chain:
        chain.append(pid)
        if pid in windows:
            break
        pid = parent.get(pid)
    hwnd = next((windows[p] for p in chain if p in windows), None)
    for p in reversed(chain):  # respaldo: otra ventana del mismo programa
        if hwnd:
            break
        hwnd = next((h for q, h in windows.items() if exe.get(q) == exe.get(p) and exe.get(p)), None)
    if hwnd:
        u.keybd_event(0x12, 0, 0, 0)  # Alt: Windows deja cambiar el foco solo tras una tecla
        u.keybd_event(0x12, 0, 2, 0)
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)  # SW_RESTORE
        u.SetForegroundWindow(hwnd)
    return bool(hwnd)


def fullscreen():
    """True si hay un juego, video o presentación en pantalla completa."""
    state = ctypes.c_int()
    ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
    return state.value in (2, 3, 4)  # QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE


def load_config():
    try:
        return {**DEFAULTS, **json.loads(CONFIG.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def shortcut(lnk, args=""):
    """Crea un acceso directo a la isla (al .exe, o a pythonw + island.py desde el código)."""
    if getattr(sys, "frozen", False):
        exe, args, folder = Path(sys.executable), args, Path(sys.executable).parent
    else:
        script = Path(__file__).resolve()
        exe, args, folder = Path(sys.executable).with_name("pythonw.exe"), f'"{script}" {args}'.strip(), script.parent
    Path(lnk).parent.mkdir(parents=True, exist_ok=True)
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');$s.TargetPath='{exe}';"
          f"$s.Arguments='{args}';$s.WorkingDirectory='{folder}';$s.IconLocation='{exe},0';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=subprocess.CREATE_NO_WINDOW)


def set_startup(on):
    """Crea o borra el acceso directo en la carpeta Inicio de Windows."""
    if on:
        shortcut(STARTUP)
    else:
        STARTUP.unlink(missing_ok=True)


# ---------- Claude Code ----------

def first_line(s, n=60):
    s = str(s or "").strip().splitlines()
    return s[0][:n] if s else ""


def describe(tool, inp):
    """(estado, [(línea de detalle, color)]) para mostrar una herramienta de Claude Code."""
    name = os.path.basename(str(inp.get("file_path") or inp.get("notebook_path") or ""))
    if tool in ("Edit", "MultiEdit"):
        edit = (inp.get("edits") or [inp])[0]
        return f"Editando {name}", [(name, GREY), ("- " + first_line(edit.get("old_string")), RED),
                                   ("+ " + first_line(edit.get("new_string")), GREEN)]
    if tool == "Write":
        return f"Escribiendo {name}", [(name, GREY), ("+ " + first_line(inp.get("content")), GREEN)]
    if tool in ("Read", "NotebookEdit"):
        return f"Leyendo {name}", []
    if tool in ("Bash", "PowerShell"):
        return "Ejecutando comando", [("$ " + first_line(inp.get("command")), WHITE)]
    if tool in ("Grep", "Glob"):
        return "Buscando", [(first_line(inp.get("pattern")), WHITE)]
    if tool in ("WebFetch", "WebSearch"):
        return "Navegando", [(first_line(inp.get("url") or inp.get("query")), WHITE)]
    if tool in ("Agent", "Task"):
        return "Delegando a un agente", [(first_line(inp.get("description")), WHITE)]
    return f"Usando {tool}", []


def permission_text(tool, inp):
    if tool in ("Bash", "PowerShell"):
        return first_line(inp.get("command"), 70)
    if inp.get("file_path"):
        return f"{tool} {os.path.basename(inp['file_path'])}"
    return tool


class ClaudeCode:
    """Estado de las sesiones de Claude Code, alimentado por claude_hook.py vía HTTP local."""
    def __init__(self):
        self.sessions = {}  # session_id -> {project, status, detail, mood, t}
        self.perms = []  # pedidos de permiso esperando un clic
        self.flash_until = 0  # expandir la isla un rato (al terminar Claude)
        self.mood_override = ("", 0)  # (ánimo, hasta) tras aprobar un permiso
        self.sound = True
        self.enabled = True  # False con la isla oculta
        self.lock = threading.Lock()

    def current(self):
        with self.lock:
            return max(self.sessions.values(), key=lambda s: s["t"], default=None), len(self.sessions)

    def ordered(self):
        """[(session_id, sesión)] de la más vieja a la más nueva, para recorrerlas desde la isla."""
        with self.lock:
            return sorted(self.sessions.items(), key=lambda kv: kv[1]["born"])

    def mood(self):
        mood, until = self.mood_override
        if time.time() < until:
            return mood
        if self.perms:
            return "curious"
        s, _ = self.current()
        return s["mood"] if s else "sleep"

    def answer(self, allow):
        if self.perms:
            p = self.perms[0]
            p["allow"] = allow
            p["done"].set()
            self.mood_override = ("love" if allow else "curious", time.time() + 3)

    def drop_perms(self, sid):
        for p in list(self.perms):
            if p["sid"] == sid:  # se respondió en la terminal o Claude siguió
                p["done"].set()

    def handle(self, ev):
        """Procesa un evento de hook; devuelve el cuerpo de la respuesta (bytes) para el hook."""
        name, sid = ev.get("hook_event_name"), ev.get("session_id", "?")
        tool, inp = ev.get("tool_name", ""), ev.get("tool_input") or {}
        with self.lock:
            s = self.sessions.setdefault(sid, {"project": os.path.basename(ev.get("cwd", "")) or "Claude",
                                               "status": "Sesión iniciada", "detail": [], "mood": "happy",
                                               "born": time.time(), "since": None, "took": None})
            s["t"] = time.time()
            s["pid"] = ev.get("_pid") or s.get("pid")
            if name == "SessionEnd":
                del self.sessions[sid]
            elif name == "UserPromptSubmit":
                s.update(status="Pensando…", detail=[(first_line(ev.get("prompt")), GREY)], mood="focused",
                         since=time.time(), took=None)
            elif name == "PreToolUse":
                s["status"], s["detail"] = describe(tool, inp)
                s["mood"] = "focused"
            elif name == "Notification":
                s.update(status=first_line(ev.get("message")) or "Te necesita", detail=[], mood="curious")
            elif name == "Stop":
                s.update(status="Listo ✓", detail=[(first_line(ev.get("last_assistant_message")), GREY)],
                         mood="happy", took=time.time() - s["since"] if s["since"] else None, since=None)
                self.flash_until = time.time() + 5
        if name in ("PostToolUse", "Stop", "UserPromptSubmit", "SessionEnd"):
            self.drop_perms(sid)
        if name == "Stop" and self.sound:
            winsound.MessageBeep(winsound.MB_OK)
        if name != "PermissionRequest" or not self.enabled:
            return b""  # isla oculta: los permisos se responden en la terminal sin esperar
        p = {"sid": sid, "text": permission_text(tool, inp), "done": threading.Event(), "allow": None}
        self.perms.append(p)
        p["done"].wait(PERMISSION_WAIT)
        self.perms.remove(p)
        if p["allow"] is None:
            return b""  # sin respuesta: Claude Code pregunta en la terminal como siempre
        decision = {"behavior": "allow"} if p["allow"] else \
                   {"behavior": "deny", "message": "Denegado desde la isla dinámica"}
        return json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                  "decision": decision}}).encode()


def serve(claude):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                ev = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            except ValueError:
                return self.send_error(400)
            body = claude.handle(ev)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---------- dibujo ----------
# Se dibuja con cairo (suavizado real) sobre una ventana "layered" de Windows con transparencia por
# píxel: bordes, círculos y texto en HD, y los fundidos son transparencias de verdad.
# Las coordenadas son px lógicos (escala 100 %); el contexto se escala a la pantalla real.

def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def dpi_scale():
    """Pide a Windows píxeles reales (sin esto la isla se ve borrosa al 125–200 %) y devuelve la escala."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return ctypes.windll.user32.GetDpiForSystem() / 96
    except (AttributeError, OSError):
        return 1.0


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class BLEND(ctypes.Structure):
    _fields_ = [("op", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("alpha", ctypes.c_ubyte), ("fmt", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("width", ctypes.c_long), ("height", ctypes.c_long),
                ("planes", ctypes.c_uint16), ("bits", ctypes.c_uint16), ("compression", ctypes.c_uint32),
                ("image_size", ctypes.c_uint32), ("xppm", ctypes.c_long), ("yppm", ctypes.c_long),
                ("used", ctypes.c_uint32), ("important", ctypes.c_uint32)]


class Layered:
    """Ventana con alfa por píxel: cairo dibuja directo en un bitmap de Windows y UpdateLayeredWindow
    lo muestra. Los píxeles transparentes dejan pasar los clics a lo que haya debajo."""
    def __init__(self, hwnd, W, H):
        u, g, vp = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.c_void_p
        u.GetDC.restype = g.CreateCompatibleDC.restype = g.CreateDIBSection.restype = vp
        g.CreateCompatibleDC.argtypes, g.SelectObject.argtypes = [vp], [vp, vp]
        g.CreateDIBSection.argtypes = [vp, vp, ctypes.c_uint, ctypes.POINTER(vp), vp, ctypes.c_uint32]
        u.UpdateLayeredWindow.argtypes = [vp, vp, vp, vp, vp, vp, ctypes.c_uint32, vp, ctypes.c_uint32]
        u.GetWindowLongW.argtypes, u.SetWindowLongW.argtypes = [vp, ctypes.c_int], [vp, ctypes.c_int, ctypes.c_long]
        # WS_EX_LAYERED: alfa por píxel. WS_EX_NOACTIVATE: los clics no le roban el foco a la app en la que
        # estás. WS_EX_TOOLWINDOW: no aparece en Alt+Tab ni en la barra de tareas.
        u.SetWindowLongW(hwnd, -20, u.GetWindowLongW(hwnd, -20) | 0x80000 | 0x08000000 | 0x80)
        self.u, self.hwnd, self.W, self.H = u, hwnd, W, H
        self.screen = u.GetDC(None)
        self.mem = g.CreateCompatibleDC(self.screen)
        bmi, bits = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), W, -H, 1, 32), vp()
        g.SelectObject(self.mem, g.CreateDIBSection(self.mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0))
        self.buf = (ctypes.c_ubyte * (W * H * 4)).from_address(bits.value)  # BGRA premultiplicado = ARGB32
        self.surface = cairo.ImageSurface.create_for_data(self.buf, cairo.FORMAT_ARGB32, W, H, W * 4)

    def show(self, x, y):
        self.surface.flush()
        size = (ctypes.c_long * 2)(self.W, self.H)
        self.u.UpdateLayeredWindow(self.hwnd, self.screen, ctypes.byref(POINT(x, y)), ctypes.byref(size),
                                   self.mem, ctypes.byref(POINT(0, 0)), 0, ctypes.byref(BLEND(0, 0, 255, 1)), 2)


B = cairo.FONT_WEIGHT_BOLD
FONTS = {"title": ("Segoe UI", 10, B), "body": ("Segoe UI", 10), "small": ("Segoe UI", 9),
         "tiny": ("Segoe UI", 8), "clock": ("Segoe UI", 11, B), "mono": ("Consolas", 8),
         "btn": ("Segoe UI", 9, B), "sym": ("Segoe UI Symbol", 9), "symbig": ("Segoe UI Symbol", 18)}


class Paint:
    """Atajos de cairo en px lógicos; colores '#rrggbb' + opacidad."""
    def __init__(self, ctx):
        self.c = ctx
        opts = cairo.FontOptions()
        opts.set_antialias(cairo.ANTIALIAS_GRAY)  # subpíxel sobre fondo transparente deja bordes de color
        ctx.set_font_options(opts)

    def rgba(self, col, a):
        self.c.set_source_rgba(*(int(col[i:i + 2], 16) / 255 for i in (1, 3, 5)), clamp(a))

    def rrect_path(self, x1, y1, x2, y2, r):
        c, r = self.c, max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
        c.new_sub_path()
        c.arc(x2 - r, y1 + r, r, -math.pi / 2, 0)
        c.arc(x2 - r, y2 - r, r, 0, math.pi / 2)
        c.arc(x1 + r, y2 - r, r, math.pi / 2, math.pi)
        c.arc(x1 + r, y1 + r, r, math.pi, 3 * math.pi / 2)
        c.close_path()

    def rrect(self, x1, y1, x2, y2, r, col, a):
        self.rrect_path(x1, y1, x2, y2, r)
        self.rgba(col, a)
        self.c.fill()

    def oval(self, x1, y1, x2, y2, col, a):
        c = self.c
        c.save()
        c.translate((x1 + x2) / 2, (y1 + y2) / 2)
        c.scale(max(0.01, (x2 - x1) / 2), max(0.01, (y2 - y1) / 2))
        c.arc(0, 0, 1, 0, 2 * math.pi)
        c.restore()
        self.rgba(col, a)
        c.fill()

    def circle(self, x, y, r, col, a):
        self.oval(x - r, y - r, x + r, y + r, col, a)

    def line(self, x1, y1, x2, y2, col, a, w):
        c = self.c
        c.move_to(x1, y1)
        c.line_to(x2, y2)
        c.set_line_width(w)
        c.set_line_cap(cairo.LINE_CAP_ROUND)
        self.rgba(col, a)
        c.stroke()

    def poly(self, pts, col, a):
        c = self.c
        c.move_to(*pts[0])
        for p in pts[1:]:
            c.line_to(*p)
        c.close_path()
        self.rgba(col, a)
        c.fill()

    def font(self, key):
        face, pt, *weight = FONTS[key]
        self.c.select_font_face(face, cairo.FONT_SLANT_NORMAL, *weight)
        self.c.set_font_size(pt * 96 / 72)

    @staticmethod
    def runs(s):
        """Parte el texto en tramos normales y de símbolos (✓ ↗ ♪ ⚡…): Segoe UI no los tiene y cairo no
        busca otra fuente solo, así que esos tramos van con Segoe UI Symbol."""
        out = []
        for ch in s:
            sym = 0x2190 <= ord(ch) <= 0x2BFF
            if out and out[-1][1] == sym:
                out[-1][0] += ch
            else:
                out.append([ch, sym])
        return out

    def font_run(self, key, sym):
        face, pt, *weight = FONTS[key]
        self.c.select_font_face("Segoe UI Symbol" if sym else face, cairo.FONT_SLANT_NORMAL, *weight)
        self.c.set_font_size(pt * 96 / 72)

    def width(self, s, font):
        total = 0
        for chunk, sym in self.runs(s):
            self.font_run(font, sym)
            total += self.c.text_extents(chunk).x_advance
        return total

    def fit(self, s, font, maxw):
        """Recorta el texto con … para que entre en maxw."""
        if self.width(s, font) <= maxw:
            return s
        while s and self.width(s + "…", font) > maxw:
            s = s[:-1]
        return s + "…"

    def text(self, x, y, s, font, col, a, anchor="w"):
        c = self.c
        self.font(font)
        ascent, descent = c.font_extents()[:2]  # alineación vertical según la fuente principal
        wd = self.width(s, font)
        x -= {"w": 0, "center": wd / 2, "e": wd}[anchor]
        c.move_to(x, y + (ascent - descent) / 2)
        self.rgba(col, a)
        for chunk, sym in self.runs(s):
            self.font_run(font, sym)
            c.show_text(chunk)  # show_text deja el cursor al final: el siguiente tramo sigue ahí
        return x, x + wd


def robot(p, x, y, s, mood, now, a=1.0):
    """Cabeza de robot de ancho ~s centrada en (x, y): respira, parpadea y los ojos cambian con el ánimo."""
    col, e = MOODS[mood], s * 0.11
    y += math.sin(now * 2.2) * s * 0.025
    glow = 0.55 + 0.45 * math.sin(now * 7) if mood == "focused" else 1  # la antena titila mientras trabaja
    lw = max(1.2, s / 18)
    p.line(x, y - s * 0.38, x, y - s * 0.52, "#dddddd", a, lw)
    p.circle(x, y - s * 0.55, e * 0.85, col, a * glow)
    for side in (-1, 1):  # orejas
        p.oval(x + side * s * 0.5 - e, y - e * 1.4, x + side * s * 0.5 + e, y + e * 1.4, "#c8c8cc", a)
    p.oval(x - s * 0.46, y - s * 0.4, x + s * 0.46, y + s * 0.4, "#f2f2f2", a)
    p.rrect(x - s * 0.33, y - s * 0.2, x + s * 0.33, y + s * 0.2, s * 0.2, "#15151b", a)
    blink = mood != "sleep" and now % 4.3 < 0.12
    for ex in (x - s * 0.15, x + s * 0.15):
        if mood == "sleep" or blink:
            p.line(ex - e * 0.8, y, ex + e * 0.8, y, col, a, lw)
        elif mood == "happy":  # ^ ^
            p.c.new_sub_path()
            p.c.arc(ex, y + e * 0.5, e * 0.85, math.radians(-155), math.radians(-25))
            p.c.set_line_width(lw)
            p.c.set_line_cap(cairo.LINE_CAP_ROUND)
            p.rgba(col, a)
            p.c.stroke()
        elif mood == "love":
            c = p.c
            c.save()
            c.translate(ex, y + e * 0.15)
            c.scale(e, e)
            c.move_to(0, 0.75)
            c.curve_to(-1.3, -0.1, -0.6, -1.1, 0, -0.35)
            c.curve_to(0.6, -1.1, 1.3, -0.1, 0, 0.75)
            c.restore()
            p.rgba(col, a)
            c.fill()
        else:  # focused: ojos chicos; curious: ojos grandes
            k = 0.6 if mood == "focused" else 0.9
            p.oval(ex - e * k, y - e * k * 1.3, ex + e * k, y + e * k * 1.3, col, a)


def art_surface(data, px):
    """Carátula recortada en cuadrado como superficie cairo (las esquinas las redondea un clip)."""
    im = Image.open(io.BytesIO(data)).convert("RGB")
    side = min(im.size)
    im = im.crop(((im.width - side) // 2, (im.height - side) // 2,
                  (im.width + side) // 2, (im.height + side) // 2)).resize((px, px), Image.LANCZOS)
    raw = bytearray(im.convert("RGBA").tobytes("raw", "BGRA"))
    return cairo.ImageSurface.create_for_data(raw, cairo.FORMAT_ARGB32, px, px, px * 4), raw


def start_tray(toggle, quit_):
    """Ícono en la bandeja de Windows: clic = mostrar/ocultar la isla."""
    try:
        import pystray
    except ImportError:
        return None
    menu = pystray.Menu(pystray.MenuItem("Mostrar / ocultar  (Ctrl+Alt+I)", lambda: toggle(), default=True),
                        pystray.MenuItem("Salir", lambda: quit_()))
    tray = pystray.Icon("isla-dinamica", robot_image().resize((64, 64), Image.LANCZOS), "Isla dinámica", menu)
    tray.run_detached()
    return tray


def robot_image():
    """Cabeza de robot de 256 px (ícono de la bandeja y del .exe), igual a la de la isla."""
    im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.line((128, 30, 128, 70), fill="#dddddd", width=14)
    d.ellipse((108, 10, 148, 50), fill=GREEN)
    d.ellipse((6, 96, 54, 176), fill="#c8c8cc")
    d.ellipse((202, 96, 250, 176), fill="#c8c8cc")
    d.ellipse((24, 50, 232, 230), fill="#f2f2f2")
    d.rounded_rectangle((54, 96, 202, 186), radius=44, fill="#15151b")
    d.ellipse((80, 120, 110, 162), fill=GREEN)
    d.ellipse((146, 120, 176, 162), fill=GREEN)
    return im


def hotkey_loop(toggle):
    """Atajo global Ctrl+Alt+I: mostrar/ocultar (si otra app ya lo usa, simplemente no se registra)."""
    u = ctypes.windll.user32
    if not u.RegisterHotKey(None, 1, 0x0001 | 0x0002 | 0x4000, ord("I")):  # MOD_ALT|MOD_CONTROL|MOD_NOREPEAT
        return
    msg = wintypes.MSG()
    while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == 0x0312:  # WM_HOTKEY
            toggle()


WIN = (BIG[0] + 90, BIG[1] + 30)  # ventana fija; la isla se dibuja adentro (margen para el rebote)
SHOULDER = 9  # radio de las curvas donde la isla se une al borde de arriba


class Island:
    def __init__(self):
        self.cfg = load_config()
        self.base_S = dpi_scale()
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = ctypes.c_void_p
        before = u.GetForegroundWindow()  # al crearse, la ventana de Tk se lleva el foco: se lo devolvemos
        self.root = root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.update()
        ctypes.windll.user32.GetParent.restype = ctypes.c_void_p
        self.hwnd = ctypes.windll.user32.GetParent(ctypes.c_void_p(root.winfo_id()))
        self.resize()
        if before and u.GetForegroundWindow() == self.hwnd:
            u.SetForegroundWindow(ctypes.c_void_p(before))
        self.w, self.h = SMALL
        self.vw = self.vh = 0.0  # velocidad del resorte
        self.hits = []  # (x1, y1, x2, y2, acción) en coordenadas de la isla
        self.art = (None, None)  # (bytes, superficie) de la carátula actual
        self.icon = (None, None)  # (bytes, superficie) del ícono de la notificación actual
        self.pinned = None  # session_id elegida a mano; None = la más reciente
        self.media, self.timer, self.cpu, self.claude = Media(), Timer(), Cpu(), ClaudeCode()
        self.levels, self.notes = Levels(), Notifications(self.media.loop)
        self.apply_config()
        self.server = serve(self.claude)
        self.alert_until = self.stats_at = 0
        self.hidden, self.mode = False, "small"
        self.shown, self.quitting = True, False  # mostrar/ocultar a mano (bandeja o Ctrl+Alt+I)
        self.cleared = False  # ya se borró la ventana estando oculta
        self.hov = {"play": 0.0, "prev": 0.0, "next": 0.0}  # 0..1: cuánto está "encendido" cada botón
        self.pressed = {}  # botón -> momento del último clic
        self.morph = 0.0  # 0 = ▶, 1 = ⏸ (se transforma de a poco)
        self.art_k = 1.0  # tamaño de la carátula: chica en pausa, grande sonando
        self.build_menu()
        self.tray = start_tray(self.toggle, self.request_quit)
        threading.Thread(target=hotkey_loop, args=(self.toggle,), daemon=True).start()
        root.bind("<Button-3>", self.popup)
        root.bind("<MouseWheel>", lambda e: self.timer.scroll(e.delta > 0))
        root.bind("<Button-1>", self.click)
        self.tick()

    # --- ajustes ---

    def resize(self):
        """(Re)crea la ventana para la escala actual (DPI de Windows × tamaño elegido)."""
        self.S = self.base_S * self.cfg["size"]
        self.W, self.H = round(WIN[0] * self.S), round(WIN[1] * self.S)
        self.X = (self.root.winfo_screenwidth() - self.W) // 2
        self.root.geometry(f"{self.W}x{self.H}+{self.X}+0")
        self.win = Layered(self.hwnd, self.W, self.H)
        self.p = Paint(cairo.Context(self.win.surface))
        self.art = self.icon = (None, None)  # se regeneran a la nueva resolución

    def apply_config(self):
        self.claude.sound = self.cfg["sound"]
        self.notes.enabled = self.cfg["notifications"]

    def set(self, key, value):
        self.cfg[key] = value
        try:
            CONFIG.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
        except OSError:
            pass  # sin poder guardar: vale para esta sesión
        self.apply_config()
        if key == "size":
            self.resize()

    def build_menu(self):
        m = self.menu = tk.Menu(self.root, tearoff=0, font=("Segoe UI", 10))
        self.vars = {k: tk.BooleanVar(value=self.cfg[k]) for k in ("sound", "hide_fullscreen", "notifications")}
        self.vars["startup"] = tk.BooleanVar(value=STARTUP.exists())
        self.size_var = tk.DoubleVar(value=self.cfg["size"])
        sizes = tk.Menu(m, tearoff=0, font=("Segoe UI", 10))
        for label, k in [("Normal", 1.0), ("Grande", 1.2), ("Muy grande", 1.4)]:
            sizes.add_radiobutton(label=label, value=k, variable=self.size_var, command=lambda k=k: self.set("size", k))
        m.add_cascade(label="Tamaño", menu=sizes)
        for key, label in [("sound", "Sonidos"), ("notifications", "Mostrar notificaciones"),
                           ("hide_fullscreen", "Ocultar en pantalla completa")]:
            m.add_checkbutton(label=label, variable=self.vars[key],
                              command=lambda key=key: self.set(key, self.vars[key].get()))
        m.add_checkbutton(label="Iniciar con Windows", variable=self.vars["startup"],
                          command=lambda: set_startup(self.vars["startup"].get()))
        m.add_separator()
        m.add_command(label="Ocultar", accelerator="Ctrl+Alt+I", command=self.toggle)
        m.add_command(label="Salir", command=self.quit)

    def popup(self, e):
        """Menú de ajustes. Un menú de Windows solo se cierra al hacer clic afuera si su ventana tiene el
        foco: la isla lo toma mientras el menú está abierto y después se lo devuelve a tu app."""
        u = ctypes.windll.user32
        before = u.GetForegroundWindow()
        u.SetForegroundWindow(ctypes.c_void_p(self.hwnd))
        try:
            self.menu.tk_popup(e.x_root, e.y_root)  # en Windows espera hasta que el menú se cierra
        finally:
            self.menu.grab_release()
            if before and u.GetForegroundWindow() in (self.hwnd, None):
                u.SetForegroundWindow(ctypes.c_void_p(before))

    def toggle(self):
        """Muestra u oculta la isla (puede llamarse desde otros hilos: solo cambia banderas)."""
        self.shown = not self.shown
        self.claude.enabled = self.shown
        if not self.shown:
            for p in list(self.claude.perms):  # los pedidos pendientes vuelven a la terminal
                p["done"].set()

    def request_quit(self):
        self.quitting = True  # desde el hilo de la bandeja: tkinter solo se toca desde su hilo

    def quit(self):
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    # --- eventos ---

    def left_edge(self):
        return (WIN[0] - self.w) / 2

    def click(self, e):
        x, y = e.x / self.S - self.left_edge(), e.y / self.S
        for x1, y1, x2, y2, action, name in reversed(self.hits):
            if x1 <= x <= x2 and y1 <= y <= y2:
                if name:
                    self.pressed[name] = time.time()  # dispara la animación de "apretar"
                return action()

    def hit(self, x1, y1, x2, y2, action, name=None):
        """Zona clicable; con name, el botón se anima al pasar el mouse y al hacer clic."""
        self.hits.append((x1, y1, x2, y2, action, name))

    def press(self, name, now):
        """Escala del botón tras un clic: se hunde y rebota (1 = reposo)."""
        t = now - self.pressed.get(name, -9)
        return 1 - 0.22 * math.exp(-t * 9) * math.cos(t * 22) if t < 0.7 else 1

    def session(self):
        """(session_id, sesión, posición, total) que muestra la tarjeta."""
        items = self.claude.ordered()
        if not items:
            return None, None, 0, 0
        ids = [sid for sid, _ in items]
        if self.pinned not in ids:
            self.pinned = None
            sid = max(items, key=lambda kv: kv[1]["t"])[0]
        else:
            sid = self.pinned
        i = ids.index(sid)
        return sid, items[i][1], i + 1, len(items)

    def next_session(self):
        items = self.claude.ordered()
        if items:
            sid, _, i, n = self.session()
            self.pinned = items[i % n][0]

    def tick(self):
        root, now, S = self.root, time.time(), self.S
        if self.quitting:
            return self.quit()
        if now - self.stats_at >= 1:
            self.stats_at = now
            self.bat, self.cpu_pct, self.ram_pct = battery(), self.cpu.percent(), ram()
            self.hidden = self.cfg["hide_fullscreen"] and fullscreen()
        left = self.timer.left()
        if left is not None and left <= 0:
            self.timer.ends = None
            self.alert_until = now + 6
            if self.cfg["sound"]:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
        px, py = root.winfo_pointerxy()
        lx, ly = (px - self.X) / S - self.left_edge(), py / S
        hover = 0 <= lx < self.w and 0 <= ly < self.h
        note = self.notes.current
        if note and hover and self.mode == "note":
            note["until"] = max(note["until"], now + 1)  # no se va mientras la lees
        if self.claude.perms:  # aunque haya hover: si se expandiera, los botones se escaparían del mouse
            mode = "perm"
        elif now < self.levels.until:
            mode = "level"
        elif note and now < note["until"]:
            mode = "note"
        elif hover or now < self.alert_until or now < self.claude.flash_until:
            mode = "big"
        else:
            mode = "small"
        self.mode = mode
        target = {"perm": PERM, "level": LEVEL, "note": NOTIF, "big": BIG, "small": SMALL}[mode]
        # resorte amortiguado: se pasa un poquito y vuelve, como en el iPhone
        self.vw = self.vw * 0.7 + (target[0] - self.w) * 0.1
        self.vh = self.vh * 0.7 + (target[1] - self.h) * 0.1
        self.w = clamp(self.w + self.vw, SMALL[0] * 0.8, WIN[0] - 2 * SHOULDER - 2)
        self.h = clamp(self.h + self.vh, SMALL[1] * 0.8, WIN[1] - 2)
        under = next((n for x1, y1, x2, y2, _, n in reversed(self.hits)
                      if n and x1 <= lx <= x2 and y1 <= ly <= y2), None) if hover else None
        for n in self.hov:
            self.hov[n] += ((n == under) - self.hov[n]) * 0.25
        self.morph += (self.media.playing - self.morph) * 0.25
        self.art_k += ((1.0 if self.media.playing else 0.9) - self.art_k) * 0.15
        visible = self.shown and not (self.hidden and mode != "perm")
        if visible or not self.cleared:  # oculta: se borra una vez y no se vuelve a dibujar
            self.draw(self.w, self.h, now, mode, target)
            self.win.show(self.X, 0)
        self.cleared = not visible
        # ahorro: 60 fps solo mientras cambia de tamaño; quieta, lo justo para que el robot respire
        moving = (abs(self.vw) + abs(self.vh) > 0.05 or abs(target[0] - self.w) + abs(target[1] - self.h) > 0.5
                  or now - max(self.pressed.values(), default=0) < 0.7  # botón rebotando
                  or (mode == "big" and (under or any(0.01 < v < 0.99 for v in self.hov.values())
                                         or abs(self.media.playing - self.morph) > 0.01
                                         or abs((1.0 if self.media.playing else 0.9) - self.art_k) > 0.002)))
        root.after(16 if moving else 250 if not visible else 33 if mode != "small" else 66, self.tick)

    # --- dibujo ---

    def shape(self, w, h):
        """Silueta: cápsula pegada arriba, con curvas hacia afuera donde toca el borde (como un notch)."""
        c, rs = self.p.c, SHOULDER
        r = min(h / 2, w / 2, 34)
        c.new_path()
        c.move_to(-rs, 0)
        c.line_to(w + rs, 0)
        c.arc_negative(w + rs, rs, rs, -math.pi / 2, -math.pi)
        c.line_to(w, h - r)
        c.arc(w - r, h - r, r, 0, math.pi / 2)
        c.line_to(r, h)
        c.arc(r, h - r, r, math.pi / 2, math.pi)
        c.line_to(0, rs)
        c.arc_negative(-rs, rs, rs, 0, -math.pi / 2)
        c.close_path()

    def draw(self, w, h, now, mode, target):
        p, c, mood = self.p, self.p.c, self.claude.mood()
        c.identity_matrix()
        c.set_operator(cairo.OPERATOR_CLEAR)
        c.paint()
        c.set_operator(cairo.OPERATOR_OVER)
        self.hits = []
        if not self.shown or (self.hidden and mode != "perm"):  # oculta, o juego/video en pantalla completa
            return
        c.scale(self.S, self.S)
        c.translate(self.left_edge(), 0)
        self.shape(w, h)
        c.set_source_rgb(0, 0, 0)
        c.fill_preserve()
        c.save()
        c.clip()  # el contenido no se sale de la isla mientras se anima
        near = clamp(1 - abs(w - target[0]) / 100 - abs(h - target[1]) / 40)  # fundido al llegar
        if mode == "perm":
            self.draw_perm(w, h, now, mood, near)
        elif mode == "level":
            self.draw_level(w, h, near)
        elif mode == "note":
            self.draw_note(w, h, near)
        elif mode == "big":
            a = clamp((h - 100) / (BIG[1] - 100)) ** 2
            if now < self.alert_until:
                p.text(w / 2, h / 2, "⏰  ¡Tiempo!", "symbig", GREEN, a, anchor="center")
            else:
                self.draw_claude(w, now, mood, a)
                p.line(18, 104, w - 18, 104, "#2a2a2e", a, 1)
                self.draw_music(w, now, a)
        else:
            self.draw_small(w, h, now, mood, clamp(1 - (h - SMALL[1]) / 30) ** 2)
        c.restore()

    def sys_info(self):
        """(texto, fuente, color) de batería o, en PCs de escritorio, CPU y RAM."""
        bat = self.bat
        if bat:
            return (f"{'⚡' if bat[1] else '▮'} {bat[0]}%", "sym",
                    GREEN if bat[1] else RED if bat[0] <= 20 else WHITE)
        return (f"CPU {self.cpu_pct}%  RAM {self.ram_pct}%", "small",
                RED if max(self.cpu_pct, self.ram_pct) >= 90 else WHITE)

    def draw_small(self, w, h, now, mood, a):
        p, m, left = self.p, self.media, self.timer.left()
        robot(p, 24, h / 2 + 1, 22, mood, now, a)
        if m.playing:  # ecualizador animado
            for i in range(3):
                bh = 3 + 5 * abs(math.sin(now * (5 + i * 1.7) + i))
                p.line(44 + i * 4.5, h / 2 + 4.5, 44 + i * 4.5, h / 2 + 4.5 - bh, GREEN, a, 2.4)
        _, s, _, _ = self.session()
        if s and s["since"]:  # Claude trabajando: cuánto lleva
            p.text(w / 2, h / 2, mmss(now - s["since"]), "clock", MOODS["focused"], a, anchor="center")
        else:
            p.text(w / 2, h / 2, time.strftime("%H:%M"), "clock", WHITE, a, anchor="center")
        if left:
            p.text(w - 18, h / 2, mmss(left), "small", GREEN, a, anchor="e")
        else:
            txt, col = (f"{self.bat[0]}%", self.sys_info()[2]) if self.bat else (f"{self.cpu_pct}%", WHITE)
            p.text(w - 18, h / 2, txt, "small", col, a, anchor="e")

    def draw_level(self, w, h, a):
        p, c, L = self.p, self.p.c, self.levels
        x, y = 26, h / 2
        if L.kind == "vol":  # parlante + ondas (o tachado si está en silencio)
            p.poly([(x - 8, y - 3.5), (x - 4, y - 3.5), (x + 1, y - 8), (x + 1, y + 8), (x - 4, y + 3.5),
                    (x - 8, y + 3.5)], WHITE, a)
            if L.muted or L.value == 0:
                p.line(x + 5, y - 4, x + 12, y + 4, WHITE, a, 1.8)
                p.line(x + 12, y - 4, x + 5, y + 4, WHITE, a, 1.8)
            else:
                for i, r in enumerate((5, 9)):
                    if L.value > 50 * i:
                        c.new_sub_path()
                        c.arc(x + 1, y, r, -0.8, 0.8)
                        c.set_line_width(1.8)
                        c.set_line_cap(cairo.LINE_CAP_ROUND)
                        p.rgba(WHITE, a)
                        c.stroke()
        else:  # sol
            p.circle(x, y, 4.5, WHITE, a)
            for i in range(8):
                ang = i * math.pi / 4
                p.line(x + 7 * math.cos(ang), y + 7 * math.sin(ang), x + 9.5 * math.cos(ang),
                       y + 9.5 * math.sin(ang), WHITE, a, 1.6)
        x1, x2 = 52, w - 58
        frac = 0 if L.kind == "vol" and L.muted else L.value / 100
        p.line(x1, y, x2, y, "#3a3a3e", a, 6)
        if frac:
            p.line(x1, y, x1 + (x2 - x1) * frac, y, WHITE, a, 6)
        p.text(w - 20, y, "Silencio" if L.kind == "vol" and L.muted else f"{L.value}%", "small", GREY, a,
               anchor="e")

    def draw_note(self, w, h, a):
        p, c, n = self.p, self.p.c, self.notes.current
        size, x0, y0 = 40, 14, (h - 40) / 2
        if n["icon"] and Image and self.icon[0] is not n["icon"]:
            try:
                self.icon = (n["icon"], art_surface(n["icon"], round(size * self.S * 2)))
            except OSError:
                self.icon = (n["icon"], None)
        if n["icon"] and self.icon[0] is n["icon"] and self.icon[1]:
            surf = self.icon[1][0]
            c.save()
            p.rrect_path(x0, y0, x0 + size, y0 + size, 11)
            c.clip()
            p.rgba("#1f1f23", a)
            c.paint()
            c.translate(x0, y0)
            c.scale(size / surf.get_width(), size / surf.get_width())
            c.set_source_surface(surf, 0, 0)
            c.get_source().set_filter(cairo.FILTER_GOOD)
            c.paint_with_alpha(clamp(a))
            c.restore()
        else:
            p.rrect(x0, y0, x0 + size, y0 + size, 11, "#2a2a2e", a)
            p.text(x0 + size / 2, y0 + size / 2, (n["app"] or "?")[:1].upper(), "title", WHITE, a, anchor="center")
        x = x0 + size + 12
        p.text(x, 16, p.fit(n["app"], "tiny", w - x - 70), "tiny", GREY, a)
        p.text(w - 22, 16, "ahora", "tiny", GREY, a, anchor="e")
        p.text(x, 33, p.fit(n["title"], "title", w - x - 22), "title", WHITE, a)
        p.text(x, 49, p.fit(n["body"], "small", w - x - 22), "small", "#c8c8cc", a)
        self.hit(0, 0, w, h, lambda: n.update(until=0))  # clic = descartar

    def draw_perm(self, w, h, now, mood, a):
        p, perms = self.p, self.claude.perms
        if not perms:
            return
        robot(p, 26, h / 2 + 2, 26, mood, now, a)
        p.circle(50, h / 2, 3, MOODS["focused"], a)
        more = len(perms) - 1
        right = w - 160 - (22 if more else 0)
        p.text(60, h / 2, p.fit(f"¿Permitir? {perms[0]['text']}", "title", right - 72), "title", WHITE, a)
        for answer, label, x, bg, fg in [(True, "Permitir", right, "#b7f07a", "#000000"),
                                         (False, "Denegar", right + 74, "#3a2626", "#ff9a9a")]:
            p.rrect(x, h / 2 - 14, x + 66, h / 2 + 14, 14, bg, a)
            p.text(x + 33, h / 2, label, "btn", fg, a, anchor="center")
            self.hit(x, 0, x + 70, h, lambda answer=answer: self.claude.answer(answer))
        if more:
            p.text(w - 20, h / 2, f"+{more}", "tiny", GREY, a, anchor="e")

    def draw_claude(self, w, now, mood, a):
        p = self.p
        sid, s, i, n = self.session()
        robot(p, 46, 50, 46, mood, now, a)
        x = 88
        chip = f"‹ {i}/{n} ›" if n > 1 else "Claude Code"
        cw = p.width(chip, "tiny") + 18
        p.rrect(w - 22 - cw, 11, w - 22, 29, 9, "#2a2a30" if n > 1 else "#1f1f23", a)
        p.text(w - 22 - cw / 2, 20, chip, "tiny", WHITE if n > 1 else GREY, a, anchor="center")
        if n > 1:
            self.hit(w - 26 - cw, 6, w - 18, 34, self.next_session)
        p.circle(x + 3, 20, 3, MOODS[mood], a)
        name = s["project"] if s else "Claude Code"
        if s and s.get("pid"):  # nombre del proyecto + ↗ = ir a su terminal
            name_w = min(p.width(name, "title"), w - cw - x - 64)
            p.text(x + 12, 20, p.fit(name, "title", name_w + 1), "title", WHITE, a)
            p.text(x + 16 + name_w, 20, "↗", "sym", GREY, a)
            self.hit(0, 0, x + 30 + name_w, 34, lambda: focus_terminal(s["pid"]))
            self.hit(0, 34, 80, 96, lambda: focus_terminal(s["pid"]))  # el robot también
        else:
            p.text(x + 12, 20, p.fit(name, "title", w - cw - x - 44), "title", WHITE, a)
        status = s["status"] if s else "Sin sesiones activas"
        if s and s["since"]:
            status += f"  ·  {mmss(now - s['since'])}"
        elif s and s["took"] and status.startswith("Listo"):
            status += f" en {mmss(s['took'])}"
        p.text(x, 40, p.fit(status, "body", w - x - 22), "body", WHITE, a)
        detail = s["detail"][:3] if s else [("abre claude en una terminal", GREY)]
        if detail:
            p.rrect(x, 52, w - 22, 58 + 13 * len(detail) + 5, 10, FIELD, a)
            for j, (line, col) in enumerate(detail):
                y = 58 + j * 13
                if col in DIFF_BG:
                    p.rrect(x + 4, y, w - 26, y + 13, 4, DIFF_BG[col], a)
                p.text(x + 10, y + 6.5, p.fit(line, "mono", w - x - 44), "mono", col, a)

    def draw_play(self, cx, y, now, a):
        """Botón ▶/⏸: con relieve (degradado + halo), crece con el mouse, rebota al clic y el ícono se
        transforma de triángulo a barras."""
        p, c, hv = self.p, self.p.c, self.hov["play"]
        r = 16 * (1 + 0.1 * hv) * self.press("play", now)
        if hv > 0.01:  # halo
            halo = cairo.RadialGradient(cx, y, r, cx, y, r + 9)
            halo.add_color_stop_rgba(0, 1, 1, 1, 0.22 * hv * a)
            halo.add_color_stop_rgba(1, 1, 1, 1, 0)
            c.set_source(halo)
            c.arc(cx, y, r + 9, 0, 2 * math.pi)
            c.fill()
        grad = cairo.RadialGradient(cx - r * 0.35, y - r * 0.45, r * 0.1, cx, y, r)  # luz desde arriba
        grad.add_color_stop_rgba(0, 1, 1, 1, a)
        grad.add_color_stop_rgba(1, 0.8, 0.8, 0.84, a)
        c.set_source(grad)
        c.arc(cx, y, r, 0, 2 * math.pi)
        c.fill()
        t, k = self.morph, r / 16
        play = [[(-4, -6.5), (1.5, -3.25), (1.5, 3.25), (-4, 6.5)], [(1.5, -3.25), (7, 0), (7, 0), (1.5, 3.25)]]
        pause = [[(-4.5, -6), (-1.3, -6), (-1.3, 6), (-4.5, 6)], [(1.3, -6), (4.5, -6), (4.5, 6), (1.3, 6)]]
        for pl, pa in zip(play, pause):
            p.poly([(cx + k * (x1 + (x2 - x1) * t), y + k * (y1 + (y2 - y1) * t))
                    for (x1, y1), (x2, y2) in zip(pl, pa)], "#111114", a)

    def draw_skip(self, bx, y, d, name, now, a):
        """⏮/⏭: fondo que aparece con el mouse; al clic se hunde y los triángulos corren hacia ese lado."""
        p, hv = self.p, self.hov[name]
        k = (1 + 0.08 * hv) * self.press(name, now)
        t = now - self.pressed.get(name, -9)
        slide = d * 7 * math.sin(math.pi * t / 0.35) if t < 0.35 else 0
        if hv > 0.01:
            p.circle(bx, y, 17 * k, "#2c2c31", a * hv)
        for off in (-5, 2):  # dos triángulos + barra
            tx = bx + d * off * k + slide
            p.poly([(tx - d * 3.5 * k, y - 5.5 * k), (tx - d * 3.5 * k, y + 5.5 * k), (tx + d * 3.5 * k, y)], WHITE, a)
        x1, x2 = bx + d * 6 * k, bx + d * 8 * k
        p.rrect(min(x1, x2), y - 5.5 * k, max(x1, x2), y + 5.5 * k, 1, WHITE, a)

    def draw_music(self, w, now, a):
        p, c, m, left = self.p, self.p.c, self.media, self.timer.left()
        y0, size, x0 = 116, 52, 22
        if m.art and Image and self.art[0] is not m.art:
            try:
                self.art = (m.art, art_surface(m.art, round(size * self.S * 2)))
            except OSError:  # imagen rota
                self.art = (m.art, None)
        c.save()  # la carátula se achica un poco en pausa y vuelve a crecer al sonar
        k = self.art_k
        c.translate(x0 + size / 2, y0 + size / 2)
        c.scale(k, k)
        c.translate(-x0 - size / 2, -y0 - size / 2)
        if m.art and self.art[0] is m.art and self.art[1]:
            surf = self.art[1][0]
            c.save()
            p.rrect_path(x0, y0, x0 + size, y0 + size, 12)
            c.clip()
            c.translate(x0, y0)
            c.scale(size / surf.get_width(), size / surf.get_width())
            c.set_source_surface(surf, 0, 0)
            c.get_source().set_filter(cairo.FILTER_GOOD)
            c.paint_with_alpha(clamp(a * (0.55 + 0.45 * (k - 0.9) / 0.1)))  # más apagada en pausa
            c.restore()
        else:
            p.rrect(x0, y0, x0 + size, y0 + size, 12, "#1f1f23", a)
            p.text(x0 + size / 2, y0 + size / 2, "♪", "symbig", "#4a4a50", a, anchor="center")
        c.restore()
        d = datetime.now()
        x = x0 + size + 14
        p.text(w - 22, y0 + 12, time.strftime("%H:%M"), "clock", WHITE, a, anchor="e")
        p.text(w - 22, y0 + 31, f"{DAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}", "small", GREY, a,
               anchor="e")
        p.text(x, y0 + 12, p.fit(m.title or "Nada sonando", "title", w - x - 90), "title", WHITE, a)
        p.text(x, y0 + 31, p.fit(m.artist, "small", w - x - 90), "small", GREY, a)
        if m.end >= 1:
            pos = min(m.pos + (now - m.stamp if m.playing else 0), m.end)  # avanza suave entre sondeos
            x1, x2, y = x, w - 104, y0 + 48
            p.line(x1, y, x2, y, "#3a3a3e", a, 4)
            p.line(x1, y, x1 + (x2 - x1) * min(pos / m.end, 1), y, WHITE, a, 4)
            p.text(w - 22, y, f"{mmss(pos)} / {mmss(m.end)}", "tiny", GREY, a, anchor="e")
        y, cx = BIG[1] - 24, w / 2
        self.draw_play(cx, y, now, a)
        self.hit(cx - 18, y - 18, cx + 18, y + 18, lambda: self.media.send("try_toggle_play_pause_async"), "play")
        for cmd, name, d in [("try_skip_previous_async", "prev", -1), ("try_skip_next_async", "next", 1)]:
            self.draw_skip(cx + d * 50, y, d, name, now, a)
            self.hit(cx + d * 50 - 18, y - 18, cx + d * 50 + 18, y + 18, lambda cmd=cmd: self.media.send(cmd), name)
        txt, font, col = self.sys_info()
        p.text(22, y, txt, font, col, a)
        label = mmss(left) if left else f"{self.timer.minutes} min"
        tw = p.width(label, "small") + 38
        p.rrect(w - 22 - tw, y - 13, w - 22, y + 13, 13, "#14301f" if left else "#1f1f23", a)
        ix, col = w - 22 - tw + 15, GREEN if left else WHITE  # cronómetro dibujado
        c.new_sub_path()
        c.arc(ix, y + 1, 5.5, 0, 2 * math.pi)
        c.set_line_width(1.5)
        p.rgba(col, a)
        c.stroke()
        p.line(ix, y + 1, ix, y - 2, col, a, 1.5)
        p.line(ix - 2, y - 7, ix + 2, y - 7, col, a, 1.5)
        p.text(ix + 11, y, label, "small", col, a)
        self.hit(w - 22 - tw, y - 14, w - 22, y + 14, self.timer.toggle)


def main():
    try:
        Island().root.mainloop()
    except OSError:  # puerto ocupado: ya hay una isla abierta
        pass


if __name__ == "__main__":
    main()
