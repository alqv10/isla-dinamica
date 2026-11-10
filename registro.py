"""Registro de errores: el .exe no tiene consola, así que cualquier error (de la isla, de sus hilos o de un
clic) se anota en %LOCALAPPDATA%\\IslaDinamica\\errores.log en vez de perderse."""
import os
import sys
import threading
import time
import traceback
from pathlib import Path

LOG = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "IslaDinamica" / "errores.log"
MAX_BYTES = 256 * 1024  # al pasar este tamaño se guarda una copia .old y se empieza de nuevo


def write(text):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > MAX_BYTES:
            LOG.replace(LOG.with_suffix(".old"))
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n{text.rstrip()}\n")
    except OSError:
        pass  # sin disco: no hay dónde anotarlo


def error(exc_type, exc, tb, where=""):
    write((f"[{where}] " if where else "") + "".join(traceback.format_exception(exc_type, exc, tb)))


def install(root=None, version=""):
    """Engancha los tres lugares donde Python avisa de errores no atrapados."""
    write(f"isla {version} iniciada (Python {sys.version.split()[0]})")
    sys.excepthook = lambda t, e, tb: error(t, e, tb, "principal")
    threading.excepthook = lambda a: error(a.exc_type, a.exc_value, a.exc_traceback, f"hilo {a.thread.name}")
    if root is not None:  # errores en clics y temporizadores de tkinter: se anotan y la isla sigue
        root.report_callback_exception = lambda t, e, tb: error(t, e, tb, "tkinter")
