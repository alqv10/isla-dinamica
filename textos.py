"""Idiomas. El código está escrito en español; _("texto") devuelve la traducción según el idioma elegido en los
ajustes ("auto" = el de Windows). Si una frase no está traducida, se muestra en español: no se rompe nada.
Para agregar un idioma: un diccionario como EN y una entrada en LANGUAGES."""
import ctypes

LANG = "es"
MISSING = set()  # frases pedidas en inglés que no tienen traducción
LANGUAGES = [("Automático", "auto"), ("Español", "es"), ("English", "en")]


def system_language():
    """"es" si Windows está en español (cualquier país), si no "en"."""
    try:
        return "es" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x0A else "en"
    except (AttributeError, OSError):
        return "es"


def set_language(choice):
    global LANG
    LANG = system_language() if choice == "auto" else choice


def _(phrase, /, **values):
    if LANG == "en" and phrase not in EN:
        MISSING.add(phrase)  # el test visual las lista
    out = EN.get(phrase, phrase) if LANG == "en" else phrase
    return out.format(**values) if values else out


def days(full=False):
    if LANG == "en":
        return (["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"] if full else
                ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
    return (["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"] if full else
            ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"])


def months(full=False):
    if LANG == "en":
        return (["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                 "November", "December"] if full else
                ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])
    return (["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
             "noviembre", "diciembre"] if full else
            ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"])


def long_date(d):
    """"Martes 10 de noviembre" / "Tuesday, November 10"."""
    if LANG == "en":
        return f"{days(True)[d.weekday()]}, {months(True)[d.month - 1]} {d.day}"
    return f"{days(True)[d.weekday()].capitalize()} {d.day} de {months(True)[d.month - 1]}"


def short_date(d):
    """"mar 10 nov" / "Tue Nov 10"."""
    if LANG == "en":
        return f"{days()[d.weekday()]} {months()[d.month - 1]} {d.day}"
    return f"{days()[d.weekday()]} {d.day} {months()[d.month - 1]}"


EN = {
    # nombres que no cambian, e idiomas (cada uno en su propio idioma)
    "Claude Code": "Claude Code", "Automático": "Automatic", "Español": "Español", "English": "English",
    # chat
    "Preguntarle a Claude…": "Ask Claude…", "Preguntarle a Claude": "Ask Claude", "Pregúntale a Claude": "Ask Claude",
    "Claude está pensando…": "Claude is thinking…", "Nueva": "New", "×": "×", "Enter envía · Esc cierra":
        "Enter sends · Esc closes", "Tú: {text}": "You: {text}", "Conversación nueva.": "New conversation.",
    "Pregúntale lo que quieras a Claude. Usa tu plan de Claude Code (sin API key).":
        "Ask Claude anything. It uses your Claude Code plan (no API key).",
    "Respondes desde una ventanita pequeña en el escritorio: sé breve y claro, en el idioma de la pregunta, sin "
    "títulos ni tablas.": "You answer from a small window on the desktop: be brief and clear, in the language of "
                          "the question, without headings or tables.",
    "No encontré Claude Code. Instálalo e inicia sesión con `claude` en una terminal.":
        "Claude Code wasn't found. Install it and sign in by running `claude` in a terminal.",
    "Claude no respondió. ¿Tienes sesión iniciada en Claude Code?":
        "Claude didn't answer. Are you signed in to Claude Code?",
    "Claude tardó demasiado en responder.": "Claude took too long to answer.",
    "No se pudo hablar con Claude: {error}": "Couldn't reach Claude: {error}",
    # menú y bandeja
    "Mostrar / ocultar": "Show / hide", "Ajustes…": "Settings…", "Salir": "Quit", "Ocultar": "Hide",
    "Empezar enfoque (25 + 5 min)": "Start focus (25 + 5 min)", "Terminar enfoque": "Stop focus",
    "Apariencia": "Appearance", "Personaje": "Character", "Modo": "Mode", "Mostrar": "Show",
    "Tamaño normal": "Normal size", "Tamaño grande": "Large size", "Tamaño muy grande": "Extra large size",
    "Color {name}": "Color: {name}", "Translúcida": "Translucent", "Solo compañero": "Companion only",
    "Notificaciones": "Notifications", "Clima": "Weather", "Próxima cita": "Next event",
    "Responder a Claude desde la isla": "Reply to Claude from the island",
    "Vista previa de capturas": "Screenshot preview", "Texto copiado": "Copied text",
    "Micrófono y cámara en uso": "Microphone and camera in use", "Sonidos": "Sounds",
    "Ocultar en pantalla completa": "Hide in full screen", "Iniciar con Windows": "Start with Windows",
    "Buscar actualizaciones": "Check for updates", "Ver registro de errores": "View error log",
    "Actualizar a la versión {version}": "Update to version {version}", "sin errores hasta ahora": "no errors so far",
    # personajes y colores
    "Robot": "Robot", "Chispa": "Spark", "Burbuja": "Bubble", "Gato": "Cat",
    "Blanco": "white", "Coral": "coral", "Lila": "lilac", "Celeste": "sky blue", "Menta": "mint",
    "Amarillo": "yellow", "Verde": "green", "Morado": "purple", "Azul": "blue", "Naranja": "orange", "Rosa": "pink",
    # widgets
    "Música": "Music", "Hoy: clima, fecha y cita": "Today: weather, date and event",
    "Sistema: CPU, RAM y batería": "System: CPU, RAM and battery",
    # atajos
    "Mostrar u ocultar la isla": "Show or hide the island", "Empezar o terminar el enfoque": "Start or stop focus",
    "Pausar o seguir la música": "Play or pause music", "Abrir los ajustes": "Open settings",
    # tarjetas
    "Captura copiada": "Screenshot copied", "{w} × {h} · pégala con Ctrl+V": "{w} × {h} · paste it with Ctrl+V",
    "Captura guardada": "Screenshot saved", "Imágenes › Capturas › {name}": "Pictures › Capturas › {name}",
    "Abrir": "Open", "Guardar": "Save", "Copiado": "Copied",
    "{app} está usando el micrófono": "{app} is using the microphone",
    "{app} está usando la cámara": "{app} is using the camera",
    "Verás un punto junto a la isla mientras siga": "You'll see a dot next to the island while it lasts",
    "Silenciar": "Mute", "Activar": "Unmute", "Micrófono silenciado": "Microphone muted",
    "Micrófono activado": "Microphone on",
    "A concentrarse": "Time to focus", "{minutes} minutos · ronda {round}": "{minutes} minutes · round {round}",
    "¡Descanso!": "Break time!", "¡Descanso largo!": "Long break!", "Terminar": "Stop",
    "{minutes} minutos · terminaste la ronda {round}": "{minutes} minutes · you finished round {round}",
    "Cargando": "Charging", "Cargador desconectado": "Charger unplugged", "Batería al {pct}%": "Battery at {pct}%",
    "Ahora el sonido sale por acá": "Sound now plays here",
    "{name} conectado": "{name} connected", "{name} desconectado": "{name} disconnected",
    "Unidad {letter}:{free} · clic para abrir": "Drive {letter}:{free} · click to open",
    " · {gb} GB libres": " · {gb} GB free", "Unidad {letter}: · ya puedes guardarla": "Drive {letter}: · safe to remove",
    "Memoria USB": "USB drive",
    "Nueva versión {version}": "New version {version}", "Isla dinámica se puede actualizar": "An update is available",
    "Descargando… se reinicia sola al terminar": "Downloading… it restarts by itself when done",
    "No se pudo descargar; prueba más tarde": "Couldn't download; try again later",
    "Luego": "Later", "Actualizar": "Update",
    "Empieza ahora · {time}": "Starts now · {time}", "Empieza en {mins} min · {time}": "Starts in {mins} min · {time}",
    # isla
    "⏰  ¡Tiempo!": "⏰  Time's up!", "en {mins} min": "in {mins} min", "ahora": "now", "Batería": "Battery",
    "Sin citas por hoy": "No more events today", "Nada sonando": "Nothing playing",
    "Sin sesiones activas": "No active sessions", "abre claude en una terminal": "open claude in a terminal",
    "Responder": "Reply", "Claude ›": "Claude ›", "Enter envía · Esc cancela": "Enter sends · Esc cancels",
    "¿Permitir? {text}": "Allow? {text}", "Permitir": "Allow", "Denegar": "Deny",
    "Enfoque {left}": "Focus {left}", "Descanso {left}": "Break {left}", "{n} min": "{n} min",
    "{status} en {time}": "{status} in {time}",
    # Claude Code
    "Editando {name}": "Editing {name}", "Escribiendo {name}": "Writing {name}", "Leyendo {name}": "Reading {name}",
    "Ejecutando comando": "Running a command", "Buscando": "Searching", "Navegando": "Browsing",
    "Delegando a un agente": "Delegating to an agent", "Usando {tool}": "Using {tool}", "Pensando…": "Thinking…",
    "Sesión iniciada": "Session started", "Listo ✓": "Done ✓", "Te necesita": "Needs you",
    "Denegado desde la isla dinámica": "Denied from the island",
    # ajustes
    "Ajustes · Isla dinámica": "Settings · Isla dinámica", "Ajustes": "Settings",
    "Los cambios se ven al instante en la isla.": "Changes show up on the island right away.",
    "Color del personaje": "Character color", "Isla": "Island", "Color de acento": "Accent color",
    "Tamaño": "Size", "Normal": "Normal", "Grande": "Large", "Muy grande": "Extra large", "Pantalla": "Screen",
    "Principal": "Main", "La del mouse": "Follow the mouse", "Pantalla {n}": "Screen {n}", "Idioma": "Language",
    "Isla abierta": "Open island", "  (no se ve en modo compañero)": "  (hidden in companion mode)",
    "Notificaciones de Windows": "Windows notifications", "Próxima cita del calendario": "Next calendar event",
    "Vista previa de capturas de pantalla": "Screenshot preview", "Mostrar el texto que copias": "Show copied text",
    "Avisar cuando se usa el micrófono o la cámara": "Alert when the microphone or camera is in use",
    "Isla translúcida": "Translucent island",
    "Ocultar con juegos y videos en pantalla completa": "Hide during full-screen games and videos",
    "Atajos de teclado": "Keyboard shortcuts",
    "Clic en un atajo y presiona la combinación (Esc cancela, Retroceso lo borra)":
        "Click a shortcut and press the keys (Esc cancels, Backspace clears)",
    "Presiona las teclas…": "Press the keys…", "Sin asignar": "Not set", "Listo.": "Done.",
    "Esa combinación ya la usa otra app: elige otra.": "Another app already uses that combination: pick another.",
    "Versión {version} · código abierto": "Version {version} · open source", "Listo": "Done",
    # instalador y desinstalador
    "Instalar Isla dinámica": "Install Isla dinámica",
    "Una píldora arriba de la pantalla con Claude Code, música,\nnotificaciones, volumen, brillo y temporizador.":
        "A pill at the top of your screen with Claude Code, music,\nnotifications, volume, brightness and a timer.",
    "Conectar con Claude Code (permisos y estado en la isla)":
        "Connect to Claude Code (permissions and status on the island)",
    "Se instala en {dest}\nArranca sola con Windows (se puede apagar con clic derecho).":
        "Installs to {dest}\nStarts with Windows (you can turn it off with a right-click).",
    "Instalar": "Install", "Reintentar": "Retry", "Cerrar": "Close",
    "Cerrando la isla si estaba abierta…": "Closing the island if it was open…", "Copiando archivos…": "Copying files…",
    "Configurando…": "Setting up…", "No se pudo instalar: {error}": "Couldn't install: {error}",
    "¡Listo! La isla ya está arriba de tu pantalla.": "Done! The island is at the top of your screen.",
    "La isla dinámica se desinstaló.": "Isla dinámica was uninstalled.",
}
