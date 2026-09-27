"""Motor del asistente: micrófono, palabra de activación, voz a texto,
Claude Code (claude -p), tarjetas visuales y voz. Comunica todo a la
interfaz mediante la función `emitir(evento: dict)`."""

import asyncio
import base64
import ctypes
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import winsound
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
if (BASE / "modelos" / f"models--Systran--faster-whisper-{CFG['modelo_whisper']}").exists():
    os.environ.setdefault("HF_HUB_OFFLINE", "1")  # ya descargado: no consultar internet

import edge_tts
import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
from vosk import KaldiRecognizer, Model, SetLogLevel

RATE = 16000
BLOCK = 1600  # 100 ms
CAPTURAS = BASE / "capturas"
HISTORIAL = BASE / "historial.json"
MP3 = BASE / "respuesta.mp3"
EDGE = next((p for p in [
    Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", "")) / "Microsoft/Edge/Application/msedge.exe",
] if p.exists()), None)
CLAUDE = shutil.which("claude") or str(Path.home() / ".local/bin/claude.exe")
SIN_VENTANA = 0x08000000  # CREATE_NO_WINDOW

FORMATO = """Responde SIEMPRE y SOLO con un objeto JSON válido (sin texto antes ni después, sin ```), con esta forma:
{"voz": "...", "detalle": "...", "tarjetas": [...]}

- voz: lo que se leerá en voz alta. Español natural y breve (1 a 3 frases). Sin markdown, sin URLs, sin rutas de archivos, sin emojis.
- detalle: opcional. Información adicional para la pantalla; puede usar **negritas**, `código` y listas con "- ". Omítelo si "voz" basta.
- tarjetas: de 0 a 4 apoyos visuales, solo cuando aporten algo. Tipos:
  {"tipo":"captura","url":"https://...","titulo":"..."}  captura de una página web (también sirve http://localhost:PUERTO para servicios locales)
  {"tipo":"imagen","url":"https://...","titulo":"..."}  URL directa a una imagen (.jpg/.png/.webp) que hayas visto en una página consultada; si no tienes una, usa "captura"
  {"tipo":"codigo","archivo":"ruta relativa","lenguaje":"python","desde":10,"codigo":"..."}  fragmento exacto, máximo 25 líneas
  {"tipo":"estado","nombre":"...","url":"https://..."}  comprueba si un servicio responde
  {"tipo":"tabla","titulo":"...","columnas":["..."],"filas":[["..."]]}
  {"tipo":"dato","titulo":"...","valor":"...","nota":"..."}
  {"tipo":"fuentes","enlaces":[{"titulo":"...","url":"https://..."}]}

Si usaste información de internet, incluye una tarjeta "fuentes"."""

SISTEMA = """Eres un asistente de voz con pantalla. """ + FORMATO + """

El texto del usuario viene de un reconocimiento de voz y puede traer errores: palabras que suenan parecido, nombres propios o técnicos mal escritos, frases cortadas. Interpreta la intención más probable según el contexto (por ejemplo "Sequiro" → "Sekiro", "yeit" → "git"). Si de verdad es ambiguo, pregunta en "voz" en vez de adivinar.

En este modo solo puedes leer y buscar: puedes revisar los proyectos del usuario y la web, pero no modifiques nada.
Si el usuario pide HACER algo con efectos (crear o modificar archivos o proyectos, instalar, ejecutar comandos, arreglar un error, refactorizar...), no lo hagas: prepáralo. Investiga lo necesario (carpeta exacta del proyecto, archivos implicados, cómo se prueba) y añade al JSON:
  "tarea": {"descripcion": "resumen de 3 a 8 palabras", "carpeta": "ruta absoluta donde trabajar", "plan": "pasos concretos y detallados para quien la ejecute, incluyendo cómo verificar el resultado"}
En ese caso "voz" explica en una o dos frases qué vas a hacer y termina preguntando si lo confirmas. Nunca digas que ya está hecho. El usuario confirmará por voz y otro proceso la ejecutará con permisos de edición."""

