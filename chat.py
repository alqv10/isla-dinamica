"""Chat con Claude desde la isla. Usa Claude Code (`claude -p`) con la sesión que ya tienes iniciada, así que
gasta tu plan (Pro/Max) y no hace falta API key. Claude responde sin herramientas: solo conversa, no toca
archivos (las capturas viajan dentro del mensaje). Corre en una carpeta propia y sin tus ajustes
(--setting-sources local), así sus hooks no avisan a la isla como si fuera otra sesión de Claude Code.
La respuesta llega por partes (stream-json) y se muestra mientras Claude la escribe."""
import base64
import ctypes
import io
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path

from textos import _

FOLDER = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "IslaDinamica" / "chat"
BG, FIELD, LINE, FG, DIM, CODE = "#141417", "#1c1c1f", "#3a3a40", "#ffffff", "#9a9a9f", "#202026"
MAX_SIDE = 1568  # las capturas se achican a esto (más grande no mejora la respuesta y gasta más del plan)


def find_claude():
    return shutil.which("claude") or shutil.which("claude.exe") or shutil.which("claude.cmd")


def png_for_claude(image):
    """PIL.Image -> bytes PNG de tamaño razonable."""
    image = image.convert("RGB")
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


class Conversation:
    """Manda preguntas a `claude -p` en un hilo, recibe la respuesta por partes y recuerda la sesión."""
    def __init__(self):
        self.session_id, self.busy, self.events = None, False, queue.Queue()

    def ask(self, text, images=()):
        exe = find_claude()
        if not exe:
            self.events.put(("done", False, _("No encontré Claude Code. Instálalo e inicia sesión con `claude` en una terminal.")))
            return
        self.busy = True
        cmd = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--include-partial-messages", "--setting-sources", "local", "--tools", "",
               "--append-system-prompt", _("Respondes desde una ventanita pequeña en el escritorio: sé breve y "
                                           "claro, en el idioma de la pregunta, sin títulos ni tablas.")]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": base64.b64encode(png).decode()}} for png in images]
        content.append({"type": "text", "text": text})
        message = json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"

        def run():
            FOLDER.mkdir(parents=True, exist_ok=True)
            final, ok = "", False
            try:
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     cwd=FOLDER, creationflags=subprocess.CREATE_NO_WINDOW)
                p.stdin.write(message.encode("utf-8"))
                p.stdin.close()
                for line in p.stdout:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("session_id"):
                        self.session_id = ev["session_id"]
                    inner = ev.get("event") or {}
                    if ev.get("type") == "stream_event" and inner.get("type") == "content_block_delta" \
                            and inner.get("delta", {}).get("type") == "text_delta":
                        self.events.put(("delta", inner["delta"]["text"]))
                    elif ev.get("type") == "result":
                        final, ok = (ev.get("result") or "").strip(), not ev.get("is_error")
                p.wait(timeout=30)
                if not final:
                    ok = False
                    final = p.stderr.read().decode("utf-8", errors="replace").strip()[-300:] or \
                        _("Claude no respondió. ¿Tienes sesión iniciada en Claude Code?")
            except (OSError, subprocess.TimeoutExpired) as e:
                final, ok = _("No se pudo hablar con Claude: {error}", error=e), False
            finally:
                self.busy = False
            self.events.put(("done", ok, final))
        threading.Thread(target=run, daemon=True, name="chat").start()

    def reset(self):
        self.session_id = None


def insert_markdown(text_widget, md):
    """Markdown sencillo con etiquetas de tkinter: **negrita**, `código`, bloques ```, listas y títulos."""
    in_code = False
    for line in md.split("\n"):
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            text_widget.insert("end", line + "\n", "codeblock")
            continue
        heading = re.match(r"#{1,6}\s+(.*)", line)
        if heading:
            text_widget.insert("end", heading.group(1) + "\n", "bold")
            continue
        bullet = re.match(r"(\s*)[-*]\s+(.*)", line)
        if bullet:
            text_widget.insert("end", bullet.group(1) + "• ", "claude")
            line = bullet.group(2)
        for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", line):
            if part.startswith("**") and part.endswith("**") and len(part) > 4:
                text_widget.insert("end", part[2:-2], "bold")
            elif part.startswith("`") and part.endswith("`") and len(part) > 2:
                text_widget.insert("end", part[1:-1], "code")
            elif part:
                text_widget.insert("end", part, "claude")
        text_widget.insert("end", "\n", "claude")


