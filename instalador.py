"""IslaDinamica-Setup.exe: copia la isla a %LOCALAPPDATA%\\Programs\\IslaDinamica, la conecta con Claude Code,
la pone a arrancar con Windows y la abre. No necesita Python ni permisos de administrador.
Lleva adentro IslaDinamica.zip (la carpeta que arma PyInstaller); ver build.py."""
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import zipfile
from pathlib import Path

DEST = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs" / "IslaDinamica"
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "IslaDinamica.zip"
BG, FG, GREY, GREEN = "#111114", "#ffffff", "#9a9a9f", "#3ddc84"


def island_running():
    r = subprocess.run(["tasklist", "/fi", "IMAGENAME eq IslaDinamica.exe", "/fo", "csv", "/nh"],
                       capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    return "IslaDinamica.exe" in r.stdout


def close_island():
    """Cierra la isla (y hooks en curso) y espera a que los procesos terminen de verdad: taskkill vuelve
    antes, y mientras tanto Windows no deja reemplazar el .exe."""
    for _ in range(30):
        subprocess.run(["taskkill", "/f", "/im", "IslaDinamica.exe"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        if not island_running():
            return
        time.sleep(0.5)


def extract():
    """Copia los archivos; si alguno sigue bloqueado (un hook que justo arrancó), cierra y reintenta."""
    DEST.mkdir(parents=True, exist_ok=True)
    for attempt in range(8):
        try:
            with zipfile.ZipFile(BUNDLE) as z:
                z.extractall(DEST)
            return
        except PermissionError:
            if attempt == 7:
                raise
            close_island()
            time.sleep(1)


def run_install(set_status, claude):
    set_status("Cerrando la isla si estaba abierta…")
    close_island()
    set_status("Copiando archivos…")
    extract()
    exe = DEST / "IslaDinamica.exe"
    set_status("Configurando…")
    args = [str(exe), "--install"] + ([] if claude else ["--no-claude"])
    r = subprocess.run(args, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode:
        raise RuntimeError(r.stderr.decode(errors="replace")[-300:] or "error al configurar")
    subprocess.Popen([str(exe)], cwd=DEST, creationflags=subprocess.DETACHED_PROCESS)


def hooks_installed():
    """¿La instalación anterior estaba conectada con Claude Code? (para respetarlo al actualizar)"""
    try:
        return "IslaDinamica" in (Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8")
    except OSError:
        return False


def main():
    if "--silent" in sys.argv:  # actualización desde la isla: sin ventanas, misma configuración
        try:
            run_install(lambda text: None, hooks_installed())
        except (OSError, RuntimeError, zipfile.BadZipFile) as e:  # sin cuadros de error: anotar y reabrir
            log = Path(os.environ.get("TEMP", DEST)) / "IslaDinamica-actualizacion.log"
            log.write_text(f"{time.ctime()}: no se pudo actualizar: {e!r}\n", encoding="utf-8")
            exe = DEST / "IslaDinamica.exe"
            if exe.exists():
                subprocess.Popen([str(exe)], cwd=DEST, creationflags=subprocess.DETACHED_PROCESS)
        return
    root = tk.Tk()
    root.title("Instalar Isla dinámica")
    root.configure(bg=BG, padx=28, pady=22)
    root.resizable(False, False)
    tk.Label(root, text="Isla dinámica", font=("Segoe UI", 18, "bold"), bg=BG, fg=FG).pack(anchor="w")
    tk.Label(root, text="Una píldora arriba de la pantalla con Claude Code, música,\nnotificaciones, "
             "volumen, brillo y temporizador.", font=("Segoe UI", 10), bg=BG, fg=GREY,
             justify="left").pack(anchor="w", pady=(4, 14))
    claude = tk.BooleanVar(value=True)
    tk.Checkbutton(root, text="Conectar con Claude Code (permisos y estado en la isla)", variable=claude,
                   font=("Segoe UI", 10), bg=BG, fg=FG, selectcolor=BG, activebackground=BG,
                   activeforeground=FG).pack(anchor="w")
    tk.Label(root, text=f"Se instala en {DEST}\nArranca sola con Windows (se puede apagar con clic derecho).",
             font=("Segoe UI", 9), bg=BG, fg=GREY, justify="left").pack(anchor="w", pady=(10, 16))
    status = tk.Label(root, text="", font=("Segoe UI", 10), bg=BG, fg=GREY)
    status.pack(anchor="w")
    btn = tk.Button(root, text="Instalar", font=("Segoe UI", 11, "bold"), bg=GREEN, fg="#000000", bd=0,
                    padx=26, pady=6, activebackground="#2fbf70", cursor="hand2")
    btn.pack(anchor="e", pady=(10, 0))

    def set_status(text, col=GREY):
        root.after(0, lambda: status.config(text=text, fg=col))

    def work():
        try:
            run_install(set_status, claude.get())
        except (OSError, RuntimeError, zipfile.BadZipFile) as e:
            set_status(f"No se pudo instalar: {e}", "#ff5555")
            root.after(0, lambda: btn.config(state="normal", text="Reintentar"))
            return
        set_status("¡Listo! La isla ya está arriba de tu pantalla.", GREEN)
        root.after(0, lambda: btn.config(state="normal", text="Cerrar", command=root.destroy))

    def start():
        btn.config(state="disabled")
        threading.Thread(target=work, daemon=True).start()

    btn.config(command=start)
    root.mainloop()


if __name__ == "__main__":
    main()
