"""Dibujo con cairo sobre una ventana con transparencia por píxel: tamaños, colores, fuentes, personajes."""
import ctypes
import io
import math
import cairo
try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None  # sin Pillow: sin carátula
from sistema import clamp


SMALL, BIG, PERM = (230, 34), (480, 214), (520, 44)  # en px lógicos (a 100 % de escala)


LEVEL, NOTIF = (300, 40), (440, 64)  # barra de volumen/brillo y notificación




ACCENTS = [("Verde", "#3ddc84"), ("Morado", "#a78bfa"), ("Azul", "#4cc2ff"), ("Naranja", "#ff9f43"),
           ("Rosa", "#ff6b9d")]


GREEN, GREY, FIELD, RED, WHITE = "#3ddc84", "#9a9a9f", "#1c1c1e", "#ff5555", "#ffffff"


DIFF_BG = {RED: "#3a1c1f", GREEN: "#14301f"}  # fondo de las líneas del diff


MOODS = {"happy": GREEN, "focused": "#f5a524", "curious": "#5ab4ff", "love": "#ff5c8a", "sleep": GREY}






PRIVACY = {"microphone": "#ff9f0a", "webcam": "#30d158"}  # naranja = micrófono, verde = cámara (como el iPhone)


def shade(col, k):
    """col oscurecido (k = 0 negro, 1 igual): fondos tenues del color de acento."""
    return "#%02x%02x%02x" % tuple(round(int(col[i:i + 2], 16) * k) for i in (1, 3, 5))


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
         "btn": ("Segoe UI", 9, B), "sym": ("Segoe UI Symbol", 9), "symbig": ("Segoe UI Symbol", 18), "big": ("Segoe UI", 18, B)}


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
        """Ancho del texto (se recuerda: medir textos era un tercio del costo de cada cuadro)."""
        cache = self.__dict__.setdefault("_widths", {})
        if (s, font) not in cache:
            if len(cache) > 2000:
                cache.clear()
            total = 0
            for chunk, sym in self.runs(s):
                self.font_run(font, sym)
                total += self.c.text_extents(chunk).x_advance
            cache[s, font] = total
        return cache[s, font]

    def fit(self, s, font, maxw):
        """Recorta el texto con … para que entre en maxw."""
        if self.width(s, font) <= maxw:
            return s
        while s and self.width(s + "…", font) > maxw:
            s = s[:-1]
        return s + "…"

    def text(self, x, y, s, font, col, a, anchor="w"):
        c = self.c
        extents = self.__dict__.setdefault("_extents", {})
        if font not in extents:  # alineación vertical según la fuente principal (no cambia: se recuerda)
            self.font(font)
            extents[font] = c.font_extents()[:2]
        ascent, descent = extents[font]
        wd = self.width(s, font)
        x -= {"w": 0, "center": wd / 2, "e": wd}[anchor]
        c.move_to(x, y + (ascent - descent) / 2)
        self.rgba(col, a)
        for chunk, sym in self.runs(s):
            self.font_run(font, sym)
            c.show_text(chunk)  # show_text deja el cursor al final: el siguiente tramo sigue ahí
        return x, x + wd


CHARACTERS = [("Robot", "robot"), ("Chispa", "chispa"), ("Burbuja", "burbuja"), ("Gato", "gato"),
              ("Zorro", "zorro"), ("Pulpo", "pulpo")]
