"""Hook de Claude Code: reenvía el evento (JSON por stdin) a la isla y devuelve su respuesta.
Si la isla está cerrada no imprime nada y Claude Code sigue como siempre."""
import http.client
import json
import os
import sys

PORT = int(os.environ.get("ISLA_PORT", 47823))  # el mismo en island.py

data = sys.stdin.buffer.read()
try:  # el proceso padre lleva a la ventana de la terminal (para traerla al frente desde la isla)
    extra = {"_sync": True} if "--pregunta" in sys.argv else {}  # el hook que espera tu respuesta a Claude
    data = json.dumps({**json.loads(data), "_pid": os.getppid(), **extra}).encode()
except ValueError:
    pass
try:
    # conectar con timeout corto: en Windows un puerto cerrado tarda ~2 s en rechazar, y eso frenaría a
    # Claude Code en cada evento con la isla cerrada. Ya conectados, se espera la respuesta (permisos).
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=0.2)
    conn.connect()
    conn.sock.settimeout(390)  # permisos: 100 s; responder a Claude: hasta ~306 s
    conn.request("POST", "/", data, {"Content-Type": "application/json"})
    sys.stdout.buffer.write(conn.getresponse().read())
except OSError:
    pass