SISTEMA_TAREA = """Eres el ejecutor de tareas de un asistente de voz. El usuario ya confirmó la tarea: hazla completa y de forma autónoma, sin pedir más confirmaciones, y verifica el resultado (ejecuta pruebas, compila o revisa la salida cuando se pueda).
No hagas nada destructivo o irreversible que no se haya pedido explícitamente: no borres datos, no hagas git push, no fuerces ni reescribas historial, no toques archivos fuera de lo necesario. Si algo bloquea la tarea, detente y explica qué falta.
Al terminar, """ + FORMATO + """
En "voz" resume lo que hiciste y el resultado (o qué falló) en 1 a 3 frases; en "detalle" lista los archivos cambiados y los comandos relevantes."""

SI = re.compile(r"\b(s[ií]|sip|confirm[oa]r?|hazlo|dale|adelante|claro|procede|ok(ay)?|de acuerdo|venga|correcto)\b")
NO = re.compile(r"\b(no|cancela(r|lo)?|olv[ií]dalo|espera|det[eé]n(te)?)\b")
CANCELAR_TAREA = re.compile(r"\b(cancela|det[eé]n|para)\w*\b.*\btareas?\b")

audio_q: "queue.Queue[bytes]" = queue.Queue()
emitir = lambda evento: None  # la interfaz lo reemplaza


def log(msg: str) -> None:
    linea = f"[{time.strftime('%H:%M:%S')}] {msg}"
    if sys.stdout:
        print(linea, flush=True)
    with open(BASE / "asistente.log", "a", encoding="utf-8") as f:
        f.write(linea + "\n")


def on_audio(indata, frames, t, status):
    audio_q.put(bytes(indata))


def vaciar_cola() -> None:
    while not audio_q.empty():
        try:
            audio_q.get_nowait()
        except queue.Empty:
            break


def rms(chunk: bytes) -> float:
    a = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


# ---------- tarjetas ----------
def es_http(url) -> bool:
    return isinstance(url, str) and urlparse(url).scheme in ("http", "https")


_edge_lock = threading.Lock()


def capturar(url: str) -> str | None:
    """Captura de pantalla de una web con Edge invisible; devuelve la ruta del PNG."""
    if not EDGE or not es_http(url):
        return None
    CAPTURAS.mkdir(exist_ok=True)
    destino = CAPTURAS / f"{int(time.time())}-{hashlib.md5(url.encode()).hexdigest()[:10]}.png"
    with _edge_lock:
        try:
            subprocess.run([
                str(EDGE), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                "--mute-audio", "--window-size=1280,800", "--virtual-time-budget=6000",
                f"--user-data-dir={BASE / 'modelos' / 'edge-perfil'}",
                f"--screenshot={destino}", url,
            ], capture_output=True, timeout=40, creationflags=SIN_VENTANA)
        except subprocess.TimeoutExpired:
            return None
    return str(destino) if destino.exists() else None


def data_uri(ruta: str | None) -> str | None:
    if not ruta or not Path(ruta).exists():
        return None
    return "data:image/png;base64," + base64.b64encode(Path(ruta).read_bytes()).decode()


def comprobar_estado(url: str) -> dict:
    if not es_http(url):
        return {"ok": False, "detalle": "URL no válida"}
    inicio = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "asistente-voz"})
        with urllib.request.urlopen(req, timeout=8) as r:
            codigo = r.status
    except urllib.error.HTTPError as e:
        codigo = e.code
    except Exception as e:
        return {"ok": False, "detalle": type(e).__name__, "ms": None}
    return {"ok": codigo < 500, "codigo": codigo, "ms": int((time.time() - inicio) * 1000)}


def resolver_tarjeta(t: dict) -> dict:
    if t["tipo"] == "captura":
        t["archivo_captura"] = capturar(t.get("url"))
        t["src"] = data_uri(t["archivo_captura"])
        t["error"] = None if t["src"] else "No se pudo capturar la página"
    elif t["tipo"] == "estado":
        t.update(comprobar_estado(t.get("url")))
    t["pendiente"] = False
    return t


