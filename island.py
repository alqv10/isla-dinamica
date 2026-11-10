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
    from winrt.windows.applicationmodel.appointments import AppointmentManager, AppointmentStoreAccessType
    from winrt.windows.devices.geolocation import Geolocator
except ImportError:
    AppointmentManager = Geolocator = None  # sin calendario ni clima
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
BIG_COMPANION = (480, 118)  # abierta sin tarjeta de Claude Code: música, clima y calendario
CONFIG = Path.home() / ".isla-dinamica.json"
VERSION = "1.3.0"
REPO = "alqv10/isla-dinamica"
DEFAULTS = {"size": 1.0, "sound": True, "hide_fullscreen": True, "notifications": True, "accent": "#3ddc84",
            "translucent": False, "weather": True, "calendar": True, "updates": True, "reply": True,
            "character": "robot", "body": "#f2f2f2",
            "mode": "claude",  # "claude" | "companion"
            "monitor": "primary",
            "screenshots": True, "clipboard_text": False, "privacy": True}  # "primary", "mouse" o el nombre de una pantalla, como \\.\DISPLAY2
ACCENTS = [("Verde", "#3ddc84"), ("Morado", "#a78bfa"), ("Azul", "#4cc2ff"), ("Naranja", "#ff9f43"),
           ("Rosa", "#ff6b9d")]
REPLY_GRACE = 6  # s que la isla ofrece "Responder" cuando Claude termina (el turno espera ese rato)
REPLY_WAIT = 300  # s para escribir la respuesta una vez abierto el cuadro
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
        self.app = ""
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
                    self.app = s.source_app_user_model_id  # quién suena (Spotify.exe, chrome.exe…)
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
        self.pending, self.wake = 0.0, threading.Event()  # cambio de volumen pedido desde la isla (rueda)
        self.output, self.device_changed, self.device_event = "", False, None  # salida de audio actual
        threading.Thread(target=self.volume, daemon=True).start()
        threading.Thread(target=self.brightness, daemon=True).start()

    def show(self, kind, value, muted=False):
        self.kind, self.value, self.muted, self.until = kind, value, muted, time.time() + 1.8

    def nudge(self, delta):
        """Sube/baja el volumen (lo aplica el hilo de audio: COM no se comparte entre hilos)."""
        self.pending += delta
        self.wake.set()

    def volume(self):
        try:
            import comtypes
            from pycaw.callbacks import MMNotificationClient
            from pycaw.pycaw import AudioUtilities
            comtypes.CoInitialize()
        except ImportError:
            return
        levels = self

        class Watch(MMNotificationClient):  # Windows avisa cuando cambia la salida (parlantes <-> auriculares)
            def on_default_device_changed(self, flow, flow_id, role, role_id, device_id):
                if flow == "eRender" and role == "eConsole":
                    levels.device_changed = True
                    levels.wake.set()
        watch = Watch()
        try:
            AudioUtilities.GetDeviceEnumerator().RegisterEndpointNotificationCallback(watch)
        except (OSError, comtypes.COMError):
            pass  # sin avisos: igual se relee el dispositivo cada minuto
        last, ep, ep_at = None, None, 0
        while True:
            try:
                if self.device_changed or time.time() - ep_at > 60:
                    dev = AudioUtilities.GetSpeakers()
                    ep, ep_at = dev.EndpointVolume, time.time()
                    if self.device_changed and dev.FriendlyName != self.output:
                        self.device_event = dev.FriendlyName  # la isla lo muestra en una tarjeta
                    self.output, self.device_changed = dev.FriendlyName, False
                if self.pending:
                    delta, self.pending = self.pending, 0.0
                    ep.SetMasterVolumeLevelScalar(clamp(ep.GetMasterVolumeLevelScalar() + delta), None)
                now = (round(ep.GetMasterVolumeLevelScalar() * 100), bool(ep.GetMute()))
                if last is not None and now != last and not self.device_event:
                    self.show("vol", *now)
                last = now
            except (OSError, comtypes.COMError, AttributeError):
                ep_at = 0  # sin dispositivo de audio por ahora
            self.wake.wait(0.1)
            self.wake.clear()

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


def fetch_json(url):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "isla-dinamica", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def weather_icon(code, day):
    """Código WMO de Open-Meteo -> símbolo que Segoe UI Symbol sí tiene."""
    if code in (0, 1):
        return "☀" if day else "☾"
    if code in (2, 3, 45, 48):
        return "☁"
    if 71 <= code <= 77 or code in (85, 86):
        return "❄"
    if code >= 95:
        return "⚡"
    return "☂"  # llovizna y lluvia


class Weather:
    """Temperatura actual (Open-Meteo, gratis y sin cuenta). La ubicación de Windows se redondea a ~10 km
    antes de mandarla."""
    def __init__(self, loop):
        self.temp, self.icon, self.enabled = None, "", True
        if Geolocator:
            asyncio.run_coroutine_threadsafe(self.poll(), loop)

    async def poll(self):
        while True:
            if self.enabled:
                try:
                    pos = (await Geolocator().get_geoposition_async()).coordinate.point.position
                    lat, lon = round(pos.latitude, 1), round(pos.longitude, 1)
                    cur = (await asyncio.to_thread(fetch_json, "https://api.open-meteo.com/v1/forecast?latitude="
                           f"{lat}&longitude={lon}&current=temperature_2m,weather_code,is_day"))["current"]
                    self.temp = round(cur["temperature_2m"])
                    self.icon = weather_icon(cur["weather_code"], cur["is_day"])
                except (OSError, ValueError, KeyError):
                    pass  # sin internet o sin permiso de ubicación: se reintenta en el próximo ciclo
            await asyncio.sleep(1800)


class Calendar:
    """Próxima cita de hoy en el calendario de Windows (Outlook, Google… si están conectados a Windows)."""
    def __init__(self, loop):
        self.next = None  # (asunto, inicio con zona horaria local)
        self.enabled = True
        if AppointmentManager:
            asyncio.run_coroutine_threadsafe(self.poll(), loop)

    async def poll(self):
        from datetime import timedelta
        try:
            store = await AppointmentManager.request_store_async(AppointmentStoreAccessType.ALL_CALENDARS_READ_ONLY)
        except OSError:
            return
        while store:
            try:
                now = datetime.now(timezone.utc)
                found = await store.find_appointments_async(now, timedelta(hours=24))
                upcoming = sorted((a.start_time, a.subject) for a in found if not a.all_day and a.start_time > now)
                self.next = (upcoming[0][1] or "Evento", upcoming[0][0].astimezone()) if upcoming else None
            except OSError:
                pass
            await asyncio.sleep(60)


def version_tuple(v):
    return tuple(int(x) for x in v.lstrip("v").split(".") if x.isdigit())


class Updater:
    """Revisa los Releases de GitHub cada 6 h; si hay versión nueva, la isla ofrece actualizar."""
    def __init__(self, loop):
        self.latest = None  # (versión, url del instalador, url de la página)
        self.state = ""  # "", "descargando", "error"
        self.enabled = True
        asyncio.run_coroutine_threadsafe(self.poll(), loop)

    async def poll(self):
        await asyncio.sleep(20)  # no competir con el arranque de Windows
        while True:
            if self.enabled:
                try:
                    rel = await asyncio.to_thread(fetch_json, f"https://api.github.com/repos/{REPO}/releases/latest")
                    asset = next((a["browser_download_url"] for a in rel["assets"] if a["name"].endswith("Setup.exe")), None)
                    if version_tuple(rel["tag_name"]) > version_tuple(VERSION) and asset:
                        self.latest = (rel["tag_name"].lstrip("v"), asset, rel["html_url"])
                except (OSError, ValueError, KeyError):
                    pass
            await asyncio.sleep(6 * 3600)

    def install(self):
        """Descarga el instalador nuevo y lo corre sin ventanas: cierra esta isla, la reemplaza y la abre."""
        if not self.latest or self.state == "descargando":
            return
        if not getattr(sys, "frozen", False):  # corriendo desde el código: mejor ver la página del Release
            import webbrowser
            webbrowser.open(self.latest[2])
            return
        self.state = "descargando"

        def go():
            import tempfile
            import urllib.request
            try:
                path = Path(tempfile.gettempdir()) / f"IslaDinamica-Setup-{self.latest[0]}.exe"
                req = urllib.request.Request(self.latest[1], headers={"User-Agent": "isla-dinamica"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    path.write_bytes(r.read())
                subprocess.Popen([str(path), "--silent"], creationflags=subprocess.DETACHED_PROCESS)
            except OSError:
                self.state = "error"
        threading.Thread(target=go, daemon=True).start()


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                ("ppid", wintypes.DWORD), ("prio", ctypes.c_long), ("flags", wintypes.DWORD),
                ("exe", ctypes.c_char * 260)]


def processes():
    """({pid: pid del padre}, {pid: nombre del .exe en minúsculas})"""
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = ctypes.c_void_p(k.CreateToolhelp32Snapshot(2, 0))  # TH32CS_SNAPPROCESS
    e, parent, exe = PROCESSENTRY32(), {}, {}
    e.size = ctypes.sizeof(e)
    ok = k.Process32First(snap, ctypes.byref(e))
    while ok:
        parent[e.pid], exe[e.pid] = e.ppid, e.exe.decode(errors="replace").lower()
        ok = k.Process32Next(snap, ctypes.byref(e))
    k.CloseHandle(snap)
    return parent, exe


def app_windows():
    """{pid: ventana principal visible} de las apps abiertas."""
    u, windows = ctypes.windll.user32, {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if u.IsWindowVisible(hwnd) and u.GetWindowTextLengthW(hwnd) and not u.GetWindow(hwnd, 4):  # sin dueño
            owner = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            windows.setdefault(owner.value, hwnd)
        return True
    u.EnumWindows(each, 0)
    return windows


def bring_to_front(hwnd):
    u = ctypes.windll.user32
    u.keybd_event(0x12, 0, 0, 0)  # Alt: Windows deja cambiar el foco solo tras una tecla
    u.keybd_event(0x12, 0, 2, 0)
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)  # SW_RESTORE
    u.SetForegroundWindow(hwnd)


def focus_terminal(pid):
    """Trae al frente la ventana de la terminal donde corre una sesión: sube por los procesos padre del
    hook hasta uno con ventana; si la cadena se corta (apps tipo Electron), busca por nombre del programa."""
    (parent, exe), windows = processes(), app_windows()
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
        bring_to_front(hwnd)
    return bool(hwnd)


def focus_app(app_id):
    """Abre la app que está sonando: su ventana si está abierta (Spotify.exe, chrome.exe…), o la lanza
    por su identificador de Windows (apps de la Tienda)."""
    if not app_id:
        return False
    name = app_id.lower() if app_id.lower().endswith(".exe") else app_id.split("!")[0].lower() + ".exe"
    (_, exe), windows = processes(), app_windows()
    hwnd = next((h for pid, h in windows.items() if exe.get(pid) == name), None)
    if hwnd:
        bring_to_front(hwnd)
        return True
    try:
        os.startfile("shell:AppsFolder\\" + app_id)
        return True
    except OSError:
        return False


class MONITORINFOEX(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT), ("work", wintypes.RECT),
                ("flags", wintypes.DWORD), ("device", ctypes.c_wchar * 32)]


