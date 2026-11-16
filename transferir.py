#!/usr/bin/env python3
"""Transferir: carpetas compartidas por el navegador y replicadas entre las computadoras de la red.

Uso:
    python3 transferir.py                     # arranca el servidor (crea la configuración la primera vez)
    python3 transferir.py --instalar          # copia el programa y lo deja arrancando al iniciar la sesión
    python3 transferir.py --unirse http://IP:8000 --clave CODIGO   # une ESTA computadora a una red existente e instala
    python3 transferir.py --info              # muestra direcciones, PINs y código de unión
    python3 transferir.py --desinstalar       # quita el arranque automático (no borra archivos)

Cada computadora unida guarda una copia completa de las carpetas y se sincroniza con las demás (descubrimiento por
difusión UDP + lista de nodos conocidos). Teléfonos y equipos sin instalar entran por el navegador con un PIN:
uno para la carpeta compartida y uno por cada carpeta personal. Sin dependencias: Python 3.8+.
"""
import argparse
import hashlib
import hmac
import html
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "2.0"
INICIO = Path.home() / ".transferir"
CONFIG = INICIO / "config.json"
ESTADO = INICIO / "estado.json"
INSTALADO = INICIO / "transferir.py"
PUERTO_DESCUBRIMIENTO = 47800
BLOQUE = 1024 * 1024
INTERVALO_SYNC = 10
TOLERANCIA_RELOJ = 300

cfg: dict = {}
candado = threading.RLock()          # protege estado (lápidas, índice, nodos)
estado: dict = {"lapidas": {}, "indice": {}, "nodos": {}}
sesiones: dict[str, str] = {}        # galleta -> área (o "equipo")

# Carpetas de ESTA computadora (no se replican: se navegan con el PIN de equipo)
LOCALES = {
    "escritorio": ("Escritorio", Path.home() / "Desktop"),
    "documentos": ("Documentos", Path.home() / "Documents"),
    "usuario": ("Carpeta de usuario", Path.home()),
}
fallos: dict[str, list] = {}         # ip -> [intentos, bloqueado_hasta]


# ---------------------------------------------------------------- configuración

def guardar_json(ruta: Path, datos):
    tmp = ruta.with_suffix(".tmp")
    tmp.write_text(json.dumps(datos, indent=2, ensure_ascii=False))
    os.replace(tmp, ruta)
    try:
        os.chmod(ruta, 0o600)
    except OSError:
        pass


def pin_nuevo(usados):
    while True:
        p = f"{secrets.randbelow(10**6):06d}"
        if p not in usados:
            return p


def config_nueva() -> dict:
    pines = []
    areas = [{"id": "compartida", "nombre": "Compartida", "carpeta": "Compartida"}]
    for i in range(1, 4):
        areas.append({"id": f"personal{i}", "nombre": f"Personal {i}", "carpeta": f"Personal {i}"})
    for a in areas:
        a["pin"] = pin_nuevo(pines)
        pines.append(a["pin"])
    return {
        "red": secrets.token_hex(8),
        "secreto": secrets.token_hex(32),
        "clave_union": secrets.token_urlsafe(6),
        "areas": areas,
        "carpeta_base": str(Path.home() / "Transferencias"),
        "puerto": 8000,
    }


def cargar(crear=True):
    global cfg, estado
    INICIO.mkdir(parents=True, exist_ok=True)
    if CONFIG.exists():
        cfg = json.loads(CONFIG.read_text())
    elif crear:
        cfg = config_nueva()
    else:
        sys.exit("Esta computadora aún no está configurada.")
    cfg.setdefault("nodo", secrets.token_hex(6))
    if "pin_equipo" not in cfg:  # propio de cada computadora: no se comparte al unirse
        cfg["pin_equipo"] = pin_nuevo([a["pin"] for a in cfg["areas"]])
    guardar_json(CONFIG, cfg)
    if ESTADO.exists():
        try:
            estado.update(json.loads(ESTADO.read_text()))
        except ValueError:
            pass
    for a in cfg["areas"]:
        carpeta(a["id"]).mkdir(parents=True, exist_ok=True)
        estado["lapidas"].setdefault(a["id"], {})
        estado["indice"].setdefault(a["id"], {})


def guardar_estado():
    with candado:
        guardar_json(ESTADO, estado)


def area(id_):
    return next((a for a in cfg["areas"] if a["id"] == id_), None)


def carpeta(id_) -> Path:
    return Path(cfg["carpeta_base"]).expanduser() / area(id_)["carpeta"]


# ---------------------------------------------------------------- utilidades

def ips_locales() -> list[str]:
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ips = {i for i in ips if not i.startswith("127.")}
    return sorted(ips) or ["127.0.0.1"]


