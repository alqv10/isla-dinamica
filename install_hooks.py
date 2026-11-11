"""Conecta la isla con Claude Code agregando hooks a ~/.claude/settings.json (guarda copia .bak).
    python install_hooks.py           instalar
    python install_hooks.py --remove  quitar
Desde el .exe lo hace el instalador (IslaDinamica.exe --install / --uninstall)."""
import json
import shutil
import sys
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"
HOOK = (Path(__file__).resolve().parent / "claude_hook.py").as_posix()
# (evento, matcher, timeout en s o None = async, argumentos extra). Los de estado van async para no frenar a
# Claude; el de permisos espera el clic en la isla, Stop espera por si respondes desde la isla
# (claude.REPLY_GRACE/REPLY_WAIT) y el de AskUserQuestion espera tu respuesta (claude.QUESTION_WAIT).
EVENTS = [("SessionStart", None, None, []), ("UserPromptSubmit", None, None, []), ("PreToolUse", "*", None, []),
          ("PreToolUse", "AskUserQuestion", 150, ["--pregunta"]), ("PostToolUse", "*", None, []),
          ("Notification", None, None, []), ("SessionEnd", None, None, []), ("PermissionRequest", "*", 120, []),
          ("Stop", None, 400, [])]


def hook_command():
    """(programa, argumentos) del hook: el .exe con --hook, o python + claude_hook.py."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).as_posix(), ["--hook"]
    return Path(sys.executable).as_posix(), [HOOK]


def ours(group):
    return any("claude_hook.py" in text or "IslaDinamica" in text
               for text in (" ".join([h.get("command", "")] + h.get("args", [])) for h in group.get("hooks", [])))


def install(remove=False):
    settings = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
    if SETTINGS.exists():
        shutil.copy(SETTINGS, SETTINGS.with_suffix(".json.bak"))
    hooks = settings.setdefault("hooks", {})
    for event in list(hooks):  # quitar los nuestros (deja intactos los demás hooks)
        hooks[event] = [g for g in hooks[event] if not ours(g)]
        if not hooks[event]:
            del hooks[event]
    if not remove:
        command, args = hook_command()
        for event, matcher, timeout, extra in EVENTS:
            h = {"type": "command", "command": command, "args": args + extra}
            h.update({"async": True} if timeout is None else {"timeout": timeout})
            hooks.setdefault(event, []).append({"matcher": matcher, "hooks": [h]} if matcher else {"hooks": [h]})
    if not hooks:
        del settings["hooks"]
    SETTINGS.parent.mkdir(exist_ok=True)
    SETTINGS.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")


def main():
    remove = "--remove" in sys.argv
    install(remove)
    print("Hooks quitados." if remove else f"Hooks instalados en {SETTINGS}")


if __name__ == "__main__":
    main()
