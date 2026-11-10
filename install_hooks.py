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
# Los de estado van async para no frenar a Claude; el de permisos espera el clic en la isla.
EVENTS = {"SessionStart": True, "UserPromptSubmit": True, "PreToolUse": True, "PostToolUse": True,
          "Notification": True, "Stop": True, "SessionEnd": True, "PermissionRequest": False}


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
        for event, is_async in EVENTS.items():
            h = {"type": "command", "command": command, "args": args}
            h.update({"async": True} if is_async else {"timeout": 120})
            hooks.setdefault(event, []).append({"matcher": "*", "hooks": [h]} if "Tool" in event or
                                               event == "PermissionRequest" else {"hooks": [h]})
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
