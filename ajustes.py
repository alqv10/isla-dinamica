"""Ventana de ajustes de la isla: personaje con vista previa, colores, modo, pantalla e interruptores.
Cada cambio se aplica en vivo (Island.set) y se guarda en ~/.isla-dinamica.json."""
import time
import tkinter as tk

import cairo
from PIL import Image, ImageTk

import island

BG, CARD, LINE, FG, DIM = "#111114", "#1c1c1f", "#2c2c31", "#ffffff", "#9a9a9f"


def preview(kind, body, mood, px, bg=CARD):
    """Personaje dibujado por la isla, como imagen para tkinter (sobre fondo opaco: sin bordes raros)."""
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, px, px)
    c = cairo.Context(surf)
    c.set_source_rgb(*(int(bg[i:i + 2], 16) / 255 for i in (1, 3, 5)))
    c.paint()
    island.character(island.Paint(c), kind, px / 2, px * 0.55, px * 0.62, mood, 1.0, 1, body)
    return ImageTk.PhotoImage(Image.frombuffer("RGBA", (px, px), bytes(surf.get_data()), "raw", "BGRA", 0, 1))


class Settings:
    def __init__(self, isl):
        self.isl, self.cfg = isl, isl.cfg
        self.k = k = isl.root.winfo_fpixels("1i") / 96  # escala de la pantalla para los px de los dibujos
        t = self.top = tk.Toplevel(isl.root, bg=BG)
        t.title("Ajustes · Isla dinámica")
        t.resizable(False, False)
        t.protocol("WM_DELETE_WINDOW", self.close)
        self.images, self.refreshers = [], []
        pad = {"padx": round(22 * k)}
        self.icon = ImageTk.PhotoImage(island.robot_image().resize((64, 64), Image.LANCZOS))
        t.iconphoto(False, self.icon)
        t.update_idletasks()
        try:  # barra de título oscura (DWMWA_USE_IMMERSIVE_DARK_MODE), como el resto de la ventana
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(t.winfo_id())
            ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 20, ctypes.byref(ctypes.c_int(1)), 4)
        except (AttributeError, OSError):
            pass

        tk.Label(t, text="Ajustes", font=("Segoe UI", 18, "bold"), bg=BG, fg=FG).pack(anchor="w", pady=(round(16 * k), 0), **pad)
        tk.Label(t, text="Los cambios se ven al instante en la isla.", font=("Segoe UI", 9), bg=BG, fg=DIM).pack(anchor="w", **pad)

        self.section("Personaje")
        row = self.row()
        self.tiles = {}
        for label, kind in island.CHARACTERS:
            box = tk.Frame(row, bg=BG)
            box.pack(side="left", padx=(0, round(10 * k)))
            tile = tk.Label(box, bg=CARD, bd=0, highlightthickness=round(2 * k), cursor="hand2")
            tile.pack()
            tk.Label(box, text=label, font=("Segoe UI", 9), bg=BG, fg=DIM).pack(pady=(round(4 * k), 0))
            tile.bind("<Button-1>", lambda e, kind=kind: self.change("character", kind))
            self.tiles[kind] = tile
        self.refreshers.append(self.refresh_tiles)
        self.swatches("Color del personaje", "body", island.BODY_COLORS)

        self.section("Isla")
        self.swatches("Color de acento", "accent", island.ACCENTS)
        self.segmented("Modo", "mode", [("Claude Code", "claude"), ("Solo compañero", "companion")])
        self.segmented("Tamaño", "size", [("Normal", 1.0), ("Grande", 1.2), ("Muy grande", 1.4)])
        mons = island.monitors()
        screens = [("Principal", "primary"), ("La del mouse", "mouse")]
        if len(mons) > 1:
            screens += [(f"Pantalla {i + 1}", m["name"]) for i, m in enumerate(mons)]
        self.segmented("Pantalla", "monitor", screens)

        self.section("Mostrar")
        for key, label in [("notifications", "Notificaciones de Windows"), ("weather", "Clima"),
                           ("calendar", "Próxima cita del calendario"), ("reply", "Responder a Claude desde la isla"),
                           ("screenshots", "Vista previa de capturas de pantalla"),
                           ("clipboard_text", "Mostrar el texto que copias"),
                           ("privacy", "Avisar cuando se usa el micrófono o la cámara"),
                           ("sound", "Sonidos"), ("translucent", "Isla translúcida"),
                           ("hide_fullscreen", "Ocultar con juegos y videos en pantalla completa"),
                           ("updates", "Buscar actualizaciones")]:
            self.switch(label, lambda key=key: self.cfg[key], lambda v, key=key: self.change(key, v))
        self.switch("Iniciar con Windows", lambda: island.STARTUP.exists(), self.set_startup)

        foot = tk.Frame(t, bg=BG)
        foot.pack(fill="x", pady=(round(14 * k), round(16 * k)), **pad)
        tk.Label(foot, text=f"Versión {island.VERSION} · código abierto", font=("Segoe UI", 8), bg=BG, fg=DIM).pack(side="left")
        done = tk.Label(foot, text="Listo", font=("Segoe UI", 10, "bold"), bg=isl.accent, fg="#000000",
                        padx=round(18 * k), pady=round(5 * k), cursor="hand2")
        done.pack(side="right")
        done.bind("<Button-1>", lambda e: self.close())
        self.done = done
        self.refresh()
        t.update_idletasks()  # centrada en la pantalla de la isla, un poco abajo
        m = isl.mon or {"x": 0, "y": 0, "w": t.winfo_screenwidth()}
        t.geometry(f"+{m['x'] + (m['w'] - t.winfo_width()) // 2}+{m['y'] + round(260 * k)}")
        t.lift()
        t.focus_force()

    # --- piezas ---

    def section(self, title):
        tk.Label(self.top, text=title.upper(), font=("Segoe UI", 8, "bold"), bg=BG, fg=DIM).pack(
            anchor="w", padx=round(22 * self.k), pady=(round(16 * self.k), round(6 * self.k)))

    def row(self, label=None):
        r = tk.Frame(self.top, bg=BG)
        r.pack(fill="x", padx=round(22 * self.k), pady=round(3 * self.k))
        if label:
            tk.Label(r, text=label, font=("Segoe UI", 10), bg=BG, fg=FG, width=18, anchor="w").pack(side="left")
        return r

    def swatches(self, label, key, colors):
        r, k = self.row(label), self.k
        size = round(26 * k)
        cans = []
        for name, col in colors:
            cv = tk.Canvas(r, width=size, height=size, bg=BG, highlightthickness=0, cursor="hand2")
            cv.pack(side="left", padx=(0, round(6 * k)))
            cv.bind("<Button-1>", lambda e, col=col: self.change(key, col))
            cans.append((cv, col))

        def refresh():
            for cv, col in cans:
                cv.delete("all")
                on = self.cfg[key] == col
                if on:  # anillo alrededor del elegido
                    cv.create_oval(1, 1, size - 2, size - 2, outline=FG, width=max(1, round(2 * k)))
                m = round(5 * k) if on else round(3 * k)
                cv.create_oval(m, m, size - m - 1, size - m - 1, fill=col, outline="")
        self.refreshers.append(refresh)

    def segmented(self, label, key, options):
        r, k = self.row(label), self.k
        bar = tk.Frame(r, bg=CARD)
        bar.pack(side="left")
        buttons = []
        for text, value in options:
            b = tk.Label(bar, text=text, font=("Segoe UI", 9), padx=round(12 * k), pady=round(5 * k), cursor="hand2")
            b.pack(side="left", padx=round(2 * k), pady=round(2 * k))
            b.bind("<Button-1>", lambda e, value=value: self.change(key, value))
            buttons.append((b, value))

        def refresh():
            for b, value in buttons:
                on = self.cfg[key] == value
                b.config(bg=self.isl.accent if on else CARD, fg="#000000" if on else FG)
        self.refreshers.append(refresh)

    def switch(self, label, get, set_):
        r, k = self.row(), self.k
        tk.Label(r, text=label, font=("Segoe UI", 10), bg=BG, fg=FG).pack(side="left")
        w, h = round(38 * k), round(22 * k)
        cv = tk.Canvas(r, width=w, height=h, bg=BG, highlightthickness=0, cursor="hand2")
        cv.pack(side="right")
        cv.bind("<Button-1>", lambda e: (set_(not get()), self.refresh()))

        def refresh():  # interruptor tipo iPhone: pastilla + círculo
            on = get()
            cv.delete("all")
            col = self.isl.accent if on else LINE
            cv.create_oval(0, 0, h - 1, h - 1, fill=col, outline="")
            cv.create_oval(w - h, 0, w - 1, h - 1, fill=col, outline="")
            cv.create_rectangle(h / 2, 0, w - h / 2, h - 1, fill=col, outline="")
            m, x = round(3 * k), (w - h if on else 0)
            cv.create_oval(x + m, m, x + h - m - 1, h - m - 1, fill=FG, outline="")
        self.refreshers.append(refresh)

    def refresh_tiles(self):
        px = round(76 * self.k)
        self.images = []
        for kind, tile in self.tiles.items():
            img = preview(kind, self.cfg["body"], "happy", px)
            self.images.append(img)  # tkinter olvida las imágenes sin una referencia
            on = self.cfg["character"] == kind
            tile.config(image=img, highlightbackground=self.isl.accent if on else BG,
                        highlightcolor=self.isl.accent if on else BG)

    # --- cambios ---

    def change(self, key, value):
        self.isl.set(key, value)
        self.isl.keep_open_until = time.time() + 4  # mostrar el cambio en la isla
        self.refresh()

    def set_startup(self, on):
        island.set_startup(on)

    def refresh(self):
        for f in self.refreshers:
            f()
        if hasattr(self, "done"):  # el botón Listo usa el color de acento
            self.done.config(bg=self.isl.accent)

    def close(self):
        self.isl.settings = None
        self.top.destroy()
