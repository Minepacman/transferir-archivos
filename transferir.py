#!/usr/bin/env python3
"""Servidor HTTP local para pasar archivos entre equipos de la misma red desde el navegador.

Uso:
    python3 transferir.py [--carpeta DIR] [--puerto 8000] [--pin 1234 | --sin-pin]

Abre en otro equipo/teléfono la dirección que se imprime al arrancar. Sin dependencias (solo Python 3.8+).
"""
import argparse
import html
import json
import os
import secrets
import socket
import sys
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BLOQUE = 1024 * 1024
CARPETA: Path = Path.cwd()
PIN: str | None = None
SESION = secrets.token_urlsafe(24)  # galleta que se entrega tras escribir el PIN


def ip_local() -> list[str]:
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no envía nada: solo elige la interfaz de salida
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ips.discard("127.0.0.1")
    return sorted(ips) or ["127.0.0.1"]


def nombre_seguro(nombre: str) -> str | None:
    """Solo el nombre del archivo, sin carpetas ni trucos ('..', rutas absolutas, caracteres de control)."""
    nombre = nombre.replace("\\", "/").split("/")[-1].strip()
    nombre = "".join(c for c in nombre if c.isprintable())
    if not nombre or nombre in (".", "..") or nombre.startswith("."):
        return None
    return nombre[:200]


def destino_libre(nombre: str) -> Path:
    ruta = CARPETA / nombre
    base, ext = os.path.splitext(nombre)
    n = 1
    while ruta.exists():
        ruta = CARPETA / f"{base} ({n}){ext}"
        n += 1
    return ruta


