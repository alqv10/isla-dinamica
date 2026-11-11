"""Isla dinámica para Windows: la píldora arriba de la pantalla (ventana, animación, modos, tarjetas, gestos,
menú y configuración). Lo demás vive en módulos aparte:
    sistema.py    lo que se le pregunta a Windows     servicios.py  música, clima, calendario, actualizaciones…
    claude.py     la conexión con Claude Code         dibujo.py     cairo, personajes y colores
    ajustes.py    la ventana de ajustes               registro.py   el registro de errores
Conectar con Claude Code: python install_hooks.py"""
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
from pathlib import Path
import cairo
try:
    from PIL import Image
except ImportError:
    Image = None  # sin Pillow: sin carátula

# Todo lo de los otros módulos queda disponible como island.X (ajustes.py, app.py y los tests lo usan así).
from sistema import *  # noqa: F401,F403
from dibujo import *  # noqa: F401,F403
from claude import *  # noqa: F401,F403
from servicios import *  # noqa: F401,F403
import textos
from textos import _, long_date, short_date


CONFIG = Path.home() / ".isla-dinamica.json"


DEFAULTS = {"size": 1.0, "sound": True, "hide_fullscreen": True, "notifications": True, "accent": "#3ddc84",
            "translucent": False, "weather": True, "calendar": True, "updates": True, "reply": True,
            "character": "robot", "body": "#f2f2f2",
            "mode": "claude",  # "claude" | "companion"
            "monitor": "primary",  # "primary", "mouse" o el nombre de una pantalla, como \\.\DISPLAY2
            "screenshots": True, "clipboard_text": False, "privacy": True,
            "widgets": ["claude", "music"],
            # atajos: en teclados en español Ctrl+Alt = AltGr, por eso solo uno viene puesto
            "hotkeys": {"toggle": "Ctrl+Alt+I", "focus": "", "play": "", "settings": ""},
            "language": "auto"}  # "auto" (el de Windows), "es" o "en"  # bloques de la isla abierta, en orden (ver dibujo.WIDGETS)


STARTUP = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup/Isla dinamica.lnk"


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


def start_tray(toggle, quit_, settings):
    """Ícono en la bandeja de Windows: clic = mostrar/ocultar la isla."""
    try:
        import pystray
    except ImportError:
        return None
    menu = pystray.Menu(pystray.MenuItem(lambda item: _("Mostrar / ocultar"), lambda: toggle(), default=True),
                        pystray.MenuItem(lambda item: _("Ajustes…"), lambda: settings()),
                        pystray.MenuItem(lambda item: _("Salir"), lambda: quit_()))
    tray = pystray.Icon("isla-dinamica", robot_image().resize((64, 64), Image.LANCZOS), "Isla dinámica", menu)
    tray.run_detached()
    return tray


HOTKEY_ACTIONS = [("toggle", "Mostrar u ocultar la isla"), ("focus", "Empezar o terminar el enfoque"),
                  ("play", "Pausar o seguir la música"), ("settings", "Abrir los ajustes")]
MODS = {"Ctrl": 0x2, "Alt": 0x1, "Shift": 0x4, "Win": 0x8}
KEYS = {**{chr(c): c for c in range(ord("A"), ord("Z") + 1)}, **{str(d): ord(str(d)) for d in range(10)},
        **{f"F{n}": 0x6F + n for n in range(1, 13)}, "Espacio": 0x20, "Inicio": 0x24, "Fin": 0x23,
        "Arriba": 0x26, "Abajo": 0x28, "Izquierda": 0x25, "Derecha": 0x27, "Insert": 0x2D, "Pausa": 0x13}


def parse_hotkey(text):
    """"Ctrl+Alt+I" -> (modificadores, tecla virtual), o None si no es un atajo válido."""
    parts = [p.strip() for p in (text or "").split("+") if p.strip()]
    if len(parts) < 2 or parts[-1] not in KEYS or not all(p in MODS for p in parts[:-1]):
        return None
    mods = 0
    for p in parts[:-1]:
        mods |= MODS[p]
    return mods, KEYS[parts[-1]]