def monitors():
    """Pantallas conectadas: [{"name", "x", "y", "w", "scale", "primary"}] (escala = DPI de esa pantalla / 96)."""
    u, out = ctypes.windll.user32, []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    def each(h, _hdc, _rect, _):
        mi = MONITORINFOEX()
        mi.size = ctypes.sizeof(mi)
        u.GetMonitorInfoW(h, ctypes.byref(mi))
        dpi, _y = wintypes.UINT(96), wintypes.UINT()
        try:
            ctypes.windll.shcore.GetDpiForMonitor(h, 0, ctypes.byref(dpi), ctypes.byref(_y))
        except OSError:
            pass
        r = mi.monitor
        out.append({"name": mi.device, "x": r.left, "y": r.top, "w": r.right - r.left, "h": r.bottom - r.top,
                    "scale": dpi.value / 96, "primary": bool(mi.flags & 1)})
        return True
    u.EnumDisplayMonitors(None, None, each, 0)
    return out


def pick_monitor(mons, choice, pointer):
    """La pantalla para la isla: la principal, la del mouse, o una elegida por nombre (si sigue conectada)."""
    if choice == "mouse":
        px, py = pointer
        for m in mons:
            if m["x"] <= px < m["x"] + m["w"] and m["y"] <= py < m["y"] + m["h"]:
                return m
    named = next((m for m in mons if m["name"] == choice), None)
    return named or next((m for m in mons if m["primary"]), mons[0] if mons else None)


def fullscreen():
    """True si hay un juego, video o presentación en pantalla completa."""
    state = ctypes.c_int()
    ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
    return state.value in (2, 3, 4)  # QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE


def removable_drives():
    """{"E": "Kingston"} de las memorias USB / tarjetas SD con algo adentro."""
    k, out = ctypes.windll.kernel32, {}
    mask = k.GetLogicalDrives()
    for i in range(26):
        root = f"{chr(65 + i)}:\\"
        if mask >> i & 1 and k.GetDriveTypeW(root) == 2:  # DRIVE_REMOVABLE
            label = ctypes.create_unicode_buffer(261)
            if k.GetVolumeInformationW(root, label, 261, None, None, None, None, 0):  # 0 = lector sin tarjeta
                out[chr(65 + i)] = label.value or "Memoria USB"
    return out


# ---------- portapapeles, micrófono/cámara, enfoque ----------

def clipboard_seq():
    return ctypes.windll.user32.GetClipboardSequenceNumber()


def clipboard_private():
    """Los gestores de contraseñas marcan lo que copian para que nadie lo muestre: se respeta."""
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    if u.IsClipboardFormatAvailable(u.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing")):
        return True
    fmt = u.RegisterClipboardFormatW("CanIncludeInClipboardHistory")  # vale 0 = "no lo guardes" (contraseñas)
    if not u.IsClipboardFormatAvailable(fmt) or not u.OpenClipboard(None):
        return False
    try:
        u.GetClipboardData.restype = k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalLock.argtypes = k.GlobalUnlock.argtypes = [ctypes.c_void_p]
        h = u.GetClipboardData(fmt)
        ptr = k.GlobalLock(h) if h else None
        if not ptr:
            return False
        try:
            return ctypes.c_uint32.from_address(ptr).value == 0
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


def clipboard_text():
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.GetClipboardData.restype = k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not u.OpenClipboard(None):
        return ""
    try:
        h = u.GetClipboardData(13)  # CF_UNICODETEXT
        ptr = k.GlobalLock(h) if h else None
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


def pictures_dir():
    """La carpeta Imágenes de verdad (puede estar en OneDrive)."""
    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]
    folder = GUID(0x33E28130, 0x4E1E, 0x4676, (ctypes.c_ubyte * 8)(0x83, 0x5A, 0x98, 0x39, 0x5C, 0x3B, 0xC3, 0xBB))
    out = ctypes.c_wchar_p()
    try:
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(out)) == 0:
            path = Path(out.value)
            ctypes.windll.ole32.CoTaskMemFree(out)
            return path
    except OSError:
        pass
    return Path.home() / "Pictures"


def in_use():
    """{"microphone": {"Discord", …}, "webcam": {…}}: apps que lo están usando ahora (Windows lo anota en el
    registro: LastUsedTimeStop = 0 mientras sigue en uso)."""
    import winreg
    base = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore"
    out = {}
    for cap in ("microphone", "webcam"):
        apps = set()
        for sub in ("NonPackaged", ""):
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + "\\" + cap + ("\\" + sub if sub else ""))
            except OSError:
                continue
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                if name == "NonPackaged":
                    continue
                try:
                    with winreg.OpenKey(key, name) as app:
                        if winreg.QueryValueEx(app, "LastUsedTimeStop")[0] == 0 and                                 winreg.QueryValueEx(app, "LastUsedTimeStart")[0] > 0:
                            # "C:#Program Files#Discord#Discord.exe" o "Microsoft.WindowsCamera_8wekyb3d8bbwe"
                            short = name.split("#")[-1].removesuffix(".exe") if "#" in name else name.split("_")[0].split(".")[-1]
                            apps.add(short)
                except OSError:
                    pass
        out[cap] = apps
    return out


def mic_mute(toggle=False):
    """Silencia/activa el micrófono por defecto; devuelve si quedó silenciado (None si no hay)."""
    try:
        from pycaw.pycaw import AudioUtilities
        vol = AudioUtilities.CreateDevice(AudioUtilities.GetMicrophone()).EndpointVolume
        if toggle:
            vol.SetMute(not vol.GetMute(), None)
        return bool(vol.GetMute())
    except (ImportError, OSError, AttributeError):
        return None


def idle_seconds():
    """Segundos desde la última tecla o movimiento del mouse."""
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("size", wintypes.UINT), ("time", wintypes.DWORD)]
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
    return (ctypes.windll.kernel32.GetTickCount() - info.time) / 1000


class Focus:
    """Pomodoro: 25 min de enfoque y 5 de descanso (15 cada 4 rondas)."""
    MINUTES = {"work": 25, "rest": 5, "long": 15}

    def __init__(self):
        self.phase, self.ends, self.round = None, 0.0, 0

    def start(self):
        self.phase, self.round = "work", 1
        self.ends = time.time() + self.MINUTES["work"] * 60

    def stop(self):
        self.phase = None

    def left(self):
        return max(0.0, self.ends - time.time()) if self.phase else None

    def progress(self):
        return 1 - self.left() / (self.MINUTES[self.phase] * 60) if self.phase else 0

    def update(self, now):
        """Pasa de fase cuando se acaba el tiempo; devuelve la fase nueva (o None si no cambió)."""
        if not self.phase or now < self.ends:
            return None
        if self.phase == "work":
            self.phase = "long" if self.round % 4 == 0 else "rest"
        else:
            self.phase, self.round = "work", self.round + 1
        self.ends = now + self.MINUTES[self.phase] * 60
        return self.phase