def mi_url():
    return f"http://{ips_locales()[0]}:{cfg['puerto']}"


def nombre_seguro(nombre: str):
    nombre = nombre.replace("\\", "/").split("/")[-1].strip()
    nombre = "".join(c for c in nombre if c.isprintable())
    if not nombre or nombre in (".", "..") or nombre.startswith(".") or nombre.endswith(".parcial"):
        return None
    return nombre[:200]


def ms(st_mtime_ns: int) -> int:
    return st_mtime_ns // 1_000_000


def firma(metodo: str, ruta: str, tiempo: str) -> str:
    return hmac.new(bytes.fromhex(cfg["secreto"]), f"{metodo}\n{ruta}\n{tiempo}".encode(), hashlib.sha256).hexdigest()


def firma_valida(metodo, ruta, cabeceras) -> bool:
    t = cabeceras.get("X-Tiempo", "")
    f = cabeceras.get("X-Firma", "")
    try:
        if abs(time.time() - int(t)) > TOLERANCIA_RELOJ:
            return False
    except ValueError:
        return False
    return hmac.compare_digest(f, firma(metodo, ruta, t))


def peticion_nodo(url_base: str, ruta: str, metodo="GET", cuerpo=None, timeout=10):
    t = str(int(time.time()))
    req = urllib.request.Request(url_base + ruta, data=cuerpo, method=metodo, headers={
        "X-Tiempo": t, "X-Firma": firma(metodo, ruta, t),
        "X-Nodo-Id": cfg["nodo"], "X-Nodo-URL": mi_url()})
    return urllib.request.urlopen(req, timeout=timeout)


# ---------------------------------------------------------------- manifiestos y réplica

def escanear(id_) -> dict:
    """Manifiesto del área: nombre -> {m: mtime ms, s: tamaño} o {m: hora de borrado, b: 1}."""
    base = carpeta(id_)
    ahora = int(time.time() * 1000)
    archivos = {}
    for p in base.iterdir():
        if p.is_file() and nombre_seguro(p.name) == p.name:
            st = p.stat()
            archivos[p.name] = {"m": ms(st.st_mtime_ns), "s": st.st_size}
    with candado:
        lapidas = estado["lapidas"][id_]
        indice = estado["indice"][id_]
        # borrado a mano en el disco (no por la página): se vuelve lápida para que no regrese de otro nodo
        for nombre in list(indice):
            if nombre not in archivos and nombre not in lapidas:
                lapidas[nombre] = ahora
        for nombre, d in archivos.items():
            if nombre in lapidas and lapidas[nombre] < d["m"]:
                del lapidas[nombre]
        estado["indice"][id_] = {n: d["m"] for n, d in archivos.items()}
        manifiesto = dict(archivos)
        for nombre, m in lapidas.items():
            if nombre not in archivos:
                manifiesto[nombre] = {"m": m, "b": 1}
    return manifiesto


def borrar_local(id_, nombre, cuando_ms):
    p = carpeta(id_) / nombre
    p.unlink(missing_ok=True)
    with candado:
        estado["lapidas"][id_][nombre] = cuando_ms
        estado["indice"][id_].pop(nombre, None)


def sincronizar_con(url):
    cambios = 0
    for a in cfg["areas"]:
        id_ = a["id"]
        with peticion_nodo(url, f"/_nodo/manifiesto?area={id_}") as r:
            remoto = json.loads(r.read())
        local = escanear(id_)
        for nombre, d in remoto.items():
            if nombre_seguro(nombre) != nombre:
                continue
            l = local.get(nombre)
            if l and l["m"] >= d["m"]:
                continue
            if d.get("b"):
                if l and not l.get("b"):
                    borrar_local(id_, nombre, d["m"])
                    cambios += 1
                else:
                    with candado:
                        estado["lapidas"][id_][nombre] = d["m"]
                continue
            destino = carpeta(id_) / nombre
            tmp = carpeta(id_) / f".{nombre}.{secrets.token_hex(4)}.parcial"
            try:
                ruta = f"/_nodo/archivo?area={id_}&nombre={urllib.parse.quote(nombre)}"
                with peticion_nodo(url, ruta, timeout=60) as r, open(tmp, "wb") as f:
                    shutil.copyfileobj(r, f, BLOQUE)
                if tmp.stat().st_size != d["s"]:
                    raise IOError("tamaño distinto (el archivo cambió durante la copia)")
                os.utime(tmp, ns=(d["m"] * 1_000_000, d["m"] * 1_000_000))
                os.replace(tmp, destino)
                with candado:
                    estado["lapidas"][id_].pop(nombre, None)
                    estado["indice"][id_][nombre] = d["m"]
                cambios += 1
                print(f"Replicado de {url}: {a['nombre']}/{nombre}")
            except Exception as e:
                tmp.unlink(missing_ok=True)
                print(f"No se pudo copiar {nombre} de {url}: {e}")
    return cambios


