"""Lo que la isla le pregunta a Windows: batería, CPU, pantallas, ventanas, portapapeles, micrófono…"""
import ctypes
import io
import os
from ctypes import wintypes
from pathlib import Path

from textos import _


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
    def each(hwnd, _lparam):
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
    (_parents, exe), windows = processes(), app_windows()
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
    def each(h, _hdc, _rect, _lparam):
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


def locked():
    """True con la PC bloqueada (Win+L) o en la pantalla de inicio de sesión: no hay escritorio de entrada."""
    u = ctypes.windll.user32
    u.OpenInputDesktop.restype = ctypes.c_void_p
    desk = u.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not desk:
        return True
    u.CloseDesktop(ctypes.c_void_p(desk))
    return False


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
                out[chr(65 + i)] = label.value or _("Memoria USB")
    return out


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


def set_clipboard_image(png):
    """Copia una imagen (bytes PNG) al portapapeles de Windows (formato CF_DIB)."""
    from PIL import Image
    buf = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGB").save(buf, "BMP")
    dib = buf.getvalue()[14:]  # sin la cabecera del archivo BMP
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    k.GlobalAlloc.restype = k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    h = k.GlobalAlloc(0x0002, len(dib))  # GMEM_MOVEABLE
    ctypes.memmove(k.GlobalLock(h), dib, len(dib))
    k.GlobalUnlock(h)
    if u.OpenClipboard(None):
        u.EmptyClipboard()
        u.SetClipboardData(8, h)  # CF_DIB: desde acá el portapapeles es dueño de la memoria
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


HEADPHONES = ("auricular", "headphone", "headset", "buds", "airpods", "hands-free", "manos libres")


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
