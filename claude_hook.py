"""Hook de Claude Code: reenvía el evento (JSON por stdin) a la isla y devuelve su respuesta.
Si la isla está cerrada no imprime nada y Claude Code sigue como siempre."""
import http.client
import json
import os
import sys

PORT = int(os.environ.get("ISLA_PORT", 47823))  # el mismo en island.py

data = sys.stdin.buffer.read()
try:  # el proceso padre lleva a la ventana de la terminal (para traerla al frente desde la isla)
    data = json.dumps({**json.loads(data), "_pid": os.getppid()}).encode()
except ValueError:
    pass
try:
    # conectar con timeout corto: en Windows un puerto cerrado tarda ~2 s en rechazar, y eso frenaría a
    # Claude Code en cada evento con la isla cerrada. Ya conectados, se espera la respuesta (permisos).
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=0.2)
    conn.connect()
    conn.sock.settimeout(110)
    conn.request("POST", "/", data, {"Content-Type": "application/json"})
    sys.stdout.buffer.write(conn.getresponse().read())
except OSError:
    pass