def registrar_nodo(id_nodo, url):
    if not id_nodo or id_nodo == cfg["nodo"] or not url.startswith("http://"):
        return
    with candado:
        estado["nodos"][id_nodo] = {"url": url, "visto": int(time.time())}


def bucle_sync():
    while True:
        time.sleep(INTERVALO_SYNC)
        with candado:
            nodos = dict(estado["nodos"])
        for id_nodo, n in nodos.items():
            try:
                sincronizar_con(n["url"])
                with candado:
                    if id_nodo in estado["nodos"]:
                        estado["nodos"][id_nodo]["visto"] = int(time.time())
            except Exception:
                pass
        with candado:  # olvida nodos que no responden en 7 días
            for k in [k for k, n in estado["nodos"].items() if time.time() - n["visto"] > 7 * 86400]:
                del estado["nodos"][k]
        for a in cfg["areas"]:
            escanear(a["id"])
        guardar_estado()


def anunciar():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    while True:
        t = str(int(time.time()))
        msg = {"red": cfg["red"], "nodo": cfg["nodo"], "puerto": cfg["puerto"], "t": t}
        msg["f"] = firma("ANUNCIO", f"{cfg['nodo']}:{cfg['puerto']}", t)
        try:
            s.sendto(json.dumps(msg).encode(), ("255.255.255.255", PUERTO_DESCUBRIMIENTO))
        except OSError:
            pass
        time.sleep(15)


def escuchar_anuncios():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    try:
        s.bind(("", PUERTO_DESCUBRIMIENTO))
    except OSError as e:
        print(f"Descubrimiento automático desactivado: {e}")
        return
    while True:
        datos, (ip, _) = s.recvfrom(2048)
        try:
            m = json.loads(datos)
            if m.get("red") != cfg["red"] or abs(time.time() - int(m["t"])) > TOLERANCIA_RELOJ:
                continue
            if hmac.compare_digest(m["f"], firma("ANUNCIO", f"{m['nodo']}:{int(m['puerto'])}", m["t"])):
                registrar_nodo(m["nodo"], f"http://{ip}:{int(m['puerto'])}")
        except (ValueError, KeyError, TypeError):
            pass


# ---------------------------------------------------------------- páginas

ESTILO = """<style>
:root{--f:#0d1117;--p:#161b22;--b:#30363d;--t:#e6edf3;--s:#8b949e;--a:#2f81f7;--v:#238636;--r:#f85149}
*{box-sizing:border-box}body{font:16px system-ui;background:var(--f);color:var(--t);margin:0 auto;padding:16px;max-width:820px}
a{color:var(--a)}button,input{font:inherit}h1{font-size:1.35em;margin:0}
.cab{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}
.pastilla{background:var(--p);border:1px solid var(--b);border-radius:20px;padding:4px 12px;color:var(--s);font-size:.85em}
#zona{border:2px dashed var(--b);border-radius:12px;padding:32px 16px;text-align:center;cursor:pointer;background:var(--p)}
#zona.sobre{border-color:var(--a)}#zona b{color:var(--a)}
.barra{height:8px;background:var(--b);border-radius:4px;overflow:hidden;margin-top:6px}.barra i{display:block;height:100%;width:0;background:var(--v)}
.subida{margin:10px 0;font-size:.9em}ul{list-style:none;padding:0}
li{display:flex;gap:12px;align-items:center;padding:10px 4px;border-bottom:1px solid var(--b)}
li a{color:var(--t);flex:1;word-break:break-all;text-decoration:none}li a:hover{color:var(--a)}
li span{color:var(--s);font-size:.85em;white-space:nowrap}.vacio{color:var(--s)}
.borrar{background:none;border:1px solid var(--b);color:var(--s);border-radius:6px;padding:4px 8px;cursor:pointer}
.borrar:hover{color:var(--r);border-color:var(--r)}.salir{background:none;border:0;color:var(--a);cursor:pointer}
details{background:var(--p);border:1px solid var(--b);border-radius:10px;padding:12px;margin-top:24px;color:var(--s)}
summary{cursor:pointer;color:var(--t)}
#pestanas{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}.pestana{background:var(--p);color:var(--s);border:1px solid var(--b);border-radius:8px;padding:8px 12px;cursor:pointer}
.pestana.activa{color:var(--t);border-color:var(--a)}#migas{margin-bottom:10px;color:var(--s);word-break:break-all}#migas a{color:var(--a);text-decoration:none}a.carpeta{color:var(--t)}code{background:var(--f);padding:2px 6px;border-radius:4px;word-break:break-all}
form.pin{background:var(--p);padding:24px;border-radius:12px;border:1px solid var(--b);max-width:340px;margin:15vh auto;text-align:center}
form.pin input{background:var(--f);color:var(--t);border:1px solid var(--b);border-radius:8px;padding:10px;width:9em;text-align:center;letter-spacing:.3em}
form.pin button{background:var(--v);color:#fff;border:0;border-radius:8px;padding:10px 16px;margin-left:6px}
.error{color:var(--r)}
</style>"""

