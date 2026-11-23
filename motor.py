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
if (BASE / "config.local.json").exists():  # datos personales, fuera de git
    CFG.update(json.loads((BASE / "config.local.json").read_text(encoding="utf-8")))

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
RECORDATORIOS = BASE / "recordatorios.json"  # avisos por voz pendientes en esta PC
EDGE = next((p for p in [
    Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", "")) / "Microsoft/Edge/Application/msedge.exe",
] if p.exists()), None)
CLAUDE = shutil.which("claude") or str(Path.home() / ".local/bin/claude.exe")
SIN_VENTANA = 0x08000000  # CREATE_NO_WINDOW
# comando que Claude ejecuta para leer la agenda local (python.exe, no pythonw, para que imprima)
CALENDARIO = f"{Path(sys.executable).with_name('python.exe').as_posix()} {(BASE / 'calendario.py').as_posix()}"
# y el que ejecuta para consultar el vigía (estado de los servicios); la clave nunca pasa por Claude
VIGIA = f"{Path(sys.executable).with_name('python.exe').as_posix()} {(BASE / 'vigia.py').as_posix()}"
ULTIMA_ACTIVIDAD = BASE / "ultima_actividad.json"  # cuándo usó el usuario el asistente por última vez
ALERTAS = BASE / "alertas.json"  # alertas del vigía ya vistas, reconocidas y resueltas
SIN_VIGIA = "vigia:sin_respuesta"  # id de la alerta propia cuando el vigía no contesta
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