class Hotkeys:
    """Atajos globales de Windows (RegisterHotKey). Corren en un hilo propio con su cola de mensajes;
    al cambiarlos se cierra ese hilo y se abre otro con los nuevos."""
    def __init__(self, on_action):
        self.on_action, self.thread_id, self.failed = on_action, None, set()

    def apply(self, mapping):
        """mapping: {"toggle": "Ctrl+Alt+I", …}. Devuelve las acciones cuyo atajo ya usa otra app."""
        u = ctypes.windll.user32
        if self.thread_id:
            u.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)  # WM_QUIT al hilo anterior
        ready = threading.Event()

        def run():
            self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
            ids, failed = {}, set()
            for i, (action, text) in enumerate(mapping.items(), start=1):
                parsed = parse_hotkey(text)
                if parsed and u.RegisterHotKey(None, i, parsed[0] | 0x4000, parsed[1]):  # MOD_NOREPEAT
                    ids[i] = action
                elif parsed:
                    failed.add(action)
            self.failed = failed
            ready.set()
            msg = wintypes.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312 and msg.wParam in ids:  # WM_HOTKEY
                    self.on_action(ids[msg.wParam])
            for i in ids:
                u.UnregisterHotKey(None, i)
        threading.Thread(target=run, daemon=True, name="atajos").start()
        ready.wait(2)
        return self.failed


