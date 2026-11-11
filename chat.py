"""Chat con Claude desde la isla. Usa Claude Code (`claude -p`) con la sesión que ya tienes iniciada, así que
gasta tu plan (Pro/Max) y no hace falta API key. Claude responde sin herramientas: solo conversa, no toca
archivos. Corre en una carpeta propia y sin tus ajustes (--setting-sources local), así sus hooks no avisan a
la isla como si fuera otra sesión de Claude Code."""
import ctypes
import json
import os
import queue
import shutil
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path

from textos import _

FOLDER = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "IslaDinamica" / "chat"
BG, FIELD, LINE, FG, DIM = "#141417", "#1c1c1f", "#3a3a40", "#ffffff", "#9a9a9f"


def find_claude():
    return shutil.which("claude") or shutil.which("claude.exe") or shutil.which("claude.cmd")


class Conversation:
    """Manda preguntas a `claude -p` en un hilo y recuerda la sesión para que Claude tenga contexto."""
    def __init__(self):
        self.session_id, self.busy, self.results = None, False, queue.Queue()

    def ask(self, text):
        exe = find_claude()
        if not exe:
            self.results.put((False, _("No encontré Claude Code. Instálalo e inicia sesión con `claude` en una terminal.")))
            return
        self.busy = True
        cmd = [exe, "-p", "--output-format", "json", "--setting-sources", "local", "--tools", "",
               "--append-system-prompt", _("Respondes desde una ventanita pequeña en el escritorio: sé breve y "
                                           "claro, en el idioma de la pregunta, sin títulos ni tablas.")]
        if self.session_id:
            cmd += ["--resume", self.session_id]

        def run():
            FOLDER.mkdir(parents=True, exist_ok=True)
            try:
                r = subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True, cwd=FOLDER, timeout=300,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                data = json.loads(r.stdout.decode("utf-8", errors="replace") or "{}")
                if data.get("session_id"):
                    self.session_id = data["session_id"]
                answer = (data.get("result") or "").strip()
                ok = bool(answer) and not data.get("is_error")
                if not ok:
                    answer = answer or r.stderr.decode("utf-8", errors="replace").strip()[-300:] or \
                        _("Claude no respondió. ¿Tienes sesión iniciada en Claude Code?")
                self.results.put((ok, answer))
            except subprocess.TimeoutExpired:
                self.results.put((False, _("Claude tardó demasiado en responder.")))
            except (OSError, ValueError) as e:
                self.results.put((False, _("No se pudo hablar con Claude: {error}", error=e)))
            finally:
                self.busy = False
        threading.Thread(target=run, daemon=True, name="chat").start()

    def reset(self):
        self.session_id = None


class ChatPanel:
    """Panel debajo de la isla: la conversación arriba y un campo para escribir abajo."""
    def __init__(self, isl):
        self.isl, self.conv = isl, Conversation()
        u = ctypes.windll.user32
        self.before = u.GetForegroundWindow()  # a dónde volver al cerrar
        S = self.S = isl.S
        t = self.top = tk.Toplevel(isl.root, bg=LINE)  # el fondo hace de borde de 1 px
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        W, H = round(480 * S), round(330 * S)
        t.geometry(f"{W}x{H}+{isl.X + (isl.W - W) // 2}+{isl.Y + round(52 * S)}")
        body = tk.Frame(t, bg=BG)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        self.text = tk.Text(body, bg=BG, fg=FG, bd=0, wrap="word", font=("Segoe UI", 10), padx=round(14 * S),
                            pady=round(10 * S), highlightthickness=0, cursor="arrow", insertbackground=BG,
                            spacing3=round(6 * S))
        self.text.tag_configure("you", foreground=isl.accent, font=("Segoe UI", 10, "bold"))
        self.text.tag_configure("claude", foreground=FG)
        self.text.tag_configure("dim", foreground=DIM, font=("Segoe UI", 9))
        self.text.tag_configure("error", foreground="#ff8a8a")
        bottom = tk.Frame(body, bg=FIELD)
        bottom.pack(side="bottom", fill="x")
        self.text.pack(side="top", fill="both", expand=True)
        self.entry = tk.Entry(bottom, bg=FIELD, fg=FG, insertbackground=FG, relief="flat", bd=0,
                              font=("Segoe UI", 11))
        self.entry.pack(side="left", fill="x", expand=True, padx=round(14 * S), pady=round(10 * S))
        tk.Label(bottom, text=_("Enter envía · Esc cierra"), bg=FIELD, fg=DIM, font=("Segoe UI", 8)).pack(
            side="right", padx=round(10 * S))
        self.entry.bind("<Return>", lambda e: self.send())
        for w in (self.entry, self.text, t):
            w.bind("<Escape>", lambda e: self.close())
        self.say(_("Pregúntale lo que quieras a Claude. Usa tu plan de Claude Code (sin API key)."), "dim")
        self.text.config(state="disabled")
        t.update()
        u.keybd_event(0x12, 0, 0, 0)  # Alt: Windows deja cambiar el foco solo tras una tecla
        u.keybd_event(0x12, 0, 2, 0)
        u.GetParent.restype = ctypes.c_void_p
        u.SetForegroundWindow(ctypes.c_void_p(u.GetParent(ctypes.c_void_p(t.winfo_id()))))
        self.entry.focus_force()
        self.poll()

    def say(self, text, tag):
        self.text.config(state="normal")
        self.text.insert("end", text + "\n", tag)
        self.text.config(state="disabled")
        self.text.see("end")

    def send(self):
        text = self.entry.get().strip()
        if not text or self.conv.busy:
            return
        self.entry.delete(0, "end")
        self.say(_("Tú: {text}", text=text), "you")
        self.conv.ask(text)

    def new(self):
        """Conversación nueva: Claude olvida lo anterior."""
        self.conv.reset()
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.config(state="disabled")
        self.say(_("Conversación nueva."), "dim")

    def poll(self):
        try:
            ok, answer = self.conv.results.get_nowait()
            self.say(answer, "claude" if ok else "error")
            if ok:
                self.isl.celebrate_t = time.time()  # el personaje festeja la respuesta
        except queue.Empty:
            pass
        if self.top.winfo_exists():
            self.top.after(100, self.poll)

    @property
    def busy(self):
        return self.conv.busy

    def close(self):
        self.isl.chat = None
        self.top.destroy()
        if self.before:
            ctypes.windll.user32.SetForegroundWindow(ctypes.c_void_p(self.before))