FORMATO = """Responde SIEMPRE y SOLO con un objeto JSON válido (sin texto antes ni después, sin ```), con esta forma:
{"voz": "...", "detalle": "...", "tarjetas": [...]}

- voz: lo que se leerá en voz alta. UNA frase directa de máximo unas 20 palabras (dos solo si de verdad hace falta). Empieza por la respuesta: sin preámbulos ("Claro", "Supongo que te refieres a..."), sin repetir la pregunta y sin cerrar ofreciendo más ayuda. Español natural, sin markdown, URLs, rutas de archivos ni emojis. Todo lo demás va en "detalle".
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


def cargar_recordatorios() -> list:
    try:
        return json.loads(RECORDATORIOS.read_text(encoding="utf-8"))
    except Exception:
        return []


def guardar_recordatorios(lista: list) -> None:
    RECORDATORIOS.write_text(json.dumps(lista, ensure_ascii=False, indent=1), encoding="utf-8")


def escribir_json(ruta: Path, datos) -> None:
    """Escritura atómica (archivo temporal + os.replace): nunca deja el archivo a medias."""
    tmp = ruta.with_suffix(".tmp")
    tmp.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, ruta)


def leer_ultima_actividad():
    from datetime import datetime
    try:
        return datetime.fromisoformat(json.loads(ULTIMA_ACTIVIDAD.read_text(encoding="utf-8"))["fecha"])
    except Exception:
        return None


def cargar_alertas() -> dict:
    try:
        return json.loads(ALERTAS.read_text(encoding="utf-8")).get("alertas", {})
    except Exception:
        return {}


def en_horario_silencioso(ahora=None) -> bool:
    """True dentro de `horario_silencioso` ["HH:MM", "HH:MM"] (puede cruzar la medianoche)."""
    rango = CFG.get("horario_silencioso")
    if not rango or len(rango) != 2:
        return False
    hm = (ahora or time.localtime())
    actual = f"{hm.tm_hour:02d}:{hm.tm_min:02d}" if hasattr(hm, "tm_hour") else f"{hm:%H:%M}"
    ini, fin = rango
    return ini <= actual < fin if ini <= fin else (actual >= ini or actual < fin)


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
        if "calendario.py" in e.get("command", ""):
            return "Revisando tu calendario"
        if "vigia.py" in e.get("command", ""):
            return "Revisando tus servicios"
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
            evento = data.get("evento")
            if not (isinstance(evento, dict) and (evento.get("id") or (evento.get("titulo") and evento.get("inicio")))):
                evento = None
            local = data.get("recordatorio_local")
            if not (isinstance(local, dict) and local.get("texto") and (local.get("en_minutos") or local.get("cuando"))):
                local = None
            cancelar = [str(i) for i in data.get("cancelar_recordatorios") or [] if i]
            return {"voz": str(data["voz"]), "detalle": str(data.get("detalle") or ""),
                    "tarjetas": tarjetas[:4], "tarea": tarea, "evento": evento,
                    "recordatorio_local": local, "cancelar_recordatorios": cancelar}
    except (json.JSONDecodeError, ValueError):
        pass
    # respuesta sin formato: la primera parte se lee, el resto se muestra
    frases = re.split(r"(?<=[.!?])\s+", texto.strip())
    return {"voz": " ".join(frases[:2]), "detalle": " ".join(frases[2:]), "tarjetas": [],
            "tarea": None, "evento": None, "recordatorio_local": None, "cancelar_recordatorios": []}


def describir_evento(ev: dict) -> str:
    if ev.get("id"):  # cambio o borrado de uno existente: se describe con sus datos guardados
        import calendario
        guardado = calendario.buscar_por_id(ev["id"]) or {"titulo": ev.get("titulo") or ev["id"], "inicio": "?"}
        antes = describir_evento({**guardado, "id": None})
        if ev.get("borrar"):
            return f"Borrar {antes}"
        return f"Cambiar {antes} → {describir_evento({**guardado, **ev, 'id': None})}"
    cuando = ev["inicio"].replace("T", " ") + (f" → {ev['fin'].replace('T', ' ')}" if ev.get("fin") else "")
    repetir = {"diario": "cada día", "semanal": "cada semana", "mensual": "cada mes", "anual": "cada año"}
    rep_txt = repetir.get(ev.get("repetir") or "", "")
    if rep_txt and ev.get("hasta"):
        rep_txt += f" hasta el {ev['hasta']}"
    extras = [x for x in ("recordatorio" if ev.get("recordatorio") else "", rep_txt, ev.get("lugar")) if x]
    return f"{ev['titulo']} — {cuando}" + (f" ({', '.join(extras)})" if extras else "")


def pregunta_evento(ev: dict) -> str:
    """La pregunta de confirmación para crear, cambiar o borrar un evento."""
    if not ev.get("id"):
        return f"¿Añado «{ev['titulo']}» a tu agenda?"
    import calendario
    titulo = (calendario.buscar_por_id(ev["id"]) or {}).get("titulo") or ev.get("titulo") or "el evento"
    return f"¿Borro «{titulo}» de tu agenda?" if ev.get("borrar") else f"¿Cambio «{titulo}» en tu agenda?"


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
        self.recordatorios: list[dict] = cargar_recordatorios()
        self.whisper = None
        self.vocabulario = ""
        self._tts = None
        self.bloqueo = threading.RLock()  # una conversación a la vez (PC o iPhone)
        self.bloqueo_whisper = threading.Lock()
        self.buzon: list[dict] = []  # novedades para el iPhone (tareas, recordatorios, tarjetas)
        self.n_buzon = 0
        self.pendiente_remoto: dict | None = None  # tarea o evento esperando un sí desde el iPhone
        self._revision_agenda = 0.0
        self._avisados: set[str] = set()  # avisos de eventos ya dichos ("id@inicio")
        from datetime import datetime
        self._arranque = datetime.now()
        # última actividad ANTERIOR a la conversación actual ("¿qué pasó mientras no estaba?")
        self.actividad_previa = leer_ultima_actividad()
        self._bloqueo_actividad = threading.Lock()
        # alertas del vigía (las revisa un hilo propio; el bucle principal solo las dice)
        self._bloqueo_alertas = threading.Lock()
        self.alertas: dict[str, dict] = cargar_alertas()
        self.servicios = {"total": None, "no_arriba": None, "revisado": None}
        self._vigia_falla_desde: float | None = None
        self._primera_revision = True
        self._por_decir: list[str] = []  # frases de alertas críticas pendientes de decir

    def registrar_actividad(self) -> None:
        """Guarda que el usuario usó el asistente ahora. Si venía de una pausa de más de
        10 min, la actividad anterior pasa a ser la «previa» (se lee antes de sobrescribirla)."""
        from datetime import datetime, timedelta
        ahora = datetime.now().replace(microsecond=0)
        with self._bloqueo_actividad:
            anterior = leer_ultima_actividad()
            if anterior is None or ahora - anterior > timedelta(minutes=10):
                self.actividad_previa = anterior
            try:
                escribir_json(ULTIMA_ACTIVIDAD, {"fecha": ahora.isoformat()})
            except OSError as e:
                log(f"No se pudo guardar la última actividad: {e}")

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

    # --- recordatorios por voz ---
    def programar_recordatorio(self, r: dict) -> None:
        if r.get("en_minutos"):
            cuando = time.localtime(time.time() + float(r["en_minutos"]) * 60)
        else:
            cuando = time.strptime(r["cuando"][:16], "%Y-%m-%dT%H:%M")
        nuevo = {"id": f"r{int(time.time() * 1000) % 100000}", "texto": r["texto"],
                 "cuando": time.strftime("%Y-%m-%dT%H:%M:%S", cuando)}
        if r.get("temporizador"):
            nuevo["temporizador"] = True
        self.recordatorios = sorted(self.recordatorios + [nuevo], key=lambda x: x["cuando"])
        guardar_recordatorios(self.recordatorios)
        log(f"Recordatorio programado: {nuevo['cuando']} {nuevo['texto']}")

    def cancelar_recordatorios(self, ids: list[str]) -> None:
        self.recordatorios = [r for r in self.recordatorios if r["id"] not in ids]
        guardar_recordatorios(self.recordatorios)
        log(f"Recordatorios cancelados: {', '.join(ids)}")

    def recordatorio_vencido(self) -> dict | None:
        ahora = time.strftime("%Y-%m-%dT%H:%M:%S")
        if self.recordatorios and self.recordatorios[0]["cuando"] <= ahora:
            r = self.recordatorios.pop(0)
            guardar_recordatorios(self.recordatorios)
            return r
        return None

    def recordar(self, r: dict) -> None:
        self.publicar({"tipo": "aviso", "texto": f"Recordatorio: {r['texto']}",
                       "voz": r["texto"] if r.get("temporizador") else f"Te recuerdo: {r['texto']}",
                       "detalle": "", "tarjetas": []})
        atraso = time.time() - time.mktime(time.strptime(r["cuando"], "%Y-%m-%dT%H:%M:%S"))
        emitir({"tipo": "aviso", "texto": f"Recordatorio: {r['texto']}"})
        for f in (1175, 1397, 1175):
            winsound.Beep(f, 140)
        if atraso > 120:  # la PC estaba apagada o el asistente cerrado a esa hora
            dia = "" if r["cuando"][:10] == time.strftime("%Y-%m-%d") else f"del {r['cuando'][:10]} "
            self.decir(f"Tenías un recordatorio {dia}para las {r['cuando'][11:16]}: {r['texto']}")
        else:
            self.decir(r["texto"] if r.get("temporizador") else f"Te recuerdo: {r['texto']}")

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
    def grabar_pregunta(self, interrumpir=None, espera: float = 8) -> np.ndarray | None:
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
            if not hablo and transcurrido > espera:
                return None
            if transcurrido > CFG["segundos_max_pregunta"]:
                break
        audio = np.frombuffer(b"".join(frames), dtype=np.int16)
        return audio.astype(np.float32) / 32768.0

    def transcribir(self, audio: np.ndarray) -> tuple[str, bool]:
        """Devuelve (texto, confiable). Poco confiable = Whisper dudó mucho."""
        with self.bloqueo_whisper:
            segs, _ = self.whisper.transcribe(
                audio, language="es", vad_filter=True, beam_size=CFG.get("beam_whisper", 1),
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
        hoy = time.localtime()
        texto = f"{SISTEMA}\n\nAhora es {DIAS[hoy.tm_wday]} {time.strftime('%Y-%m-%d %H:%M', hoy)}."
        texto += f"""

