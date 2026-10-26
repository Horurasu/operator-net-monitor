#!/usr/bin/env python3
"""Consumo de Red — consumo diario por cable / WiFi y sitios visitados.

  python3 consumo_red.py              abre la ventana (y arranca el registro si no corre)
  python3 consumo_red.py --registrar  registro en segundo plano, sin ventana

Funciona en Linux y Windows. Todo se guarda en una base SQLite local."""
import calendar
import csv
import datetime as dt
import json
import os
import queue
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time

try:
    import psutil
except ImportError:
    psutil = None

ES_WIN = os.name == "nt"
PUERTO = 47653                      # candado: solo un registrador a la vez
NO_VENTANA = 0x08000000 if ES_WIN else 0

DIAS = ["LUN", "MAR", "MIÉ", "JUE", "VIE", "SÁB", "DOM"]
DIAS_LARGO = ["LUNES", "MARTES", "MIÉRCOLES", "JUEVES", "VIERNES", "SÁBADO", "DOMINGO"]
MESES = ["", "ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO", "JULIO",
         "AGOSTO", "SEPTIEMBRE", "OCTUBRE", "NOVIEMBRE", "DICIEMBRE"]

# Paleta ochentera: fondo noche, neón
BG = "#0e0b16"
LINEA = "#2a2340"
TXT = "#e8e3f5"
SUB = "#7d7596"
CIAN = "#2de2e6"
ROSA = "#ff3cac"
AMAR = "#f9c80e"
LILA = "#9d8cff"
APAGADO = "#4a4360"      # días que aún no llegan o sin registro
FINDE = "#17122a"        # fondo de sábado y domingo en la gráfica
# Cada color significa una sola cosa: cian = cable, rosa = WiFi, lila = otra, amarillo = hoy
COLOR_TIPO = {"cable": CIAN, "wifi": ROSA, "otra": LILA}
SOMBRA = {"cable": "#178e91", "wifi": "#a8226f", "otra": "#5f5599"}   # subida en "solo X"
COL = 46                 # ancho de cada día en la gráfica


# ------------------------------------------------------------- almacén ----
def carpeta_datos():
    if ES_WIN:
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ConsumoRed")
    return os.path.expanduser("~/.local/share/consumo-red")


CARPETA = carpeta_datos()
RUTA_DB = os.path.join(CARPETA, "consumo.db")
CARPETA_CSV_VIEJA = os.path.join(CARPETA, "sitios")   # registros de la versión anterior


