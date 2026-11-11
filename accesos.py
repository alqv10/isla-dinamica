"""Atajos de apps: botones en la isla abierta para programas, carpetas o sitios web, con el ícono real de
Windows. Se abren con un clic (si el programa ya está abierto, se trae al frente)."""
import ctypes
import io
import os
from ctypes import wintypes
from pathlib import Path


class SHFILEINFOW(ctypes.Structure):
    _fields_ = [("hIcon", ctypes.c_void_p), ("iIcon", ctypes.c_int), ("dwAttributes", wintypes.DWORD),
                ("szDisplayName", ctypes.c_wchar * 260), ("szTypeName", ctypes.c_wchar * 80)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("width", ctypes.c_long), ("height", ctypes.c_long),
                ("planes", ctypes.c_uint16), ("bits", ctypes.c_uint16), ("compression", ctypes.c_uint32),
                ("image_size", ctypes.c_uint32), ("xppm", ctypes.c_long), ("yppm", ctypes.c_long),
                ("used", ctypes.c_uint32), ("important", ctypes.c_uint32)]


def is_web(target):
    return target.lower().startswith(("http://", "https://"))


def icon_png(target, size=48):
    """Ícono de Windows de un programa, acceso directo o carpeta, como PNG (bytes); None para webs o si falla."""
    if is_web(target) or not os.path.exists(target):
        return None
    from PIL import Image
    shell, user, gdi = ctypes.windll.shell32, ctypes.windll.user32, ctypes.windll.gdi32
    shell.SHGetFileInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.UINT, wintypes.UINT]
    info = SHFILEINFOW()
    if not shell.SHGetFileInfoW(target, 0, ctypes.byref(info), ctypes.sizeof(info), 0x100):  # SHGFI_ICON (grande)
        return None
    vp = ctypes.c_void_p
    user.DrawIconEx.argtypes = [vp, ctypes.c_int, ctypes.c_int, vp, ctypes.c_int, ctypes.c_int, wintypes.UINT, vp,
                                wintypes.UINT]
    user.DestroyIcon.argtypes = [vp]
    gdi.CreateCompatibleDC.restype = gdi.CreateDIBSection.restype = vp
    gdi.CreateCompatibleDC.argtypes, gdi.SelectObject.argtypes = [vp], [vp, vp]
    gdi.CreateDIBSection.argtypes = [vp, vp, wintypes.UINT, ctypes.POINTER(vp), vp, wintypes.DWORD]
    gdi.DeleteObject.argtypes = gdi.DeleteDC.argtypes = [vp]
    dc = gdi.CreateCompatibleDC(None)
    bits = vp()
    bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), size, -size, 1, 32)
    bmp = gdi.CreateDIBSection(dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    old = gdi.SelectObject(dc, bmp)
    user.DrawIconEx(dc, 0, 0, info.hIcon, size, size, 0, None, 3)  # DI_NORMAL: con transparencia
    raw = ctypes.string_at(bits, size * size * 4)
    gdi.SelectObject(dc, old)
    gdi.DeleteObject(bmp)
    gdi.DeleteDC(dc)
    user.DestroyIcon(info.hIcon)
    im = Image.frombuffer("RGBA", (size, size), raw, "raw", "BGRA", 0, 1)
    if not im.getchannel("A").getbbox():  # íconos viejos sin canal alfa: usar el color tal cual
        im = Image.frombuffer("RGB", (size, size), raw, "raw", "BGRX", 0, 1).convert("RGBA")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def display_name(target):
    if is_web(target):
        host = target.split("//", 1)[1].split("/", 1)[0]
        return host.removeprefix("www.").split(".")[0].capitalize()
    p = Path(target)
    return (p.stem or p.name or str(p)).replace("-", " ").replace("_", " ")[:18]


def launch(target):
    """Abre el atajo. Un programa que ya está abierto se trae al frente en vez de abrir otro."""
    if target.lower().endswith(".exe"):
        import sistema
        name = Path(target).name.lower()
        (_parents, exe), windows = sistema.processes(), sistema.app_windows()
        hwnd = next((h for pid, h in windows.items() if exe.get(pid) == name), None)
        if hwnd:
            sistema.bring_to_front(hwnd)
            return
    os.startfile(target)