Agenda: el usuario tiene una agenda local en esta PC (no usa Google Calendar). Léela ejecutando con Bash exactamente:
  {CALENDARIO} eventos --desde hoy|mañana|AAAA-MM-DD --dias N [--buscar "texto"]
Cada línea empieza con el id del evento entre corchetes. Úsalo para cualquier pregunta sobre su agenda, actividades, citas, reuniones, cumpleaños o disponibilidad. Si pregunta cuándo o dónde es algo suyo (un concierto, viaje, cita, partido, evento, "mi..."), busca PRIMERO en la agenda con --buscar y un rango amplio (--desde hoy --dias 366) antes de ir a la web. Calcula tú el rango (por ejemplo "esta semana" = desde hoy hasta el domingo). En "voz" di las horas de forma natural ("a las cuatro de la tarde"); si hay varios eventos, muéstralos en una tarjeta "tabla" (columnas Día, Hora, Evento).
Si el usuario pide AÑADIR un evento, actividad, cita, comida, recordatorio o cumpleaños, no uses "tarea": añade al JSON
  "evento": {{"titulo": "...", "inicio": "AAAA-MM-DD" (todo el día) o "AAAA-MM-DDTHH:MM", "fin": opcional (mismo formato; por defecto 1 hora o 1 día), "repetir": opcional "diario"|"semanal"|"mensual"|"anual", "hasta": opcional "AAAA-MM-DD" (último día en que se repite), "lugar": opcional, "descripcion": opcional, "recordatorio_min": opcional (minutos antes para avisar), "recordatorio": opcional true}}
Para CAMBIAR uno existente, busca su id y devuelve "evento": {{"id": "e...", y solo los campos que cambian}}; para BORRARLO, "evento": {{"id": "e...", "borrar": true}} (si se repite, se borra la serie completa: dilo).
Usa la próxima fecha futura que encaje ("el 10 de diciembre" = el próximo 10 de diciembre; "a las 3" sin día = hoy si aún no pasa). Los cumpleaños y aniversarios son de todo el día y se repiten cada año; su título es "Cumpleaños de <nombre>". Si falta la hora de una cita, pregúntala en vez de inventarla. Títulos cortos con mayúscula inicial ("Comida", "Dentista"). En "voz" repite qué vas a hacer y cuándo, y termina preguntando si lo confirmas; nunca digas que ya está hecho.
Un "recuérdame X" para otro día o para dentro de más de 12 horas es un evento con "recordatorio": true (titulo = lo que hay que recordar, inicio con fecha y hora): dura 15 minutos y el asistente lo dice en voz alta a esa hora. Los demás eventos con hora se avisan por voz {CFG.get("minutos_aviso_eventos", 10)} minutos antes. Si no dice la hora, pregúntala."""
        texto += """

Recordatorios por voz en esta PC: si pide que le recuerdes algo dentro de poco ("en 20 minutos", "en una hora", "a las seis" de hoy), añade al JSON
  "recordatorio_local": {"texto": "lo que hay que recordar, breve", "en_minutos": N}   o   {"texto": "...", "cuando": "AAAA-MM-DDTHH:MM"}
El asistente lo dirá en voz alta a esa hora (solo si la PC está encendida). No hace falta confirmación: en "voz" di cuándo avisarás ("Vale, te aviso a las seis y cuarto")."""
        if self.recordatorios:
            lista = "\n".join(f"- id {r['id']}: {r['cuando'].replace('T', ' ')} {r['texto']}" for r in self.recordatorios)
            texto += f"""
Recordatorios pendientes en esta PC:
{lista}
Para cancelar alguno, añade "cancelar_recordatorios": ["id", ...] y confírmalo en "voz"."""
        texto += self.sistema_servicios()
        if self.proyectos:
            lista = "\n".join(f"- {n}: {p}" for n, p in sorted(self.proyectos.items()))
            texto += f"\n\nProyectos conocidos del usuario (nombre: carpeta):\n{lista}"
        return texto

    def sistema_servicios(self) -> str:
        import vigia
        if not vigia.configurado():
            return ""
        from datetime import date, timedelta
        hoy = date.today()
        sabado = hoy - timedelta(days=(hoy.weekday() - 5) % 7)  # el sábado más reciente (o hoy)
        lunes = sabado + timedelta(days=2)
        fin_semana = f"--desde {sabado} --hasta {lunes}" if lunes <= hoy else f"--desde {sabado} (aún no termina)"
        previa = self.actividad_previa
        ausencia = (f"La última vez que el usuario usó el asistente antes de esta conversación fue el "
                    f"{previa:%Y-%m-%d} a las {previa:%H:%M}.") if previa else \
            "No se sabe cuándo usó el asistente por última vez."
        return f"""

