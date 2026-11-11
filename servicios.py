"""Lo que corre de fondo: música, volumen y brillo, notificaciones, clima, calendario, actualizaciones,
temporizador y enfoque."""
import asyncio
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
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
from sistema import clamp
from claude import first_line


VERSION = "1.7.0"


REPO = "alqv10/isla-dinamica"


PLAYING = 4  # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING


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
