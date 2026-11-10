"""Punto de entrada de IslaDinamica.exe:
    IslaDinamica.exe              abre la isla
    IslaDinamica.exe --hook       hook de Claude Code (lo llama Claude Code, no la persona)
    IslaDinamica.exe --install    conecta con Claude Code, accesos directos, arranque con Windows
    IslaDinamica.exe --uninstall  deshace todo lo anterior y borra la carpeta"""
import os
import subprocess
import sys
from pathlib import Path

NAME = "Isla dinámica"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\IslaDinamica"
START_MENU = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Isla dinamica.lnk"


def install():
    import winreg

    import install_hooks
    import island
    if "--no-claude" not in sys.argv:
        install_hooks.install()
    island.shortcut(START_MENU)
    island.set_startup(True)
    exe = Path(sys.executable)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:  # aparece en "Aplicaciones instaladas"
        for name, value in [("DisplayName", NAME), ("Publisher", "Isla dinámica (código abierto)"),
                            ("DisplayIcon", f"{exe},0"), ("DisplayVersion", island.VERSION),
                            ("InstallLocation", str(exe.parent)), ("UninstallString", f'"{exe}" --uninstall')]:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)


def uninstall():
    import ctypes
    import winreg

    import install_hooks
    import island
    subprocess.run(["taskkill", "/f", "/im", "IslaDinamica.exe", "/fi", f"PID ne {os.getpid()}"],
                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    install_hooks.install(remove=True)
    island.set_startup(False)
    START_MENU.unlink(missing_ok=True)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass
    # la carpeta se borra cuando este proceso ya terminó (un .exe en uso no se puede borrar)
    folder = Path(sys.executable).parent
    subprocess.Popen(f'cmd /c ping -n 3 127.0.0.1 >nul & rmdir /s /q "{folder}"',
                     creationflags=subprocess.CREATE_NO_WINDOW)
    ctypes.windll.user32.MessageBoxW(None, "La isla dinámica se desinstaló.", NAME, 0x40)


if __name__ == "__main__":
    if "--hook" in sys.argv:
        import claude_hook  # noqa: F401  (hace todo al importarse)
    elif "--install" in sys.argv:
        install()
    elif "--uninstall" in sys.argv:
        uninstall()
    else:
        import island
        island.main()