Servicios: el usuario monitorea sus servidores y sitios con Uptime Kuma a través del «vigía» (solo lectura). Para cualquier pregunta sobre si sus servicios, servidores, sitios o APIs están arriba, caídos o tuvieron problemas, ejecuta con Bash exactamente uno de:
  {VIGIA} estado [--todos]        (qué NO está arriba ahora; --todos lista todos)
  {VIGIA} alertas                 (alertas activas: critico o atencion)
  {VIGIA} incidentes --desde hoy|ayer|AAAA-MM-DD[THH:MM] [--hasta AAAA-MM-DD[THH:MM]] [--dias N]   (caídas en un rango; horas locales; --hasta es exclusivo)
Rangos: "el fin de semana" = del sábado 00:00 al lunes 00:00 más recientes, es decir {fin_semana}. "Anoche", "hoy", "ayer", "esta semana": calcula tú el rango. {ausencia} "Mientras no estaba" o "desde la última vez" = desde ese momento hasta ahora (si fue hace menos de 2 horas o no se sabe, pregunta el rango). "En vacaciones" u otro periodo sin fechas claras: pregunta el rango en "voz" en vez de adivinarlo.
En "voz", un resumen corto: qué cayó, cuánto tiempo y si ya volvió ("El sábado se cayó la API de facturas dos horas; ya volvió"). Si no hubo incidentes, dilo en una frase ("Todo estuvo arriba el fin de semana"). El detalle va en una tarjeta "tabla" con columnas Servicio, Cayó, Volvió, Duración (Volvió = "sigue caído" si no ha vuelto). Si la herramienta responde "No pude contactar al vigía", dilo así; nunca inventes el estado."""

    def herramientas(self) -> str:
        permitidas = f"{CFG['herramientas_permitidas']},Bash({CALENDARIO} eventos:*)"
        import vigia
        if vigia.configurado():
            permitidas += f",Bash({VIGIA}:*)"  # solo lectura
        return permitidas

    def preguntar_a_claude(self, pregunta: str, remoto: bool = False) -> dict:
        progreso = (lambda t: self.publicar({"tipo": "progreso", "texto": t})) if remoto \
            else (lambda t: emitir({"tipo": "progreso", "texto": t}))
        if remoto:
            pregunta = ("[El usuario te habla desde su iPhone, lejos de la PC: no uses recordatorio_local "
                        "(para recordatorios usa un evento con \"recordatorio\": true; se avisa por voz en la PC y aquí). Lo que hagas en la PC "
                        "(volumen, abrir apps) no afecta al teléfono.]\n\n" + pregunta)
        if self.nota_contexto:
            pregunta = f"{self.nota_contexto}\n\n{pregunta}"
            self.nota_contexto = ""
        cmd = [CLAUDE, "-p", pregunta, "--output-format", "stream-json", "--verbose",
               "--append-system-prompt", self.sistema(),
               "--allowedTools", self.herramientas()]
        # sin servidores MCP ni ajustes de proyecto: arranca ~1.5 s más rápido
        cmd += ["--strict-mcp-config", "--setting-sources", "user"]
        if CFG.get("esfuerzo_pregunta"):
            cmd += ["--effort", CFG["esfuerzo_pregunta"]]
        if CFG.get("modelo_pregunta"):
            cmd += ["--model", CFG["modelo_pregunta"]]
        if self.sesion["id"] and time.time() - self.sesion["ultima"] < CFG["minutos_contexto"] * 60:
            cmd += ["--resume", self.sesion["id"]]
        progreso("Pensando…")
        resultado = correr_claude(cmd, CFG["carpeta_proyectos"], 300, progreso)
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
        self.publicar({"tipo": "aviso", "texto": ("Tarea terminada: " if estado == "hecha" else "Tarea fallida: ") + desc,
                       "voz": resp["voz"], "detalle": resp["detalle"], "tarjetas": resp["tarjetas"]})

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
        if resp["voz"]:
            self.hablar(resp["voz"])
        else:
            emitir({"tipo": "estado", "estado": "reposo"})

    def atender(self, texto: str) -> bool:
        """Responde a una frase. Devuelve False si la conversación terminó
        (entonces no se escucha un seguimiento)."""
        with self.bloqueo:
            self.registrar_actividad()
            return self._atender(texto)

    def _atender(self, texto: str) -> bool:
        log(f"Tú: {texto}")
        emitir({"tipo": "pregunta", "texto": texto})
        t = texto.lower()
        if self.tareas_activas and CANCELAR_TAREA.search(t):
            n = self.cancelar_tarea()
            self.decir("Listo, cancelé la tarea." if n == 1 else f"Listo, cancelé {n} tareas.")
            return True
        if re.fullmatch(r"\W*(cancela|olvídalo|nada)\W*", t):
            emitir({"tipo": "estado", "estado": "reposo"})
            return False
        if re.search(r"\b(nueva conversación|empecemos de nuevo)\b", t):
            self.nueva_conversacion()
            self.decir("Listo, empezamos de nuevo.")
            return True
        try:
            import acciones  # aquí y no arriba: acciones importa motor
            rapido = acciones.atajo(texto, self)
        except Exception as e:
            log(f"Atajo local falló, pregunto a Claude: {e}")
            rapido = None
        if rapido is not None:
            despues, seguir = rapido.pop("despues", None), not rapido.pop("terminar", False)
            log(f"Asistente (local): {rapido['voz'] or rapido['detalle']}")
            self.responder(texto, rapido)
            if despues:
                despues()
            return seguir
        emitir({"tipo": "estado", "estado": "pensando"})
        resp = self.preguntar_a_claude(texto)
        tarea, evento = resp.pop("tarea", None), resp.pop("evento", None)
        local, cancelar = resp.pop("recordatorio_local", None), resp.pop("cancelar_recordatorios", [])
        log(f"Asistente: {resp['voz']}")
        if cancelar:
            self.cancelar_recordatorios(cancelar)
        if local:
            try:
                self.programar_recordatorio(local)
            except (ValueError, TypeError) as e:
                log(f"Recordatorio no válido {local}: {e}")
                resp["voz"] = "Perdona, no pude programar ese recordatorio. ¿Me dices otra vez cuándo?"
        if tarea:
            propuesta = f"**Tarea propuesta:** {tarea['descripcion']}\n`{tarea.get('carpeta', '')}`\n{tarea['plan']}"
            pregunta = f"¿Ejecuto «{tarea['descripcion']}»?"
        elif evento:
            propuesta = f"**Evento propuesto:** {describir_evento(evento)}"
            pregunta = pregunta_evento(evento)
        cid = f"k{int(time.time() * 1000)}"
        if tarea or evento:
            resp["detalle"] = (resp["detalle"] + "\n\n" if resp["detalle"] else "") + propuesta
            while not self.confirmaciones.empty():  # clics viejos no cuentan
                self.confirmaciones.get_nowait()
            emitir({"tipo": "confirmar", "id": cid, "pregunta": pregunta})
        self.responder(texto, resp)
        if not (tarea or evento):
            return True
        ok = self.pedir_confirmacion()
        emitir({"tipo": "confirmado", "id": cid, "valor": ok})
        log(f"{'Confirmado' if ok else 'Descartado'}: {pregunta}")
        if not ok:
            self.hablar("Vale, no hago nada.")
        elif tarea:
            tarea["id"] = cid
            self.lanzar_tarea(tarea)
            self.hablar("De acuerdo, empiezo. Te aviso cuando termine.")
        else:
            self.crear_evento(evento)
        return True

    def seguimiento(self) -> bool:
        """Tras una respuesta hablada, escucha unos segundos sin palabra de activación.
        Devuelve True si hubo otra pregunta (y ya se atendió)."""
        segundos = CFG.get("segundos_seguimiento", 0)
        if not segundos or self.silenciado:
            return False
        vaciar_cola()
        emitir({"tipo": "estado", "estado": "seguimiento"})
        audio = self.grabar_pregunta(espera=segundos)
        texto = ""
        if audio is not None:
            emitir({"tipo": "estado", "estado": "transcribiendo"})
            texto, confiable = self.transcribir(audio)
            if texto and not confiable:
                log(f"Seguimiento dudoso, ignorado: {texto}")  # suele ser ruido o la tele
                texto = ""
        if not texto:
            emitir({"tipo": "estado", "estado": "reposo"})
            return False
        return self.atender(texto)

    def crear_evento(self, evento: dict) -> None:
        r = self._crear_evento(evento)
        emitir({"tipo": "respuesta", **r})
        self.hablar(r["voz"])

    def _crear_evento(self, evento: dict) -> dict:
        """Crea, cambia o borra (según traiga "id" / "borrar") un evento de la agenda local."""
        import calendario
        try:
            if evento.get("id") and evento.get("borrar"):
                ev, voz = calendario.borrar_evento(evento["id"]), "Listo, lo quité de tu agenda."
            elif evento.get("id"):
                ev, voz = calendario.modificar_evento(evento["id"], evento), "Listo, ya lo cambié."
            else:
                ev, voz = calendario.crear_evento(evento), "Listo, ya está en tu agenda."
        except KeyError:
            return {"voz": "No encontré ese evento en tu agenda.", "detalle": "", "tarjetas": []}
        except (ValueError, TypeError) as e:
            log(f"Evento no válido {evento}: {e}")
            return {"voz": "No pude guardar el evento: la fecha no es válida. ¿Me lo dices otra vez?",
                    "detalle": "", "tarjetas": []}
        log(f"Agenda: {voz} {describir_evento({**ev, 'id': None})} [{ev['id']}]")
        emitir({"tipo": "agenda", "dia": ev["inicio"][:10],
                "resaltar": None if evento.get("borrar") else ev["id"]})
        self.publicar({"tipo": "agenda"})
        return {"voz": voz, "detalle": "", "tarjetas": []}

    # --- avisos de la agenda ---
    def evento_por_avisar(self) -> dict | None:
        """La aparición de un evento cuya hora de aviso acaba de llegar (revisa cada 20 s)."""
        if time.time() - self._revision_agenda < 20:
            return None
        self._revision_agenda = time.time()
        import calendario
        from datetime import date, datetime, timedelta
        ahora = datetime.now()
        previo = CFG.get("minutos_aviso_eventos", 10)
        for o in calendario.listar(date.today(), 2):
            if o["todo_el_dia"]:
                continue
            if o.get("recordatorio"):
                minutos = o.get("recordatorio_min") or 0
            elif o.get("recordatorio_min") is not None:
                minutos = o["recordatorio_min"]
            elif previo:
                minutos = previo
            else:
                continue
            clave = f"{o['id']}@{o['inicio_real']:%Y-%m-%dT%H:%M}"
            aviso = o["inicio_real"] - timedelta(minutes=minutos)
            # solo avisos recientes: lo que pasó con el asistente cerrado no se anuncia tarde
            if clave not in self._avisados and max(aviso, self._arranque) <= ahora < aviso + timedelta(minutes=5):
                self._avisados.add(clave)
                return {**o, "minutos": minutos}
        return None

    def avisar_evento(self, o: dict) -> None:
        from acciones import hora_hablada
        if o.get("recordatorio"):
            texto = f"Te recuerdo: {o['titulo']}"
        elif o["minutos"] == 0:
            texto = f"Ya es hora: {o['titulo']}"
        else:
            texto = f"En {o['minutos']} minutos: {o['titulo']}, a {hora_hablada(o['inicio_real'])}"
        self.publicar({"tipo": "aviso", "texto": texto, "voz": texto, "detalle": "", "tarjetas": []})
        emitir({"tipo": "aviso", "texto": texto})
        emitir({"tipo": "agenda"})
        for f in (1175, 1397, 1175):
            winsound.Beep(f, 140)
        self.decir(texto)

    # --- alertas del vigía ---
    def vigilar_alertas(self) -> None:
        """Hilo propio: cada `segundos_revision_vigia` consulta el vigía. Va aparte del bucle
        principal para que un vigía caído (5 s de espera por consulta) no frene la escucha."""
        import vigia
        if not vigia.configurado():
            log("Vigía sin configurar (vigia_url / vigia_clave): lo de servicios queda desactivado.")
            return
        periodo = max(15, CFG.get("segundos_revision_vigia", 60))
        while True:
            try:
                self.revisar_alertas()
            except Exception as e:
                log(f"Error al revisar las alertas del vigía: {e}")
            time.sleep(periodo)

    def revisar_alertas(self) -> None:
        import vigia
        from datetime import datetime
        try:
            respuesta, estado = vigia.alertas(), vigia.estado()
        except vigia.ErrorVigia as err:
            self._vigia_sin_respuesta(str(err))
            return
        ahora = datetime.now().astimezone().isoformat(timespec="seconds")
        cambios = []
        with self._bloqueo_alertas:
            if self._vigia_falla_desde is not None:
                log("El vigía volvió a responder.")
            self._vigia_falla_desde = None
            self.servicios = {"total": estado.get("total") or len(estado.get("monitores", [])),
                              "no_arriba": len(vigia.no_arriba(estado)), "revisado": ahora}
            activas = {x["id"]: x for x in respuesta.get("alertas", []) if x.get("id")}
            for aid, x in activas.items():
                datos = {k: x.get(k) for k in ("tipo", "severidad", "monitor", "grupo", "desde",
                                                "duracion", "msg", "texto", "caido_en_silencio")}
                previa = self.alertas.get(aid)
                if previa is None:
                    self.alertas[aid] = {"id": aid, **datos, "vista": ahora, "reconocida": None, "resuelta": None}
                    cambios.append(("nueva", self.alertas[aid]))
                else:
                    previa.update(datos)
                    if previa.get("resuelta"):  # la misma caída volvió: se muestra otra vez
                        previa.update(resuelta=None, reconocida=None)
                        cambios.append(("nueva", previa))
            # solo con una respuesta válida se dan por resueltas (incluida la de «sin respuesta»)
            for aid, x in self.alertas.items():
                if not x.get("resuelta") and aid not in activas:
                    x["resuelta"] = ahora
                    cambios.append(("resuelta", x))
            self._podar_alertas()
            self._guardar_alertas()
        self._notificar(cambios)

    def _vigia_sin_respuesta(self, motivo: str) -> None:
        """Conserva el último estado conocido; tras 5 min sin respuesta crea su propia alerta."""
        from datetime import datetime
        cambios = []
        with self._bloqueo_alertas:
            if self._vigia_falla_desde is None:
                self._vigia_falla_desde = time.time()
                log(f"Vigía: {motivo}. Conservo el último estado conocido.")
            previa = self.alertas.get(SIN_VIGIA)
            if time.time() - self._vigia_falla_desde >= 300 and (previa is None or previa.get("resuelta")):
                desde = datetime.fromtimestamp(self._vigia_falla_desde).astimezone().isoformat(timespec="seconds")
                self.alertas[SIN_VIGIA] = {
                    "id": SIN_VIGIA, "tipo": "sin_respuesta", "severidad": "atencion", "monitor": "vigía",
                    "grupo": None, "desde": desde, "duracion": None, "msg": motivo,
                    "texto": "No puedo contactar al vigía", "caido_en_silencio": False,
                    "vista": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "reconocida": None, "resuelta": None}
                self._guardar_alertas()
                cambios.append(("nueva", self.alertas[SIN_VIGIA]))
        self._notificar(cambios)

    def _podar_alertas(self) -> None:
        """Olvida las resueltas hace más de 30 días (con el candado tomado)."""
        from datetime import datetime, timedelta
        limite = datetime.now().astimezone() - timedelta(days=30)
        for aid in [a for a, x in self.alertas.items() if x.get("resuelta")]:
            try:
                if datetime.fromisoformat(self.alertas[aid]["resuelta"]) < limite:
                    del self.alertas[aid]
            except ValueError:
                del self.alertas[aid]

    def _guardar_alertas(self) -> None:
        try:
            escribir_json(ALERTAS, {"alertas": self.alertas})
        except OSError as e:
            log(f"No se pudo guardar alertas.json: {e}")

    @staticmethod
    def alerta_publica(x: dict) -> dict:
        return {k: x.get(k) for k in ("id", "tipo", "severidad", "monitor", "grupo", "desde", "duracion",
                                       "msg", "texto", "caido_en_silencio", "reconocida", "resuelta")}

    def _notificar(self, cambios: list[tuple[str, dict]]) -> None:
        """Avisa a la interfaz y al iPhone, y prepara la voz de las críticas nuevas."""
        if not cambios:
            return
        for accion, x in cambios:
            log(f"Alerta {accion}: [{x.get('severidad')}] {x.get('texto')}")
            ev = {"tipo": "alerta", "accion": accion, "alerta": self.alerta_publica(x)}
            emitir(ev)
            self.publicar(ev)
        nuevas = [x for accion, x in cambios
                  if accion == "nueva" and x.get("severidad") == "critico" and not x.get("anunciada")]
        if not nuevas:
            return
        with self._bloqueo_alertas:
            for x in nuevas:
                x["anunciada"] = True  # una sola vez, aunque ahora no se pueda hablar
            self._guardar_alertas()
        if not self.puede_hablar_alertas():
            log("Alertas críticas sin voz (horario silencioso, micrófono silenciado o alertas_voz apagado).")
            return
        from datetime import datetime
        from acciones import lista, nombres_hablados
        arranque = self._arranque.astimezone()

        def antes_de_arrancar(x):
            try:
                return datetime.fromisoformat(x.get("desde") or "") < arranque
            except ValueError:
                return False

        fuera = [x for x in nuevas if antes_de_arrancar(x)]
        recientes = [x for x in nuevas if not antes_de_arrancar(x)]
        frases = []
        if fuera:  # surgieron con el asistente apagado: una sola frase agrupada
            nombres = nombres_hablados([x.get("monitor") or x.get("texto") for x in fuera])
            frases.append(f"Mientras estabas fuera hubo un problema crítico: {nombres}." if len(fuera) == 1
                          else f"Mientras estabas fuera hubo {len(fuera)} problemas críticos: {nombres}.")
        if len(recientes) == 1:
            x = recientes[0]
            frases.append(f"Alerta crítica: {x['monitor']} está caído." if x.get("tipo") == "caido" and x.get("monitor")
                          else f"Alerta crítica: {x.get('texto')}.")
        elif recientes:
            frases.append(f"Hay {len(recientes)} alertas críticas: "
                          f"{lista([x.get('monitor') or x.get('texto') for x in recientes][:3])}"
                          + (f" y {len(recientes) - 3} más." if len(recientes) > 3 else "."))
        with self._bloqueo_alertas:
            self._por_decir += frases

    def puede_hablar_alertas(self) -> bool:
        return bool(CFG.get("alertas_voz", True)) and not self.silenciado and not en_horario_silencioso()

    def alerta_por_decir(self) -> str | None:
        """La siguiente frase de alerta crítica, si ahora se puede hablar (si no, se descarta:
        ya está en el panel)."""
        with self._bloqueo_alertas:
            if not self._por_decir:
                return None
            if not self.puede_hablar_alertas():
                self._por_decir.clear()
                return None
            return self._por_decir.pop(0)

    def decir_alerta(self, frase: str) -> None:
        emitir({"tipo": "aviso", "texto": frase})
        for f in (1568, 1175, 1568, 1175):
            winsound.Beep(f, 120)
        self.decir(frase)

    def estado_alertas(self) -> dict:
        """Para el panel y el iPhone: resumen de servicios y alertas activas (críticas primero)."""
        import vigia
        if not vigia.configurado():
            return {"configurado": False, "alertas": []}
        with self._bloqueo_alertas:
            activas = [self.alerta_publica(x) for x in self.alertas.values() if not x.get("resuelta")]
            activas.sort(key=lambda x: x.get("desde") or "", reverse=True)
            activas.sort(key=lambda x: x.get("severidad") != "critico")
            return {"configurado": True, **self.servicios, "sin_respuesta": self._vigia_falla_desde is not None,
                    "alertas": activas}

    def reconocer_alerta(self, aid: str) -> bool:
        from datetime import datetime
        with self._bloqueo_alertas:
            x = self.alertas.get(aid)
            if not x or x.get("reconocida"):
                return False
            x["reconocida"] = datetime.now().astimezone().isoformat(timespec="seconds")
            self._guardar_alertas()
        log(f"Alerta vista: {x.get('texto')}")
        ev = {"tipo": "alerta", "accion": "reconocida", "alerta": self.alerta_publica(x)}
        emitir(ev)
        self.publicar(ev)
        return True

    # --- acceso desde el iPhone (remoto.py) ---
    def publicar(self, evento: dict) -> None:
        self.n_buzon += 1
        self.buzon = (self.buzon + [{"n": self.n_buzon, **evento}])[-100:]

    def novedades(self, desde: int) -> dict:
        return {"n": self.n_buzon, "eventos": [e for e in self.buzon if e["n"] > desde]}

    def atender_remoto(self, texto: str) -> dict:
        """Como atender(), pero devuelve la respuesta en vez de hablarla en la PC. Las
        confirmaciones llegan después por confirmar_remoto() o con un «sí» escrito o dicho."""
        with self.bloqueo:
            self.registrar_actividad()
            log(f"Tú (iPhone): {texto}")
            t = texto.lower()
            if self.pendiente_remoto:
                v = es_si_o_no(texto)
                if v is not None:
                    return self.confirmar_remoto(v)
                self.pendiente_remoto = None  # cambió de tema: se descarta lo pendiente
            if self.tareas_activas and CANCELAR_TAREA.search(t):
                n = self.cancelar_tarea()
                return {"voz": "Listo, cancelé la tarea." if n == 1 else f"Listo, cancelé {n} tareas.",
                        "detalle": "", "tarjetas": []}
            if re.search(r"\b(nueva conversación|empecemos de nuevo)\b", t):
                self.sesion["id"] = None
                self.nota_contexto = ""
                return {"voz": "Listo, empezamos de nuevo.", "detalle": "", "tarjetas": [], "limpiar": True}
            try:
                import acciones
                rapido = acciones.atajo(texto, self, remoto=True)
            except Exception as e:
                log(f"Atajo local falló, pregunto a Claude: {e}")
                rapido = None
            if rapido is not None:
                despues = rapido.pop("despues", None)
                rapido.pop("terminar", None)
                if despues:
                    despues()
                log(f"Asistente (local, iPhone): {rapido['voz'] or rapido['detalle']}")
                return rapido
            resp = self.preguntar_a_claude(texto, remoto=True)
            tarea, evento = resp.pop("tarea", None), resp.pop("evento", None)
            resp.pop("recordatorio_local", None)
            cancelar = resp.pop("cancelar_recordatorios", [])
            if cancelar:
                self.cancelar_recordatorios(cancelar)
            log(f"Asistente (iPhone): {resp['voz']}")
            if tarea or evento:
                cid = f"k{int(time.time() * 1000)}"
                if tarea:
                    tarea["id"] = cid
                    propuesta = f"**Tarea propuesta:** {tarea['descripcion']}\n`{tarea.get('carpeta', '')}`\n{tarea['plan']}"
                    pregunta = f"¿Ejecuto «{tarea['descripcion']}»?"
                else:
                    propuesta = f"**Evento propuesto:** {describir_evento(evento)}"
                    pregunta = pregunta_evento(evento)
                resp["detalle"] = (resp["detalle"] + "\n\n" if resp["detalle"] else "") + propuesta
                self.pendiente_remoto = {"id": cid, "tarea": tarea, "evento": evento, "pregunta": pregunta}
                resp["confirmar"] = {"id": cid, "pregunta": pregunta}
            # capturas y estados se resuelven aparte y llegan como novedades
            for i, tj in enumerate(resp["tarjetas"]):
                tj["id"] = f"t{int(time.time() * 1000)}-{i}"
                tj["pendiente"] = tj["tipo"] in ("captura", "estado")
            item = {"fecha": time.strftime("%Y-%m-%d %H:%M"), "pregunta": texto,
                    **{k: v for k, v in resp.items() if k != "confirmar"}}
            pendientes = [(tj, self.tareas.submit(resolver_tarjeta, tj)) for tj in resp["tarjetas"] if tj["pendiente"]]

            def al_terminar():
                for tj, fut in pendientes:
                    try:
                        self.publicar({"tipo": "tarjeta", "tarjeta": fut.result()})
                    except Exception as e:
                        tj.update(pendiente=False, error=str(e))
                        self.publicar({"tipo": "tarjeta", "tarjeta": tj})
                guardar_en_historial(item)

            threading.Thread(target=al_terminar, daemon=True).start()
            return resp

    def confirmar_remoto(self, valor: bool) -> dict:
        with self.bloqueo:
            p, self.pendiente_remoto = self.pendiente_remoto, None
            if not p:
                return {"voz": "No hay nada pendiente de confirmar.", "detalle": "", "tarjetas": []}
            log(f"{'Confirmado' if valor else 'Descartado'} desde el iPhone: {p['pregunta']}")
            base = {"confirmado": {"id": p["id"], "valor": valor}}
            if not valor:
                return {"voz": "Vale, no hago nada.", "detalle": "", "tarjetas": [], **base}
            if p["tarea"]:
                self.lanzar_tarea(p["tarea"])
                return {"voz": "De acuerdo, empiezo. Te aviso cuando termine.", "detalle": "", "tarjetas": [], **base}
            return {**self._crear_evento(p["evento"]), **base}

    # --- bucle principal ---
    def ejecutar(self) -> None:
        SetLogLevel(-1)
        limpiar_capturas()
        threading.Thread(target=self.vigilar_alertas, daemon=True, name="vigia").start()
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
        if CFG.get("clave_remota"):
            import remoto
            remoto.iniciar(self)

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
                vencido = self.recordatorio_vencido()
                if vencido:
                    self.recordar(vencido)
                    emitir({"tipo": "agenda"})
                    wake.Reset()
                    continue
                try:
                    proximo = self.evento_por_avisar()
                except Exception as e:
                    log(f"No se pudo revisar la agenda: {e}")
                    proximo = None
                if proximo:
                    self.avisar_evento(proximo)
                    wake.Reset()
                    continue
                # alertas críticas: solo con el asistente libre (ni una respuesta ni el iPhone en curso)
                if self._por_decir and self.bloqueo.acquire(blocking=False):
                    try:
                        frase = self.alerta_por_decir()
                        if frase:
                            self.decir_alerta(frase)
                    finally:
                        self.bloqueo.release()
                    if frase:
                        wake.Reset()
                        continue
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
                # tras "no te entendí" también se escucha: así basta con repetirlo
                if not texto or not confiable or self.atender(texto):
                    while self.seguimiento():
                        pass
                wake.Reset()