def buscar_proyectos() -> dict[str, str]:
    """Carpetas con pinta de proyecto (git, package.json, pyproject...) en los sitios
    habituales. Sus nombres ayudan a Whisper a transcribirlos y a Claude a ubicarlos."""
    raiz = Path(CFG["carpeta_proyectos"])
    bases = [raiz] + [raiz / p for p in ("Desktop", "Documents", "OneDrive/Desktop", "OneDrive/Documents",
                                         "source/repos", "proyectos", "projects", "dev", "code")]
    marcas = (".git", "package.json", "pyproject.toml", "requirements.txt", "Cargo.toml", "go.mod", "pom.xml")
    encontrados = {}
    for base in bases:
        try:
            for d in base.iterdir():
                if d.is_dir() and not d.name.startswith(".") and any((d / m).exists() for m in marcas):
                    encontrados.setdefault(d.name, str(d))
        except OSError:
            continue
    return encontrados


def limpiar_capturas(dias: int = 7) -> None:
    if CAPTURAS.exists():
        limite = time.time() - dias * 86400
        for f in CAPTURAS.glob("*.png"):
            if f.stat().st_mtime < limite:
                f.unlink(missing_ok=True)


# ---------- historial ----------
def cargar_historial() -> list:
    try:
        return json.loads(HISTORIAL.read_text(encoding="utf-8"))
    except Exception:
        return []


def guardar_en_historial(item: dict) -> None:
    h = cargar_historial()
    guardado = dict(item)
    guardado["tarjetas"] = [{k: v for k, v in t.items() if k != "src"} for t in item["tarjetas"]]
    h.append(guardado)
    HISTORIAL.write_text(json.dumps(h[-100:], ensure_ascii=False, indent=1), encoding="utf-8")


def item_historial(indice: int) -> dict | None:
    h = cargar_historial()
    if not 0 <= indice < len(h):
        return None
    item = h[indice]
    for t in item["tarjetas"]:
        if t.get("tipo") == "captura":
            t["src"] = data_uri(t.get("archivo_captura"))
            if not t["src"]:
                t["error"] = "La captura ya no está guardada"
    return item


# ---------- Claude ----------
def describir_herramienta(nombre: str, e: dict) -> str:
    if nombre == "Read":
        return f"Leyendo {Path(e.get('file_path', '')).name}"
    if nombre == "Grep":
        return f"Buscando “{e.get('pattern', '')}” en el código"
    if nombre == "Glob":
        return f"Buscando archivos {e.get('pattern', '')}"
    if nombre == "WebSearch":
        return f"Buscando en la web: {e.get('query', '')}"
    if nombre == "WebFetch":
        return f"Consultando {urlparse(e.get('url', '')).netloc or 'una página'}"
    if nombre in ("Bash", "PowerShell"):
        return f"Ejecutando {e.get('command', '')[:50]}"
    if nombre in ("Edit", "MultiEdit", "NotebookEdit"):
        return f"Editando {Path(e.get('file_path') or e.get('notebook_path') or '').name}"
    if nombre == "Write":
        return f"Escribiendo {Path(e.get('file_path', '')).name}"
    if nombre == "TodoWrite":
        return "Organizando los pasos"
    if nombre in ("Task", "Agent"):
        return f"Delegando: {e.get('description', 'una subtarea')}"
    return f"Usando {nombre}"


def interpretar(texto: str) -> dict:
    limpio = re.sub(r"^```(?:json)?|```$", "", texto.strip(), flags=re.M).strip()
    ini, fin = limpio.find("{"), limpio.rfind("}")
    try:
        data = json.loads(limpio[ini:fin + 1])
        if isinstance(data, dict) and data.get("voz"):
            tarjetas = [t for t in data.get("tarjetas") or [] if isinstance(t, dict) and t.get("tipo")]
            tarea = data.get("tarea")
            if not (isinstance(tarea, dict) and tarea.get("descripcion") and tarea.get("plan")):
                tarea = None
            return {"voz": str(data["voz"]), "detalle": str(data.get("detalle") or ""),
                    "tarjetas": tarjetas[:4], "tarea": tarea}
    except (json.JSONDecodeError, ValueError):
        pass
    # respuesta sin formato: la primera parte se lee, el resto se muestra
    frases = re.split(r"(?<=[.!?])\s+", texto.strip())
    return {"voz": " ".join(frases[:2]), "detalle": " ".join(frases[2:]), "tarjetas": [], "tarea": None}


