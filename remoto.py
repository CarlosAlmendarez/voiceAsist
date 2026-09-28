"""Acceso desde el iPhone (u otro dispositivo) a través de Tailscale.

Servidor HTTP local (solo 127.0.0.1) que `tailscale serve` publica con HTTPS dentro
de la tailnet. Sirve una página web con botón para hablar (ui-remoto/) y una API
que también usa el Atajo de Siri. Todo se procesa en la PC con el mismo Motor.
Toda petición necesita la clave `clave_remota` (cookie o cabecera X-Clave)."""

import asyncio
import hmac
import io
import json
import threading
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import bottle
import edge_tts

from motor import BASE, CFG, log

UI = BASE / "ui-remoto"
app = bottle.Bottle()
motor = None  # Motor en marcha, lo asigna iniciar()


# ---------- autenticación ----------
def autorizado() -> bool:
    clave = CFG.get("clave_remota") or ""
    dada = bottle.request.get_header("X-Clave") or bottle.request.get_cookie("clave") or ""
    return bool(clave) and hmac.compare_digest(dada, clave)


def protegido(ruta):
    def envoltura(*a, **k):
        if not autorizado():
            bottle.abort(401, "Falta la clave de acceso.")
        return ruta(*a, **k)
    return envoltura


def respuesta_json(datos) -> str:
    bottle.response.content_type = "application/json; charset=utf-8"
    return json.dumps(datos, ensure_ascii=False)


# ---------- página ----------
@app.get("/")
def inicio():
    clave = bottle.request.query.get("clave")
    if clave:  # primer acceso desde el enlace con la clave: se guarda en una cookie
        if not hmac.compare_digest(clave, CFG.get("clave_remota") or ""):
            bottle.abort(401, "Clave incorrecta.")
        bottle.response.set_cookie("clave", clave, max_age=400 * 86400, path="/",
                                   httponly=True, secure=True, samesite="strict")
        bottle.redirect("/")
    if not autorizado():
        bottle.response.status = 401
        return "<h1>Asistente</h1><p>Abre el enlace con la clave que te dio el asistente.</p>"
    return bottle.static_file("index.html", root=str(UI))


@app.get("/<archivo:re:(remoto\\.(js|css)|manifest\\.json|icono\\.png)>")
def estatico(archivo):
    return bottle.static_file(archivo, root=str(UI))


# ---------- API ----------
@app.post("/api/preguntar")
@protegido
def preguntar():
    texto = ((bottle.request.json or {}).get("texto") or "").strip()
    if not texto:
        bottle.abort(400, "Falta el texto.")
    return respuesta_json(motor.atender_remoto(texto))


@app.post("/api/audio")
@protegido
def audio():
    from faster_whisper import decode_audio
    datos = bottle.request.body.read()
    if len(datos) < 1000:
        return respuesta_json({"texto": "", "voz": "No te escuché.", "detalle": "", "tarjetas": []})
    muestras = decode_audio(io.BytesIO(datos), sampling_rate=16000)
    texto, confiable = motor.transcribir(muestras)
    if not texto:
        return respuesta_json({"texto": "", "voz": "No te escuché.", "detalle": "", "tarjetas": []})
    if not confiable:
        return respuesta_json({"texto": texto, "voz": "No te entendí bien. ¿Me lo repites?",
                               "detalle": "", "tarjetas": []})
    return respuesta_json({"texto": texto, **motor.atender_remoto(texto)})


@app.post("/api/confirmar")
@protegido
def confirmar():
    valor = bool((bottle.request.json or {}).get("valor"))
    return respuesta_json(motor.confirmar_remoto(valor))


@app.get("/api/novedades")
@protegido
def novedades():
    desde = int(bottle.request.query.get("desde") or 0)
    return respuesta_json(motor.novedades(desde))


@app.get("/api/voz")
@protegido
def voz():
    texto = (bottle.request.query.get("t") or "")[:1500]
    audio = bytearray()

    async def generar():
        async for parte in edge_tts.Communicate(texto, CFG["voz_neural"], rate=CFG["velocidad_neural"]).stream():
            if parte["type"] == "audio":
                audio.extend(parte["data"])

    asyncio.run(generar())
    bottle.response.content_type = "audio/mpeg"
    return bytes(audio)


# ---------- servidor ----------
class ServidorHilos(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class Silencioso(WSGIRequestHandler):
    def log_message(self, *a):  # sin una línea por petición en el registro
        pass


def iniciar(m) -> None:
    global motor
    motor = m
    puerto = CFG.get("puerto_remoto", 8765)
    try:
        servidor = make_server("127.0.0.1", puerto, app, server_class=ServidorHilos, handler_class=Silencioso)
    except OSError as e:
        log(f"No se pudo iniciar el acceso remoto en el puerto {puerto}: {e}")
        return
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    log(f"Acceso remoto escuchando en 127.0.0.1:{puerto} (publícalo con: tailscale serve --bg {puerto})")