# accesorios: (nombre, cuándo se gana, cuándo lo usa)
ACCESSORIES = {"phones": ("Auriculares", 0), "glasses": ("Lentes", 3), "hat": ("Gorro de fiesta", 10)}


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
    elif kind == "zorro":
        orange = body if body != "#f2f2f2" else "#ff9a4d"  # en blanco sería un zorro ártico: mejor naranja
        for side in (-1, 1):  # orejas puntiagudas, con la punta oscura del color del ánimo
            ox = x + side * s * 0.28
            ear = [(ox - s * 0.15, y - s * 0.1), (ox + side * s * 0.08, y - s * 0.52), (ox + s * 0.15, y - s * 0.1)]
            p.poly(ear, orange, a)
            tip = ear[1]
            p.poly([(tip[0] - side * s * 0.02 - s * 0.05, tip[1] + s * 0.14), tip,
                    (tip[0] - side * s * 0.02 + s * 0.05, tip[1] + s * 0.14)], col, a * glow)
        p.poly([(x - s * 0.48, y - s * 0.2), (x + s * 0.48, y - s * 0.2), (x + s * 0.2, y + s * 0.3), (x, y + s * 0.42),
                (x - s * 0.2, y + s * 0.3)], orange, a)  # cabeza en forma de V
        p.c.new_path()
        p.oval(x - s * 0.48, y - s * 0.4, x + s * 0.48, y + s * 0.18, orange, a)
        p.poly([(x - s * 0.34, y + s * 0.02), (x + s * 0.34, y + s * 0.02), (x + s * 0.12, y + s * 0.3), (x, y + s * 0.38),
                (x - s * 0.12, y + s * 0.3)], "#fff6ec", a)  # hocico claro
        eyes(p, x, y - s * 0.06, s * 0.17, e, mood, INK, lw, now, look)
        p.circle(x, y + s * 0.3, s * 0.05, INK, a)
    elif kind == "pulpo":
        wave = math.sin(now * (6 if busy else 2.6))
        for i in range(5):  # tentáculos que se mueven
            tx = x + (i - 2) * s * 0.17
            sway = wave * s * 0.05 * (1 if i % 2 else -1)
            p.c.move_to(tx, y + s * 0.12)
            p.c.curve_to(tx + sway, y + s * 0.3, tx - sway, y + s * 0.4, tx + sway * 1.5, y + s * 0.5)
            p.c.set_line_width(s * 0.11)
            p.c.set_line_cap(cairo.LINE_CAP_ROUND)
            p.rgba(edge, a)
            p.c.stroke()
        p.oval(x - s * 0.42, y - s * 0.44, x + s * 0.42, y + s * 0.26, body, a)
        for side in (-1, 1):  # cachetes del color del ánimo
            p.circle(x + side * s * 0.25, y + s * 0.06, s * 0.06, col, a * 0.7 * glow)
        eyes(p, x, y - s * 0.06, s * 0.15, e, mood, INK, lw, now, look)
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


def accessory(p, which, kind, x, y, s, now, a=1.0, accent="#3ddc84"):
    """Accesorio encima del personaje (las posiciones son aproximadas: sirven para todos)."""
    top = y - s * (0.42 if kind in ("robot", "burbuja", "pulpo") else 0.36)
    if which == "phones":  # auriculares: vincha y almohadillas
        p.c.new_sub_path()
        p.c.arc(x, y - s * 0.02, s * 0.52, math.pi * 1.08, math.pi * 1.92)
        p.c.set_line_width(max(1.5, s * 0.07))
        p.rgba("#2b2b33", a)
        p.c.stroke()
        for side in (-1, 1):
            p.rrect(x + side * s * 0.52 - s * 0.09, y - s * 0.14, x + side * s * 0.52 + s * 0.09, y + s * 0.12,
                    s * 0.07, accent, a)
    elif which == "glasses":
        for side in (-1, 1):
            p.c.new_sub_path()
            p.c.arc(x + side * s * 0.16, y - s * 0.02, s * 0.12, 0, 2 * math.pi)
            p.c.set_line_width(max(1.2, s * 0.045))
            p.rgba("#16161c", a)
            p.c.stroke()
        p.line(x - s * 0.04, y - s * 0.03, x + s * 0.04, y - s * 0.03, "#16161c", a, max(1.2, s * 0.045))
    elif which == "hat":  # gorro de fiesta con pompón
        p.poly([(x - s * 0.17, top + s * 0.04), (x + s * 0.17, top + s * 0.04), (x + s * 0.03, top - s * 0.42)], accent, a)
        for k in (0.35, 0.65):
            yy = top + s * 0.04 - s * 0.46 * k
            half = s * 0.17 * (1 - k)
            p.line(x - half, yy, x + half, yy, "#ffffff", a * 0.8, max(1, s * 0.04))
        p.circle(x + s * 0.03, top - s * 0.44, s * 0.07, "#ffd166", a)


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


THEMES = [("Negro", "black"), ("Vidrio", "glass"), ("Aurora", "aurora"), ("Carátula", "album")]  # fondos


def album_color(data):
    """Color más vivo de una carátula (bytes): el que tiñe la isla con el fondo "Carátula"."""
    import colorsys
    im = Image.open(io.BytesIO(data)).convert("RGB").resize((48, 48))
    best, score = (90, 90, 110), -1
    for count, (r, g, b) in im.getcolors(48 * 48):
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        sc = count * (0.2 + s) * (0.3 + v) * (v > 0.15)  # frecuente, saturado y no muy oscuro
        if sc > score:
            best, score = (r, g, b), sc
    return best


# Bloques de la isla abierta (widgets): nombre, alto. El usuario elige cuáles y en qué orden.
WIDGETS = {"claude": ("Claude Code", 104), "music": ("Música", 110), "today": ("Hoy: clima, fecha y cita", 58),
           "system": ("Sistema: CPU, RAM y batería", 52), "stats": ("Tu día: Claude, enfoque y música", 62),
           "gallery": ("Capturas recientes", 84)}
WIN = (BIG[0] + 90, 6 + sum(h for _name, h in WIDGETS.values()) + 30)  # ventana fija: entra la isla más alta posible


SHOULDER = 9  # radio de las curvas donde la isla se une al borde de arriba