class ChatPanel:
    """Panel debajo de la isla: la conversación arriba y un campo para escribir abajo (Ctrl+V pega capturas)."""
    def __init__(self, isl, text="", image=None):
        self.isl, self.conv = isl, Conversation()
        self.last_answer, self.attached = "", []
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = ctypes.c_void_p
        self.before = u.GetForegroundWindow()  # a dónde volver al cerrar
        S = self.S = isl.S
        t = self.top = tk.Toplevel(isl.root, bg=LINE)  # el fondo hace de borde de 1 px
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        W, H = round(480 * S), round(360 * S)
        t.geometry(f"{W}x{H}+{isl.X + (isl.W - W) // 2}+{isl.Y + round(52 * S)}")
        body = tk.Frame(t, bg=BG)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        self.text = tk.Text(body, bg=BG, fg=FG, bd=0, wrap="word", font=("Segoe UI", 10), padx=round(14 * S),
                            pady=round(10 * S), highlightthickness=0, cursor="arrow", insertbackground=BG,
                            spacing3=round(4 * S), selectbackground="#3a3a50")
        tags = {"you": {"foreground": isl.accent, "font": ("Segoe UI", 10, "bold"), "spacing1": round(8 * S)},
                "claude": {"foreground": FG}, "bold": {"foreground": FG, "font": ("Segoe UI", 10, "bold")},
                "code": {"foreground": "#ffd38a", "font": ("Consolas", 10), "background": CODE},
                "codeblock": {"foreground": "#d7e3ff", "font": ("Consolas", 9), "background": CODE,
                              "lmargin1": round(8 * S), "lmargin2": round(8 * S)},
                "dim": {"foreground": DIM, "font": ("Segoe UI", 9)}, "error": {"foreground": "#ff8a8a"}}
        for name, opts in tags.items():
            self.text.tag_configure(name, **opts)
        bottom = tk.Frame(body, bg=FIELD)
        bottom.pack(side="bottom", fill="x")
        self.attach_label = tk.Label(body, bg=BG, fg=isl.accent, font=("Segoe UI", 9), anchor="w", cursor="hand2")
        self.text.pack(side="top", fill="both", expand=True)
        self.entry = tk.Entry(bottom, bg=FIELD, fg=FG, insertbackground=FG, relief="flat", bd=0, font=("Segoe UI", 11))
        self.entry.pack(side="left", fill="x", expand=True, padx=round(14 * S), pady=round(10 * S))
        mic = tk.Label(bottom, text=_("Dictar"), bg=LINE, fg=FG, font=("Segoe UI", 9, "bold"), cursor="hand2",
                       padx=round(10 * S), pady=round(3 * S))
        mic.pack(side="right", padx=(0, round(10 * S)))
        mic.bind("<Button-1>", lambda e: self.dictate())
        tk.Label(bottom, text=_("Enter envía · Ctrl+V pega una captura · Esc cierra"), bg=FIELD, fg=DIM,
                 font=("Segoe UI", 8)).pack(side="right", padx=round(10 * S))
        self.entry.bind("<Return>", lambda e: self.send())
        self.entry.bind("<Control-v>", self.paste)
        self.attach_label.bind("<Button-1>", lambda e: self.clear_attachments())
        for w in (self.entry, self.text, t):
            w.bind("<Escape>", lambda e: self.close())
        self.say(_("Pregúntale lo que quieras a Claude. Usa tu plan de Claude Code (sin API key)."), "dim")
        if image is not None:
            self.attach(image)
        if text:
            self.entry.insert(0, text)
        t.update()
        u.keybd_event(0x12, 0, 0, 0)  # Alt: Windows deja cambiar el foco solo tras una tecla
        u.keybd_event(0x12, 0, 2, 0)
        u.GetParent.restype = ctypes.c_void_p
        u.SetForegroundWindow(ctypes.c_void_p(u.GetParent(ctypes.c_void_p(t.winfo_id()))))
        self.entry.focus_force()
        self.entry.icursor("end")
        self.poll()

    # --- texto ---

    def say(self, text, tag):
        self.text.config(state="normal")
        self.text.insert("end", text + "\n", tag)
        self.text.config(state="disabled")
        self.text.see("end")

    # --- capturas adjuntas ---

    def attach(self, image):
        self.attached.append(png_for_claude(image))
        n = len(self.attached)
        self.attach_label.config(text=_("+ {n} captura(s) adjunta(s) · clic para quitar", n=n))
        self.attach_label.pack(side="bottom", fill="x", padx=round(14 * self.S), pady=(0, round(4 * self.S)))

    def clear_attachments(self):
        self.attached = []
        self.attach_label.pack_forget()

    def paste(self, e):
        """Ctrl+V: si hay una imagen en el portapapeles se adjunta; si es texto, se pega normal."""
        from PIL import Image, ImageGrab
        try:
            im = ImageGrab.grabclipboard()
        except OSError:
            return None
        if isinstance(im, Image.Image):
            self.attach(im)
            return "break"
        return None

    # --- conversación ---

    def send(self):
        text = self.entry.get().strip()
        if not text or self.conv.busy:
            return
        self.entry.delete(0, "end")
        handled = self.isl.reminder_from_text(text)  # "recuérdame en 20 min…" lo resuelve la isla, sin Claude
        self.say(_("Tú: {text}", text=text) + (_("  (+ {n} captura(s))", n=len(self.attached)) if self.attached else ""),
                 "you")
        if handled:
            self.say(handled, "claude")
            return
        self.text.mark_set("answer", "end-1c")
        self.text.mark_gravity("answer", "left")
        self.conv.ask(text, self.attached)
        self.clear_attachments()

    def dictate(self):
        """Dictado por voz con el de Windows (Win+H): escribe lo que dices en el campo del chat."""
        self.entry.focus_force()
        u = ctypes.windll.user32

        def press():
            u.keybd_event(0x5B, 0, 0, 0)  # Win
            u.keybd_event(0x48, 0, 0, 0)  # H
            u.keybd_event(0x48, 0, 2, 0)
            u.keybd_event(0x5B, 0, 2, 0)
        self.top.after(150, press)  # con el foco ya en el campo

    def new(self):
        """Conversación nueva: Claude olvida lo anterior."""
        self.conv.reset()
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.config(state="disabled")
        self.say(_("Conversación nueva."), "dim")

    def copy_last(self):
        if self.last_answer:
            self.top.clipboard_clear()
            self.top.clipboard_append(self.last_answer)
            self.say(_("Copiado."), "dim")

    def poll(self):
        """Muestra lo que va llegando: el texto crudo mientras se escribe y con formato al terminar."""
        changed = False
        while True:
            try:
                ev = self.conv.events.get_nowait()
            except queue.Empty:
                break
            self.text.config(state="normal")
            if ev[0] == "delta":
                self.text.insert("end-1c", ev[1], "claude")
            else:
                _kind, ok, final = ev
                self.text.delete("answer", "end-1c")  # el texto crudo se reemplaza por el formateado
                if ok:
                    insert_markdown(self.text, final)
                    self.last_answer = final
                    self.isl.celebrate_t = time.time()  # el personaje festeja la respuesta
                else:
                    self.text.insert("end-1c", final + "\n", "error")
            self.text.config(state="disabled")
            changed = True
        if changed:
            self.text.see("end")
        if self.top.winfo_exists():
            self.top.after(60, self.poll)

    @property
    def busy(self):
        return self.conv.busy

    def close(self):
        self.isl.chat = None
        self.top.destroy()
        if self.before:
            ctypes.windll.user32.SetForegroundWindow(ctypes.c_void_p(self.before))