def es_si_o_no(texto: str) -> bool | None:
    t = texto.lower()
    si, no = bool(SI.search(t)), bool(NO.search(t))
    return si if si != no else None


def correr_claude(cmd: list, cwd: str, limite_seg: float, al_progreso, al_iniciar=None) -> dict | None:
    """Ejecuta `claude -p` en modo stream-json; informa cada herramienta usada y
    devuelve el evento final "result" (None si falló o se canceló)."""
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", creationflags=SIN_VENTANA)
    if al_iniciar:
        al_iniciar(proc)
    vigilante = threading.Timer(limite_seg, proc.kill)
    vigilante.start()
    resultado = None
    try:
        for linea in proc.stdout:
            try:
                ev = json.loads(linea)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "assistant":
                for c in ev["message"].get("content", []):
                    if c.get("type") == "tool_use":
                        al_progreso(describir_herramienta(c.get("name", ""), c.get("input") or {}))
            elif ev.get("type") == "result":
                resultado = ev
        proc.wait()
    finally:
        vigilante.cancel()
    if not resultado:
        log(f"Error de Claude (código {proc.returncode}): {proc.stderr.read()[-500:]}")
    return resultado


class Motor:
    def __init__(self):
        self.silenciado = False
        self.detener_voz = threading.Event()
        self.activar_manual = threading.Event()
        self.textos: "queue.Queue[str]" = queue.Queue()
        self.avisos: "queue.Queue[dict]" = queue.Queue()  # tareas terminadas por anunciar
        self.confirmaciones: queue.Queue = queue.Queue()  # bool (botón) o str (texto escrito)
        self.confirmando = False
        self.sesion = {"id": None, "ultima": 0.0}
        self.nota_contexto = ""  # resultado de la última tarea, para la siguiente pregunta
        self.tareas = ThreadPoolExecutor(max_workers=2)
        self.tareas_activas: dict[str, dict] = {}
        self.proyectos: dict[str, str] = {}
        self.whisper = None
        self.vocabulario = ""
        self._tts = None

    # --- control desde la interfaz ---
    def silenciar(self, valor: bool) -> None:
        self.silenciado = valor
        emitir({"tipo": "silenciado", "valor": valor})
        if not valor:
            emitir({"tipo": "estado", "estado": "reposo"})

    def preguntar_texto(self, texto: str) -> None:
        if not texto.strip():
            return
        (self.confirmaciones if self.confirmando else self.textos).put(texto.strip())

    def confirmar(self, valor: bool) -> None:
        self.confirmaciones.put(bool(valor))

    def cancelar_tarea(self, tid: str | None = None) -> int:
        """Cancela una tarea (o todas si no se indica cuál). Devuelve cuántas."""
        n = 0
        for i, t in list(self.tareas_activas.items()):
            if tid in (None, i):
                t["cancelada"] = True
                if t["proc"]:
                    t["proc"].kill()
                n += 1
        return n

    def nueva_conversacion(self) -> None:
        self.sesion["id"] = None
        self.nota_contexto = ""
        emitir({"tipo": "limpiar"})

    # --- voz ---
    def decir(self, texto: str) -> None:
        """Mensaje propio (sin Claude): se muestra y se lee."""
        emitir({"tipo": "respuesta", "voz": texto, "detalle": "", "tarjetas": []})
        self.hablar(texto)

    def hablar(self, texto: str) -> None:
        self.detener_voz.clear()
        limpio = re.sub(r"[`*#_>|]", "", texto)
        emitir({"tipo": "estado", "estado": "hablando"})
        try:
            if CFG["motor_voz"] != "neural":
                raise RuntimeError("voz de Windows elegida")
            self._hablar_neural(limpio)
        except Exception as e:
            if CFG["motor_voz"] == "neural":
                log(f"Voz neural no disponible ({e}); uso la de Windows.")
            emitir({"tipo": "palabras", "palabras": []})
            self._hablar_windows(limpio)
        vaciar_cola()  # descarta lo que el micrófono captó mientras hablaba
        emitir({"tipo": "estado", "estado": "reposo"})

    def _hablar_neural(self, texto: str) -> None:
        palabras, audio = [], bytearray()

        async def generar():
            com = edge_tts.Communicate(texto, CFG["voz_neural"], rate=CFG["velocidad_neural"],
                                       boundary="WordBoundary")
            async for parte in com.stream():
                if parte["type"] == "audio":
                    audio.extend(parte["data"])
                elif parte["type"] == "WordBoundary":
                    palabras.append({"t": parte["offset"] // 10000, "w": parte["text"]})

        asyncio.run(generar())
        MP3.write_bytes(audio)
        mci = ctypes.windll.winmm.mciSendStringW
        estado = ctypes.create_unicode_buffer(64)
        mci("close voz", None, 0, None)
        mci(f'open "{MP3}" type mpegvideo alias voz', None, 0, None)
        emitir({"tipo": "palabras", "palabras": palabras})
        mci("play voz", None, 0, None)
        while not self.detener_voz.is_set():
            time.sleep(0.05)
            mci("status voz mode", estado, 64, None)
            if estado.value != "playing":
                break
        mci("close voz", None, 0, None)

    def _hablar_windows(self, texto: str) -> None:
        if self._tts is None:
            import pyttsx3
            self._tts = pyttsx3.init()
            for v in self._tts.getProperty("voices"):
                if CFG["voz"].lower() in v.name.lower():
                    self._tts.setProperty("voice", v.id)
                    break
            self._tts.setProperty("rate", CFG["velocidad_voz"])
        self._tts.say(texto)
        self._tts.runAndWait()

    # --- escuchar ---
    def grabar_pregunta(self, interrumpir=None) -> np.ndarray | None:
        ruido, frames = [], []
        hablo, silencio, inicio, ultimo_nivel = False, 0.0, time.time(), 0.0
        while True:
            if interrumpir and interrumpir():
                return None
            try:
                chunk = audio_q.get(timeout=0.1)
            except queue.Empty:
                continue
            nivel = rms(chunk)
            if time.time() - ultimo_nivel > 0.08:
                emitir({"tipo": "nivel", "valor": min(nivel / 3000, 1.0)})
                ultimo_nivel = time.time()
            if len(ruido) < 3:  # primeros 300 ms: nivel de ruido de fondo
                ruido.append(nivel)
                continue
            umbral = max(np.mean(ruido) * 1.8, 250)
            frames.append(chunk)
            if nivel > umbral:
                hablo, silencio = True, 0.0
            elif hablo:
                silencio += BLOCK / RATE
                if silencio >= CFG["segundos_silencio_fin"]:
                    break
            transcurrido = time.time() - inicio
            if not hablo and transcurrido > 8:
                return None
            if transcurrido > CFG["segundos_max_pregunta"]:
                break
        audio = np.frombuffer(b"".join(frames), dtype=np.int16)
        return audio.astype(np.float32) / 32768.0

    def transcribir(self, audio: np.ndarray) -> tuple[str, bool]:
        """Devuelve (texto, confiable). Poco confiable = Whisper dudó mucho."""
        segs, _ = self.whisper.transcribe(
            audio, language="es", vad_filter=True, beam_size=5,
            condition_on_previous_text=False, initial_prompt=self.vocabulario or None)
        segs = [s for s in segs if s.no_speech_prob < 0.6]
        texto = " ".join(s.text for s in segs).strip()
        if not texto:
            return "", True
        # media de avg_logprob ponderada por la longitud de cada segmento
        logprob = sum(s.avg_logprob * len(s.text) for s in segs) / max(sum(len(s.text) for s in segs), 1)
        return texto, logprob >= CFG.get("confianza_minima_transcripcion", -1.0)

    def pedir_confirmacion(self) -> bool:
        """Espera un sí o un no, por voz, escrito o con los botones de la interfaz."""
        self.confirmando = True
        limite = time.time() + CFG.get("segundos_confirmar", 25)
        try:
            while time.time() < limite:
                emitir({"tipo": "estado", "estado": "confirmando"})
                audio = self.grabar_pregunta(interrumpir=lambda: not self.confirmaciones.empty())
                if not self.confirmaciones.empty():
                    v = self.confirmaciones.get()
                    v = es_si_o_no(v) if isinstance(v, str) else v
                elif audio is None:
                    continue
                else:
                    emitir({"tipo": "estado", "estado": "transcribiendo"})
                    texto, _ = self.transcribir(audio)
                    log(f"Tú (confirmación): {texto}")
                    v = es_si_o_no(texto) if texto else None
                if v is not None:
                    return v
                self.hablar("Perdona, ¿la hago? Dime sí o no.")
            return False
        finally:
            self.confirmando = False
            while not self.confirmaciones.empty():
                self.confirmaciones.get_nowait()

    # --- Claude ---
    def sistema(self) -> str:
        if not self.proyectos:
            return SISTEMA
        lista = "\n".join(f"- {n}: {p}" for n, p in sorted(self.proyectos.items()))
        return f"{SISTEMA}\n\nProyectos conocidos del usuario (nombre: carpeta):\n{lista}"

    def preguntar_a_claude(self, pregunta: str) -> dict:
        if self.nota_contexto:
            pregunta = f"{self.nota_contexto}\n\n{pregunta}"
            self.nota_contexto = ""
        cmd = [CLAUDE, "-p", pregunta, "--output-format", "stream-json", "--verbose",
               "--append-system-prompt", self.sistema(),
               "--allowedTools", CFG["herramientas_permitidas"]]
        if CFG.get("modelo_pregunta"):
            cmd += ["--model", CFG["modelo_pregunta"]]
        if self.sesion["id"] and time.time() - self.sesion["ultima"] < CFG["minutos_contexto"] * 60:
            cmd += ["--resume", self.sesion["id"]]
        emitir({"tipo": "progreso", "texto": "Pensando…"})
        resultado = correr_claude(cmd, CFG["carpeta_proyectos"], 300,
                                  lambda t: emitir({"tipo": "progreso", "texto": t}))
        if not resultado:
            return {"voz": "Hubo un error al consultar a Claude. Revisa el registro.",
                    "detalle": "", "tarjetas": [], "tarea": None}
        self.sesion.update(id=resultado.get("session_id"), ultima=time.time())
        return interpretar(resultado.get("result") or "No obtuve respuesta.")

    # --- tareas en segundo plano ---
    def lanzar_tarea(self, tarea: dict) -> None:
        info = {"proc": None, "cancelada": False}
        self.tareas_activas[tarea["id"]] = info
        emitir({"tipo": "tarea", "id": tarea["id"], "estado": "en_curso",
                "descripcion": tarea["descripcion"], "paso": "Empezando…"})
        threading.Thread(target=self._correr_tarea, args=(tarea, info), daemon=True).start()

    def _correr_tarea(self, tarea: dict, info: dict) -> None:
        tid, desc = tarea["id"], tarea["descripcion"]
        carpeta = tarea.get("carpeta") or ""
        if not Path(carpeta).is_dir():
            carpeta = CFG["carpeta_proyectos"]
        prompt = f"Tarea confirmada: {desc}\nCarpeta de trabajo: {carpeta}\n\nPlan:\n{tarea['plan']}"
        cmd = [CLAUDE, "-p", prompt, "--output-format", "stream-json", "--verbose",
               "--append-system-prompt", SISTEMA_TAREA,
               "--permission-mode", CFG.get("permisos_tarea", "auto"),
               "--model", CFG.get("modelo_tarea", "opus"), "--effort", CFG.get("esfuerzo_tarea", "high")]
        if CFG.get("herramientas_bloqueadas_tarea"):
            cmd += ["--disallowedTools", CFG["herramientas_bloqueadas_tarea"]]
        log(f"Tarea iniciada: {desc} ({carpeta})")

        def al_iniciar(proc):
            info["proc"] = proc
            if info["cancelada"]:
                proc.kill()

        resultado = correr_claude(cmd, carpeta, CFG.get("minutos_max_tarea", 60) * 60,
                                  lambda t: emitir({"tipo": "tarea", "id": tid, "estado": "en_curso", "paso": t}),
                                  al_iniciar)
        self.tareas_activas.pop(tid, None)
        if info["cancelada"]:
            log(f"Tarea cancelada: {desc}")
            emitir({"tipo": "tarea", "id": tid, "estado": "cancelada"})
            return
        if not resultado or resultado.get("is_error"):
            resp = {"voz": f"La tarea «{desc}» no pudo terminar. Revisa el registro para ver qué pasó.",
                    "detalle": str((resultado or {}).get("result") or ""), "tarjetas": []}
            estado = "error"
        else:
            resp = interpretar(resultado.get("result") or "Terminé, pero no obtuve un resumen.")
            resp.pop("tarea", None)
            estado = "hecha"
        log(f"Tarea {estado}: {desc} — {resp['voz']}")
        self.nota_contexto = (f"[Contexto: acabas de ejecutar en segundo plano la tarea «{desc}» en {carpeta}. "
                              f"Resultado: {resp['voz']} {resp['detalle']}]")
        emitir({"tipo": "tarea", "id": tid, "estado": estado})
        self.avisos.put({"descripcion": desc, "estado": estado, **resp})

    def anunciar(self, aviso: dict) -> None:
        emitir({"tipo": "aviso", "texto": ("Tarea terminada: " if aviso["estado"] == "hecha"
                                           else "Tarea fallida: ") + aviso["descripcion"]})
        winsound.Beep(988, 120)
        self.responder(f"[tarea] {aviso['descripcion']}",
                       {k: aviso[k] for k in ("voz", "detalle", "tarjetas")})

    # --- flujo de una pregunta ---
    def responder(self, pregunta: str, resp: dict) -> None:
        """Muestra una respuesta, resuelve sus tarjetas, la guarda y la lee."""
        item = {"fecha": time.strftime("%Y-%m-%d %H:%M"), "pregunta": pregunta, **resp}
        for i, t in enumerate(resp["tarjetas"]):
            t["id"] = f"t{int(time.time() * 1000)}-{i}"
            t["pendiente"] = t["tipo"] in ("captura", "estado")
        emitir({"tipo": "respuesta", **resp})

        pendientes = [(t, self.tareas.submit(resolver_tarjeta, t))
                      for t in resp["tarjetas"] if t["pendiente"]]

        def al_terminar():
            for t, fut in pendientes:
                try:
                    emitir({"tipo": "tarjeta", "tarjeta": fut.result()})
                except Exception as e:
                    t.update(pendiente=False, error=str(e))
                    emitir({"tipo": "tarjeta", "tarjeta": t})
            guardar_en_historial(item)

        threading.Thread(target=al_terminar, daemon=True).start()
        self.hablar(resp["voz"])

    def atender(self, texto: str) -> None:
        log(f"Tú: {texto}")
        emitir({"tipo": "pregunta", "texto": texto})
        t = texto.lower()
        if self.tareas_activas and CANCELAR_TAREA.search(t):
            n = self.cancelar_tarea()
            self.decir("Listo, cancelé la tarea." if n == 1 else f"Listo, cancelé {n} tareas.")
            return
        if re.fullmatch(r"\W*(cancela|olvídalo|nada)\W*", t):
            emitir({"tipo": "estado", "estado": "reposo"})
            return
        if re.search(r"\b(nueva conversación|empecemos de nuevo)\b", t):
            self.nueva_conversacion()
            self.decir("Listo, empezamos de nuevo.")
            return
        emitir({"tipo": "estado", "estado": "pensando"})
        resp = self.preguntar_a_claude(texto)
        tarea = resp.pop("tarea", None)
        log(f"Asistente: {resp['voz']}")
        if tarea:
            tarea["id"] = f"k{int(time.time() * 1000)}"
            resp["detalle"] = (resp["detalle"] + "\n\n" if resp["detalle"] else "") + \
                f"**Tarea propuesta:** {tarea['descripcion']}\n`{tarea.get('carpeta', '')}`\n{tarea['plan']}"
            while not self.confirmaciones.empty():  # clics viejos no cuentan
                self.confirmaciones.get_nowait()
            emitir({"tipo": "confirmar", "id": tarea["id"], "descripcion": tarea["descripcion"]})
        self.responder(texto, resp)
        if not tarea:
            return
        ok = self.pedir_confirmacion()
        emitir({"tipo": "confirmado", "id": tarea["id"], "valor": ok})
        log(f"Tarea {'confirmada' if ok else 'descartada'}: {tarea['descripcion']}")
        if ok:
            self.lanzar_tarea(tarea)
            self.hablar("De acuerdo, empiezo. Te aviso cuando termine.")
        else:
            self.hablar("Vale, no hago nada.")

    # --- bucle principal ---
    def ejecutar(self) -> None:
        SetLogLevel(-1)
        limpiar_capturas()
        palabra = CFG["palabra_activacion"].lower()
        emitir({"tipo": "estado", "estado": "cargando"})
        log("Cargando modelos...")
        self.proyectos = buscar_proyectos()
        # nombres que Whisper debe reconocer: vocabulario propio + nombres de proyectos
        self.vocabulario = ", ".join(CFG.get("vocabulario", []) + list(self.proyectos))[:600]
        vosk = Model(str(next((BASE / "modelos").glob("vosk-model-*"))))
        # Vocabulario completo (no gramática cerrada): con una lista de solo
        # [palabra, "[unk]"] el reconocedor forzaba cualquier frase a la palabra.
        wake = KaldiRecognizer(vosk, RATE)
        wake.SetWords(True)
        self.whisper = WhisperModel(CFG["modelo_whisper"], device="cpu", compute_type="int8",
                                    download_root=str(BASE / "modelos"))

        with sd.RawInputStream(samplerate=RATE, blocksize=BLOCK, dtype="int16",
                               channels=1, callback=on_audio):
            log(f'Listo. Di "{palabra}" para activarme.')
            emitir({"tipo": "estado", "estado": "reposo"})
            while True:
                try:
                    self.anunciar(self.avisos.get_nowait())
                    wake.Reset()
                    continue
                except queue.Empty:
                    pass
                try:
                    self.atender(self.textos.get_nowait())
                    wake.Reset()
                    continue
                except queue.Empty:
                    pass
                try:
                    chunk = audio_q.get(timeout=0.1)
                except queue.Empty:
                    chunk = None
                if self.silenciado:
                    vaciar_cola()
                    continue

                if self.activar_manual.is_set():
                    self.activar_manual.clear()
                elif chunk is None or not wake.AcceptWaveform(chunk):
                    continue
                else:
                    confs = [w["conf"] for w in json.loads(wake.Result()).get("result", [])
                             if w["word"] == palabra]
                    if not confs:
                        continue
                    if max(confs) < CFG["confianza_minima"]:
                        log(f"Ignorado: parecía \"{palabra}\" (confianza {max(confs):.2f})")
                        continue

                wake.Reset()
                vaciar_cola()
                winsound.Beep(880, 150)
                emitir({"tipo": "estado", "estado": "escuchando"})
                audio = self.grabar_pregunta()
                if audio is None:
                    winsound.Beep(440, 150)
                    emitir({"tipo": "estado", "estado": "reposo"})
                    continue
                winsound.Beep(660, 100)
                emitir({"tipo": "estado", "estado": "transcribiendo"})
                texto, confiable = self.transcribir(audio)
                if not texto:
                    emitir({"tipo": "pregunta", "texto": "…"})
                    self.decir("No te entendí.")
                elif not confiable:
                    log(f"Transcripción dudosa, descartada: {texto}")
                    emitir({"tipo": "pregunta", "texto": texto})
                    self.decir("No te entendí bien. ¿Me lo repites?")
                else:
                    self.atender(texto)
                wake.Reset()