PRIVACY = {"microphone": "#ff9f0a", "webcam": "#30d158"}  # naranja = micrófono, verde = cámara (como el iPhone)
HEADPHONES = ("auricular", "headphone", "headset", "buds", "airpods", "hands-free", "manos libres")


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
        self.reply_enabled = True
        self.reply = None  # oferta de "Responder" tras el último Stop (ver wait_reply)
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

    # --- responder a Claude desde la isla ---
    # Cuando Claude termina, el hook Stop espera REPLY_GRACE s. Si en ese rato tocas "Responder", espera
    # a que escribas; lo escrito vuelve como {"decision": "block", "reason": ...}, que para Claude Code
    # significa "no termines todavía: sigue con esto". Así le llega a ESA sesión, sin tocar la terminal.

    def wait_reply(self, sid):
        old, r = self.reply, {"sid": sid, "done": threading.Event(), "open": False, "text": None,
                              "until": time.time() + REPLY_GRACE}
        if old and not old["open"]:
            old["cancel"] = True
            old["done"].set()  # otra sesión terminó después: la oferta pasa a la más reciente
        self.reply = r
        r["done"].wait(REPLY_GRACE)
        deadline = time.time() + REPLY_WAIT
        while r["open"] and r["text"] is None and not r.get("cancel") and time.time() < deadline:
            time.sleep(0.1)  # abriste el cuadro: esperar a que escribas (o canceles)
        if self.reply is r:
            self.reply = None
        text = (r["text"] or "").strip()
        return json.dumps({"decision": "block", "reason": text}).encode() if text else b""

    def open_reply(self):
        r = self.reply
        if r and not r["done"].is_set() and not r.get("cancel"):
            r["open"] = True
            return r

    def send_reply(self, r, text):
        r["text"] = text
        r["done"].set()
        if text.strip():
            with self.lock:
                s = self.sessions.get(r["sid"])
                if s:
                    s.update(status="Pensando…", detail=[(first_line(text), GREY)], mood="focused",
                             since=time.time(), took=None)

    def drop_reply(self, sid=None):
        """Cancela la oferta (de esa sesión, o la que haya)."""
        r = self.reply
        if r and (sid is None or r["sid"] == sid):
            r["cancel"] = True
            r["done"].set()

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
        if name in ("UserPromptSubmit", "SessionEnd"):  # escribiste en la terminal: ya no hace falta
            self.drop_reply(sid)
        if name == "Stop" and self.reply_enabled and self.enabled:
            return self.wait_reply(sid)
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