PAGINA_PIN = """<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Transferir archivos</title>%ESTILO%
<form class="pin" method="post" action="/pin"><h1>Transferir archivos</h1><p>Escribe tu PIN</p>
<input name="pin" inputmode="numeric" autofocus autocomplete="off" aria-label="PIN"><button>Entrar</button>%ERROR%</form></html>"""

PAGINA = """<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Transferir</title>%ESTILO%
<div class="cab"><h1 id="titulo">Transferir</h1><span class="pastilla">%NODOS%</span>
<form method="post" action="/salir"><button class="salir">Salir</button></form></div>
<nav id="pestanas"></nav>
<div id="migas"></div>
<div id="zona" tabindex="0" role="button">Arrastra archivos aquí o <b>toca para elegir</b><input id="archivos" type="file" multiple hidden></div>
<div id="subidas"></div>
<ul id="lista"></ul>
%EXTRA%
<script>
const RAICES=%RAICES%;let raiz=RAICES[0].id,ruta='';
const zona=document.getElementById('zona'),inp=document.getElementById('archivos');
zona.onclick=()=>inp.click();zona.onkeydown=e=>{if(e.key==='Enter'||e.key===' ')inp.click()};
inp.onchange=()=>{subir([...inp.files]);inp.value=''};
['dragenter','dragover'].forEach(ev=>zona.addEventListener(ev,e=>{e.preventDefault();zona.classList.add('sobre')}));
['dragleave','drop'].forEach(ev=>zona.addEventListener(ev,e=>{e.preventDefault();zona.classList.remove('sobre')}));
zona.addEventListener('drop',e=>subir([...e.dataTransfer.files]));
const q=o=>new URLSearchParams(o).toString();
function fmt(n){for(const u of['B','KB','MB','GB']){if(n<1024||u==='GB')return(u==='B'?n:n.toFixed(1))+' '+u;n/=1024}}
function info(){return RAICES.find(r=>r.id===raiz)}
function pestanas(){const n=document.getElementById('pestanas');n.innerHTML='';if(RAICES.length<2)return;
  for(const r of RAICES){const b=document.createElement('button');b.textContent=r.nombre;b.className='pestana'+(r.id===raiz?' activa':'');
    b.onclick=()=>{raiz=r.id;ruta='';cargar()};n.append(b)}}
function migas(){const m=document.getElementById('migas');m.innerHTML='';document.getElementById('titulo').textContent=info().nombre;
  if(!info().carpetas)return;const partes=ruta?ruta.split('/'):[];
  const ir=(t,r)=>{const a=document.createElement('a');a.href='#';a.textContent=t;a.onclick=e=>{e.preventDefault();ruta=r;cargar()};m.append(a)};
  ir(info().nombre,'');partes.forEach((p,i)=>{m.append(' / ');ir(p,partes.slice(0,i+1).join('/'))})}
async function subir(fs){for(const f of fs)await uno(f);cargar()}
function uno(f){return new Promise(ok=>{
  const d=document.createElement('div');d.className='subida';const t=document.createElement('span');t.textContent=f.name+' — 0 %';
  const b=document.createElement('div');b.className='barra';const i=document.createElement('i');b.append(i);d.append(t,b);
  document.getElementById('subidas').prepend(d);
  const x=new XMLHttpRequest();x.open('PUT','/subir?'+q({raiz,ruta,nombre:f.name}));
  x.upload.onprogress=e=>{if(e.lengthComputable){const p=Math.round(e.loaded*100/e.total);i.style.width=p+'%';t.textContent=f.name+' — '+p+' %'}};
  x.onload=()=>{if(x.status===401)location.reload();t.textContent=f.name+(x.status===200?' — listo':' — error: '+x.responseText);i.style.width='100%';ok()};
  x.onerror=()=>{t.textContent=f.name+' — se cortó la conexión';ok()};x.send(f)})}
async function borrar(n){if(!confirm('¿Borrar «'+n+'»? También se borra en las demás computadoras.'))return;
  await fetch('/borrar?'+q({raiz,nombre:n}),{method:'POST'});cargar()}
async function cargar(){
  pestanas();migas();
  const r=await fetch('/lista?'+q({raiz,ruta}));if(r.status===401){location.reload();return}
  const ul=document.getElementById('lista');ul.innerHTML='';
  if(!r.ok){ul.innerHTML='';const li=document.createElement('li');li.className='vacio';li.textContent=await r.text();ul.append(li);return}
  const l=await r.json();
  if(!l.carpetas.length&&!l.archivos.length){ul.innerHTML='<li class="vacio">Vacío.</li>';return}
  for(const c of l.carpetas){const li=document.createElement('li');const a=document.createElement('a');a.href='#';a.className='carpeta';
    a.textContent='📁 '+c;a.onclick=e=>{e.preventDefault();ruta=ruta?ruta+'/'+c:c;cargar()};li.append(a);ul.append(li)}
  for(const a of l.archivos){const li=document.createElement('li');const e=document.createElement('a');
    e.href='/archivo?'+q({raiz,ruta:ruta?ruta+'/'+a.nombre:a.nombre});e.textContent=a.nombre;e.download=a.nombre;
    const s=document.createElement('span');s.textContent=fmt(a.tamano);li.append(e,s);
    if(info().borrar){const x=document.createElement('button');x.className='borrar';x.textContent='Borrar';x.onclick=()=>borrar(a.nombre);li.append(x)}
    ul.append(li)}}
cargar();setInterval(()=>{if(!info().carpetas)cargar()},5000);
</script></html>"""