def abrir_db():
    os.makedirs(CARPETA, exist_ok=True)
    db = sqlite3.connect(RUTA_DB, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS trafico (fecha TEXT, itf TEXT, rx INTEGER, tx INTEGER,
                                            PRIMARY KEY (fecha, itf));
        CREATE TABLE IF NOT EXISTS sitios (id INTEGER PRIMARY KEY, ts REAL, dominio TEXT,
                                           base TEXT, servicio TEXT, via TEXT, ip TEXT);
        CREATE INDEX IF NOT EXISTS sitios_ts ON sitios(ts);
        CREATE TABLE IF NOT EXISTS estado (clave TEXT PRIMARY KEY, valor TEXT);
    """)
    return db


def leer_estado(db, clave):
    fila = db.execute("SELECT valor FROM estado WHERE clave = ?", (clave,)).fetchone()
    return fila[0] if fila else ""


def tamano(b):
    if b <= 0:
        return "0"
    if b >= 1e9:
        return f"{b / 1e9:.2f} GB"
    if b >= 1e6:
        return f"{b / 1e6:.1f} MB"
    return f"{b / 1e3:.0f} KB"


def corto(b):
    """Tamaño en 4-5 caracteres para encima de las barras: 1.2G, 143M, 279K."""
    if b >= 1e9:
        return f"{b / 1e9:.1f}G" if b < 1e10 else f"{b / 1e9:.0f}G"
    if b >= 1e6:
        return f"{b / 1e6:.0f}M"
    return f"{b / 1e3:.0f}K"


def barrita(valor, maximo, ancho=12):
    """Barra de texto proporcional: ████▌"""
    if maximo <= 0 or valor <= 0:
        return ""
    octavos = round(ancho * 8 * valor / maximo)
    return "█" * (octavos // 8) + " ▏▎▍▌▋▊▉"[octavos % 8].strip()


# ---------------------------------------------------------- interfaces ----
VIRTUALES = ("docker", "veth", "br-", "virbr", "vmnet", "vboxnet", "tun", "tap", "wg",
             "tailscale", "zt", "vethernet", "isatap", "teredo", "bluetooth", "npcap")


def tipo_itf(nombre):
    """'cable', 'wifi', 'otra', o None si es virtual y no se cuenta."""
    n = nombre.lower()
    if (n == "lo" or n.startswith(VIRTUALES) or "*" in n
            or any(k in n for k in ("loopback", "virtual", "pseudo"))):
        return None
    sysfs = "/sys/class/net/" + nombre
    if not ES_WIN and os.path.isdir(sysfs):
        if os.path.exists(sysfs + "/wireless") or os.path.exists(sysfs + "/phy80211"):
            return "wifi"
        if not os.path.exists(sysfs + "/device"):
            return None
    if n.startswith("wl") or any(k in n for k in ("wi-fi", "wifi", "wireless", "inalámbric",
                                                   "inalambric")):
        return "wifi"
    if n.startswith(("en", "eth")) or any(k in n for k in ("ethernet", "área local",
                                                            "area local", "local area")):
        return "cable"
    return "otra"


def leer_contadores():
    """{interfaz: (bytes_recibidos, bytes_enviados)} acumulados desde el arranque."""
    if psutil:
        return {n: (c.bytes_recv, c.bytes_sent)
                for n, c in psutil.net_io_counters(pernic=True).items()}
    if os.path.exists("/proc/net/dev"):
        res = {}
        with open("/proc/net/dev") as f:
            for linea in f.readlines()[2:]:
                nombre, _, resto = linea.partition(":")
                v = resto.split()
                res[nombre.strip()] = (int(v[0]), int(v[8]))
        return res
    if ES_WIN:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-NetAdapterStatistics | Select-Object Name,ReceivedBytes,"
                              "SentBytes | ConvertTo-Json -Compress"],
                             capture_output=True, text=True, timeout=30, creationflags=NO_VENTANA)
        j = json.loads(out.stdout or "[]")
        j = [j] if isinstance(j, dict) else j
        return {x["Name"]: (int(x["ReceivedBytes"]), int(x["SentBytes"])) for x in j}
    return {}


def leer_vnstat():
    """Historial de vnstat si está instalado (Linux): {itf: {fecha: (rx, tx)}}."""
    if not shutil.which("vnstat"):
        return {}
    try:
        out = subprocess.run(["vnstat", "--json", "d"], capture_output=True, text=True, timeout=20)
        j = json.loads(out.stdout)
    except Exception:
        return {}
    factor = 1024 if str(j.get("jsonversion", "2")) == "1" else 1  # vnstat 1.x usa KiB
    datos = {}
    for itf in j.get("interfaces", []):
        tr = itf.get("traffic", {})
        dias = {}
        for x in tr.get("day") or tr.get("days") or []:
            f = x["date"]
            dias[dt.date(f["year"], f["month"], f["day"])] = (x["rx"] * factor, x["tx"] * factor)
        datos[itf.get("name") or itf.get("id")] = dias
    return datos


# -------------------------------------------------------------- sitios ----
# (terminaciones de dominio, servicio, color del icono, letra, color de la letra)
SERVICIOS = [
    (("youtube.com", "googlevideo.com", "ytimg.com", "youtu.be", "youtube-nocookie.com",
      "yt3.ggpht.com"), "YouTube", "#ff0000", "▶", "#ffffff"),
    (("netflix.com", "nflxvideo.net", "nflximg.net", "nflxext.com", "nflxso.net"),
     "Netflix", "#141414", "N", "#e50914"),
    (("spotify.com", "scdn.co", "spotifycdn.com", "spotilocal.com"), "Spotify", "#1db954", "S", "#000000"),
    (("facebook.com", "fbcdn.net", "facebook.net", "fb.com", "messenger.com"),
     "Facebook", "#1877f2", "F", "#ffffff"),
    (("instagram.com", "cdninstagram.com"), "Instagram", "#e1306c", "I", "#ffffff"),
    (("whatsapp.com", "whatsapp.net", "wa.me"), "WhatsApp", "#25d366", "W", "#ffffff"),
    (("tiktok.com", "tiktokcdn.com", "tiktokv.com", "byteoversea.com", "ibytedtos.com",
      "tiktokcdn-us.com"), "TikTok", "#111111", "T", "#25f4ee"),
    (("twitter.com", "x.com", "twimg.com", "t.co"), "X / Twitter", "#222222", "X", "#ffffff"),
    (("twitch.tv", "ttvnw.net", "jtvnw.net"), "Twitch", "#9146ff", "T", "#ffffff"),
    (("primevideo.com", "aiv-cdn.net", "media-amazon.com"), "Prime Video", "#00a8e1", "P", "#ffffff"),
    (("disneyplus.com", "disney-plus.net", "dssott.com", "bamgrid.com"), "Disney+", "#113ccf", "D", "#ffffff"),
    (("max.com", "hbomax.com", "hbo.com"), "Max", "#002be7", "M", "#ffffff"),
    (("zoom.us", "zoom.com"), "Zoom", "#2d8cff", "Z", "#ffffff"),
    (("teams.microsoft.com", "teams.live.com", "skype.com"), "Teams", "#6264a7", "T", "#ffffff"),
    (("meet.google.com",), "Google Meet", "#00897b", "M", "#ffffff"),
    (("gmail.com", "mail.google.com"), "Gmail", "#ea4335", "M", "#ffffff"),
    (("drive.google.com", "docs.google.com", "googleusercontent.com"), "Google Drive/Docs", "#fbbc04", "D", "#000000"),
    (("claude.ai", "anthropic.com", "claudeusercontent.com"), "Claude", "#d97757", "C", "#ffffff"),
    (("chatgpt.com", "openai.com", "oaistatic.com", "oaiusercontent.com"), "ChatGPT", "#10a37f", "G", "#ffffff"),
    (("github.com", "githubusercontent.com", "githubassets.com"), "GitHub", "#24292f", "G", "#ffffff"),
    (("ubuntu.com", "canonical.com", "snapcraft.io", "launchpad.net", "debian.org",
      "archlinux.org", "fedoraproject.org", "windowsupdate.com", "update.microsoft.com"),
     "Actualizaciones", "#e95420", "U", "#ffffff"),
    (("steampowered.com", "steamcontent.com", "steamstatic.com", "steamserver.net"), "Steam", "#1b2838", "S", "#66c0f4"),
    (("discord.com", "discord.gg", "discordapp.com", "discordapp.net"), "Discord", "#5865f2", "D", "#ffffff"),
    (("office.com", "office365.com", "live.com", "outlook.com", "sharepoint.com",
      "onedrive.com"), "Microsoft 365", "#d83b01", "O", "#ffffff"),
    (("amazon.com", "amazon.com.mx", "amazon.es", "ssl-images-amazon.com"), "Amazon", "#ff9900", "A", "#000000"),
    (("mercadolibre.com", "mercadolibre.com.mx", "mercadolibre.com.ar", "mlstatic.com",
      "mercadopago.com"), "Mercado Libre", "#ffe600", "M", "#2d3277"),
    (("wikipedia.org", "wikimedia.org"), "Wikipedia", "#eeeeee", "W", "#000000"),
    (("reddit.com", "redd.it", "redditmedia.com", "redditstatic.com"), "Reddit", "#ff4500", "R", "#ffffff"),
    (("linkedin.com", "licdn.com"), "LinkedIn", "#0a66c2", "L", "#ffffff"),
    (("pinterest.com", "pinimg.com"), "Pinterest", "#e60023", "P", "#ffffff"),
    (("telegram.org", "t.me", "telegram.me"), "Telegram", "#26a5e4", "T", "#ffffff"),
    (("apple.com", "icloud.com", "mzstatic.com"), "Apple", "#555555", "A", "#ffffff"),
    (("roblox.com", "rbxcdn.com"), "Roblox", "#e2231a", "R", "#ffffff"),
    (("epicgames.com", "unrealengine.com", "fortnite.com"), "Epic Games", "#2a2a2a", "E", "#ffffff"),
    (("snapchat.com", "sc-cdn.net"), "Snapchat", "#fffc00", "S", "#000000"),
    (("google.com", "gstatic.com", "googleapis.com", "google.com.mx"), "Google", "#4285f4", "G", "#ffffff"),
]
ICONO_DE = {s[1]: s[2:] for s in SERVICIOS}

SLD_PAISES = {"com", "co", "org", "net", "gob", "gov", "edu", "ac", "ne", "or"}


def servicio_de(dominio):
    d = dominio.lower().rstrip(".")
    for terminaciones, nombre, *_ in SERVICIOS:
        for t in terminaciones:
            if d == t or d.endswith("." + t):
                return nombre
    for clave, nombre in (("spotify", "Spotify"), ("netflix", "Netflix"), ("tiktok", "TikTok"),
                          ("youtube", "YouTube"), ("whatsapp", "WhatsApp")):
        if clave in d:
            return nombre
    return ""


def dominio_base(dominio):
    partes = dominio.lower().rstrip(".").split(".")
    if len(partes) >= 3 and len(partes[-1]) == 2 and partes[-2] in SLD_PAISES:
        base = partes[-3:]
    else:
        base = partes[-2:]
    if base[0] == "www" and len(base) > 2:
        base = base[1:]
    return ".".join(base)


def parsear_tshark(linea):
    campos = (linea.rstrip("\n").split("\t") + [""] * 6)[:6]
    ts, dns, sni, ip4, ip6, udp = campos
    dominio = sni or dns
    if not dominio:
        return None
    try:
        t = float(ts)
    except ValueError:
        t = time.time()
    via = "DNS" if not sni else ("QUIC" if udp else "HTTPS")
    return {"ts": t, "dominio": dominio, "via": via, "ip": "" if via == "DNS" else (ip4 or ip6)}


# ---------------------------------------------------------- registrador ----
def tomar_candado():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", PUERTO))
    except OSError:
        s.close()
        return None
    s.listen(8)

    def atender():
        while True:
            conn, _ = s.accept()
            conn.close()
    threading.Thread(target=atender, daemon=True).start()
    return s


def registrador_activo():
    try:
        with socket.create_connection(("127.0.0.1", PUERTO), timeout=0.5):
            return True
    except OSError:
        return False


def lanzar_registrador():
    exe = sys.executable
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if ES_WIN:
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(w):
            exe = w
        kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen([exe, os.path.abspath(__file__), "--registrar"], **kw)


class Registrador:
    """Corre siempre en segundo plano: suma el tráfico de cada interfaz por día y guarda
    cada sitio al que se conecta la máquina. La ventana solo lee lo que esto escribe."""

    def __init__(self):
        self.db = abrir_db()
        self.cola = queue.Queue()
        self.ultimos = {}           # dominio -> ts (para no repetir el mismo en 3 s)
        self.pendiente = {}         # (fecha, itf) -> [rx, tx]
        self.previo = None

    def correr(self):
        candado = tomar_candado()
        if not candado:
            return                  # ya hay otro registrando
        if not ES_WIN:
            signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
        self.importar_csv_viejos()
        threading.Thread(target=self.hilo_sitios, daemon=True).start()
        prox_muestra = prox_guardado = 0
        try:
            while True:
                ahora = time.time()
                if ahora >= prox_muestra:
                    self.muestrear()
                    prox_muestra = ahora + 10
                if ahora >= prox_guardado:
                    self.guardar()
                    prox_guardado = ahora + 30
                self.drenar()
                time.sleep(1)
        finally:
            self.guardar()

    # ---- tráfico ----
    def muestrear(self):
        try:
            actual = {n: v for n, v in leer_contadores().items() if tipo_itf(n)}
        except Exception as e:
            self.cola.put(("estado", "trafico_error", str(e)))
            return
        if self.previo is not None:
            hoy = dt.date.today().isoformat()
            for n, (rx, tx) in actual.items():
                p = self.previo.get(n)
                if not p:
                    continue
                # Si el contador bajó, la interfaz se reinició: lo leído es lo nuevo
                drx = rx - p[0] if rx >= p[0] else rx
                dtx = tx - p[1] if tx >= p[1] else tx
                acc = self.pendiente.setdefault((hoy, n), [0, 0])
                acc[0] += drx
                acc[1] += dtx
        self.previo = actual

    def guardar(self):
        self.db.executemany(
            "INSERT INTO trafico VALUES (?, ?, ?, ?) ON CONFLICT(fecha, itf) "
            "DO UPDATE SET rx = rx + excluded.rx, tx = tx + excluded.tx",
            [(f, n, rx, tx) for (f, n), (rx, tx) in self.pendiente.items()])
        self.db.execute("INSERT OR REPLACE INTO estado VALUES ('latido', ?)", (str(time.time()),))
        self.db.commit()
        self.pendiente.clear()

    # ---- sitios ----
    def drenar(self):
        hubo = False
        while True:
            try:
                item = self.cola.get_nowait()
            except queue.Empty:
                break
            if item[0] == "estado":
                self.db.execute("INSERT OR REPLACE INTO estado VALUES (?, ?)", item[1:])
            else:
                self.guardar_sitio(item[1])
            hubo = True
        if hubo:
            self.db.commit()

    def guardar_sitio(self, s):
        dominio = s["dominio"].strip().lower().rstrip(".")
        if not dominio or "." not in dominio or dominio.endswith((".arpa", ".local", ".lan")):
            return
        t = s["ts"]
        if t - self.ultimos.get(dominio, 0) < 3:
            return
        if len(self.ultimos) > 5000:
            self.ultimos = {d: x for d, x in self.ultimos.items() if t - x < 3}
        self.ultimos[dominio] = t
        self.db.execute("INSERT INTO sitios (ts, dominio, base, servicio, via, ip) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (t, dominio, dominio_base(dominio), servicio_de(dominio), s["via"], s["ip"]))

    def estado(self, clave, valor):
        self.cola.put(("estado", clave, valor))

    def hilo_sitios(self):
        while True:
            try:
                if ES_WIN:
                    self.sitios_cache_dns()
                else:
                    self.sitios_tshark()
            except Exception as e:
                self.estado("captura_error", str(e))
            time.sleep(60)

    def sitios_tshark(self):
        if not shutil.which("tshark"):
            self.estado("captura_error", "Falta tshark. Corre instalar.sh para instalarlo.")
            return
        cmd = ["tshark", "-l", "-n", "-Q", "-i", "any",
               "-f", "port 53 or port 443 or port 853",
               "-Y", "dns.flags.response == 0 or tls.handshake.type == 1",
               "-T", "fields", "-E", "separator=\t", "-E", "occurrence=f",
               "-e", "frame.time_epoch", "-e", "dns.qry.name",
               "-e", "tls.handshake.extensions_server_name",
               "-e", "ip.dst", "-e", "ipv6.dst", "-e", "udp.srcport"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
        self.estado("captura", "tshark · DNS + HTTPS + QUIC")
        self.estado("captura_error", "")
        errores = []
        threading.Thread(target=lambda: errores.extend(proc.stderr), daemon=True).start()
        for linea in proc.stdout:
            s = parsear_tshark(linea)
            if s:
                self.cola.put(("sitio", s))
        proc.wait()
        msg = " ".join(l.strip() for l in errores if l.strip())
        if "ermission" in msg or "denied" in msg:
            msg = "Sin permiso para capturar. Corre instalar.sh y vuelve a iniciar sesión."
        self.estado("captura_error", msg or "tshark se detuvo; reintentando…")

    def sitios_cache_dns(self):
        """Windows: lee la caché DNS del sistema (sin instalar nada ni ser administrador).
        En 'ipconfig /displaydns' el nombre va justo antes de cada línea de guiones."""
        self.estado("captura", "caché DNS de Windows")
        self.estado("captura_error", "")
        vistos = set()
        while True:
            out = subprocess.run(["ipconfig", "/displaydns"], capture_output=True,
                                 timeout=60, creationflags=NO_VENTANA)
            lineas = out.stdout.decode("ascii", "ignore").splitlines()
            actuales = set()
            for i, l in enumerate(lineas):
                if l.strip().startswith("-----") and i > 0 and lineas[i - 1].strip():
                    actuales.add(lineas[i - 1].strip().lower())
            for d in actuales - vistos:
                self.cola.put(("sitio", {"ts": time.time(), "dominio": d, "via": "DNS", "ip": ""}))
            vistos = actuales
            time.sleep(15)

    def importar_csv_viejos(self):
        if leer_estado(self.db, "csv_importado") or not os.path.isdir(CARPETA_CSV_VIEJA):
            return
        for nombre in sorted(os.listdir(CARPETA_CSV_VIEJA)):
            if not nombre.endswith(".csv"):
                continue
            with open(os.path.join(CARPETA_CSV_VIEJA, nombre), encoding="utf-8") as fh:
                for fila in csv.DictReader(fh):
                    try:
                        t = dt.datetime.strptime(fila["fecha_hora"], "%Y-%m-%d %H:%M:%S").timestamp()
                    except (KeyError, ValueError):
                        continue
                    d = fila.get("dominio", "")
                    self.db.execute("INSERT INTO sitios (ts, dominio, base, servicio, via, ip) "
                                    "VALUES (?, ?, ?, ?, ?, ?)",
                                    (t, d, dominio_base(d), servicio_de(d),
                                     fila.get("via", ""), fila.get("ip", "")))
        self.db.execute("INSERT OR REPLACE INTO estado VALUES ('csv_importado', '1')")
        self.db.commit()


# ============================================================== ventana ====
import tkinter as tk                                    # noqa: E402
from tkinter import ttk, messagebox, filedialog, font   # noqa: E402

F = {}   # fuentes, se llenan al crear la ventana

# Letras de 5x7 para los iconos pixelados
GLIFOS = {
    "A": ".###.#...##...#######...##...##...#", "B": "####.#...##...#####.#...##...#####.",
    "C": ".###.#...##....#....#....#...#.###.", "D": "####.#...##...##...##...##...#####.",
    "E": "######....#....####.#....#....#####", "F": "######....#....####.#....#....#....",
    "G": ".###.#...##....#.####...##...#.###.", "H": "#...##...##...#######...##...##...#",
    "I": ".###...#....#....#....#....#...###.", "J": "..###...#....#....#....#.#..#..##..",
    "K": "#...##..#.#.#..##...#.#..#..#.#...#", "L": "#....#....#....#....#....#....#####",
    "M": "#...###.###.#.##.#.##...##...##...#", "N": "#...###..##.#.##..###...##...##...#",
    "O": ".###.#...##...##...##...##...#.###.", "P": "####.#...##...#####.#....#....#....",
    "Q": ".###.#...##...##...##.#.##..#..##.#", "R": "####.#...##...#####.#.#..#..#.#...#",
    "S": ".#####....#.....###.....#....#####.", "T": "#####..#....#....#....#....#....#..",
    "U": "#...##...##...##...##...##...#.###.", "V": "#...##...##...##...##...#.#.#...#..",
    "W": "#...##...##...##.#.##.#.##.#.#.#.#.", "X": "#...##...#.#.#...#...#.#.#...##...#",
    "Y": "#...##...#.#.#...#....#....#....#..", "Z": "#####....#...#...#...#...#....#####",
    "?": ".###.#...#....#...#...#.........#..", "▶": "#....##...###..####.###..##...#....",
}


def crear_icono(fondo, letra, color):
    """Icono de 16x16 estilo pixel: cuadro de color con una letra de 5x7 al doble."""
    g = GLIFOS.get(letra, GLIFOS["?"])
    filas = []
    for y in range(16):
        fila = []
        for x in range(16):
            gx, gy = (x - 3) // 2, (y - 1) // 2
            on = 0 <= gx < 5 and 0 <= gy < 7 and x >= 3 and y >= 1 and g[gy * 5 + gx] == "#"
            fila.append(color if on else fondo)
        filas.append("{" + " ".join(fila) + "}")
    img = tk.PhotoImage(width=16, height=16)
    img.put(" ".join(filas))
    return img


class Iconos:
    def __init__(self):
        self.cache = {}

    def de(self, servicio, base):
        if servicio in ICONO_DE:
            clave = ("s", servicio)
            fondo, letra, color = ICONO_DE[servicio]
        else:
            letra = (base[:1] or "?").upper()
            letra = letra if letra in GLIFOS else "?"
            clave = ("b", letra)
            fondo, color = LINEA, SUB
        if clave not in self.cache:
            self.cache[clave] = crear_icono(fondo, letra, color)
        return self.cache[clave]


def boton(padre, texto, cmd):
    b = tk.Label(padre, text=texto, bg=BG, fg=TXT, font=F["chico"], padx=10, pady=3,
                 cursor="hand2", highlightthickness=1, highlightbackground=LINEA)
    b.bind("<Button-1>", lambda e: cmd())
    b.bind("<Enter>", lambda e: b.configure(fg=CIAN, highlightbackground=CIAN))
    b.bind("<Leave>", lambda e: b.configure(fg=TXT, highlightbackground=LINEA))
    return b


class Selector(tk.Frame):
    """Botones de opción pegados: [ TODO ][ CABLE ][ WIFI ]."""

    def __init__(self, padre, opciones, al_cambiar, colores=None):
        super().__init__(padre, bg=LINEA, padx=1, pady=1)
        self.al_cambiar = al_cambiar
        self.colores = colores or {}
        self.botones = {}
        self.valor = opciones[0][0]
        for i, (clave, texto) in enumerate(opciones):
            b = tk.Label(self, text=texto, font=F["chico"], padx=12, pady=3, cursor="hand2")
            b.grid(row=0, column=i, padx=(0 if i == 0 else 1, 0))
            b.bind("<Button-1>", lambda e, c=clave: self.elegir(c))
            self.botones[clave] = b
        self.pintar()

    def elegir(self, clave):
        self.valor = clave
        self.pintar()
        self.al_cambiar()

    def poner(self, clave):
        """Marca una opción sin avisar (para sincronizar con otro control)."""
        self.valor = clave
        self.pintar()

    def pintar(self):
        for clave, b in self.botones.items():
            if clave == self.valor:
                b.configure(bg=self.colores.get(clave, CIAN), fg=BG)
            else:
                b.configure(bg=BG, fg=SUB)


def separador(padre):
    tk.Frame(padre, bg=LINEA, height=1).pack(fill="x", padx=20)


def texto_rico(marco, partes, fuente=None):
    """Rellena un Frame con trozos de texto de distintos colores: [(texto, color)]."""
    for w in marco.winfo_children():
        w.destroy()
    for texto, color in partes:
        tk.Label(marco, text=texto, bg=BG, fg=color, font=fuente or F["chico"], padx=0,
                 bd=0).pack(side="left")


def estilos(raiz):
    fams = set(font.families(raiz))
    mono = next((f for f in ("JetBrains Mono", "IBM Plex Mono", "DejaVu Sans Mono", "Consolas",
                             "Liberation Mono", "Ubuntu Mono", "Courier New") if f in fams), "Courier")
    F.update(base=(mono, 10), chico=(mono, 9), mini=(mono, 8), titulo=(mono, 15, "bold"),
             grande=(mono, 17, "bold"))
    s = ttk.Style(raiz)
    s.theme_use("clam")
    s.configure(".", background=BG, foreground=TXT, font=F["base"])
    s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    s.configure("Treeview", background=BG, fieldbackground=BG, foreground=TXT,
                rowheight=22, borderwidth=0, font=F["base"])
    s.configure("Treeview.Heading", background=BG, foreground=SUB, relief="flat",
                font=F["mini"], borderwidth=0)
    s.map("Treeview.Heading", background=[("active", BG)], foreground=[("active", CIAN)])
    s.map("Treeview", background=[("selected", LINEA)], foreground=[("selected", CIAN)])
    for barra in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        s.configure(barra, troughcolor=BG, background=LINEA, bordercolor=BG,
                    arrowcolor=SUB, lightcolor=LINEA, darkcolor=LINEA, gripcount=0)
        s.map(barra, background=[("active", SUB)])


def tabla(padre, columnas, icono=False):
    """columnas: [(clave, título, ancho, alineación)]"""
    marco = tk.Frame(padre, bg=BG)
    t = ttk.Treeview(marco, columns=[c[0] for c in columnas],
                     show="tree headings" if icono else "headings")
    if icono:
        t.column("#0", width=34, stretch=False)
    for c, txt, w, a in columnas:
        t.heading(c, text=txt, anchor=a)
        t.column(c, width=w, anchor=a)
    sb = ttk.Scrollbar(marco, orient="vertical", command=t.yview)
    t.configure(yscrollcommand=sb.set)
    t.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    return marco, t


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Consumo de Red")
        self.geometry("980x820")
        self.minsize(760, 640)
        self.configure(bg=BG)
        estilos(self)
        self.db = abrir_db()
        self.por_tipo = {}
        self.itfs = {}
        self.dias = []                  # todos los días del mes mostrado
        self.pico = None
        self.inicio = dt.date.today()
        hoy = dt.date.today()
        self.mes = (hoy.year, hoy.month)
        self.sel = hoy                  # día seleccionado en gráfica y tabla
        self.orden = ("fecha", True)    # (columna, de mayor a menor)
        self.x_ini = 0
        self.dia_detalle = None
        self.parpadeo = False
        if not registrador_activo():
            try:
                lanzar_registrador()
            except Exception as e:
                messagebox.showwarning("Consumo de Red", f"No se pudo arrancar el registro:\n{e}")
        self._construir()
        self.bind("<Left>", lambda e: self.mover_sel(-1))
        self.bind("<Right>", lambda e: self.mover_sel(1))
        self.bind("<Home>", lambda e: self.ir_hoy())
        self.cargar()
        self.after(30000, self.ciclo)
        self.after(700, self.latir)

    def _construir(self):
        top = tk.Frame(self, bg=BG, padx=20, pady=16)
        top.pack(fill="x")
        tk.Label(top, text="CONSUMO DE RED", bg=BG, fg=TXT, font=F["titulo"]).pack(side="left")
        tk.Label(top, text="▂▃▅▇", bg=BG, fg=ROSA, font=F["base"]).pack(side="left", padx=8)
        boton(top, "SITIOS ▸", self.abrir_sitios).pack(side="right")
        boton(top, "? AYUDA", self.ayuda).pack(side="right", padx=8)
        self.rec = tk.Label(top, text="● REGISTRANDO", bg=BG, fg=ROSA, font=F["chico"])
        self.rec.pack(side="right", padx=8)
        separador(self)

        # Qué conexión ver + mes
        nav = tk.Frame(self, bg=BG, padx=20, pady=12)
        nav.pack(fill="x")
        tk.Label(nav, text="VER", bg=BG, fg=SUB, font=F["mini"]).pack(side="left", padx=(0, 8))
        self.vista = Selector(nav, [("todo", "CABLE + WIFI"), ("cable", "SOLO CABLE"),
                                    ("wifi", "SOLO WIFI")], self.mostrar,
                              {"todo": TXT, **COLOR_TIPO})
        self.vista.pack(side="left")
        boton(nav, "HOY", self.ir_hoy).pack(side="right", padx=(12, 0))
        self.btn_sig = tk.Label(nav, bg=BG, font=F["chico"], cursor="hand2")
        self.btn_sig.pack(side="right")
        self.lbl_mes = tk.Label(nav, bg=BG, fg=TXT, font=F["base"], width=16)
        self.lbl_mes.pack(side="right")
        self.btn_ant = tk.Label(nav, bg=BG, font=F["chico"], cursor="hand2")
        self.btn_ant.pack(side="right")
        self.btn_ant.bind("<Button-1>", lambda e: self.mover_mes(-1))
        self.btn_sig.bind("<Button-1>", lambda e: self.mover_mes(1))

        # Cifras del mes
        cifras = tk.Frame(self, bg=BG, padx=20, pady=4)
        cifras.pack(fill="x")
        self.v, self.sub = {}, {}
        for i, (clave, titulo) in enumerate([("total", "TOTAL DEL MES"), ("rx", "BAJADA ↓"),
                                             ("tx", "SUBIDA ↑"), ("prom", "PROMEDIO DIARIO"),
                                             ("max", "DÍA PICO ★")]):
            c = tk.Frame(cifras, bg=BG)
            c.grid(row=0, column=i, sticky="nw")
            cifras.columnconfigure(i, weight=1)
            tk.Label(c, text=titulo, bg=BG, fg=SUB, font=F["mini"]).pack(anchor="w")
            self.v[clave] = tk.Label(c, text="—", bg=BG, fg=TXT, font=F["grande"])
            self.v[clave].pack(anchor="w")
            self.sub[clave] = tk.Frame(c, bg=BG)
            self.sub[clave].pack(anchor="w")
        self.v["max"].configure(cursor="hand2")
        self.v["max"].bind("<Button-1>", lambda e: self.pico and self.seleccionar(self.pico))

        # Detalle del día bajo el cursor / seleccionado
        self.detalle = tk.Frame(self, bg=BG, padx=20)
        self.detalle.pack(fill="x", pady=(14, 0))

        # Gráfica: eje fijo a la izquierda + tira de días desplazable
        graf = tk.Frame(self, bg=BG, padx=20)
        graf.pack(fill="x", pady=(6, 0))
        self.eje = tk.Canvas(graf, width=62, height=230, bg=BG, highlightthickness=0)
        self.eje.pack(side="left")
        self.canvas = tk.Canvas(graf, height=230, bg=BG, highlightthickness=0,
                                xscrollincrement=COL)
        self.canvas.pack(side="left", fill="x", expand=True)
        barra = tk.Frame(self, bg=BG, padx=20)
        barra.pack(fill="x")
        sb = ttk.Scrollbar(barra, orient="horizontal", command=self.canvas.xview)
        sb.pack(fill="x", padx=(62, 0))
        self.canvas.configure(xscrollcommand=sb.set)
        c = self.canvas
        c.bind("<Configure>", lambda e: self.dibujar(centrar=True))
        c.bind("<ButtonPress-1>", self._presionar)
        c.bind("<B1-Motion>", self._arrastrar)
        c.bind("<ButtonRelease-1>", self._soltar)
        c.bind("<Motion>", self._sobre)
        c.bind("<Leave>", lambda e: self.mostrar_detalle(self.sel))
        c.bind("<MouseWheel>", lambda e: c.xview_scroll(-1 if e.delta > 0 else 1, "units"))
        c.bind("<Button-4>", lambda e: c.xview_scroll(-1, "units"))
        c.bind("<Button-5>", lambda e: c.xview_scroll(1, "units"))

        self.leyenda = tk.Frame(self, bg=BG, padx=20)
        self.leyenda.pack(fill="x", pady=(6, 0))

        # Orden de la tabla
        orden = tk.Frame(self, bg=BG, padx=20, pady=10)
        orden.pack(fill="x", pady=(8, 0))
        tk.Label(orden, text="DÍAS DEL MES", bg=BG, fg=TXT, font=F["chico"]).pack(side="left")
        self.btn_dir = boton(orden, "", self.invertir_orden)
        self.btn_dir.pack(side="right", padx=(8, 0))
        self.sel_orden = Selector(orden, [("fecha", "FECHA"), ("total", "CONSUMO"),
                                          ("rx", "BAJADA"), ("tx", "SUBIDA")],
                                  lambda: self.ordenar_por(self.sel_orden.valor, nuevo=True))
        self.sel_orden.pack(side="right")
        tk.Label(orden, text="ORDENAR POR", bg=BG, fg=SUB, font=F["mini"]).pack(side="right", padx=8)

        self.titulos = {"fecha": "FECHA", "dia": "DÍA", "cable": "CABLE", "wifi": "WIFI",
                        "rx": "BAJADA ↓", "tx": "SUBIDA ↑", "total": "TOTAL", "uso": "   USO",
                        "nota": ""}
        marco, self.tree = tabla(self, [
            ("fecha", self.titulos["fecha"], 95, "w"), ("dia", self.titulos["dia"], 95, "w"),
            ("cable", self.titulos["cable"], 90, "e"), ("wifi", self.titulos["wifi"], 90, "e"),
            ("rx", self.titulos["rx"], 90, "e"), ("tx", self.titulos["tx"], 90, "e"),
            ("total", self.titulos["total"], 95, "e"), ("uso", self.titulos["uso"], 110, "w"),
            ("nota", "", 120, "w")])
        marco.pack(fill="both", expand=True, padx=20)
        for col in self.titulos:
            if col != "nota":
                self.tree.heading(col, command=lambda c=col: self.ordenar_por(c))
        self.tree.tag_configure("hoy", foreground=AMAR)
        self.tree.tag_configure("cero", foreground=SUB)
        self.tree.bind("<<TreeviewSelect>>", self._fila_elegida)

        self.pie = tk.Label(self, bg=BG, fg=SUB, font=F["mini"], anchor="w", justify="left",
                            padx=20, pady=10)
        self.pie.pack(fill="x")

    def abrir_sitios(self):
        if getattr(self, "ventana_sitios", None) and self.ventana_sitios.winfo_exists():
            self.ventana_sitios.lift()
            return
        self.ventana_sitios = VentanaSitios(self)

    def ayuda(self):
        v = tk.Toplevel(self, bg=BG, padx=24, pady=20)
        v.title("Ayuda")
        v.resizable(False, False)
        partes = [
            ("CÓMO LEER LA APP", TXT, F["titulo"]),
            ("", SUB, F["chico"]),
            ("COLORES (siempre significan lo mismo)", SUB, F["mini"]),
            ("■ CIAN      consumo por cable (Ethernet)", CIAN, F["chico"]),
            ("■ ROSA      consumo por WiFi", ROSA, F["chico"]),
            ("■ LILA      otra conexión (USB, móvil…)", LILA, F["chico"]),
            ("◆ AMARILLO  el día de hoy", AMAR, F["chico"]),
            ("  GRIS      día sin consumo, o que aún no llega", SUB, F["chico"]),
            ("", SUB, F["chico"]),
            ("GRÁFICA", SUB, F["mini"]),
            ("Cada barra es un día. Debajo dice qué día de la semana es y su número.", TXT, F["chico"]),
            ("El fondo más oscuro marca sábado y domingo.", TXT, F["chico"]),
            ("En 'CABLE + WIFI' cada barra se parte en cable y WiFi.", TXT, F["chico"]),
            ("En 'SOLO CABLE' / 'SOLO WIFI' se parte en bajada (claro) y subida (oscuro).", TXT, F["chico"]),
            ("Pasa el ratón o haz clic en una barra para ver el detalle de ese día.", TXT, F["chico"]),
            ("Arrastra, usa la rueda del ratón o las flechas ← → para moverte.", TXT, F["chico"]),
            ("El botón HOY (o la tecla Inicio) te regresa al día actual.", TXT, F["chico"]),
            ("", SUB, F["chico"]),
            ("TABLA", SUB, F["mini"]),
            ("Clic en un encabezado para ordenar; otro clic invierte el orden.", TXT, F["chico"]),
            ("★ PICO = el día que más se consumió en el mes.", TXT, F["chico"]),
            ("", SUB, F["chico"]),
            ("● REGISTRANDO", SUB, F["mini"]),
            ("El consumo y los sitios se guardan solos en segundo plano,", TXT, F["chico"]),
            ("aunque la ventana esté cerrada.", TXT, F["chico"]),
        ]
        for texto, color, fuente in partes:
            tk.Label(v, text=texto, bg=BG, fg=color, font=fuente, anchor="w").pack(fill="x")
        boton(v, "ENTENDIDO", v.destroy).pack(anchor="e", pady=(16, 0))

    # --------------------------------------------------------- lógica ----
    def ciclo(self):
        self.cargar(centrar=False)
        if not registrador_activo():
            try:
                lanzar_registrador()
            except Exception:
                pass
        self.after(30000, self.ciclo)

    def latir(self):
        self.parpadeo = not self.parpadeo
        vivo = time.time() - float(leer_estado(self.db, "latido") or 0) < 90
        if vivo:
            self.rec.configure(text="● REGISTRANDO", fg=ROSA if self.parpadeo else SUB)
        else:
            self.rec.configure(text="○ SIN REGISTRO", fg=SUB)
        self.after(700, self.latir)

    def cargar(self, centrar=True):
        por_itf = {}
        for fecha, itf, rx, tx in self.db.execute("SELECT fecha, itf, rx, tx FROM trafico"):
            por_itf.setdefault(itf, {})[dt.date.fromisoformat(fecha)] = (rx, tx)
        # vnstat (si está) aporta historial de antes de instalar; por día gana el mayor
        for itf, dias in leer_vnstat().items():
            mio = por_itf.setdefault(itf, {})
            for d, v in dias.items():
                if sum(v) > sum(mio.get(d, (0, 0))):
                    mio[d] = v
        self.por_tipo = {"cable": {}, "wifi": {}, "otra": {}}
        self.itfs = {}
        for itf, dias in por_itf.items():
            tipo = tipo_itf(itf)
            if not tipo:
                continue
            self.itfs[itf] = tipo
            destino = self.por_tipo[tipo]
            for d, (rx, tx) in dias.items():
                acc = destino.setdefault(d, [0, 0])
                acc[0] += rx
                acc[1] += tx
        fechas = [d for dias in self.por_tipo.values() for d in dias]
        self.inicio = min(fechas) if fechas else dt.date.today()
        self.mostrar(centrar=centrar)

    # ---- navegación ----
    def mes_vecino(self, paso):
        y, m = self.mes
        m += paso
        if m == 0:
            return y - 1, 12
        if m == 13:
            return y + 1, 1
        return y, m

    def mes_valido(self, ym):
        hoy = dt.date.today()
        return (self.inicio.year, self.inicio.month) <= ym <= (hoy.year, hoy.month)

    def mover_mes(self, paso):
        y, m = self.mes_vecino(paso)
        if not self.mes_valido((y, m)):
            return
        hoy = dt.date.today()
        self.mes = (y, m)
        d = dt.date(y, m, min(hoy.day, calendar.monthrange(y, m)[1]))
        self.sel = max(self.inicio, min(d, hoy))
        self.mostrar()

    def ir_hoy(self):
        hoy = dt.date.today()
        self.mes = (hoy.year, hoy.month)
        self.sel = hoy
        self.mostrar()

    def mover_sel(self, paso):
        d = self.sel + dt.timedelta(days=paso)
        if not self.inicio <= d <= dt.date.today():
            return
        if (d.year, d.month) != self.mes:
            self.mes = (d.year, d.month)
            self.sel = d
            self.mostrar()
        else:
            self.seleccionar(d)

    def seleccionar(self, d, centrar=True, desde_tabla=False):
        self.sel = d
        self.dibujar(centrar=centrar)
        self.mostrar_detalle(d, forzar=True)
        iid = d.isoformat()
        if not desde_tabla and self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.see(iid)

    # ---- orden de la tabla ----
    def ordenar_por(self, col, nuevo=False):
        col = "total" if col == "uso" else col
        if col == self.orden[0] and not nuevo:
            self.orden = (col, not self.orden[1])
        else:
            self.orden = (col, True)
        self.llenar_tabla()

    def invertir_orden(self):
        self.orden = (self.orden[0], not self.orden[1])
        self.llenar_tabla()

    # ---- pintar ----
    def mostrar(self, centrar=True):
        modo = self.vista.valor
        y, m = self.mes
        hoy = dt.date.today()
        tipos = ["cable", "wifi", "otra"] if modo == "todo" else [modo]
        self.dias = []
        for dia in range(1, calendar.monthrange(y, m)[1] + 1):
            d = dt.date(y, m, dia)
            por = {t: sum(self.por_tipo[t].get(d, (0, 0))) for t in ("cable", "wifi", "otra")}
            rx = sum(self.por_tipo[t].get(d, (0, 0))[0] for t in tipos)
            tx = sum(self.por_tipo[t].get(d, (0, 0))[1] for t in tipos)
            if modo == "todo":
                segs = [(por[t], COLOR_TIPO[t]) for t in tipos]
            else:
                segs = [(rx, COLOR_TIPO[modo]), (tx, SOMBRA[modo])]
            self.dias.append({"d": d, "rx": rx, "tx": tx, "total": rx + tx, "segs": segs,
                              "valido": self.inicio <= d <= hoy, **por})
        validos = [x for x in self.dias if x["valido"]]
        usados = [x for x in validos if x["total"] > 0]
        self.pico = max(usados, key=lambda x: x["total"])["d"] if usados else None
        if validos and not validos[0]["d"] <= self.sel <= validos[-1]["d"]:
            self.sel = validos[-1]["d"]

        # Mes y flechas
        self.lbl_mes["text"] = f"{MESES[m]} {y}"
        for btn, paso, fmt in ((self.btn_ant, -1, "◀ {}"), (self.btn_sig, 1, "{} ▶")):
            ym = self.mes_vecino(paso)
            btn.configure(text=fmt.format(MESES[ym[1]][:3]),
                          fg=CIAN if self.mes_valido(ym) else LINEA)

        # Cifras
        trx = sum(x["rx"] for x in validos)
        ttx = sum(x["tx"] for x in validos)
        tot = trx + ttx
        self.v["total"]["text"] = tamano(tot)
        if modo == "todo":
            partes = []
            for t in ("cable", "wifi", "otra"):
                suma = sum(x[t] for x in validos)
                if t != "otra" or suma:
                    partes += [("■ ", COLOR_TIPO[t]), (f"{t.upper()} {tamano(suma)}  ", SUB)]
        else:
            partes = [("■ ", COLOR_TIPO[modo]), (f"SOLO {modo.upper()}", SUB)]
        texto_rico(self.sub["total"], partes, F["mini"])
        self.v["rx"]["text"] = tamano(trx)
        self.v["tx"]["text"] = tamano(ttx)
        for clave, val in (("rx", trx), ("tx", ttx)):
            texto_rico(self.sub[clave], [(f"{val * 100 / tot:.0f}% DEL TOTAL" if tot else "", SUB)],
                       F["mini"])
        self.v["prom"]["text"] = tamano(tot / len(usados)) if usados else "0"
        texto_rico(self.sub["prom"], [(f"EN {len(usados)} DÍAS CON USO", SUB)], F["mini"])
        if self.pico:
            self.v["max"]["text"] = f"{DIAS[self.pico.weekday()]} {self.pico.day}"
            total_pico = next(x["total"] for x in usados if x["d"] == self.pico)
            texto_rico(self.sub["max"], [(f"{tamano(total_pico)} · CLIC PARA VER", SUB)], F["mini"])
        else:
            self.v["max"]["text"] = "—"
            texto_rico(self.sub["max"], [], F["mini"])

        # Leyenda de la gráfica
        if modo == "todo":
            ley = [(CIAN, "CABLE"), (ROSA, "WIFI")]
            if "otra" in self.itfs.values():
                ley.append((LILA, "OTRA"))
        else:
            ley = [(COLOR_TIPO[modo], "BAJADA ↓"), (SOMBRA[modo], "SUBIDA ↑")]
        ley += [(FINDE, "SÁB Y DOM"), (None, "◆ HOY"), (None, "★ PICO")]
        for w in self.leyenda.winfo_children():
            w.destroy()
        tk.Label(self.leyenda, text="", bg=BG, width=7).pack(side="left")
        for color, texto in ley:
            if color:
                tk.Frame(self.leyenda, bg=color, width=10, height=10, highlightthickness=1,
                         highlightbackground=LINEA).pack(side="left", padx=(0, 5))
            fg = AMAR if texto == "◆ HOY" else SUB
            tk.Label(self.leyenda, text=texto, bg=BG, fg=fg, font=F["mini"]).pack(side="left",
                                                                                  padx=(0, 16))
        tk.Label(self.leyenda, text="ARRASTRA o ← → PARA MOVERTE", bg=BG, fg=LINEA,
                 font=F["mini"]).pack(side="right")

        self.tree["displaycolumns"] = (list(self.titulos) if modo == "todo" else
                                       [c for c in self.titulos if c not in ("cable", "wifi")])
        self.llenar_tabla(ver_sel=centrar)

        itfs = "  ".join(f"{n} = {t}" for n, t in sorted(self.itfs.items())) or "ninguna aún"
        self.pie["text"] = (f"REGISTRANDO DESDE {self.inicio:%d/%m/%Y} · INTERFACES: {itfs} · "
                            f"1 GB = 10⁹ BYTES")
        self.dibujar(centrar=centrar)
        self.mostrar_detalle(self.sel, forzar=True)

    def llenar_tabla(self, ver_sel=True):
        col, desc = self.orden
        hoy = dt.date.today()
        filas = [x for x in self.dias if x["valido"]]
        if col == "fecha":
            filas.sort(key=lambda x: x["d"], reverse=desc)
        elif col == "dia":
            filas.sort(key=lambda x: (x["d"].weekday(), x["d"]), reverse=desc)
        else:
            filas.sort(key=lambda x: (x[col], x["d"]), reverse=desc)
        maximo = max((x["total"] for x in filas), default=0)
        self.tree.delete(*self.tree.get_children())
        for x in filas:
            d = x["d"]
            notas = (["◆ HOY"] if d == hoy else []) + (["★ PICO"] if d == self.pico else [])
            if x["total"] == 0:
                notas.append("SIN USO")
            tags = ("hoy",) if d == hoy else ("cero",) if x["total"] == 0 else ()
            self.tree.insert("", "end", iid=d.isoformat(), tags=tags, values=(
                d.strftime("%d/%m/%Y"), DIAS_LARGO[d.weekday()], tamano(x["cable"]),
                tamano(x["wifi"]), tamano(x["rx"]), tamano(x["tx"]), tamano(x["total"]),
                barrita(x["total"], maximo), "  ".join(notas)))

        # Encabezados con flecha y controles de orden sincronizados
        for c, titulo in self.titulos.items():
            marca = (" ▼" if desc else " ▲") if c == col or (c == "uso" and col == "total") else ""
            self.tree.heading(c, text=titulo + marca)
        self.sel_orden.poner(col)
        if col == "fecha":
            self.btn_dir["text"] = "▼ RECIENTES PRIMERO" if desc else "▲ ANTIGUOS PRIMERO"
        elif col == "dia":
            self.btn_dir["text"] = "▼ DOMINGO PRIMERO" if desc else "▲ LUNES PRIMERO"
        else:
            self.btn_dir["text"] = "▼ MAYOR A MENOR" if desc else "▲ MENOR A MAYOR"

        iid = self.sel.isoformat()
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            if ver_sel:
                self.tree.see(iid)

    def _fila_elegida(self, e):
        sel = self.tree.selection()
        if sel:
            d = dt.date.fromisoformat(sel[0])
            if d != self.sel:
                self.seleccionar(d, desde_tabla=True)

    def mostrar_detalle(self, d, forzar=False):
        if d == self.dia_detalle and not forzar:
            return
        self.dia_detalle = d
        x = next((x for x in self.dias if x["d"] == d), None)
        if not x:
            return
        hoy = dt.date.today()
        partes = [("◆ " if d == hoy else "▸ ", AMAR if d == hoy else CIAN),
                  (f"{DIAS_LARGO[d.weekday()]} {d.day} DE {MESES[d.month]}", TXT)]
        if d == hoy:
            partes.append(("  (HOY)", AMAR))
        if d > hoy:
            partes.append(("   · ESTE DÍA AÚN NO LLEGA", SUB))
        elif d < self.inicio:
            partes.append(("   · ANTES DE EMPEZAR A REGISTRAR", SUB))
        elif x["total"] == 0:
            partes.append(("   · SIN CONSUMO", SUB))
        else:
            partes.append((f"   TOTAL {tamano(x['total'])}", TXT))
            if self.vista.valor == "todo":
                for t in ("cable", "wifi", "otra"):
                    if t != "otra" or x[t]:
                        partes += [("   ■ ", COLOR_TIPO[t]), (f"{t.upper()} {tamano(x[t])}", SUB)]
            partes.append((f"   ↓ {tamano(x['rx'])}   ↑ {tamano(x['tx'])}", SUB))
            if d == self.pico:
                partes.append(("   ★ DÍA PICO", TXT))
        texto_rico(self.detalle, partes)

    # ---- gráfica ----
    def dibujar(self, centrar=False):
        c, eje = self.canvas, self.eje
        c.delete("all")
        eje.delete("all")
        W, H = c.winfo_width(), c.winfo_height()
        if not self.dias or W < 10:
            return
        arr, aba = 26, 44           # espacio para el valor arriba y el día abajo
        alto = H - arr - aba
        base = arr + alto
        ancho_total = len(self.dias) * COL
        self.x_ini = max(0, (W - ancho_total) / 2)
        ancho_region = max(W, ancho_total)
        c.configure(scrollregion=(0, 0, ancho_region, H))
        maximo = max((x["total"] for x in self.dias if x["valido"]), default=0)
        hoy = dt.date.today()

        for k in range(3):
            yv = base - alto * k / 2
            c.create_line(0, yv, ancho_region, yv, fill=LINEA, dash=(1, 3))
            if maximo > 0:
                eje.create_text(56, yv, text=tamano(maximo * k / 2), anchor="e", fill=SUB,
                                font=F["mini"])

        for i, x in enumerate(self.dias):
            d = x["d"]
            x0 = self.x_ini + i * COL
            cx = x0 + COL / 2
            if d.weekday() >= 5:
                c.create_rectangle(x0, 0, x0 + COL, H, fill=FINDE, width=0)
            if d == self.sel:
                c.create_rectangle(x0 + 1, 1, x0 + COL - 1, H - 1, outline=SUB, dash=(2, 2))
            if x["valido"] and x["total"] > 0 and maximo > 0:
                y = base
                for valor, col in x["segs"]:
                    h = alto * valor / maximo
                    if h >= 0.5:
                        c.create_rectangle(cx - 12, y - h, cx + 12, y, fill=col, width=0)
                        y -= h
                c.create_text(cx, y - 9, text=("★" if d == self.pico else "") + corto(x["total"]),
                              fill=TXT if d in (self.sel, self.pico) else SUB, font=F["mini"])
            else:
                c.create_line(cx - 12, base, cx + 12, base, fill=SUB if x["valido"] else LINEA,
                              dash=() if x["valido"] else (2, 2))
            if d == hoy:
                color = AMAR
            elif x["valido"]:
                color = TXT
            else:
                color = APAGADO
            c.create_text(cx, base + 13, text=DIAS[d.weekday()], fill=color if d == hoy else
                          (SUB if x["valido"] else APAGADO), font=F["mini"])
            c.create_text(cx, base + 30, text=str(d.day), fill=color, font=F["chico"])
            if d == hoy:
                c.create_rectangle(cx - 17, base + 4, cx + 17, base + 40, outline=AMAR)

        if centrar and ancho_total > W:
            i = (self.sel - self.dias[0]["d"]).days
            centro = self.x_ini + i * COL + COL / 2
            c.xview_moveto(max(0, centro - W / 2) / ancho_region)

    def _dia_en(self, e):
        i = int((self.canvas.canvasx(e.x) - self.x_ini) // COL)
        return self.dias[i] if 0 <= i < len(self.dias) else None

    def _presionar(self, e):
        self.arrastre = (e.x, False)
        self.canvas.scan_mark(e.x, 0)

    def _arrastrar(self, e):
        x0, movido = self.arrastre
        if movido or abs(e.x - x0) > 4:
            self.arrastre = (x0, True)
            self.canvas.scan_dragto(e.x, 0, gain=1)

    def _soltar(self, e):
        if not self.arrastre[1]:
            x = self._dia_en(e)
            if x and x["valido"]:
                self.seleccionar(x["d"], centrar=False)

    def _sobre(self, e):
        x = self._dia_en(e)
        self.canvas.configure(cursor="hand2" if x and x["valido"] else "")
        if x:
            self.mostrar_detalle(x["d"])


# ------------------------------------------------------- ventana sitios ----
class VentanaSitios(tk.Toplevel):
    """Muestra lo que el registrador guarda: a qué dominios se conecta la máquina
    (consultas DNS + nombre del servidor en HTTPS/QUIC), igual que lo vería el módem."""

    def __init__(self, master):
        super().__init__(master)
        self.title("Sitios visitados")
        self.geometry("940x640")
        self.minsize(700, 460)
        self.configure(bg=BG)
        self.db = abrir_db()
        self.iconos = Iconos()
        self.firma = None
        self.dia = dt.date.today()

        barra = tk.Frame(self, bg=BG, padx=20, pady=16)
        barra.pack(fill="x")
        tk.Label(barra, text="SITIOS VISITADOS", bg=BG, fg=TXT, font=F["titulo"]).pack(side="left")
        boton(barra, "EXPORTAR CSV", self.exportar).pack(side="right")
        separador(self)

        nav = tk.Frame(self, bg=BG, padx=20, pady=12)
        nav.pack(fill="x")
        self.pestana = Selector(nav, [("res", "RESUMEN"), ("vivo", "EN VIVO")], self.cambiar_pestana)
        self.pestana.pack(side="left")
        tk.Label(nav, text="BUSCAR", bg=BG, fg=SUB, font=F["mini"]).pack(side="left", padx=(20, 6))
        self.filtro = tk.StringVar()
        self.filtro.trace_add("write", lambda *a: self.refrescar(forzar=True))
        tk.Entry(nav, textvariable=self.filtro, width=20, bg=BG, fg=TXT, insertbackground=CIAN,
                 relief="flat", highlightthickness=1, highlightbackground=LINEA,
                 highlightcolor=CIAN, font=F["base"]).pack(side="left", ipady=2)
        der = tk.Label(nav, text="▶", bg=BG, fg=SUB, font=F["base"], cursor="hand2")
        der.pack(side="right")
        self.lbl_dia = tk.Label(nav, bg=BG, fg=TXT, font=F["base"], width=20)
        self.lbl_dia.pack(side="right")
        izq = tk.Label(nav, text="◀", bg=BG, fg=SUB, font=F["base"], cursor="hand2")
        izq.pack(side="right")
        izq.bind("<Button-1>", lambda e: self.mover_dia(-1))
        der.bind("<Button-1>", lambda e: self.mover_dia(1))

        cuerpo = tk.Frame(self, bg=BG)
        cuerpo.pack(fill="both", expand=True, padx=20, pady=(4, 0))
        self.m_res, self.t_res = tabla(cuerpo, [
            ("sitio", "SITIO", 230, "w"), ("veces", "CONEXIONES", 95, "e"),
            ("ultima", "ÚLTIMA", 80, "center"), ("doms", "DOMINIOS", 380, "w")], icono=True)
        self.m_vivo, self.t_vivo = tabla(cuerpo, [
            ("hora", "HORA", 80, "center"), ("dominio", "DOMINIO", 340, "w"),
            ("servicio", "SERVICIO", 170, "w"), ("via", "VÍA", 60, "center"),
            ("ip", "IP DESTINO", 150, "w")], icono=True)
        self.t_res.tag_configure("otro", foreground=SUB)
        self.titulos_res = {"sitio": "SITIO", "veces": "CONEXIONES", "ultima": "ÚLTIMA",
                            "doms": "DOMINIOS"}
        self.orden_res = ("veces", True)
        for col in self.titulos_res:
            self.t_res.heading(col, command=lambda c=col: self.ordenar_res(c))
        self.m_res.pack(fill="both", expand=True)

        self.pie = tk.Label(self, bg=BG, fg=SUB, font=F["mini"], anchor="w", justify="left",
                            padx=20, pady=10)
        self.pie.pack(fill="x")
        self.ciclo()

    def ciclo(self):
        if self.winfo_exists():
            self.refrescar()
            self.after(4000, self.ciclo)

    def cambiar_pestana(self):
        self.m_res.pack_forget()
        self.m_vivo.pack_forget()
        (self.m_res if self.pestana.valor == "res" else self.m_vivo).pack(fill="both", expand=True)
        self.refrescar(forzar=True)

    def dias_con_datos(self):
        filas = self.db.execute("SELECT DISTINCT date(ts, 'unixepoch', 'localtime') FROM sitios")
        dias = {dt.date.fromisoformat(f[0]) for f in filas if f[0]}
        dias.add(dt.date.today())
        return sorted(dias)

    def mover_dia(self, paso):
        dias = self.dias_con_datos()
        i = dias.index(self.dia) if self.dia in dias else len(dias) - 1
        self.dia = dias[max(0, min(len(dias) - 1, i + paso))]
        self.refrescar(forzar=True)

    def rango(self):
        t0 = time.mktime(self.dia.timetuple())
        return t0, time.mktime((self.dia + dt.timedelta(days=1)).timetuple())

    def resumen(self):
        """[(clave, veces, servicio, ultimo_ts, {dominios}, {bases})] del día, ordenado."""
        grupos = {}
        filas = self.db.execute(
            "SELECT servicio, base, dominio, COUNT(*), MAX(ts) FROM sitios "
            "WHERE ts >= ? AND ts < ? GROUP BY servicio, base, dominio", self.rango())
        for serv, base, dom, n, t in filas:
            # Servicios conocidos se agrupan en una fila; lo demás, por dominio
            g = grupos.setdefault(serv or base, [0, serv, t, set(), set()])
            g[0] += n
            g[2] = max(g[2], t)
            g[3].add(dom)
            g[4].add(base)
        return sorted(((k, *v) for k, v in grupos.items()), key=lambda x: x[1], reverse=True)

    def refrescar(self, forzar=False):
        hoy = dt.date.today()
        self.lbl_dia.configure(
            text=(f"◆ HOY · {DIAS[hoy.weekday()]} {hoy:%d/%m}" if self.dia == hoy
                  else f"{DIAS[self.dia.weekday()]} {self.dia:%d/%m/%Y}"),
            fg=AMAR if self.dia == hoy else TXT)
        ultimo = self.db.execute("SELECT MAX(id) FROM sitios").fetchone()[0]
        firma = (ultimo, self.dia, self.filtro.get(), self.pestana.valor)
        if forzar or firma != self.firma:
            self.firma = firma
            if self.pestana.valor == "res":
                self.llenar_resumen()
            else:
                self.llenar_vivo()

        captura = leer_estado(self.db, "captura") or "—"
        error = leer_estado(self.db, "captura_error")
        total = self.db.execute("SELECT COUNT(*) FROM sitios WHERE ts >= ? AND ts < ?",
                                self.rango()).fetchone()[0]
        self.pie.configure(
            fg=ROSA if error else SUB,
            text=(f"⚠ {error}\n" if error else "") +
                 "ICONO DE COLOR = SERVICIO CONOCIDO · ICONO GRIS = OTRO SITIO (TAMBIÉN SE GUARDA) · "
                 "CLIC EN UN ENCABEZADO PARA ORDENAR\n"
                 f"CAPTURA: {captura} · {total} CONEXIONES ESTE DÍA · SE REGISTRA SIEMPRE "
                 f"EN SEGUNDO PLANO · DATOS EN {RUTA_DB}")

    def ordenar_res(self, col):
        desc = not self.orden_res[1] if col == self.orden_res[0] else col != "sitio"
        self.orden_res = (col, desc)
        self.refrescar(forzar=True)

    def llenar_resumen(self):
        f = self.filtro.get().strip().lower()
        col, desc = self.orden_res
        for c, titulo in self.titulos_res.items():
            self.t_res.heading(c, text=titulo + ((" ▼" if desc else " ▲") if c == col else ""))
        claves = {"sitio": lambda x: (x[2] or x[0]).lower(), "veces": lambda x: x[1],
                  "ultima": lambda x: x[3], "doms": lambda x: len(x[4])}
        filas = sorted(self.resumen(), key=claves[col], reverse=desc)
        self.t_res.delete(*self.t_res.get_children())
        for clave, veces, serv, t, doms, bases in filas:
            if f and f not in serv.lower() and not any(f in d for d in doms):
                continue
            nombre = serv or ", ".join(sorted(bases)[:2])
            lista = ", ".join(sorted(doms)[:4]) + (f" … (+{len(doms) - 4})" if len(doms) > 4 else "")
            self.t_res.insert("", "end", image=self.iconos.de(serv, clave),
                              values=(nombre, veces, dt.datetime.fromtimestamp(t).strftime("%H:%M:%S"),
                                      lista), tags=() if serv else ("otro",))

    def llenar_vivo(self):
        f = self.filtro.get().strip().lower()
        self.t_vivo.delete(*self.t_vivo.get_children())
        filas = self.db.execute(
            "SELECT ts, dominio, base, servicio, via, ip FROM sitios WHERE ts >= ? AND ts < ? "
            "ORDER BY id DESC LIMIT 1500", self.rango())
        for t, dom, base, serv, via, ip in filas:
            if f and f not in dom and f not in serv.lower():
                continue
            self.t_vivo.insert("", "end", image=self.iconos.de(serv, base),
                               values=(dt.datetime.fromtimestamp(t).strftime("%H:%M:%S"),
                                       dom, serv, via, ip))

    def exportar(self):
        filas = self.resumen()
        if not filas:
            messagebox.showinfo("Sitios visitados", "Ese día no hay nada registrado.", parent=self)
            return
        ruta = filedialog.asksaveasfilename(
            parent=self, defaultextension=".csv",
            initialfile=f"resumen_sitios_{self.dia:%Y-%m-%d}.csv", filetypes=[("CSV", "*.csv")])
        if not ruta:
            return
        with open(ruta, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["servicio", "sitios", "conexiones", "ultima_vez", "dominios"])
            for clave, veces, serv, t, doms, bases in filas:
                w.writerow([serv, " ".join(sorted(bases)), veces,
                            dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S"),
                            " ".join(sorted(doms))])


if __name__ == "__main__":
    if "--registrar" in sys.argv:
        Registrador().correr()
    else:
        App().mainloop()