def shade(col, k):
    """col oscurecido (k = 0 negro, 1 igual): fondos tenues del color de acento."""
    return "#%02x%02x%02x" % tuple(round(int(col[i:i + 2], 16) * k) for i in (1, 3, 5))


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
        self.bmp = g.CreateDIBSection(self.mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        g.SelectObject(self.mem, self.bmp)
        self.buf = (ctypes.c_ubyte * (W * H * 4)).from_address(bits.value)  # BGRA premultiplicado = ARGB32
        self.surface = cairo.ImageSurface.create_for_data(self.buf, cairo.FORMAT_ARGB32, W, H, W * 4)

    def close(self):
        """Libera el bitmap (al cambiar de tamaño o de pantalla se crea uno nuevo)."""
        g = ctypes.windll.gdi32
        self.surface.finish()
        g.DeleteObject.argtypes = g.DeleteDC.argtypes = [ctypes.c_void_p]
        g.DeleteObject(self.bmp)
        g.DeleteDC(self.mem)
        self.u.ReleaseDC(None, ctypes.c_void_p(self.screen))

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


CHARACTERS = [("Robot", "robot"), ("Chispa", "chispa"), ("Burbuja", "burbuja"), ("Gato", "gato")]
BODY_COLORS = [("Blanco", "#f2f2f2"), ("Coral", "#ff9478"), ("Lila", "#c9b8ff"), ("Celeste", "#8fd8ff"),
               ("Menta", "#93eeb5"), ("Amarillo", "#ffe08a")]
INK = "#15151b"  # ojos y visor oscuros


def eyes(p, x, y, gap, e, mood, col, lw, now, look=(0, 0)):
    """Par de ojos según el ánimo: ^ ^ contento, corazones, rayas dormido, chicos trabajando, grandes curioso.
    look = hacia dónde miran (-1..1 en x e y): los ojos se corren un poco hacia el mouse."""
    blink = mood != "sleep" and now % 4.3 < 0.12
    x, y = x + look[0] * e * 0.5, y + look[1] * e * 0.35
    for ex in (x - gap, x + gap):
        if mood == "sleep" or blink:
            p.line(ex - e * 0.8, y, ex + e * 0.8, y, col, p.alpha, lw)
        elif mood == "happy":  # ^ ^
            p.c.new_sub_path()
            p.c.arc(ex, y + e * 0.5, e * 0.85, math.radians(-155), math.radians(-25))
            p.c.set_line_width(lw)
            p.c.set_line_cap(cairo.LINE_CAP_ROUND)
            p.rgba(col, p.alpha)
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
            p.rgba(col, p.alpha)
            c.fill()
        else:  # focused: ojos chicos; curious: ojos grandes
            k = 0.6 if mood == "focused" else 0.9
            p.oval(ex - e * k, y - e * k * 1.3, ex + e * k, y + e * k * 1.3, col, p.alpha)


def character(p, kind, x, y, s, mood, now, a=1.0, body="#f2f2f2", look=(0, 0), hop=0.0):
    """Personaje de ancho ~s centrado en (x, y): respira, parpadea y los ojos cambian con el ánimo.
    El color del ánimo va en un detalle propio de cada uno (antena, destello, brote, orejas).
    look: hacia dónde mira; hop: 0..1, cuánto está saltando."""
    col, e = MOODS[mood], s * 0.11
    p.alpha = a
    busy = mood == "focused"
    y += math.sin(now * (4.5 if busy else 2.2)) * s * 0.025 - hop * s * 0.3
    glow = 0.55 + 0.45 * math.sin(now * 7) if busy else 1  # titila mientras trabaja
    lw = max(1.2, s / 18)
    edge = shade(body, 0.82)
    if kind == "chispa":  # estrella de cuatro puntas que late
        k = s * 0.5 * (1 + (0.07 if busy else 0.04) * math.sin(now * (6 if busy else 2.5)))
        c = p.c
        tips = [(0, -1), (1, 0), (0, 1), (-1, 0)]
        c.move_to(x, y - k)
        for i in range(4):  # de punta a punta, curvando hacia el centro
            (x1, y1), (x2, y2) = tips[i], tips[(i + 1) % 4]
            cx, cy = x + (x1 + x2) * k * 0.3, y + (y1 + y2) * k * 0.3  # 0.3: estrella rellena
            c.curve_to(cx, cy, cx, cy, x + x2 * k, y + y2 * k)
        c.close_path()
        p.rgba(body, a)
        c.fill()
        sx, sy, r = x + s * 0.42, y - s * 0.4, s * 0.1  # destello del ánimo
        p.line(sx - r, sy, sx + r, sy, col, a * glow, lw)
        p.line(sx, sy - r, sx, sy + r, col, a * glow, lw)
        eyes(p, x, y + s * 0.02, s * 0.1, e * 0.75, mood, INK, lw, now, look)
    elif kind == "burbuja":  # gota que se estira cuando trabaja
        t = math.sin(now * (6 if busy else 2.2)) * (0.07 if busy else 0.035)
        w, h = s * 0.46 * (1 + t), s * 0.4 * (1 - t)
        p.line(x, y - h, x + s * 0.06, y - h - s * 0.14, "#7bd88f" if body == "#93eeb5" else edge, a, lw)
        p.circle(x + s * 0.1, y - h - s * 0.15, e * 0.75, col, a * glow)  # brote del ánimo
        p.oval(x - w, y - h, x + w, y + h, body, a)
        p.oval(x - w * 0.55, y - h * 0.7, x - w * 0.15, y - h * 0.35, WHITE, a * 0.45)  # brillo
        eyes(p, x, y + s * 0.03, s * 0.14, e, mood, INK, lw, now, look)
    elif kind == "gato":
        for side in (-1, 1):  # orejas, con el interior del color del ánimo
            ox = x + side * s * 0.27
            ear = [(ox - s * 0.17, y - s * 0.12), (ox + side * s * 0.06, y - s * 0.5), (ox + s * 0.17, y - s * 0.12)]
            p.poly(ear, body, a)
            mx, my = sum(q[0] for q in ear) / 3, sum(q[1] for q in ear) / 3 + s * 0.02
            p.poly([(mx + (qx - mx) * 0.55, my + (qy - my) * 0.55) for qx, qy in ear], col, a * glow)
        p.oval(x - s * 0.46, y - s * 0.34, x + s * 0.46, y + s * 0.38, body, a)
        eyes(p, x, y + s * 0.0, s * 0.17, e, mood, INK, lw, now, look)
        p.poly([(x - s * 0.04, y + s * 0.13), (x + s * 0.04, y + s * 0.13), (x, y + s * 0.18)], "#ff8fa3", a)
        for side in (-1, 1):  # bigotes
            p.line(x + side * s * 0.2, y + s * 0.16, x + side * s * 0.4, y + s * 0.13, edge, a, lw * 0.6)
    else:  # robot
        p.line(x, y - s * 0.38, x, y - s * 0.52, "#dddddd", a, lw)
        p.circle(x, y - s * 0.55, e * 0.85, col, a * glow)
        for side in (-1, 1):  # orejas
            p.oval(x + side * s * 0.5 - e, y - e * 1.4, x + side * s * 0.5 + e, y + e * 1.4, edge, a)
        p.oval(x - s * 0.46, y - s * 0.4, x + s * 0.46, y + s * 0.4, body, a)
        p.rrect(x - s * 0.33, y - s * 0.2, x + s * 0.33, y + s * 0.2, s * 0.2, INK, a)
        eyes(p, x, y, s * 0.15, e, mood, col, lw, now, look)


def robot(p, x, y, s, mood, now, a=1.0):
    character(p, "robot", x, y, s, mood, now, a)


def art_surface(data, px):
    """Carátula recortada en cuadrado como superficie cairo (las esquinas las redondea un clip)."""
    im = Image.open(io.BytesIO(data)).convert("RGB")
    side = min(im.size)
    im = im.crop(((im.width - side) // 2, (im.height - side) // 2,
                  (im.width + side) // 2, (im.height + side) // 2)).resize((px, px), Image.LANCZOS)
    raw = bytearray(im.convert("RGBA").tobytes("raw", "BGRA"))
    return cairo.ImageSurface.create_for_data(raw, cairo.FORMAT_ARGB32, px, px, px * 4), raw


def start_tray(toggle, quit_, settings):
    """Ícono en la bandeja de Windows: clic = mostrar/ocultar la isla."""
    try:
        import pystray
    except ImportError:
        return None
    menu = pystray.Menu(pystray.MenuItem("Mostrar / ocultar  (Ctrl+Alt+I)", lambda: toggle(), default=True),
                        pystray.MenuItem("Ajustes…", lambda: settings()),
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
        dpi_scale()  # pide píxeles reales a Windows (antes de medir pantallas)
        self.mon = pick_monitor(monitors(), self.cfg["monitor"], self.pointer())
        self.base_S = self.mon["scale"] if self.mon else 1.0
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = ctypes.c_void_p
        before = u.GetForegroundWindow()  # al crearse, la ventana de Tk se lleva el foco: se lo devolvemos
        self.root = root = tk.Tk()
        import registro
        registro.install(root, VERSION)
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
        loop = self.media.loop
        self.weather, self.calendar, self.updater = Weather(loop), Calendar(loop), Updater(loop)
        self.card = None  # tarjeta propia: {"kind": "update" | "cal", "until", ...}
        self.update_offered = None  # versión que ya se ofreció (la tarjeta sale una sola vez)
        self.cal_alerted = set()  # citas ya avisadas
        self.reply_box = None  # ventana para escribirle a Claude
        self.apply_config()
        self.server = serve(self.claude)
        self.alert_until = self.stats_at = 0
        self.hidden, self.mode = False, "small"
        self.shown, self.quitting = True, False  # mostrar/ocultar a mano (bandeja o Ctrl+Alt+I)
        self.cleared = False  # ya se borró la ventana estando oculta
        self.keep_open_until = 0  # abierta a la fuerza (menú de ajustes recién usado)
        self.drag = None  # (x inicial, y inicial) de un arrastre sobre la música
        self.ac = self.drives = None  # cargador y memorias USB (None = todavía no se miró)
        self.focus = Focus()
        self.clip_seq, self.clip_at = clipboard_seq(), 0.0  # lo que ya estaba copiado no se muestra
        self.inuse = {}  # micrófono/cámara en uso: {"microphone": {"Discord"}, …}
        self.poke_t = self.celebrate_t = -9.0  # cariño (clic en el personaje) y festejo (Claude terminó)
        self.last_flash, self.idle, self.ptr = 0.0, 0.0, (0, 0)
        self.drag_dx = self.drag_vis = 0.0  # cuánto se arrastró / cuánto se ve corrido el contenido
        self.hov = {"play": 0.0, "prev": 0.0, "next": 0.0}  # 0..1: cuánto está "encendido" cada botón
        self.pressed = {}  # botón -> momento del último clic
        self.morph = 0.0  # 0 = ▶, 1 = ⏸ (se transforma de a poco)
        self.art_k = 1.0  # tamaño de la carátula: chica en pausa, grande sonando
        self.build_menu()
        self.settings, self.settings_requested = None, False  # ventana de ajustes (ajustes.py)
        self.tray = start_tray(self.toggle, self.request_quit, self.request_settings)
        threading.Thread(target=hotkey_loop, args=(self.toggle,), daemon=True).start()
        root.bind("<Button-3>", self.popup)
        root.bind("<MouseWheel>", self.wheel)
        root.bind("<ButtonPress-1>", self.press_start)
        root.bind("<B1-Motion>", self.press_move)
        root.bind("<ButtonRelease-1>", self.press_end)
        root.bind("<Double-Button-1>", self.double_click)
        self.tick()

    # --- ajustes ---

    def resize(self):
        """(Re)crea la ventana para la escala actual (DPI de Windows × tamaño elegido)."""
        self.S = self.base_S * self.cfg["size"]
        self.W, self.H = round(WIN[0] * self.S), round(WIN[1] * self.S)
        m = self.mon or {"x": 0, "y": 0, "w": self.root.winfo_screenwidth()}
        self.X, self.Y = m["x"] + (m["w"] - self.W) // 2, m["y"]
        self.root.geometry(f"{self.W}x{self.H}+{self.X}+{self.Y}")
        if getattr(self, "win", None):
            self.win.close()
        self.win = Layered(self.hwnd, self.W, self.H)
        self.p = Paint(cairo.Context(self.win.surface))
        self.art = self.icon = (None, None)  # se regeneran a la nueva resolución

    @staticmethod
    def pointer():
        pt = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def follow_monitor(self):
        """Mueve la isla si cambió la pantalla elegida (o la del mouse, o se desconectó un monitor)."""
        m = pick_monitor(monitors(), self.cfg["monitor"], self.pointer())
        if m and m != self.mon:
            self.mon, self.base_S = m, m["scale"]
            self.resize()

    def char(self, p, x, y, size, mood, now, a=1.0):
        """Dibuja el personaje elegido: mira hacia el mouse, salta cuando le haces clic o Claude termina,
        muestra el anillo del enfoque y "z z" si estás hace rato sin usar la PC."""
        t = now - max(self.poke_t, self.celebrate_t)
        hop = math.sin(t * math.pi * 2.5) ** 2 * math.exp(-t * 2.2) if 0 <= t < 1.6 else 0.0
        if 0 <= now - self.poke_t < 1.2:
            mood = "love"  # le hiciste cariño
        sx, sy = self.X + (self.left_edge() + x) * self.S, self.Y + y * self.S  # dónde está, en la pantalla
        dx, dy = self.ptr[0] - sx, self.ptr[1] - sy
        dist = math.hypot(dx, dy) or 1
        k = min(1.0, dist / (160 * self.S))
        if self.focus.phase:  # anillo de enfoque alrededor
            r = size * 0.68
            col = self.accent if self.focus.phase == "work" else MOODS["curious"]
            p.c.new_sub_path()
            p.c.arc(x, y, r, 0, 2 * math.pi)
            p.c.set_line_width(max(1.5, size / 14))
            p.rgba(shade(col, 0.3), a)
            p.c.stroke()
            p.c.new_sub_path()
            p.c.arc(x, y, r, -math.pi / 2, -math.pi / 2 + 2 * math.pi * self.focus.progress())
            p.rgba(col, a)
            p.c.stroke()
        character(p, self.cfg["character"], x, y, size, mood, now, a, self.cfg["body"],
                  look=(dx / dist * k, dy / dist * k), hop=hop)
        if mood == "sleep" and self.idle > 300:  # z z flotando
            for i in range(2):
                ph = (now * 0.5 + i * 0.5) % 1
                p.text(x + size * (0.45 + ph * 0.3), y - size * (0.3 + ph * 0.5), "z", "tiny" if i else "small",
                       GREY, a * (1 - ph))

    def poke(self):
        self.poke_t = time.time()

    @property
    def companion(self):
        return self.cfg["mode"] == "companion"

    def mood(self, now):
        """Ánimo del personaje: el de Claude Code, o en modo compañero, según lo que pasa en tu PC."""
        if not self.companion:
            mood = self.claude.mood()
            return "sleep" if mood == "happy" and self.idle > 300 else mood  # hace rato que no estás
        if self.focus.phase == "work":
            return "focused"
        if (self.notes.current and now < self.notes.current["until"]) or (self.card and now < self.card["until"]):
            return "curious"  # llegó algo
        if now < self.levels.until or self.timer.left():
            return "focused"
        if self.media.playing:
            return "love" if now % 24 < 2.5 else "happy"  # disfruta la música (y a veces se enamora)
        hour = datetime.now().hour
        return "sleep" if hour >= 23 or hour < 7 or self.idle > 300 else "happy"

    def apply_config(self):
        self.claude.sound = self.cfg["sound"]
        self.claude.enabled = getattr(self, "shown", True) and not self.companion  # compañero: Claude va a la terminal
        self.notes.enabled = self.cfg["notifications"]
        self.claude.reply_enabled = self.cfg["reply"]
        self.weather.enabled, self.calendar.enabled = self.cfg["weather"], self.cfg["calendar"]
        self.updater.enabled = self.cfg["updates"]
        self.accent = self.cfg["accent"]
        MOODS["happy"] = self.accent  # el robot contento usa el color elegido

    def set(self, key, value):
        self.cfg[key] = value
        try:
            CONFIG.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
        except OSError:
            pass  # sin poder guardar: vale para esta sesión
        self.apply_config()
        if key == "size":
            self.resize()
        if key == "monitor":
            self.follow_monitor()
        # el menú rápido muestra lo mismo que la ventana de ajustes
        var = {"size": "size_var", "accent": "accent_var", "character": "char_var", "body": "body_var",
               "mode": "mode_var"}.get(key)
        if var and hasattr(self, var):
            getattr(self, var).set(value)
        elif key in getattr(self, "vars", {}):
            self.vars[key].set(value)

    def build_menu(self):
        font = ("Segoe UI", 10)
        m = self.menu = tk.Menu(self.root, tearoff=0, font=font)
        m.add_command(label="Ajustes…", command=self.open_settings)
        m.add_command(label="Empezar enfoque (25 + 5 min)", command=self.toggle_focus)
        m.add_separator()
        self.vars = {k: tk.BooleanVar(value=self.cfg[k]) for k in
                     ("sound", "hide_fullscreen", "notifications", "translucent", "weather", "calendar", "updates",
                      "reply", "screenshots", "clipboard_text", "privacy")}
        self.vars["startup"] = tk.BooleanVar(value=STARTUP.exists())
        self.size_var, self.accent_var = tk.DoubleVar(value=self.cfg["size"]), tk.StringVar(value=self.cfg["accent"])

        def check(menu, key, label):
            menu.add_checkbutton(label=label, variable=self.vars[key],
                                 command=lambda: self.set(key, self.vars[key].get()))

        look = tk.Menu(m, tearoff=0, font=font)
        for label, k in [("Tamaño normal", 1.0), ("Tamaño grande", 1.2), ("Tamaño muy grande", 1.4)]:
            look.add_radiobutton(label=label, value=k, variable=self.size_var, command=lambda k=k: self.set("size", k))
        look.add_separator()
        for label, col in ACCENTS:
            look.add_radiobutton(label=f"Color {label.lower()}", value=col, variable=self.accent_var,
                                 command=lambda col=col: self.set("accent", col))
        look.add_separator()
        check(look, "translucent", "Translúcida")
        m.add_cascade(label="Apariencia", menu=look)
        self.char_var, self.body_var = tk.StringVar(value=self.cfg["character"]), tk.StringVar(value=self.cfg["body"])
        self.mode_var = tk.StringVar(value=self.cfg["mode"])
        who = tk.Menu(m, tearoff=0, font=font)
        for label, k in CHARACTERS:
            who.add_radiobutton(label=label, value=k, variable=self.char_var, command=lambda k=k: self.set("character", k))
        who.add_separator()
        for label, col in BODY_COLORS:
            who.add_radiobutton(label=f"Color {label.lower()}", value=col, variable=self.body_var,
                                command=lambda col=col: self.set("body", col))
        m.add_cascade(label="Personaje", menu=who)
        modes = tk.Menu(m, tearoff=0, font=font)
        for label, k in [("Claude Code", "claude"), ("Solo compañero", "companion")]:
            modes.add_radiobutton(label=label, value=k, variable=self.mode_var, command=lambda k=k: self.set("mode", k))
        m.add_cascade(label="Modo", menu=modes)
        show = tk.Menu(m, tearoff=0, font=font)
        for key, label in [("notifications", "Notificaciones"), ("weather", "Clima"), ("calendar", "Próxima cita"),
                           ("reply", "Responder a Claude desde la isla"), ("screenshots", "Vista previa de capturas"),
                           ("clipboard_text", "Texto copiado"), ("privacy", "Micrófono y cámara en uso"),
                           ("sound", "Sonidos")]:
            check(show, key, label)
        m.add_cascade(label="Mostrar", menu=show)
        check(m, "hide_fullscreen", "Ocultar en pantalla completa")
        m.add_checkbutton(label="Iniciar con Windows", variable=self.vars["startup"],
                          command=lambda: set_startup(self.vars["startup"].get()))
        check(m, "updates", "Buscar actualizaciones")
        m.add_separator()
        m.add_command(label="Ver registro de errores", command=self.open_log)
        m.add_command(label="Ocultar", accelerator="Ctrl+Alt+I", command=self.toggle)
        m.add_command(label="Salir", command=self.quit)
        m.configure(postcommand=self.menu_update_item)

    def menu_update_item(self):
        """Antes de abrir el menú: el texto del enfoque y, si hay versión nueva, "Actualizar" arriba de todo."""
        for i in range(self.menu.index("end") + 1):
            if self.menu.type(i) == "command" and "enfoque" in self.menu.entrycget(i, "label"):
                self.menu.entryconfig(i, label="Terminar enfoque" if self.focus.phase else "Empezar enfoque (25 + 5 min)")
        label = f"Actualizar a la versión {self.updater.latest[0]}" if self.updater.latest else None
        if label and self.menu.entrycget(0, "label") != label:
            if self.menu.entrycget(0, "label").startswith("Actualizar"):
                self.menu.delete(0)
            self.menu.insert_command(0, label=label, command=self.updater.install)

    def popup(self, e):
        """Menú de ajustes. Un menú de Windows solo se cierra al hacer clic afuera si su ventana tiene el
        foco: la isla lo toma mientras el menú está abierto y después se lo devuelve a tu app."""
        u = ctypes.windll.user32
        before = u.GetForegroundWindow()
        bring_to_front(ctypes.c_void_p(self.hwnd))  # con Alt: Windows a veces no cede el primer plano sin tecla
        self.keep_open_until = float("inf")  # abierta mientras elijes, aunque el mouse esté en el menú
        try:
            self.menu.tk_popup(e.x_root, e.y_root)  # en Windows espera hasta que el menú se cierra
        finally:
            self.keep_open_until = time.time() + 6  # y unos segundos más, para ver cómo quedó el cambio
            self.menu.grab_release()
            if before and u.GetForegroundWindow() in (self.hwnd, None) and not self.settings:
                u.SetForegroundWindow(ctypes.c_void_p(before))  # (si abriste Ajustes, el foco queda ahí)

    def toggle(self):
        """Muestra u oculta la isla (puede llamarse desde otros hilos: solo cambia banderas)."""
        self.shown = not self.shown
        self.claude.enabled = self.shown and not self.companion
        if not self.shown:
            for p in list(self.claude.perms):  # los pedidos pendientes vuelven a la terminal
                p["done"].set()

    def open_log(self):
        import registro
        if not registro.LOG.exists():
            registro.write("sin errores hasta ahora")
        os.startfile(registro.LOG)

    def request_settings(self):
        self.settings_requested = True  # desde el hilo de la bandeja: se abre en el próximo tick

    def open_settings(self):
        if self.settings:
            self.settings.top.lift()
            self.settings.top.focus_force()
            return
        from ajustes import Settings
        self.settings = Settings(self)

    def request_quit(self):
        self.quitting = True  # desde el hilo de la bandeja: tkinter solo se toca desde su hilo

    def quit(self):
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    # --- eventos ---

    def left_edge(self):
        return (WIN[0] - self.w) / 2

    def local(self, e):
        """Coordenadas de un evento del mouse, en px lógicos de la isla."""
        return e.x / self.S - self.left_edge(), e.y / self.S

    def in_music(self, y):
        """¿Está y sobre la parte de música de la isla abierta?"""
        return self.mode == "big" and (self.companion or y >= 106)

    def press_start(self, e):
        x, y = self.local(e)
        self.drag = (x, y) if self.in_music(y) and not self.hit_name(x, y) else None
        if not self.drag:
            self.click(e)

    def press_move(self, e):
        if self.drag:
            self.drag_dx = self.local(e)[0] - self.drag[0]

    def press_end(self, e):
        """Deslizar sobre la música = cambiar de canción (a la derecha: anterior; a la izquierda: siguiente)."""
        if self.drag:
            dx = self.drag_dx
            self.drag, self.drag_dx = None, 0.0
            if abs(dx) > 50:
                self.media.send("try_skip_previous_async" if dx > 0 else "try_skip_next_async")
                self.pressed["prev" if dx > 0 else "next"] = time.time()  # el botón también se anima

    def double_click(self, e):
        x, y = self.local(e)
        if self.in_music(y) and not self.hit_name(x, y) and x < self.w - 120:  # carátula o título
            focus_app(self.media.app)

    def wheel(self, e):
        """Rueda: sobre el temporizador cambia los minutos; en el resto de la isla, el volumen."""
        x, y = self.local(e)
        if self.hit_name(x, y) == "timer":
            self.timer.scroll(e.delta > 0)
        else:
            self.levels.nudge(0.02 * (1 if e.delta > 0 else -1) * max(1, abs(e.delta) // 120))

    def hit_name(self, x, y):
        """Nombre de la zona clicable en (x, y) ("button" si no tiene nombre), o None."""
        for x1, y1, x2, y2, _, name in reversed(self.hits):
            if x1 <= x <= x2 and y1 <= y <= y2:
                return name or "button"
        return None

    def click(self, e):
        x, y = self.local(e)
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

    def check_cards(self, now):
        """Tarjetas propias de la isla: versión nueva disponible, o una cita que empieza en 10 min."""
        up = self.updater.latest
        if up and self.update_offered != up[0]:
            self.update_offered = up[0]
            self.card = {"kind": "update", "until": now + 12}
        nxt = self.calendar.next
        if nxt:
            mins = (nxt[1] - datetime.now(timezone.utc)).total_seconds() / 60
            if 0 <= mins <= 10 and nxt not in self.cal_alerted:
                self.cal_alerted.add(nxt)
                self.card = {"kind": "cal", "until": now + 10, "title": nxt[0], "start": nxt[1]}
                if self.cfg["sound"]:
                    winsound.MessageBeep(winsound.MB_ICONASTERISK)

    def check_clipboard(self):
        """Algo nuevo en el portapapeles: una captura (vista previa + Guardar/Abrir) o, si lo activas, texto."""
        seq = clipboard_seq()
        if seq == self.clip_seq:
            return
        self.clip_seq = seq
        u = ctypes.windll.user32
        if clipboard_private():
            return
        if self.cfg["screenshots"] and u.IsClipboardFormatAvailable(8):  # CF_DIB: una imagen
            from PIL import ImageGrab
            try:
                im = ImageGrab.grabclipboard()
            except OSError:
                return
            if Image and isinstance(im, Image.Image):
                self.shot_card(im)
        elif self.cfg["clipboard_text"] and u.IsClipboardFormatAvailable(13):
            text = " ".join(clipboard_text().split())
            if text:
                self.card = {"kind": "clip", "icon": "clip", "title": "Copiado",
                             "body": text[:200], "until": time.time() + 3}

    def shot_card(self, im):
        buf = io.BytesIO()
        im.save(buf, "PNG")
        data = buf.getvalue()
        card = {"kind": "shot", "icon": "clip", "title": "Captura copiada",
                "body": f"{im.width} × {im.height} · pégala con Ctrl+V", "until": time.time() + 7,
                "thumb": art_surface(data, round(40 * self.S * 2))}

        def save():
            folder = pictures_dir() / "Capturas"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"captura-{datetime.now():%Y%m%d-%H%M%S}.png"
            path.write_bytes(data)
            card.update(title="Captura guardada", body=f"Imágenes › Capturas › {path.name}", until=time.time() + 5,
                        buttons=[("Mostrar", lambda: subprocess.Popen(["explorer", "/select,", str(path)]), True)])

        def open_():
            import tempfile
            path = Path(tempfile.gettempdir()) / "isla-captura.png"
            path.write_bytes(data)
            os.startfile(path)
            card["until"] = 0
        card["buttons"] = [("Abrir", open_, False), ("Guardar", save, True)]
        self.card = card

    def check_privacy(self):
        """Una app empezó a usar el micrófono o la cámara: tarjeta (con Silenciar) y un punto junto a la isla."""
        cur = in_use() if self.cfg["privacy"] else {}
        for cap, apps in cur.items():
            for app in sorted(apps - self.inuse.get(cap, set())):
                mic = cap == "microphone"
                card = {"kind": "privacy", "icon": "mic" if mic else "cam", "until": time.time() + 6,
                        "title": f"{app} está usando {'el micrófono' if mic else 'la cámara'}",
                        "body": "Verás un punto junto a la isla mientras siga"}
                if mic:
                    def toggle(card=card):
                        muted = mic_mute(toggle=True)
                        card.update(body="Micrófono silenciado" if muted else "Micrófono activado",
                                    until=time.time() + 4,
                                    buttons=[("Activar" if muted else "Silenciar", toggle, not muted)])
                    muted = mic_mute()
                    card["buttons"] = [("Activar" if muted else "Silenciar", toggle, not muted)]
                self.card = card
        self.inuse = cur

    def focus_card(self, phase):
        minutes = Focus.MINUTES[phase]
        if phase == "work":
            title, body = "A concentrarse", f"{minutes} minutos · ronda {self.focus.round}"
        else:
            title = "¡Descanso largo!" if phase == "long" else "¡Descanso!"
            body = f"{minutes} minutos · terminaste la ronda {self.focus.round}"
        self.card = {"kind": "focus", "icon": "focus", "title": title, "body": body, "until": time.time() + 8,
                     "buttons": [("Terminar", self.stop_focus, False)]}
        self.celebrate_t = time.time()
        if self.cfg["sound"]:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)

    def toggle_focus(self):
        if self.focus.phase:
            self.stop_focus()
        else:
            self.focus.start()
            self.focus_card("work")

    def stop_focus(self):
        self.focus.stop()
        if self.card and self.card["kind"] == "focus":
            self.card["until"] = 0

    def device_card(self, icon, title, body, action=None):
        self.card = {"kind": "device", "icon": icon, "title": title, "body": body, "action": action,
                     "until": time.time() + 5}

    def check_devices(self, now):
        """Cargador, salida de audio y memorias USB: una tarjeta cuando algo se conecta o se desconecta."""
        if self.bat:
            if self.ac is not None and self.bat[1] != self.ac:
                self.device_card("plug" if self.bat[1] else "battery",
                                 "Cargando" if self.bat[1] else "Cargador desconectado", f"Batería al {self.bat[0]}%")
            self.ac = self.bat[1]
        out = self.levels.device_event
        if out:
            self.levels.device_event = None
            phones = any(word in out.lower() for word in HEADPHONES)
            self.device_card("headphones" if phones else "speaker", out, "Ahora el sonido sale por acá")
        drives = removable_drives()
        if self.drives is not None:
            import shutil
            for letter in sorted(drives.keys() - self.drives.keys()):
                root = letter + ":\\"
                try:
                    free = f" · {shutil.disk_usage(root).free / 1e9:.1f} GB libres"
                except OSError:
                    free = ""
                self.device_card("usb", f"{drives[letter]} conectado", f"Unidad {letter}:{free} · clic para abrir",
                                 lambda root=root: os.startfile(root))
            for letter in sorted(self.drives.keys() - drives.keys()):
                self.device_card("usb", f"{self.drives[letter]} desconectado", f"Unidad {letter}: · ya puedes guardarla")
        self.drives = drives

    def next_session(self):
        items = self.claude.ordered()
        if items:
            sid, _, i, n = self.session()
            self.pinned = items[i % n][0]

    def tick(self):
        root, now, S = self.root, time.time(), self.S
        if self.quitting:
            return self.quit()
        if self.settings_requested:
            self.settings_requested = False
            self.open_settings()
        if now - self.stats_at >= 1:
            self.stats_at = now
            self.bat, self.cpu_pct, self.ram_pct = battery(), self.cpu.percent(), ram()
            self.hidden = self.cfg["hide_fullscreen"] and fullscreen()
            self.follow_monitor()
            self.check_devices(now)
            self.check_privacy()
            self.idle = idle_seconds()
        left = self.timer.left()
        if left is not None and left <= 0:
            self.timer.ends = None
            self.alert_until = now + 6
            if self.cfg["sound"]:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
        if now - self.clip_at >= 0.5:
            self.clip_at = now
            self.check_clipboard()
        phase = self.focus.update(now)
        if phase:
            self.focus_card(phase)
        if self.claude.flash_until != self.last_flash:  # Claude terminó: festejo
            self.last_flash = self.claude.flash_until
            if not self.companion:
                self.celebrate_t = now
        px, py = root.winfo_pointerxy()
        self.ptr = (px, py)
        lx, ly = (px - self.X) / S - self.left_edge(), (py - self.Y) / S
        hover = 0 <= lx < self.w and 0 <= ly < self.h
        note = self.notes.current
        if note and self.card and self.card["kind"] == "shot" and note["app"] in ("Herramienta Recortes", "Snipping Tool"):
            self.notes.current = note = None  # la captura ya se muestra en la isla: sin aviso repetido
        if note and hover and self.mode == "note":
            note["until"] = max(note["until"], now + 1)  # no se va mientras la lees
        self.check_cards(now)
        if self.reply_box and (self.claude.reply is not self.reply_box[1] or self.reply_box[1].get("cancel")):
            self.close_reply_box(None)  # Claude ya siguió (respondiste en la terminal o se acabó el tiempo)
        card = self.card if self.card and now < self.card["until"] else None
        if card and hover and self.mode == "card":
            card["until"] = max(card["until"], now + 1)
        reply = self.claude.reply
        replying = self.reply_box is not None or (reply and not reply.get("cancel") and now < reply["until"])
        if self.claude.perms:  # aunque haya hover: si se expandiera, los botones se escaparían del mouse
            mode = "perm"
        elif now < self.levels.until:
            mode = "level"
        elif note and now < note["until"]:
            mode = "note"
        elif card:
            mode = "card"
        elif hover or replying or now < self.keep_open_until or now < self.alert_until or (now < self.claude.flash_until and not self.companion):
            mode = "big"
        else:
            mode = "small"
        self.mode = mode
        big = BIG_COMPANION if self.companion else BIG
        target = {"perm": PERM, "level": LEVEL, "note": NOTIF, "card": NOTIF, "big": big, "small": SMALL}[mode]
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
        self.drag_vis += (self.drag_dx - self.drag_vis) * 0.35
        self.art_k += ((1.0 if self.media.playing else 0.9) - self.art_k) * 0.15
        visible = self.shown and not (self.hidden and mode != "perm")
        if visible or not self.cleared:  # oculta: se borra una vez y no se vuelve a dibujar
            self.draw(self.w, self.h, now, mode, target)
            self.win.show(self.X, self.Y)
        self.cleared = not visible
        # ahorro: 60 fps solo mientras cambia de tamaño; quieta, lo justo para que el robot respire
        moving = (abs(self.vw) + abs(self.vh) > 0.05 or abs(target[0] - self.w) + abs(target[1] - self.h) > 0.5
                  or now - max(self.pressed.values(), default=0) < 0.7  # botón rebotando
                  or self.drag or abs(self.drag_vis) > 0.5  # arrastrando la música
                  or now - max(self.poke_t, self.celebrate_t) < 1.6  # el personaje saltando
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
        p, c, mood = self.p, self.p.c, self.mood(now)
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
        if self.cfg["translucent"]:  # vidrio oscuro: se ve un poco lo de atrás, con un borde sutil
            c.set_source_rgba(0.03, 0.03, 0.05, 0.78)
            c.fill_preserve()
            c.set_source_rgba(1, 1, 1, 0.09)
            c.set_line_width(1)
            c.stroke_preserve()
        else:
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
        elif mode == "card":
            self.draw_card(w, h, now, near)
        elif mode == "big":
            a = clamp((h - (40 if self.companion else 100)) / (target[1] - (40 if self.companion else 100))) ** 2
            if now < self.alert_until:
                p.text(w / 2, h / 2, "⏰  ¡Tiempo!", "symbig", self.accent, a, anchor="center")
            elif self.companion:
                self.draw_music(w, now, a, dy=-98)
            else:
                self.draw_claude(w, now, mood, a)
                p.line(18, 104, w - 18, 104, "#2a2a2e", a, 1)
                self.draw_music(w, now, a)
        else:
            self.draw_small(w, h, now, mood, clamp(1 - (h - SMALL[1]) / 30) ** 2)
        c.restore()
        for i, cap in enumerate(c for c in ("microphone", "webcam") if self.inuse.get(c)):
            p.circle(w + 10 + i * 10, 10, 3.5, PRIVACY[cap], 1)

    def sys_info(self):
        """(texto, fuente, color) de batería o, en PCs de escritorio, CPU y RAM."""
        bat = self.bat
        if bat:
            return (f"{'⚡' if bat[1] else '▮'} {bat[0]}%", "sym",
                    self.accent if bat[1] else RED if bat[0] <= 20 else WHITE)
        return (f"CPU {self.cpu_pct}%  RAM {self.ram_pct}%", "small",
                RED if max(self.cpu_pct, self.ram_pct) >= 90 else WHITE)

    def draw_small(self, w, h, now, mood, a):
        p, m, left = self.p, self.media, self.timer.left()
        self.char(p, 24, h / 2 + 1, 22, mood, now, a)
        self.hit(2, 0, 40, h, self.poke)
        if m.playing:  # ecualizador animado
            for i in range(3):
                bh = 3 + 5 * abs(math.sin(now * (5 + i * 1.7) + i))
                p.line(44 + i * 4.5, h / 2 + 4.5, 44 + i * 4.5, h / 2 + 4.5 - bh, self.accent, a, 2.4)
        _, s, _, _ = self.session()
        if s and s["since"] and not self.companion:  # Claude trabajando: cuánto lleva
            p.text(w / 2, h / 2, mmss(now - s["since"]), "clock", MOODS["focused"], a, anchor="center")
        else:
            p.text(w / 2, h / 2, time.strftime("%H:%M"), "clock", WHITE, a, anchor="center")
        mins = self.minutes_to_event()
        if self.focus.phase:
            txt, col = mmss(self.focus.left()), self.accent if self.focus.phase == "work" else MOODS["curious"]
        elif left:
            txt, col = mmss(left), self.accent
        elif mins is not None and mins <= 60:  # cita pronto: cuánto falta
            txt, col = f"en {mins} min" if mins else "ahora", self.accent
        elif self.bat:
            txt, col = f"{self.bat[0]}%", self.sys_info()[2]
        elif self.weather.temp is not None and self.cfg["weather"]:  # escritorio: el clima es más útil que la CPU
            txt, col = f"{self.weather.icon} {self.weather.temp}°", WHITE
        else:
            txt, col = f"{self.cpu_pct}%", WHITE
        p.text(w - 18, h / 2, txt, "small", col, a, anchor="e")

    def minutes_to_event(self):
        nxt = self.calendar.next if self.cfg["calendar"] else None
        if nxt:
            return max(0, round((nxt[1] - datetime.now(timezone.utc)).total_seconds() / 60))

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

    def draw_card(self, w, h, now, a):
        """Tarjeta propia: actualización, cita, dispositivo, captura, micrófono/cámara o enfoque.
        Con "buttons" [(texto, acción, principal)] muestra botones; si no, clic = su acción y se cierra."""
        p, c, card = self.p, self.p.c, self.card
        x0, y0, size = 14, (h - 40) / 2, 40
        kind, buttons = card["kind"], card.get("buttons", [])
        if kind == "update":
            ver = self.updater.latest[0] if self.updater.latest else ""
            title = f"Nueva versión {ver}"
            body = {"descargando": "Descargando… se reinicia sola al terminar",
                    "error": "No se pudo descargar; prueba más tarde"}.get(self.updater.state,
                                                                           "Isla dinámica se puede actualizar")
            if self.updater.state == "":
                buttons = [("Luego", lambda: card.update(until=0), False), ("Actualizar", self.updater.install, True)]
        elif kind == "cal":
            mins = max(0, round((card["start"] - datetime.now(timezone.utc)).total_seconds() / 60))
            title = card["title"]
            body = f"Empieza {'ahora' if not mins else f'en {mins} min'} · {card['start'].strftime('%H:%M')}"
        else:
            title, body = card["title"], card["body"]
        if card.get("thumb"):  # captura: miniatura con esquinas redondeadas
            surf = card["thumb"][0]
            c.save()
            p.rrect_path(x0, y0, x0 + size, y0 + size, 11)
            c.clip()
            c.translate(x0, y0)
            c.scale(size / surf.get_width(), size / surf.get_width())
            c.set_source_surface(surf, 0, 0)
            c.get_source().set_filter(cairo.FILTER_GOOD)
            c.paint_with_alpha(clamp(a))
            c.restore()
        else:
            p.rrect(x0, y0, x0 + size, y0 + size, 11, shade(self.accent, 0.25), a)
            cx, cy = x0 + size / 2, y0 + size / 2
            if kind == "update":
                self.char(p, cx, cy + 3, 26, "happy", now, a)
            elif kind == "cal":  # hojita con argollas
                p.rrect(cx - 11, cy - 9, cx + 11, cy + 12, 3, WHITE, a)
                p.rrect(cx - 11, cy - 9, cx + 11, cy - 3, 3, self.accent, a)
                for dx in (-5, 5):
                    p.line(cx + dx, cy - 12, cx + dx, cy - 7, WHITE, a, 2)
            else:
                self.draw_device_icon(card.get("icon", ""), cx, cy, a)
        x, right = x0 + size + 12, w - 22
        for label, act, primary in reversed(buttons):
            bw = p.width(label, "btn") + 24
            p.rrect(right - bw, h / 2 - 13, right, h / 2 + 13, 13, self.accent if primary else "#2a2a30", a)
            p.text(right - bw / 2, h / 2, label, "btn", "#000000" if primary else WHITE, a, anchor="center")
            self.hit(right - bw, 0, right, h, act)
            right -= bw + 8
        if not buttons:  # clic = la acción de la tarjeta (abrir el USB…) y se cierra
            act = card.get("action")
            self.hit(0, 0, w, h, lambda: (act and act(), card.update(until=0)))
        p.text(x, 23, p.fit(title, "title", right - x - 8), "title", WHITE, a)
        p.text(x, 42, p.fit(body, "small", right - x - 8), "small", "#c8c8cc", a)

    def draw_device_icon(self, kind, cx, cy, a):
        p, c, col = self.p, self.p.c, self.accent
        if kind == "plug":  # rayo
            p.poly([(cx + 2, cy - 11), (cx - 7, cy + 2), (cx - 1, cy + 2), (cx - 3, cy + 11), (cx + 7, cy - 2),
                    (cx + 1, cy - 2)], col, a)
        elif kind == "battery":
            p.rrect(cx - 11, cy - 6, cx + 9, cy + 6, 2.5, WHITE, a)
            p.rrect(cx - 9, cy - 4, cx + 7, cy + 4, 1.5, shade(col, 0.25), a)
            p.rrect(cx - 9, cy - 4, cx - 9 + 16 * self.bat[0] / 100 if self.bat else cx, cy + 4, 1.5, col, a)
            p.rrect(cx + 9, cy - 2.5, cx + 12, cy + 2.5, 1, WHITE, a)
        elif kind == "headphones":
            c.new_sub_path()
            c.arc(cx, cy + 2, 10, math.pi, 2 * math.pi)
            c.set_line_width(2.4)
            p.rgba(WHITE, a)
            c.stroke()
            for dx in (-10, 10):
                p.rrect(cx + dx - 3.5, cy, cx + dx + 3.5, cy + 10, 3, col, a)
        elif kind == "speaker":
            p.poly([(cx - 9, cy - 4), (cx - 4, cy - 4), (cx + 2, cy - 9), (cx + 2, cy + 9), (cx - 4, cy + 4),
                    (cx - 9, cy + 4)], WHITE, a)
            c.new_sub_path()
            c.arc(cx + 3, cy, 7, -0.8, 0.8)
            c.set_line_width(2)
            p.rgba(col, a)
            c.stroke()
        elif kind == "mic":
            p.rrect(cx - 4.5, cy - 11, cx + 4.5, cy + 3, 4.5, PRIVACY["microphone"], a)
            c.new_sub_path()
            c.arc(cx, cy - 2, 8, 0.15 * math.pi, 0.85 * math.pi)
            c.set_line_width(2)
            p.rgba(WHITE, a)
            c.stroke()
            p.line(cx, cy + 6, cx, cy + 11, WHITE, a, 2)
        elif kind == "cam":
            p.rrect(cx - 11, cy - 7, cx + 4, cy + 7, 3, WHITE, a)
            p.poly([(cx + 5, cy - 2), (cx + 11, cy - 6), (cx + 11, cy + 6), (cx + 5, cy + 2)], WHITE, a)
            p.circle(cx - 6, cy - 2, 2, PRIVACY["webcam"], a)
        elif kind == "focus":  # reloj
            c.new_sub_path()
            c.arc(cx, cy, 10, 0, 2 * math.pi)
            c.set_line_width(2.4)
            p.rgba(WHITE, a)
            c.stroke()
            p.line(cx, cy, cx, cy - 6, col, a, 2.2)
            p.line(cx, cy, cx + 4, cy + 2, col, a, 2.2)
        elif kind == "clip":  # hoja copiada
            p.rrect(cx - 7, cy - 9, cx + 9, cy + 11, 3, shade(WHITE, 0.55), a)
            p.rrect(cx - 10, cy - 12, cx + 6, cy + 8, 3, WHITE, a)
            for i in range(3):
                p.line(cx - 6, cy - 6 + i * 5, cx + 2, cy - 6 + i * 5, col, a, 1.6)
        else:  # usb: pendrive
            p.rrect(cx - 6, cy - 4, cx + 6, cy + 12, 3, WHITE, a)
            p.rrect(cx - 4, cy - 11, cx + 4, cy - 4, 1, col, a)

    def open_reply_box(self):
        """Cuadro para escribirle a Claude debajo de la isla. Acá sí se toma el foco: pediste escribir."""
        r = self.claude.open_reply()
        if not r or self.reply_box:
            return
        u, S = ctypes.windll.user32, self.S
        before = u.GetForegroundWindow()
        top = tk.Toplevel(self.root, bg="#3a3a40")  # el fondo hace de borde de 1 px
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        W, H = round(440 * S), round(46 * S)
        top.geometry(f"{W}x{H}+{self.X + (self.W - W) // 2}+{self.Y + round((BIG[1] + 10) * S)}")
        frame = tk.Frame(top, bg="#1c1c1e")
        frame.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(frame, text="Claude ›", bg="#1c1c1e", fg=self.accent, font=("Segoe UI", 10, "bold")).pack(
            side="left", padx=(12, 4))
        tk.Label(frame, text="Enter envía · Esc cancela", bg="#1c1c1e", fg=GREY, font=("Segoe UI", 8)).pack(
            side="right", padx=10)
        entry = tk.Entry(frame, bg="#1c1c1e", fg=WHITE, insertbackground=WHITE, relief="flat", bd=0,
                         font=("Segoe UI", 11))
        entry.pack(side="left", fill="both", expand=True, pady=8)
        self.reply_box = (top, r, before)
        entry.bind("<Return>", lambda e: self.close_reply_box(entry.get()))
        entry.bind("<Escape>", lambda e: self.close_reply_box(None))
        top.update()
        u.keybd_event(0x12, 0, 0, 0)  # Alt: Windows deja cambiar el foco solo tras una tecla
        u.keybd_event(0x12, 0, 2, 0)
        u.SetForegroundWindow(ctypes.c_void_p(u.GetParent(ctypes.c_void_p(top.winfo_id()))))
        entry.focus_force()

    def close_reply_box(self, text):
        """text = lo que escribiste (se le manda a Claude) o None (cancelar)."""
        if not self.reply_box:
            return
        top, r, before = self.reply_box
        self.reply_box = None
        if text and text.strip():
            self.claude.send_reply(r, text)
        else:
            r["cancel"] = True
            r["done"].set()
        top.destroy()
        if before:  # volver a la app en la que estabas
            ctypes.windll.user32.SetForegroundWindow(ctypes.c_void_p(before))

    def draw_perm(self, w, h, now, mood, a):
        p, perms = self.p, self.claude.perms
        if not perms:
            return
        self.char(p, 26, h / 2 + 2, 26, mood, now, a)
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
        self.char(p, 46, 50, 46, mood, now, a)
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
        r = self.claude.reply
        if r and s and r["sid"] == sid and self.reply_box is None and not r.get("cancel") and now < r["until"]:
            bw, bx = 86, w - 22 - 86  # botón "Responder": el color se va vaciando con el tiempo que queda
            frac = clamp((r["until"] - now) / REPLY_GRACE)
            p.rrect(bx, 31, bx + bw, 49, 9, shade(self.accent, 0.35), a)
            p.c.save()
            p.c.rectangle(bx, 31, bw * frac, 18)
            p.c.clip()
            p.rrect(bx, 31, bx + bw, 49, 9, self.accent, a)
            p.c.restore()
            p.text(bx + bw / 2, 40, "Responder", "btn", WHITE, a, anchor="center")
            self.hit(bx - 4, 26, bx + bw + 4, 58, self.open_reply_box)
            p.text(x, 40, p.fit(status, "body", bx - x - 10), "body", WHITE, a)
        else:
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

    def draw_music(self, w, now, a, dy=0):
        p, c, m, left = self.p, self.p.c, self.media, self.timer.left()
        y0, size, x0 = 116 + dy, 52, 22
        if m.art and Image and self.art[0] is not m.art:
            try:
                self.art = (m.art, art_surface(m.art, round(size * self.S * 2)))
            except OSError:  # imagen rota
                self.art = (m.art, None)
        sx = self.drag_vis * 0.7  # arrastrando: carátula y título siguen al mouse y se apagan
        fade = a * (1 - min(1, abs(sx) / 120) * 0.7)
        c.save()  # la carátula se achica un poco en pausa y vuelve a crecer al sonar
        c.translate(sx, 0)
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
            c.paint_with_alpha(clamp(fade * (0.55 + 0.45 * (k - 0.9) / 0.1)))  # más apagada en pausa
            c.restore()
        else:
            p.rrect(x0, y0, x0 + size, y0 + size, 12, "#1f1f23", a)
            p.text(x0 + size / 2, y0 + size / 2, "♪", "symbig", "#4a4a50", a, anchor="center")
        c.restore()
        d = datetime.now()
        x = x0 + size + 14
        cx1, _ = p.text(w - 22, y0 + 12, time.strftime("%H:%M"), "clock", WHITE, a, anchor="e")
        if self.weather.temp is not None and self.cfg["weather"]:  # clima a la izquierda de la hora
            p.text(cx1 - 8, y0 + 12, f"{self.weather.icon} {self.weather.temp}°", "small", GREY, a, anchor="e")
        nxt = self.calendar.next if self.cfg["calendar"] else None
        if nxt:  # próxima cita de hoy en vez de la fecha
            when = nxt[1].strftime("%H:%M")
            p.text(w - 22, y0 + 31, f"{when} {p.fit(nxt[0], 'small', 90)}", "small", self.accent, a, anchor="e")
        else:
            p.text(w - 22, y0 + 31, f"{DAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}", "small", GREY, a,
                   anchor="e")
        p.text(x + sx, y0 + 12, p.fit(m.title or "Nada sonando", "title", w - x - 90), "title", WHITE, fade)
        p.text(x + sx, y0 + 31, p.fit(m.artist, "small", w - x - 90), "small", GREY, fade)
        if m.end >= 1:
            pos = min(m.pos + (now - m.stamp if m.playing else 0), m.end)  # avanza suave entre sondeos
            x1, x2, y = x, w - 104, y0 + 48
            p.line(x1, y, x2, y, "#3a3a3e", a, 4)
            p.line(x1, y, x1 + (x2 - x1) * clamp(pos / m.end), y, WHITE, a, 4)
            p.text(w - 22, y, f"{mmss(pos)} / {mmss(m.end)}", "tiny", GREY, a, anchor="e")
        y, cx = BIG[1] - 24 + dy, w / 2
        self.draw_play(cx, y, now, a)
        self.hit(cx - 18, y - 18, cx + 18, y + 18, lambda: self.media.send("try_toggle_play_pause_async"), "play")
        for cmd, name, d in [("try_skip_previous_async", "prev", -1), ("try_skip_next_async", "next", 1)]:
            self.draw_skip(cx + d * 50, y, d, name, now, a)
            self.hit(cx + d * 50 - 18, y - 18, cx + d * 50 + 18, y + 18, lambda cmd=cmd: self.media.send(cmd), name)
        txt, font, col = self.sys_info()
        p.text(22, y, txt, font, col, a)
        if self.focus.phase:  # el enfoque usa la misma ficha
            left = self.focus.left()
            label = ("Enfoque " if self.focus.phase == "work" else "Descanso ") + mmss(left)
        else:
            label = mmss(left) if left else f"{self.timer.minutes} min"
        tw = p.width(label, "small") + 38
        p.rrect(w - 22 - tw, y - 13, w - 22, y + 13, 13, shade(self.accent, 0.22) if left else "#1f1f23", a)
        ix, col = w - 22 - tw + 15, self.accent if left else WHITE  # cronómetro dibujado
        c.new_sub_path()
        c.arc(ix, y + 1, 5.5, 0, 2 * math.pi)
        c.set_line_width(1.5)
        p.rgba(col, a)
        c.stroke()
        p.line(ix, y + 1, ix, y - 2, col, a, 1.5)
        p.line(ix - 2, y - 7, ix + 2, y - 7, col, a, 1.5)
        p.text(ix + 11, y, label, "small", col, a)
        self.hit(w - 22 - tw, y - 14, w - 22, y + 14, self.timer.toggle, "timer")


def main():
    try:
        Island().root.mainloop()
    except OSError:  # puerto ocupado: ya hay una isla abierta
        pass


if __name__ == "__main__":
    main()
