"""python test_island.py — prueba estado de Claude Code, permisos de punta a punta (hook real) e instalador."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["ISLA_PORT"] = "47899"  # puerto aparte: no choca con la isla abierta ni con sesiones reales
import install_hooks
import island

island.REPLY_GRACE = 0.3  # que el Stop no espere 6 s en cada prueba

HOOK = Path(__file__).parent / "claude_hook.py"


def run_hook(ev):
    r = subprocess.run([sys.executable, HOOK], input=json.dumps(ev).encode(), capture_output=True, timeout=20)
    assert r.returncode == 0, r.stderr
    return r.stdout


def answer_when_asked(cc, allow):
    def go():
        while not cc.perms:
            time.sleep(0.02)
        cc.answer(allow)
    threading.Thread(target=go, daemon=True).start()


cc = island.ClaudeCode()
base = {"session_id": "grace", "cwd": "C:/proyectos/webshop"}

cc.handle({**base, "hook_event_name": "PreToolUse", "tool_name": "Edit",
           "tool_input": {"file_path": "C:/x/checkout.ts", "old_string": "total = 0", "new_string": "total = sum()"}})
s, n = cc.current()
assert (s["project"], s["status"], n) == ("webshop", "Editando checkout.ts", 1)
assert s["detail"][1] == ("- total = 0", island.RED) and cc.mood() == "focused"

server = island.serve(cc)
perm = {**base, "hook_event_name": "PermissionRequest", "tool_name": "Bash",
        "tool_input": {"command": "git push origin main"}}
answer_when_asked(cc, True)
out = json.loads(run_hook(perm))
assert out == {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
assert cc.mood() == "love" and not cc.perms

answer_when_asked(cc, False)
assert json.loads(run_hook(perm))["hookSpecificOutput"]["decision"]["behavior"] == "deny"

cc.enabled = False  # isla oculta: el permiso vuelve a la terminal al instante
t0 = time.time()
assert run_hook(perm) == b"" and time.time() - t0 < 5 and not cc.perms
cc.enabled = True

island.PERMISSION_WAIT = 0.3  # nadie responde: el hook no decide y Claude Code pregunta en la terminal
assert run_hook(perm) == b""

island.PERMISSION_WAIT = 10  # respondido en la terminal: PostToolUse libera el pedido pendiente
t = threading.Thread(target=lambda: run_hook(perm))
t.start()
while not cc.perms:
    time.sleep(0.02)
run_hook({**base, "hook_event_name": "PostToolUse", "tool_name": "Bash"})
t.join(5)
assert not t.is_alive() and not cc.perms

cc.handle({**base, "hook_event_name": "UserPromptSubmit", "prompt": "arregla el checkout"})
assert cc.current()[0]["since"] and cc.current()[0]["took"] is None
assert run_hook({**base, "hook_event_name": "Stop", "last_assistant_message": "Hecho"}) == b""
s = cc.current()[0]
assert s["status"] == "Listo ✓" and s["since"] is None and s["took"] >= 0
assert s["pid"] == os.getpid()  # el hook manda su proceso padre (este test) para ubicar la terminal
# responder desde la isla: lo escrito vuelve como "block" y Claude sigue con eso
def reply_when_offered(text):
    def go():
        while not cc.reply:
            time.sleep(0.02)
        cc.send_reply(cc.open_reply(), text)
    threading.Thread(target=go, daemon=True).start()
reply_when_offered("ahora agrega tests")
out = json.loads(run_hook({**base, "hook_event_name": "Stop", "last_assistant_message": "Hecho"}))
assert out == {"decision": "block", "reason": "ahora agrega tests"} and cc.current()[0]["status"] == "Pensando…"
cc.reply_enabled = False  # apagado: el Stop no espera
t0 = time.time()
assert run_hook({**base, "hook_event_name": "Stop"}) == b"" and time.time() - t0 < 2
cc.reply_enabled = True
assert island.version_tuple("v1.10.0") > island.version_tuple("1.9.3")
mons = [{"name": "A", "x": 0, "y": 0, "w": 2560, "h": 1440, "scale": 1.0, "primary": True},
        {"name": "B", "x": 2560, "y": 0, "w": 1920, "h": 1080, "scale": 1.5, "primary": False}]
assert island.pick_monitor(mons, "mouse", (3000, 500))["name"] == "B"
assert island.pick_monitor(mons, "B", (0, 0))["name"] == "B"
assert island.pick_monitor(mons, "desconectada", (0, 0))["name"] == "A"  # vuelve a la principal
cc.handle({"session_id": "otra", "cwd": "C:/y/blog", "hook_event_name": "SessionStart"})
assert [sid for sid, _ in cc.ordered()] == ["grace", "otra"]
cc.handle({"session_id": "otra", "hook_event_name": "SessionEnd"})
run_hook({**base, "hook_event_name": "SessionEnd"})
cc.mood_override = ("", 0)  # el "curioso" de 3 s tras Denegar
assert cc.current() == (None, 0) and cc.mood() == "sleep"

server.shutdown()
server.server_close()
assert run_hook(perm) == b""  # isla cerrada: no rompe Claude Code

with tempfile.TemporaryDirectory() as d:
    install_hooks.SETTINGS = Path(d) / "settings.json"
    other = {"hooks": [{"type": "command", "command": "echo grace"}]}
    install_hooks.SETTINGS.write_text(json.dumps({"model": "x", "hooks": {"Stop": [other]}}))
    install_hooks.main()
    install_hooks.main()  # idempotente
    cfg = json.loads(install_hooks.SETTINGS.read_text())
    assert cfg["hooks"]["Stop"][0] == other and len(cfg["hooks"]["Stop"]) == 2
    assert cfg["hooks"]["PermissionRequest"][0]["hooks"][0]["timeout"] == 120
    sys.argv.append("--remove")
    install_hooks.main()
    assert json.loads(install_hooks.SETTINGS.read_text()) == {"model": "x", "hooks": {"Stop": [other]}}

print("ok")