class Island:
    def __init__(self):
        self.cfg = load_config()
        dpi_scale()  # pide píxeles reales a Windows (antes de medir pantallas)
        self.mon = pick_monitor(monitors(), self.cfg["monitor"], self.pointer())
        self.base_S = self.mon["scale"] if self.mon else 1.0
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = ctypes.c_void_p
        before = u.GetForegroundWindow()  # al crearse, la ventana de Tk se lleva el foco: se lo devolvemos
        textos.set_language(self.cfg["language"])
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
        self.shown, self.quitting = True, False  # mostrar/ocultar a mano (bandeja o atajo)
        self.cleared = False  # ya se borró la ventana estando oculta
        self.hit_dy = 0  # corrimiento de las zonas clicables del bloque que se está dibujando
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
        self.actions = []  # acciones pedidas desde otros hilos (atajos): se hacen en el próximo tick
        self.hotkeys = Hotkeys(self.actions.append)
        self.hotkeys.apply(self.cfg["hotkeys"])
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
        if key == "language":  # menús, bandeja y ajustes en el idioma nuevo
            textos.set_language(value)
            self.build_menu()
            if self.tray:
                self.tray.update_menu()
            if self.settings:
                self.root.after(0, self.reopen_settings)
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
        m.add_command(label=_("Ajustes…"), command=self.open_settings)
        m.add_command(label=_("Empezar enfoque (25 + 5 min)"), command=self.toggle_focus)
        self.focus_item = m.index("end")
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
            look.add_radiobutton(label=_(label), value=k, variable=self.size_var, command=lambda k=k: self.set("size", k))
        look.add_separator()
        for label, col in ACCENTS:
            look.add_radiobutton(label=_("Color {name}", name=_(label).lower()), value=col, variable=self.accent_var,
                                 command=lambda col=col: self.set("accent", col))
        look.add_separator()
        check(look, "translucent", _("Translúcida"))
        m.add_cascade(label=_("Apariencia"), menu=look)
        self.char_var, self.body_var = tk.StringVar(value=self.cfg["character"]), tk.StringVar(value=self.cfg["body"])
        self.mode_var = tk.StringVar(value=self.cfg["mode"])
        who = tk.Menu(m, tearoff=0, font=font)
        for label, k in CHARACTERS:
            who.add_radiobutton(label=_(label), value=k, variable=self.char_var, command=lambda k=k: self.set("character", k))
        who.add_separator()
        for label, col in BODY_COLORS:
            who.add_radiobutton(label=_("Color {name}", name=_(label).lower()), value=col, variable=self.body_var,
                                command=lambda col=col: self.set("body", col))
        m.add_cascade(label=_("Personaje"), menu=who)
        modes = tk.Menu(m, tearoff=0, font=font)
        for label, k in [("Claude Code", "claude"), ("Solo compañero", "companion")]:
            modes.add_radiobutton(label=_(label), value=k, variable=self.mode_var, command=lambda k=k: self.set("mode", k))
        m.add_cascade(label=_("Modo"), menu=modes)
        show = tk.Menu(m, tearoff=0, font=font)
        for key, label in [("notifications", "Notificaciones"), ("weather", "Clima"), ("calendar", "Próxima cita"),
                           ("reply", "Responder a Claude desde la isla"), ("screenshots", "Vista previa de capturas"),
                           ("clipboard_text", "Texto copiado"), ("privacy", "Micrófono y cámara en uso"),
                           ("sound", "Sonidos")]:
            check(show, key, _(label))
        m.add_cascade(label=_("Mostrar"), menu=show)
        check(m, "hide_fullscreen", _("Ocultar en pantalla completa"))
        m.add_checkbutton(label=_("Iniciar con Windows"), variable=self.vars["startup"],
                          command=lambda: set_startup(self.vars["startup"].get()))
        check(m, "updates", _("Buscar actualizaciones"))
        m.add_separator()
        m.add_command(label=_("Ver registro de errores"), command=self.open_log)
        m.add_command(label=_("Ocultar"), accelerator=self.cfg["hotkeys"].get("toggle", ""), command=self.toggle)
        self.hide_item = m.index("end")
        m.add_command(label=_("Salir"), command=self.quit)
        self.update_item = None  # "Actualizar a la versión…" (se agrega arriba cuando hay una nueva)
        m.configure(postcommand=self.menu_update_item)

    def menu_update_item(self):
        """Antes de abrir el menú: el texto del enfoque y, si hay versión nueva, "Actualizar" arriba de todo."""
        shift = 1 if self.update_item else 0  # el ítem de actualizar, si está, corre los demás uno abajo
        self.menu.entryconfig(self.hide_item + shift, accelerator=self.cfg["hotkeys"].get("toggle", ""))
        self.menu.entryconfig(self.focus_item + shift, label=_("Terminar enfoque") if self.focus.phase
                              else _("Empezar enfoque (25 + 5 min)"))
        label = _("Actualizar a la versión {version}", version=self.updater.latest[0]) if self.updater.latest else None
        if label and label != self.update_item:
            if self.update_item:
                self.menu.delete(0)
            self.menu.insert_command(0, label=label, command=self.updater.install)
            self.update_item = label

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
            registro.write(_("sin errores hasta ahora"))
        os.startfile(registro.LOG)

    def run_action(self, action):
        """Lo que hace cada atajo de teclado."""
        if action == "toggle":
            self.toggle()
        elif action == "focus":
            self.toggle_focus()
        elif action == "play":
            self.media.send("try_toggle_play_pause_async")
            self.pressed["play"] = time.time()
        elif action == "settings":
            self.open_settings()

    def set_hotkey(self, action, text):
        """Cambia un atajo; devuelve True si quedó registrado (False si otra app ya lo usa)."""
        self.set("hotkeys", {**self.cfg["hotkeys"], action: text})
        return action not in self.hotkeys.apply(self.cfg["hotkeys"])

    def reopen_settings(self):
        if self.settings:
            self.settings.close()
        self.open_settings()

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

    def blocks(self):
        """[(bloque, y de arriba, alto)] de la isla abierta, según los widgets elegidos."""
        names = [b for b in self.cfg["widgets"] if b in WIDGETS and not (b == "claude" and self.companion)]
        out, y = [], 0 if names[:1] == ["claude"] else 6
        for b in names or ["music"]:
            out.append((b, y, WIDGETS[b][1]))
            y += WIDGETS[b][1]
        return out

    def big_size(self):
        b = self.blocks()
        return BIG[0], b[-1][1] + b[-1][2] + 4

    def in_music(self, y):
        """¿Está y sobre el bloque de música de la isla abierta?"""
        return self.mode == "big" and any(b == "music" and top <= y < top + h for b, top, h in self.blocks())

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
        for x1, y1, x2, y2, _act, name in reversed(self.hits):
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
        dy = self.hit_dy  # los bloques se dibujan corridos hacia abajo: sus zonas también
        self.hits.append((x1, y1 + dy, x2, y2 + dy, action, name))

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
        card = {"kind": "shot", "icon": "clip", "title": _("Captura copiada"),
                "body": _("{w} × {h} · pégala con Ctrl+V", w=im.width, h=im.height), "until": time.time() + 7,
                "thumb": art_surface(data, round(40 * self.S * 2))}

        def save():
            folder = pictures_dir() / "Capturas"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"captura-{datetime.now():%Y%m%d-%H%M%S}.png"
            path.write_bytes(data)
            card.update(title=_("Captura guardada"), body=_("Imágenes › Capturas › {name}", name=path.name),
                        until=time.time() + 5, buttons=[(_("Mostrar"), lambda: subprocess.Popen(["explorer", "/select,", str(path)]), True)])

        def open_():
            import tempfile
            path = Path(tempfile.gettempdir()) / "isla-captura.png"
            path.write_bytes(data)
            os.startfile(path)
            card["until"] = 0
        card["buttons"] = [(_("Abrir"), open_, False), (_("Guardar"), save, True)]
        self.card = card

    def check_privacy(self):
        """Una app empezó a usar el micrófono o la cámara: tarjeta (con Silenciar) y un punto junto a la isla."""
        cur = in_use() if self.cfg["privacy"] else {}
        for cap, apps in cur.items():
            for app in sorted(apps - self.inuse.get(cap, set())):
                mic = cap == "microphone"
                card = {"kind": "privacy", "icon": "mic" if mic else "cam", "until": time.time() + 6,
                        "title": _("{app} está usando el micrófono" if mic else "{app} está usando la cámara", app=app),
                        "body": _("Verás un punto junto a la isla mientras siga")}
                if mic:
                    def toggle(card=card):
                        muted = mic_mute(toggle=True)
                        card.update(body=_("Micrófono silenciado") if muted else _("Micrófono activado"),
                                    until=time.time() + 4,
                                    buttons=[(_("Activar") if muted else _("Silenciar"), toggle, not muted)])
                    muted = mic_mute()
                    card["buttons"] = [(_("Activar") if muted else _("Silenciar"), toggle, not muted)]
                self.card = card
        self.inuse = cur

    def focus_card(self, phase):
        minutes = Focus.MINUTES[phase]
        if phase == "work":
            title, body = _("A concentrarse"), _("{minutes} minutos · ronda {round}", minutes=minutes, round=self.focus.round)
        else:
            title = _("¡Descanso largo!") if phase == "long" else _("¡Descanso!")
            body = _("{minutes} minutos · terminaste la ronda {round}", minutes=minutes, round=self.focus.round)
        self.card = {"kind": "focus", "icon": "focus", "title": title, "body": body, "until": time.time() + 8,
                     "buttons": [(_("Terminar"), self.stop_focus, False)]}
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
                                 _("Cargando") if self.bat[1] else _("Cargador desconectado"),
                                 _("Batería al {pct}%", pct=self.bat[0]))
            self.ac = self.bat[1]
        out = self.levels.device_event
        if out:
            self.levels.device_event = None
            phones = any(word in out.lower() for word in HEADPHONES)
            self.device_card("headphones" if phones else "speaker", out, _("Ahora el sonido sale por acá"))
        drives = removable_drives()
        if self.drives is not None:
            import shutil
            for letter in sorted(drives.keys() - self.drives.keys()):
                root = letter + ":\\"
                try:
                    free = _(" · {gb} GB libres", gb=f"{shutil.disk_usage(root).free / 1e9:.1f}")
                except OSError:
                    free = ""
                self.device_card("usb", _("{name} conectado", name=drives[letter]),
                                 _("Unidad {letter}:{free} · clic para abrir", letter=letter, free=free),
                                 lambda root=root: os.startfile(root))
            for letter in sorted(self.drives.keys() - drives.keys()):
                self.device_card("usb", _("{name} desconectado", name=self.drives[letter]),
                                 _("Unidad {letter}: · ya puedes guardarla", letter=letter))
        self.drives = drives

    def next_session(self):
        items = self.claude.ordered()
        if items:
            sid, _s, i, n = self.session()
            self.pinned = items[i % n][0]

    def tick(self):
        root, now, S = self.root, time.time(), self.S
        if self.quitting:
            return self.quit()
        if self.settings_requested:
            self.settings_requested = False
            self.open_settings()
        while self.actions:
            self.run_action(self.actions.pop(0))
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
        big = self.big_size()
        target = {"perm": PERM, "level": LEVEL, "note": NOTIF, "card": NOTIF, "big": big, "small": SMALL}[mode]
        # resorte amortiguado: se pasa un poquito y vuelve, como en el iPhone
        self.vw = self.vw * 0.7 + (target[0] - self.w) * 0.1
        self.vh = self.vh * 0.7 + (target[1] - self.h) * 0.1
        self.w = clamp(self.w + self.vw, SMALL[0] * 0.8, WIN[0] - 2 * SHOULDER - 2)
        self.h = clamp(self.h + self.vh, SMALL[1] * 0.8, WIN[1] - 2)
        under = next((n for x1, y1, x2, y2, _act, n in reversed(self.hits)
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
        self.hits, self.hit_dy = [], 0
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
            a = clamp((h - 40) / (target[1] - 40)) ** 2
            if now < self.alert_until:
                p.text(w / 2, h / 2, _("⏰  ¡Tiempo!"), "symbig", self.accent, a, anchor="center")
            else:
                self.draw_blocks(w, now, mood, a)
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
        _sid, s, _i, _n = self.session()
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
            txt, col = _("en {mins} min", mins=mins) if mins else _("ahora"), self.accent
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
            title = _("Nueva versión {version}", version=ver)
            body = {"descargando": _("Descargando… se reinicia sola al terminar"),
                    "error": _("No se pudo descargar; prueba más tarde")}.get(self.updater.state,
                                                                              _("Isla dinámica se puede actualizar"))
            if self.updater.state == "":
                buttons = [(_("Luego"), lambda: card.update(until=0), False),
                           (_("Actualizar"), self.updater.install, True)]
        elif kind == "cal":
            mins = max(0, round((card["start"] - datetime.now(timezone.utc)).total_seconds() / 60))
            title = card["title"]
            when = card["start"].strftime("%H:%M")
            body = _("Empieza ahora · {time}", time=when) if not mins else _("Empieza en {mins} min · {time}",
                                                                           mins=mins, time=when)
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
        top.geometry(f"{W}x{H}+{self.X + (self.W - W) // 2}+{self.Y + round((self.big_size()[1] + 10) * S)}")
        frame = tk.Frame(top, bg="#1c1c1e")
        frame.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(frame, text=_("Claude ›"), bg="#1c1c1e", fg=self.accent, font=("Segoe UI", 10, "bold")).pack(
            side="left", padx=(12, 4))
        tk.Label(frame, text=_("Enter envía · Esc cancela"), bg="#1c1c1e", fg=GREY, font=("Segoe UI", 8)).pack(
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
        p.text(60, h / 2, p.fit(_("¿Permitir? {text}", text=perms[0]["text"]), "title", right - 72), "title", WHITE, a)
        for answer, label, x, bg, fg in [(True, _("Permitir"), right, "#b7f07a", "#000000"),
                                         (False, _("Denegar"), right + 74, "#3a2626", "#ff9a9a")]:
            p.rrect(x, h / 2 - 14, x + 66, h / 2 + 14, 14, bg, a)
            p.text(x + 33, h / 2, label, "btn", fg, a, anchor="center")
            self.hit(x, 0, x + 70, h, lambda answer=answer: self.claude.answer(answer))
        if more:
            p.text(w - 20, h / 2, f"+{more}", "tiny", GREY, a, anchor="e")

    def draw_blocks(self, w, now, mood, a):
        """La isla abierta: los bloques elegidos, uno debajo del otro con una línea entre medio."""
        c = self.p.c
        names = [b for b, _top, _h in self.blocks()]
        for i, (b, top, h) in enumerate(self.blocks()):
            if i:
                self.p.line(18, top, w - 18, top, "#2a2a2e", a, 1)
            c.save()
            if b == "music":  # el bloque de música se ubica solo (usa coordenadas de la isla)
                self.draw_music(w, now, a, dy=top - 104, show_sys="system" not in names)
            else:
                c.translate(0, top)
                self.hit_dy = top
                {"claude": lambda: self.draw_claude(w, now, mood, a), "today": lambda: self.draw_today(w, a),
                 "system": lambda: self.draw_system(w, a)}[b]()
                self.hit_dy = 0
            c.restore()

    def draw_today(self, w, a):
        """Hoy: clima grande, fecha completa y próxima cita."""
        p, d = self.p, datetime.now()
        x = 22
        if self.weather.temp is not None and self.cfg["weather"]:
            p.text(x, 30, self.weather.icon, "symbig", WHITE, a)
            p.text(x + 34, 30, f"{self.weather.temp}°", "big", WHITE, a)
            x += 104
        p.text(x, 21, long_date(d), "title", WHITE, a)
        nxt = self.calendar.next if self.cfg["calendar"] else None
        if nxt:
            mins = self.minutes_to_event()
            when = _("en {mins} min", mins=mins) if mins is not None and mins < 60 else nxt[1].strftime("%H:%M")
            p.text(x, 40, p.fit(f"{when} · {nxt[0]}", "small", w - x - 22), "small", self.accent, a)
        else:
            p.text(x, 40, _("Sin citas por hoy"), "small", GREY, a)

    def draw_system(self, w, a):
        """Sistema: barras de CPU, RAM y batería (si hay)."""
        p = self.p
        bars = [("CPU", self.cpu_pct), ("RAM", self.ram_pct)] + ([(_("Batería"), self.bat[0])] if self.bat else [])
        col_w = (w - 44) / len(bars)
        for i, (label, pct) in enumerate(bars):
            x = 22 + i * col_w
            hot = pct <= 20 if label == _("Batería") else pct >= 90
            p.text(x, 18, label, "small", GREY, a)
            p.text(x + col_w - 16, 18, f"{pct}%", "small", RED if hot else WHITE, a, anchor="e")
            p.line(x, 36, x + col_w - 16, 36, "#2c2c31", a, 6)
            if pct:
                p.line(x, 36, x + (col_w - 16) * pct / 100, 36, RED if hot else self.accent, a, 6)

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
        status = s["status"] if s else _("Sin sesiones activas")
        if s and s["since"]:
            status += f"  ·  {mmss(now - s['since'])}"
        elif s and s["took"]:
            status = _("{status} en {time}", status=status, time=mmss(s["took"]))
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
            p.text(bx + bw / 2, 40, _("Responder"), "btn", WHITE, a, anchor="center")
            self.hit(bx - 4, 26, bx + bw + 4, 58, self.open_reply_box)
            p.text(x, 40, p.fit(status, "body", bx - x - 10), "body", WHITE, a)
        else:
            p.text(x, 40, p.fit(status, "body", w - x - 22), "body", WHITE, a)
        detail = s["detail"][:3] if s else [(_("abre claude en una terminal"), GREY)]
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

    def draw_music(self, w, now, a, dy=0, show_sys=True):
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
        cx1, _end = p.text(w - 22, y0 + 12, time.strftime("%H:%M"), "clock", WHITE, a, anchor="e")
        if self.weather.temp is not None and self.cfg["weather"]:  # clima a la izquierda de la hora
            p.text(cx1 - 8, y0 + 12, f"{self.weather.icon} {self.weather.temp}°", "small", GREY, a, anchor="e")
        nxt = self.calendar.next if self.cfg["calendar"] else None
        if nxt:  # próxima cita de hoy en vez de la fecha
            when = nxt[1].strftime("%H:%M")
            p.text(w - 22, y0 + 31, f"{when} {p.fit(nxt[0], 'small', 90)}", "small", self.accent, a, anchor="e")
        else:
            p.text(w - 22, y0 + 31, short_date(d), "small", GREY, a,
                   anchor="e")
        p.text(x + sx, y0 + 12, p.fit(m.title or _("Nada sonando"), "title", w - x - 90), "title", WHITE, fade)
        p.text(x + sx, y0 + 31, p.fit(m.artist, "small", w - x - 90), "small", GREY, fade)
        if m.end >= 1:
            pos = min(m.pos + (now - m.stamp if m.playing else 0), m.end)  # avanza suave entre sondeos
            x1, x2, y = x, w - 104, y0 + 48
            p.line(x1, y, x2, y, "#3a3a3e", a, 4)
            p.line(x1, y, x1 + (x2 - x1) * clamp(pos / m.end), y, WHITE, a, 4)
            p.text(w - 22, y, f"{mmss(pos)} / {mmss(m.end)}", "tiny", GREY, a, anchor="e")
        y, cx = 190 + dy, w / 2
        self.draw_play(cx, y, now, a)
        self.hit(cx - 18, y - 18, cx + 18, y + 18, lambda: self.media.send("try_toggle_play_pause_async"), "play")
        for cmd, name, d in [("try_skip_previous_async", "prev", -1), ("try_skip_next_async", "next", 1)]:
            self.draw_skip(cx + d * 50, y, d, name, now, a)
            self.hit(cx + d * 50 - 18, y - 18, cx + d * 50 + 18, y + 18, lambda cmd=cmd: self.media.send(cmd), name)
        if show_sys:
            txt, font, col = self.sys_info()
            p.text(22, y, txt, font, col, a)
        if self.focus.phase:  # el enfoque usa la misma ficha
            left = self.focus.left()
            label = _("Enfoque {left}" if self.focus.phase == "work" else "Descanso {left}", left=mmss(left))
        else:
            label = mmss(left) if left else _("{n} min", n=self.timer.minutes)
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