def tamano(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return str(n)


PAGINA_PIN = """<!doctype html><html lang="es"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Transferir archivos</title>
<style>body{font:16px system-ui;background:#0d1117;color:#e6edf3;display:grid;place-items:center;height:100vh;margin:0}
form{background:#161b22;padding:24px;border-radius:12px;border:1px solid #30363d}input,button{font:inherit;padding:10px;border-radius:8px;border:1px solid #30363d}
input{background:#0d1117;color:#e6edf3;width:8em;text-align:center;letter-spacing:.3em}button{background:#238636;color:#fff;border:0;margin-left:8px}
p{color:#f85149}</style>
<form method="post" action="/pin"><h2>Escribe el PIN</h2><input name="pin" inputmode="numeric" autofocus autocomplete="off"><button>Entrar</button>%ERROR%</form></html>"""

PAGINA = """<!doctype html><html lang="es"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Transferir archivos</title>
<style>
:root{--f:#0d1117;--p:#161b22;--b:#30363d;--t:#e6edf3;--s:#8b949e;--a:#2f81f7;--v:#238636}
*{box-sizing:border-box}body{font:16px system-ui;background:var(--f);color:var(--t);margin:0;padding:16px;max-width:820px;margin:auto}
h1{font-size:1.4em}#zona{border:2px dashed var(--b);border-radius:12px;padding:32px 16px;text-align:center;cursor:pointer;background:var(--p)}
#zona.sobre{border-color:var(--a)}#zona b{color:var(--a)}.barra{height:8px;background:var(--b);border-radius:4px;overflow:hidden;margin-top:6px}
.barra i{display:block;height:100%;width:0;background:var(--v)}.subida{margin:10px 0;font-size:.9em}
ul{list-style:none;padding:0}li{display:flex;gap:12px;align-items:center;padding:10px;border-bottom:1px solid var(--b)}
li a{color:var(--t);flex:1;word-break:break-all;text-decoration:none}li a:hover{color:var(--a)}li span{color:var(--s);font-size:.85em;white-space:nowrap}
.vacio{color:var(--s)}
</style>
<h1>Transferir archivos</h1>
<div id="zona" tabindex="0">Arrastra archivos aquí o <b>toca para elegir</b><input id="archivos" type="file" multiple hidden></div>
<div id="subidas"></div>
<h2>Archivos en %CARPETA%</h2>
<ul id="lista"></ul>
<script>
const zona=document.getElementById('zona'),inp=document.getElementById('archivos');
zona.onclick=()=>inp.click();zona.onkeydown=e=>{if(e.key==='Enter'||e.key===' ')inp.click()};
inp.onchange=()=>{subir([...inp.files]);inp.value=''};
['dragenter','dragover'].forEach(ev=>zona.addEventListener(ev,e=>{e.preventDefault();zona.classList.add('sobre')}));
['dragleave','drop'].forEach(ev=>zona.addEventListener(ev,e=>{e.preventDefault();zona.classList.remove('sobre')}));
zona.addEventListener('drop',e=>subir([...e.dataTransfer.files]));
function fmt(n){for(const u of['B','KB','MB','GB']){if(n<1024||u==='GB')return(u==='B'?n:n.toFixed(1))+' '+u;n/=1024}}
async function subir(fs){for(const f of fs)await uno(f);cargar()}
function uno(f){return new Promise(ok=>{
  const d=document.createElement('div');d.className='subida';d.textContent=f.name+' — 0 %';
  const b=document.createElement('div');b.className='barra';const i=document.createElement('i');b.append(i);d.append(b);
  document.getElementById('subidas').prepend(d);
  const x=new XMLHttpRequest();x.open('PUT','/subir?nombre='+encodeURIComponent(f.name));
  x.upload.onprogress=e=>{if(e.lengthComputable){const p=Math.round(e.loaded*100/e.total);i.style.width=p+'%';d.firstChild.textContent=f.name+' — '+p+' %'}};
  x.onload=()=>{d.firstChild.textContent=f.name+(x.status===200?' — listo':' — error: '+x.responseText);i.style.width='100%';ok()};
  x.onerror=()=>{d.firstChild.textContent=f.name+' — se cortó la conexión';ok()};x.send(f)})}
async function cargar(){
  const r=await fetch('/lista');const l=await r.json();const ul=document.getElementById('lista');ul.innerHTML='';
  if(!l.length){ul.innerHTML='<li class="vacio">Todavía no hay archivos.</li>';return}
  for(const a of l){const li=document.createElement('li');const e=document.createElement('a');
    e.href='/archivo/'+encodeURIComponent(a.nombre);e.textContent=a.nombre;e.download=a.nombre;
    const s=document.createElement('span');s.textContent=fmt(a.tamano);li.append(e,s);ul.append(li)}}
cargar();setInterval(cargar,5000);
</script></html>"""


class Manejador(BaseHTTPRequestHandler):
    server_version = "Transferir/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.client_address[0]} · {fmt % args}\n")

    def autorizado(self) -> bool:
        if PIN is None:
            return True
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return "sesion" in c and secrets.compare_digest(c["sesion"].value, SESION)

    def responder(self, codigo, cuerpo: bytes, tipo="text/html; charset=utf-8", extra=None):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        ruta = urllib.parse.urlparse(self.path).path
        if not self.autorizado():
            return self.responder(200, PAGINA_PIN.replace("%ERROR%", "").encode())
        if ruta == "/":
            return self.responder(200, PAGINA.replace("%CARPETA%", html.escape(str(CARPETA))).encode())
        if ruta == "/lista":
            archivos = [{"nombre": p.name, "tamano": p.stat().st_size}
                        for p in sorted(CARPETA.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
                        if p.is_file() and not p.name.startswith(".")]
            return self.responder(200, json.dumps(archivos).encode(), "application/json")
        if ruta.startswith("/archivo/"):
            nombre = nombre_seguro(urllib.parse.unquote(ruta[len("/archivo/"):]))
            archivo = (CARPETA / nombre) if nombre else None
            if not archivo or not archivo.is_file() or archivo.resolve().parent != CARPETA:
                return self.responder(404, b"No existe")
            n = archivo.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(n))
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(archivo.name))
            self.end_headers()
            with open(archivo, "rb") as f:
                while bloque := f.read(BLOQUE):
                    self.wfile.write(bloque)
            return
        self.responder(404, b"No existe")

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/pin":
            return self.responder(404, b"No existe")
        n = min(int(self.headers.get("Content-Length") or 0), 1000)
        datos = urllib.parse.parse_qs(self.rfile.read(n).decode(errors="replace"))
        if PIN is not None and secrets.compare_digest(datos.get("pin", [""])[0].strip(), PIN):
            return self.responder(303, b"", extra={"Location": "/",
                                                    "Set-Cookie": f"sesion={SESION}; HttpOnly; SameSite=Strict; Path=/"})
        self.responder(401, PAGINA_PIN.replace("%ERROR%", "<p>PIN incorrecto</p>").encode())

    def do_PUT(self):
        url = urllib.parse.urlparse(self.path)
        if url.path != "/subir":
            return self.responder(404, b"No existe")
        if not self.autorizado():
            return self.responder(401, "Falta el PIN".encode(), "text/plain; charset=utf-8")
        nombre = nombre_seguro(urllib.parse.parse_qs(url.query).get("nombre", [""])[0])
        if not nombre:
            return self.responder(400, "Nombre de archivo no válido".encode(), "text/plain; charset=utf-8")
        try:
            restante = int(self.headers["Content-Length"])
        except (TypeError, ValueError):
            return self.responder(411, "Falta Content-Length".encode(), "text/plain; charset=utf-8")
        destino = destino_libre(nombre)
        temporal = CARPETA / f".{destino.name}.{secrets.token_hex(4)}.parcial"
        try:
            with open(temporal, "wb") as f:
                while restante > 0:
                    bloque = self.rfile.read(min(BLOQUE, restante))
                    if not bloque:
                        raise ConnectionError("conexión cortada")
                    f.write(bloque)
                    restante -= len(bloque)
            temporal.rename(destino)
        except Exception as e:
            temporal.unlink(missing_ok=True)
            return self.responder(500, f"No se guardó: {e}".encode(), "text/plain; charset=utf-8")
        print(f"Recibido: {destino.name} ({tamano(destino.stat().st_size)})")
        self.responder(200, b"ok", "text/plain")


def main():
    global CARPETA, PIN
    ap = argparse.ArgumentParser(description="Pasar archivos entre equipos de la misma red con el navegador.")
    ap.add_argument("--carpeta", default=str(Path.home() / "Transferencias"),
                    help="carpeta que se comparte y donde se guardan las subidas (por defecto ~/Transferencias)")
    ap.add_argument("--puerto", type=int, default=8000)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--pin", help="PIN que se pide en el navegador (por defecto uno al azar de 4 dígitos)")
    g.add_argument("--sin-pin", action="store_true", help="no pedir PIN (cualquiera en la red puede entrar)")
    a = ap.parse_args()

    CARPETA = Path(a.carpeta).expanduser().resolve()
    CARPETA.mkdir(parents=True, exist_ok=True)
    PIN = None if a.sin_pin else (a.pin or f"{secrets.randbelow(10000):04d}")

    servidor = ThreadingHTTPServer(("0.0.0.0", a.puerto), Manejador)
    print(f"Carpeta compartida: {CARPETA}")
    for ip in ip_local():
        print(f"Abre en el otro equipo:  http://{ip}:{a.puerto}")
    print(f"PIN: {PIN}" if PIN else "Sin PIN: cualquiera en esta red puede entrar.")
    print("Ctrl+C para detener.")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nDetenido.")


if __name__ == "__main__":
    main()