EXTRA_UNIRSE = """<details><summary>Agregar otra computadora a la red</summary>
<p>Los teléfonos y equipos de paso solo necesitan esta dirección y un PIN. Para que otra computadora guarde una copia
de las carpetas compartida y personales, se sincronice y arranque sola al encenderla (necesita Python 3):</p>
<ol><li><a href="/_descargar">Descarga transferir.py</a></li>
<li>En esa computadora ejecuta:<br><code>python3 transferir.py --unirse %URL% --clave CÓDIGO</code><br>
(el código de unión lo da quien administra la red; en Windows usa <code>py</code> en vez de <code>python3</code>)</li></ol>
<p>Desde otra computadora también puedes abrir estas carpetas en el explorador de archivos por red (SMB):
<code>%SMB%</code></p>
</details>"""


class Manejador(BaseHTTPRequestHandler):
    server_version = "Transferir/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # -- respuestas
    def responder(self, codigo, cuerpo: bytes = b"", tipo="text/plain; charset=utf-8", extra=None):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(cuerpo)

    def texto(self, codigo, msg):
        self.responder(codigo, msg.encode())

    def enviar_archivo(self, ruta: Path, adjunto=True):
        n = ruta.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(n))
        if adjunto:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(ruta.name))
        self.end_headers()
        with open(ruta, "rb") as f:
            shutil.copyfileobj(f, self.wfile, BLOQUE)

    def leer_cuerpo(self, maximo):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(min(n, maximo)) if n else b""

    # -- sesiones
    def area_sesion(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return sesiones.get(c["sesion"].value) if "sesion" in c else None

    def bloqueado(self):
        f = fallos.get(self.client_address[0])
        return f and f[1] > time.time()

    def fallo(self):
        f = fallos.setdefault(self.client_address[0], [0, 0])
        f[0] += 1
        if f[0] >= 5:
            f[0], f[1] = 0, time.time() + 60

    def pagina_pin(self, codigo=200, error=""):
        self.responder(codigo, PAGINA_PIN.replace("%ESTILO%", ESTILO).replace("%ERROR%", error).encode(),
                       "text/html; charset=utf-8")

    # -- carpetas que puede ver cada sesión
    def raices(self, sesion):
        if sesion == "equipo":
            r = [{"id": "compartida", "nombre": area("compartida")["nombre"], "borrar": True}]
            r += [{"id": k, "nombre": n, "carpetas": True} for k, (n, _) in LOCALES.items()]
            return r
        return [{"id": sesion, "nombre": area(sesion)["nombre"], "borrar": True}]

    def resolver(self, sesion, raiz, ruta=""):
        """Ruta real dentro de una raíz permitida, o None. Las áreas replicadas son planas."""
        if raiz not in [r["id"] for r in self.raices(sesion)]:
            return None
        partes = [p for p in (ruta or "").replace("\\", "/").split("/") if p]
        if any(p in (".", "..") or p.startswith(".") for p in partes):
            return None
        if raiz in LOCALES:
            base = LOCALES[raiz][1].resolve()
        else:
            if len(partes) > 1:
                return None
            base = carpeta(raiz).resolve()
        p = base.joinpath(*partes).resolve() if partes else base
        if p != base and base not in p.parents:
            return None  # enlace simbólico que se sale de la carpeta
        return p

    def archivo_de(self, id_, nombre):
        nombre = nombre_seguro(nombre or "")
        if not nombre:
            return None
        p = carpeta(id_) / nombre
        return p if p.is_file() and p.resolve().parent == carpeta(id_).resolve() else None

    # -- GET
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path.startswith("/_nodo/"):
            return self.get_nodo(url, urllib.parse.parse_qs(url.query))
        sesion = self.area_sesion()
        if not sesion:
            return self.pagina_pin()
        if url.path == "/":
            n = len(estado["nodos"])
            nodos = "Sin otras computadoras" if n == 0 else f"Replicando con {n} computadora{'s' if n > 1 else ''}"
            ip = ips_locales()[0]
            extra = EXTRA_UNIRSE.replace("%URL%", html.escape(mi_url())).replace(
                "%SMB%", f"smb://{ip}" if platform.system() != "Windows" else f"\\\\{ip}")
            pagina = (PAGINA.replace("%ESTILO%", ESTILO).replace("%NODOS%", nodos)
                      .replace("%RAICES%", json.dumps(self.raices(sesion)).replace("</", "<\\/"))
                      .replace("%EXTRA%", extra if sesion == "equipo" else ""))
            return self.responder(200, pagina.encode(), "text/html; charset=utf-8")
        if url.path == "/lista":
            d = self.resolver(sesion, q.get("raiz", ""), q.get("ruta", ""))
            if not d or not d.is_dir():
                return self.texto(404, "Esa carpeta no existe")
            try:
                entradas = list(d.iterdir())
                carpetas, archivos = [], []
                for p in entradas:
                    if p.name.startswith(".") or p.name.endswith(".parcial"):
                        continue
                    try:
                        if p.is_dir():
                            carpetas.append(p.name)
                        elif p.is_file():
                            st = p.stat()
                            archivos.append((st.st_mtime, {"nombre": p.name, "tamano": st.st_size}))
                    except OSError:
                        continue
            except PermissionError:
                return self.texto(403, "Esta computadora no dio permiso para leer esa carpeta. En macOS: Ajustes del "
                                       "Sistema → Privacidad y seguridad → Acceso total al disco → agrega "
                                       + str(Path(sys.executable).resolve()))
            carpetas.sort(key=str.lower)
            archivos.sort(key=lambda t: t[0], reverse=True)
            return self.responder(200, json.dumps({"carpetas": carpetas, "archivos": [a for _, a in archivos]}).encode(),
                                  "application/json")
        if url.path == "/archivo":
            p = self.resolver(sesion, q.get("raiz", ""), q.get("ruta", ""))
            if not p or not p.is_file():
                return self.texto(404, "No existe")
            try:
                return self.enviar_archivo(p)
            except PermissionError:
                return self.texto(403, "Sin permiso para leer ese archivo")
        if url.path == "/_descargar":
            return self.enviar_archivo(Path(__file__).resolve())
        self.texto(404, "No existe")

    def get_nodo(self, url, q):
        if not firma_valida("GET", self.path, self.headers):
            return self.texto(403, "Firma inválida")
        registrar_nodo(self.headers.get("X-Nodo-Id"), self.headers.get("X-Nodo-URL", ""))
        id_ = q.get("area", [""])[0]
        if not area(id_):
            return self.texto(404, "Área desconocida")
        if url.path == "/_nodo/manifiesto":
            return self.responder(200, json.dumps(escanear(id_)).encode(), "application/json")
        if url.path == "/_nodo/archivo":
            p = self.archivo_de(id_, q.get("nombre", [""])[0])
            return self.enviar_archivo(p, adjunto=False) if p else self.texto(404, "No existe")
        self.texto(404, "No existe")

    # -- POST
    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path == "/pin":
            if self.bloqueado():
                return self.pagina_pin(429, '<p class="error">Demasiados intentos. Espera un minuto.</p>')
            pin = urllib.parse.parse_qs(self.leer_cuerpo(200).decode(errors="replace")).get("pin", [""])[0].strip()
            sesion = "equipo" if hmac.compare_digest(cfg["pin_equipo"], pin) else next(
                (a["id"] for a in cfg["areas"] if hmac.compare_digest(a["pin"], pin)), None)
            if not sesion:
                self.fallo()
                return self.pagina_pin(401, '<p class="error">PIN incorrecto</p>')
            token = secrets.token_urlsafe(24)
            sesiones[token] = sesion
            return self.responder(303, extra={"Location": "/", "Set-Cookie":
                                              f"sesion={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000"})
        if url.path == "/salir":
            c = SimpleCookie(self.headers.get("Cookie", ""))
            if "sesion" in c:
                sesiones.pop(c["sesion"].value, None)
            return self.responder(303, extra={"Location": "/", "Set-Cookie": "sesion=; Max-Age=0; Path=/"})
        if url.path == "/borrar":
            sesion = self.area_sesion()
            if not sesion:
                return self.texto(401, "Falta el PIN")
            raiz = q.get("raiz", "")
            if raiz in LOCALES or not self.resolver(sesion, raiz):
                return self.texto(403, "Aquí no se puede borrar")
            p = self.archivo_de(raiz, q.get("nombre", ""))
            if not p:
                return self.texto(404, "No existe")
            borrar_local(raiz, p.name, int(time.time() * 1000))
            guardar_estado()
            return self.texto(200, "ok")
        if url.path == "/_nodo/unirse":
            if self.bloqueado():
                return self.texto(429, "Demasiados intentos")
            try:
                datos = json.loads(self.leer_cuerpo(2000) or b"{}")
            except ValueError:
                datos = {}
            if not hmac.compare_digest(str(datos.get("clave", "")), cfg["clave_union"]):
                self.fallo()
                return self.texto(403, "Código de unión incorrecto")
            registrar_nodo(datos.get("nodo"), str(datos.get("url", "")))
            red = {k: cfg[k] for k in ("red", "secreto", "clave_union", "areas")}
            red["nodos"] = {cfg["nodo"]: {"url": mi_url(), "visto": int(time.time())}}
            return self.responder(200, json.dumps(red).encode(), "application/json")
        self.texto(404, "No existe")

    # -- PUT (subida)
    def do_PUT(self):
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path != "/subir":
            return self.texto(404, "No existe")
        sesion = self.area_sesion()
        if not sesion:
            return self.texto(401, "Falta el PIN")
        raiz = q.get("raiz", "")
        base = self.resolver(sesion, raiz, q.get("ruta", ""))
        nombre = nombre_seguro(q.get("nombre", ""))
        if not base or not base.is_dir():
            return self.texto(404, "Esa carpeta no existe")
        if not nombre:
            return self.texto(400, "Nombre de archivo no válido")
        try:
            restante = int(self.headers["Content-Length"])
        except (TypeError, ValueError):
            return self.texto(411, "Falta Content-Length")
        destino = base / nombre
        raiz_nombre, ext = os.path.splitext(nombre)
        n = 1
        while destino.exists():
            destino = base / f"{raiz_nombre} ({n}){ext}"
            n += 1
        tmp = base / f".{destino.name}.{secrets.token_hex(4)}.parcial"
        try:
            with open(tmp, "wb") as f:
                while restante > 0:
                    bloque = self.rfile.read(min(BLOQUE, restante))
                    if not bloque:
                        raise ConnectionError("conexión cortada")
                    f.write(bloque)
                    restante -= len(bloque)
            os.replace(tmp, destino)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            self.close_connection = True
            return self.texto(500, f"No se guardó: {e}")
        if raiz not in LOCALES:
            escanear(raiz)
        print(f"Recibido: {destino}")
        self.texto(200, "ok")


# ---------------------------------------------------------------- instalación

def instalar():
    INICIO.mkdir(parents=True, exist_ok=True)
    origen = Path(__file__).resolve()
    if origen != INSTALADO:
        shutil.copy2(origen, INSTALADO)
    py = sys.executable
    sistema = platform.system()
    if sistema == "Darwin":
        plist = Path.home() / "Library/LaunchAgents/local.transferir.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>local.transferir</string>
<key>ProgramArguments</key><array><string>{html.escape(py)}</string><string>-u</string><string>{html.escape(str(INSTALADO))}</string></array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>{html.escape(str(INICIO / 'registro.log'))}</string>
<key>StandardErrorPath</key><string>{html.escape(str(INICIO / 'registro.log'))}</string>
</dict></plist>""")
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        subprocess.run(["launchctl", "load", "-w", str(plist)], check=True)
    elif sistema == "Linux":
        unidad = Path.home() / ".config/systemd/user/transferir.service"
        unidad.parent.mkdir(parents=True, exist_ok=True)
        unidad.write_text(f"[Unit]\nDescription=Transferir archivos\nAfter=network-online.target\n\n[Service]\n"
                          f"ExecStart={py} -u {INSTALADO}\nRestart=always\n\n[Install]\nWantedBy=default.target\n")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "transferir.service"], check=True)
        subprocess.run(["loginctl", "enable-linger", os.environ.get("USER", "")], capture_output=True)
    elif sistema == "Windows":
        pyw = Path(py).with_name("pythonw.exe")
        inicio = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/transferir.vbs"
        inicio.write_text(f'CreateObject("WScript.Shell").Run """{pyw if pyw.exists() else py}"" ""{INSTALADO}""", 0\n')
        subprocess.Popen([str(pyw if pyw.exists() else py), str(INSTALADO)], creationflags=0x08000000)
    else:
        sys.exit(f"Sistema no soportado para arranque automático: {sistema}")
    print(f"Instalado en {INSTALADO}; arranca solo al iniciar la sesión.")


def desinstalar():
    sistema = platform.system()
    if sistema == "Darwin":
        plist = Path.home() / "Library/LaunchAgents/local.transferir.plist"
        subprocess.run(["launchctl", "unload", "-w", str(plist)], capture_output=True)
        plist.unlink(missing_ok=True)
    elif sistema == "Linux":
        subprocess.run(["systemctl", "--user", "disable", "--now", "transferir.service"], capture_output=True)
        (Path.home() / ".config/systemd/user/transferir.service").unlink(missing_ok=True)
    elif sistema == "Windows":
        (Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/transferir.vbs").unlink(missing_ok=True)
    print("Arranque automático quitado. Los archivos y la configuración siguen en su lugar.")


def unirse(url, clave, instalar_despues=True):
    global cfg
    url = url.rstrip("/")
    INICIO.mkdir(parents=True, exist_ok=True)
    previo = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    nodo = previo.get("nodo", secrets.token_hex(6))
    puerto = previo.get("puerto", 8000)
    cfg = {"nodo": nodo, "puerto": puerto}
    cuerpo = json.dumps({"clave": clave, "nodo": nodo, "url": mi_url()}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url + "/_nodo/unirse", data=cuerpo, method="POST",
                                                            headers={"Content-Type": "application/json"}), timeout=10) as r:
            red = json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"No se pudo unir: {e.read().decode(errors='replace')}")
    except OSError as e:
        sys.exit(f"No se pudo conectar con {url}: {e}")
    nueva = {**red, "nodo": nodo, "puerto": puerto,
             "carpeta_base": previo.get("carpeta_base", str(Path.home() / "Transferencias"))}
    nodos = nueva.pop("nodos", {})
    guardar_json(CONFIG, nueva)
    est = json.loads(ESTADO.read_text()) if ESTADO.exists() else {"lapidas": {}, "indice": {}, "nodos": {}}
    est.setdefault("nodos", {}).update(nodos)
    guardar_json(ESTADO, est)
    print("Esta computadora ya es parte de la red.")
    if instalar_despues:
        instalar()


def info():
    cargar(crear=False)
    print(f"Carpetas en: {Path(cfg['carpeta_base']).expanduser()}")
    for ip in ips_locales():
        print(f"Dirección: http://{ip}:{cfg['puerto']}")
    for a in cfg["areas"]:
        print(f"PIN {a['nombre']:<12} {a['pin']}")
    print(f"PIN {'Este equipo':<12} {cfg['pin_equipo']}   (Escritorio, Documentos y carpeta de usuario de ESTA computadora)")
    print(f"Código de unión: {cfg['clave_union']}")
    print(f"Computadoras conocidas: {len(estado['nodos'])}")
    for n in estado["nodos"].values():
        print(f"  {n['url']}")


def servir():
    cargar()
    for a in cfg["areas"]:
        escanear(a["id"])
    guardar_estado()
    servidor = ThreadingHTTPServer(("0.0.0.0", cfg["puerto"]), Manejador)
    servidor.daemon_threads = True
    for f in (bucle_sync, anunciar, escuchar_anuncios):
        threading.Thread(target=f, daemon=True).start()
    print(f"Transferir {VERSION} en " + ", ".join(f"http://{ip}:{cfg['puerto']}" for ip in ips_locales()), flush=True)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        guardar_estado()


def main():
    ap = argparse.ArgumentParser(description="Carpetas compartidas por el navegador, replicadas entre computadoras.")
    ap.add_argument("--instalar", action="store_true", help="arrancar solo al iniciar la sesión")
    ap.add_argument("--desinstalar", action="store_true")
    ap.add_argument("--unirse", metavar="URL", help="unir esta computadora a la red de otra (http://IP:8000)")
    ap.add_argument("--clave", help="código de unión (con --unirse)")
    ap.add_argument("--sin-instalar", action="store_true", help="con --unirse: no activar el arranque automático")
    ap.add_argument("--puerto", type=int, help="cambiar el puerto (se guarda en la configuración)")
    ap.add_argument("--info", action="store_true", help="mostrar direcciones, PINs y código de unión")
    a = ap.parse_args()
    if a.unirse:
        if not a.clave:
            sys.exit("Falta --clave CÓDIGO")
        unirse(a.unirse, a.clave, not a.sin_instalar)
    elif a.instalar:
        cargar()
        instalar()
    elif a.desinstalar:
        desinstalar()
    elif a.info:
        info()
    else:
        if a.puerto:
            cargar()
            cfg["puerto"] = a.puerto
            guardar_json(CONFIG, cfg)
        servir()


if __name__ == "__main__":
    main()
