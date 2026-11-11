"""Conexión con Claude Code: estado de las sesiones, permisos y respuestas, alimentado por claude_hook.py
por HTTP local."""
import json
import os
import threading
import time
import winsound
from textos import _
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dibujo import GREEN, GREY, RED, WHITE


PORT = int(os.environ.get("ISLA_PORT", 47823))  # el mismo en claude_hook.py


PERMISSION_WAIT = 100  # s esperando un clic antes de devolver el permiso a la terminal


REPLY_GRACE = 6  # s que la isla ofrece "Responder" cuando Claude termina (el turno espera ese rato)


REPLY_WAIT = 300  # s para escribir la respuesta una vez abierto el cuadro


def first_line(s, n=60):
    s = str(s or "").strip().splitlines()
    return s[0][:n] if s else ""


def describe(tool, inp):
    """(estado, [(línea de detalle, color)]) para mostrar una herramienta de Claude Code."""
    name = os.path.basename(str(inp.get("file_path") or inp.get("notebook_path") or ""))
    if tool in ("Edit", "MultiEdit"):
        edit = (inp.get("edits") or [inp])[0]
        return _("Editando {name}", name=name), [(name, GREY), ("- " + first_line(edit.get("old_string")), RED),
                                   ("+ " + first_line(edit.get("new_string")), GREEN)]
    if tool == "Write":
        return _("Escribiendo {name}", name=name), [(name, GREY), ("+ " + first_line(inp.get("content")), GREEN)]
    if tool in ("Read", "NotebookEdit"):
        return _("Leyendo {name}", name=name), []
    if tool in ("Bash", "PowerShell"):
        return _("Ejecutando comando"), [("$ " + first_line(inp.get("command")), WHITE)]
    if tool in ("Grep", "Glob"):
        return _("Buscando"), [(first_line(inp.get("pattern")), WHITE)]
    if tool in ("WebFetch", "WebSearch"):
        return _("Navegando"), [(first_line(inp.get("url") or inp.get("query")), WHITE)]
    if tool in ("Agent", "Task"):
        return _("Delegando a un agente"), [(first_line(inp.get("description")), WHITE)]
    return _("Usando {tool}", tool=tool), []


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
        s, _count = self.current()
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
                    s.update(status=_("Pensando…"), detail=[(first_line(text), GREY)], mood="focused",
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
                                               "status": _("Sesión iniciada"), "detail": [], "mood": "happy",
                                               "born": time.time(), "since": None, "took": None})
            s["t"] = time.time()
            s["pid"] = ev.get("_pid") or s.get("pid")
            if name == "SessionEnd":
                del self.sessions[sid]
            elif name == "UserPromptSubmit":
                s.update(status=_("Pensando…"), detail=[(first_line(ev.get("prompt")), GREY)], mood="focused",
                         since=time.time(), took=None)
            elif name == "PreToolUse":
                s["status"], s["detail"] = describe(tool, inp)
                s["mood"] = "focused"
            elif name == "Notification":
                s.update(status=first_line(ev.get("message")) or _("Te necesita"), detail=[], mood="curious")
            elif name == "Stop":
                s.update(status=_("Listo ✓"), detail=[(first_line(ev.get("last_assistant_message")), GREY)],
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
                   {"behavior": "deny", "message": _("Denegado desde la isla dinámica")}
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
