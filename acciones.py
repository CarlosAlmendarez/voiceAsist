"""Respuestas rápidas sin Claude: frases cortas y comunes (hora, volumen, música,
abrir apps, temporizadores, clima, agenda) que se resuelven en local en menos
de un segundo. `atajo()` devuelve None si la frase no encaja; entonces decide Claude."""

import ctypes
import json
import re
import subprocess
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from motor import CFG, SIN_VENTANA, log

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]
NUMEROS = {"un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6,
           "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "quince": 15, "veinte": 20,
           "treinta": 30, "cuarenta": 40, "cuarenta y cinco": 45, "sesenta": 60, "noventa": 90}
# códigos WMO de Open-Meteo
CLIMA = {0: "despejado", 1: "mayormente despejado", 2: "parcialmente nublado", 3: "nublado",
         45: "con niebla", 48: "con niebla", 51: "con llovizna", 53: "con llovizna", 55: "con llovizna",
         56: "con llovizna helada", 57: "con llovizna helada", 61: "con lluvia ligera", 63: "con lluvia",
         65: "con lluvia fuerte", 66: "con lluvia helada", 67: "con lluvia helada", 71: "con nieve ligera",
         73: "con nieve", 75: "con nieve fuerte", 77: "con aguanieve", 80: "con chubascos",
         81: "con chubascos", 82: "con chubascos fuertes", 85: "con nevadas", 86: "con nevadas",
         95: "con tormenta", 96: "con tormenta y granizo", 99: "con tormenta y granizo"}
SITIOS = {"youtube": "https://www.youtube.com", "google": "https://www.google.com",
          "gmail": "https://mail.google.com", "correo": "https://mail.google.com",
          "whatsapp": "https://web.whatsapp.com",
          "netflix": "https://www.netflix.com", "github": "https://github.com",
          "drive": "https://drive.google.com", "maps": "https://maps.google.com",
          "youtube music": "https://music.youtube.com", "musica": "https://music.youtube.com",
          "podcasts": "https://music.youtube.com/podcasts", "podcast": "https://music.youtube.com/podcasts"}
APPS_FIJAS = {"calculadora": "calc.exe", "bloc de notas": "notepad.exe", "explorador": "explorer.exe",
              "explorador de archivos": "explorer.exe", "configuracion": "ms-settings:",
              "administrador de tareas": "taskmgr.exe", "panel de control": "control.exe"}

_apps: list[dict] | None = None
_ubicacion: dict | None = None


def normalizar(texto: str) -> str:
    t = unicodedata.normalize("NFD", texto.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")  # sin acentos
    t = re.sub(r"[¿?¡!.,;:\"«»]", " ", t)
    t = re.sub(r"^(oye |oiga |asistente |por favor )+|( por favor| asistente)+$", "", " ".join(t.split()))
    return t.strip()


def numero(t: str) -> float | None:
    if t in ("media",):
        return 0.5
    if re.fullmatch(r"\d+([.,]\d+)?", t):
        return float(t.replace(",", "."))
    return NUMEROS.get(t)


def hora_hablada(dt: datetime) -> str:
    """"las 4 y media de la tarde", "la una de la mañana"..."""
    h12 = dt.hour % 12 or 12
    art = "la una" if h12 == 1 else f"las {h12}"
    mins = {0: "", 15: " y cuarto", 30: " y media"}.get(dt.minute, f" y {dt.minute}")
    periodo = ("de la madrugada" if dt.hour < 6 else "de la mañana" if dt.hour < 12
               else "de la tarde" if dt.hour < 20 else "de la noche")
    return f"{art}{mins} {periodo}"


def fecha_hablada(d: date) -> str:
    return f"{DIAS[d.weekday()]} {d.day} de {MESES[d.month - 1]}"


def lista(cosas: list[str]) -> str:
    return cosas[0] if len(cosas) == 1 else f"{', '.join(cosas[:-1])} y {cosas[-1]}"


def resp(voz: str, detalle: str = "", tarjetas: list | None = None, **extra) -> dict:
    return {"voz": voz, "detalle": detalle, "tarjetas": tarjetas or [], **extra}


# ---------- control del PC ----------
def tecla(vk: int) -> None:
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP


def volumen():
    from pycaw.pycaw import AudioUtilities
    return AudioUtilities.GetSpeakers().EndpointVolume


def cambiar_volumen(nivel: float | None = None, delta: float = 0) -> int:
    v = volumen()
    v.SetMute(0, None)
    nuevo = min(max((v.GetMasterVolumeLevelScalar() if nivel is None else nivel) + delta, 0.0), 1.0)
    v.SetMasterVolumeLevelScalar(nuevo, None)
    return round(nuevo * 100)


def apps_instaladas() -> list[dict]:
    """Aplicaciones del menú Inicio (también las de la Store), vía Get-StartApps."""
    global _apps
    if _apps is None:
        salida = subprocess.run(["powershell", "-NoProfile", "-Command",
                                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                                 "Get-StartApps | ConvertTo-Json -Compress"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                creationflags=SIN_VENTANA)
        datos = json.loads(salida.stdout or "[]")
        _apps = [{"nombre": normalizar(a["Name"]), "visible": a["Name"], "id": a["AppID"]}
                 for a in (datos if isinstance(datos, list) else [datos])]
    return _apps


def buscar_app(nombre: str) -> dict | None:
    apps = apps_instaladas()
    for criterio in (lambda a: a["nombre"] == nombre, lambda a: a["nombre"].startswith(nombre),
                     lambda a: re.search(rf"\b{re.escape(nombre)}\b", a["nombre"])):
        encontradas = sorted((a for a in apps if criterio(a)), key=lambda a: len(a["nombre"]))
        if encontradas:
            return encontradas[0]
    return None


def abrir(objetivo: str) -> dict | None:
    if re.search(r"\b(proyecto|archivo|carpeta|codigo|y|que|donde|como)\b", objetivo):
        return None  # algo más complejo: que lo resuelva Claude
    if objetivo in APPS_FIJAS:
        subprocess.Popen(["cmd", "/c", "start", "", APPS_FIJAS[objetivo]], creationflags=SIN_VENTANA)
        return resp(f"Abriendo {objetivo}.")
    app = buscar_app(objetivo)
    if app:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app['id']}"])
        return resp(f"Abriendo {app['visible']}.")
    if objetivo in SITIOS:
        import webbrowser
        webbrowser.open(SITIOS[objetivo])
        return resp(f"Abriendo {objetivo}.")
    return resp(f"No encontré la aplicación {objetivo}.")


# ---------- música y podcasts (YouTube Music en el navegador) ----------
def buscar_youtube(consulta: str, ultimo_episodio: bool) -> tuple[dict | None, str]:
    """Busca en YouTube sin clave (yt-dlp). Con `ultimo_episodio`, localiza el canal del
    podcast y toma su video más reciente de 15 minutos o más (se salta clips y shorts).
    Devuelve (video, nombre del canal o "")."""
    import yt_dlp
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True,
                            "playlistend": 15, "extractor_args": {"youtube": {"lang": ["es"]}}})
    entradas = ydl.extract_info(f"ytsearch5:{consulta}{' podcast' if ultimo_episodio else ''}",
                                download=False).get("entries") or []
    videos = [e for e in entradas if e.get("ie_key") == "Youtube"]
    if ultimo_episodio:
        # el canal viene como resultado propio o, si no, es el del primer video
        tab = next((e for e in entradas if e.get("ie_key") == "YoutubeTab" and str(e.get("id")).startswith("UC")), None)
        canal_id = tab["id"] if tab else videos[0].get("channel_id") if videos else None
        nombre = (tab.get("channel") or tab.get("title")) if tab else videos[0].get("channel") if videos else ""
        if canal_id:
            recientes = ydl.extract_info(f"https://www.youtube.com/channel/{canal_id}/videos",
                                         download=False).get("entries") or []
            video = next((e for e in recientes if (e.get("duration") or 0) >= 900), recientes[0] if recientes else None)
            if video:
                return video, nombre or ""
    return (videos[0] if videos else None), ""


def reproducir(consulta: str, ultimo_episodio: bool, remoto: bool = False) -> dict:
    import webbrowser
    video, canal = buscar_youtube(consulta, ultimo_episodio)
    if not video:
        return resp(f"No encontré {consulta} en YouTube Music.")
    url = f"https://music.youtube.com/watch?v={video['id']}"
    nombre = f"el último episodio de {canal}" if canal else consulta.title()
    if remoto:  # desde el iPhone: se abre en el teléfono, no en la PC
        return resp(f"Aquí tienes {nombre}; tócalo para escucharlo.", f"**{video.get('title', '')}**",
                    tarjetas=[{"tipo": "fuentes", "enlaces": [{"titulo": video.get("title", consulta), "url": url}]}],
                    abrir=url)
    webbrowser.open(url)
    voz = f"Poniendo {nombre}."
    return resp(voz, f"**{video.get('title', '')}**", terminar=True,
                tarjetas=[{"tipo": "fuentes", "enlaces": [{"titulo": video.get("title", consulta), "url": url}]}])


# ---------- clima (Open-Meteo, sin clave) ----------
def leer_json(url: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "asistente-voz"}),
                                timeout=8) as r:
        return json.loads(r.read().decode())


def ubicar(ciudad: str | None = None) -> dict | None:
    global _ubicacion
    if ciudad is None and _ubicacion:
        return _ubicacion
    nombre = ciudad or CFG.get("ciudad")
    if not nombre:
        return None
    datos = leer_json("https://geocoding-api.open-meteo.com/v1/search?count=1&language=es&name="
                      + urllib.parse.quote(nombre)).get("results")
    if not datos:
        return None
    lugar = {"nombre": datos[0]["name"], "lat": datos[0]["latitude"], "lon": datos[0]["longitude"]}
    if ciudad is None:
        _ubicacion = lugar
    return lugar


def pronostico(lugar: dict) -> dict:
    return leer_json(
        f"https://api.open-meteo.com/v1/forecast?latitude={lugar['lat']}&longitude={lugar['lon']}"
        "&current=temperature_2m,weather_code&timezone=auto&forecast_days=7"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max")


def clima(ciudad: str | None, dia: int, semana: bool = False, lluvia: bool = False) -> dict:
    lugar = ubicar(ciudad)
    if not lugar:
        return resp("No sé en qué ciudad estás. Agrégala como «ciudad» en config.local.json." if not ciudad
                    else f"No encontré la ciudad {ciudad}.")
    p = pronostico(lugar)
    d = p["daily"]
    maxi, mini = round(d["temperature_2m_max"][dia]), round(d["temperature_2m_min"][dia])
    prob = d["precipitation_probability_max"][dia] or 0
    cielo = CLIMA.get(d["weather_code"][dia], "")
    cuando = "hoy" if dia == 0 else "mañana"
    en = f" en {lugar['nombre']}" if ciudad else ""
    if lluvia:
        juicio = "Sí, probablemente" if prob >= 60 else "Puede que sí" if prob >= 30 else "No parece"
        voz = f"{juicio}: {prob} por ciento de probabilidad de lluvia {cuando}{en}."
    elif semana:
        maximas = [round(x) for x in d["temperature_2m_max"]]
        lluviosos = [DIAS[date.fromisoformat(d["time"][i]).weekday()] for i in range(7)
                     if (d["precipitation_probability_max"][i] or 0) >= 50]
        voz = (f"Esta semana{en}, máximas de {min(maximas)} a {max(maximas)} grados"
               + (f"; posible lluvia el {lista(lluviosos)}." if lluviosos else " y sin lluvia a la vista."))
    elif dia == 0:
        ahora = p["current"]
        voz = (f"Ahora{en} hay {round(ahora['temperature_2m'])} grados, "
               f"{CLIMA.get(ahora['weather_code'], '')}. Máxima de {maxi}, mínima de {mini}"
               + (f" y {prob} por ciento de probabilidad de lluvia." if prob >= 20 else "."))
    else:
        voz = (f"Mañana{en} estará {cielo}, con máxima de {maxi} y mínima de {mini}"
               + (f", y {prob} por ciento de probabilidad de lluvia." if prob >= 20 else "."))
    dias = 7 if semana else 3
    filas = [[fecha_hablada(date.fromisoformat(d["time"][i])).capitalize(), CLIMA.get(d["weather_code"][i], ""),
              f"{round(d['temperature_2m_max'][i])}° / {round(d['temperature_2m_min'][i])}°",
              f"{d['precipitation_probability_max'][i] or 0} %"] for i in range(dias)]
    return resp(voz, tarjetas=[{"tipo": "tabla", "titulo": f"Clima en {lugar['nombre']}",
                                "columnas": ["Día", "Cielo", "Máx / mín", "Lluvia"], "filas": filas}])


# ---------- agenda ----------
def agenda(motor, dia: date) -> tuple[str, list]:
    """Frase hablada y filas de tabla con los eventos y recordatorios de un día."""
    filas, partes = [], []
    import calendario
    for o in calendario.listar(dia, 1):
        titulo = o["titulo"]
        if o["todo_el_dia"]:
            partes.append(titulo)
            filas.append(["Todo el día", titulo])
        else:
            ini = o["inicio_real"]
            partes.append(f"{titulo} a {hora_hablada(ini)}")
            filas.append([f"{ini:%H:%M}", titulo])
    for r in motor.recordatorios:
        if r["cuando"][:10] == dia.isoformat():
            ini = datetime.fromisoformat(r["cuando"])
            partes.append(f"recordarte {r['texto'].lower()} a {hora_hablada(ini)}")
            filas.append([f"{ini:%H:%M}", f"Recordatorio: {r['texto']}"])
    cuando = "hoy" if dia == date.today() else "mañana" if dia == date.today() + timedelta(days=1) \
        else f"el {fecha_hablada(dia)}"
    if not partes:
        return f"No tienes nada agendado {cuando}.", filas
    if len(partes) == 1:
        return f"{cuando.capitalize()} tienes {partes[0]}.", filas
    return f"{cuando.capitalize()} tienes {len(partes)} cosas: {lista(partes)}.", filas


def tabla_agenda(filas: list, titulo: str) -> list:
    return [{"tipo": "tabla", "titulo": titulo, "columnas": ["Hora", "Qué"], "filas": filas}] if filas else []


# ---------- servicios (vigía) ----------
SERVICIOS = re.compile(
    r"(como|que tal) (estan|andan|van) (mis|los) (servicios|servidores|sitios)( hoy| ahora)?|"
    r"(hay|tengo) (algo|algun servicio|algun servidor|algun sitio) caido( ahora)?|"
    r"(dame el |cual es el |como esta el )?estado de (mis|los) (servicios|servidores)|"
    r"(esta|estan) todo arriba|esta todo bien con (mis|los) (servicios|servidores)|se cayo algo")


def nombres_hablados(nombres: list[str], maximo: int = 3) -> str:
    if len(nombres) <= maximo:
        return lista(nombres)
    return f"{', '.join(nombres[:maximo])} y {len(nombres) - maximo} más"


def servicios() -> dict:
    """«¿Cómo están mis servicios?» → frase + tabla con lo que no está arriba."""
    import vigia
    try:
        e = vigia.estado()
    except vigia.ErrorVigia as err:
        return resp(f"{err}.")
    malos = vigia.no_arriba(e)
    total = e.get("total") or len(e.get("monitores", []))
    if not malos:
        return resp(f"Todo arriba: los {total} servicios responden.")
    n = len(malos)
    todos_caidos = all(m.get("estado") == "down" for m in malos)
    que = ("caído" if todos_caidos else "con problemas") if n == 1 else ("caídos" if todos_caidos else "con problemas")
    voz = f"Hay {n} servicio{'s' if n != 1 else ''} {que}: {nombres_hablados([m['nombre'] for m in malos])}."
    filas = [[vigia.nombre_monitor(m), m.get("estado_es") or m.get("estado", ""),
              f"{vigia.hora_corta(vigia.leer_hora(m.get('desde')))} ({m.get('hace', '?')})",
              str(m.get("msg") or "")[:80]] for m in malos]
    return resp(voz, tarjetas=[{"tipo": "tabla", "titulo": f"Servicios con problemas ({n} de {total})",
                                "columnas": ["Servicio", "Estado", "Desde", "Detalle"], "filas": filas}])


SEGURIDAD = re.compile(
    r"(como|que tal) (esta|anda|va) la seguridad( de (mis|los) (servidores|servicios))?( hoy| ahora)?|"
    r"(dame el |cual es el )?estado de (la )?seguridad( de (mis|los) servidores)?|"
    r"(hay )?algo raro en (mis|los) (servidores|servicios)( hoy| ahora)?|"
    r"(hay )?(algun|algo de) (problema|aviso|alerta) de seguridad( hoy| ahora)?")


def seguridad() -> dict:
    """«¿Cómo está la seguridad?» → frase corta + tabla por host (y el CSD si viene)."""
    import vigia
    try:
        r = vigia.seguridad()
    except vigia.ErrorVigia as err:
        return resp(f"{err}.")
    hosts = r.get("hosts") or {}
    corto = lambda h: h.split(".")[0]  # "web1.ejemplo.com" → "web1", más natural en voz
    sin_parentesis = lambda t: re.sub(r"\s*\([^)]*\)", "", str(t)).strip()
    avisos = []  # (nivel, quién, texto) de lo crítico o de atención
    filas = []
    for host, d in hosts.items():
        hall = d.get("hallazgos") or []
        avisos += [(h.get("nivel"), corto(host), h.get("texto") or h.get("id")) for h in hall
                   if h.get("nivel") in ("critico", "atencion")]
        m = d.get("metricas") or {}
        datos = [f"{etq}: {vigia.valor_metrica(m[k])}" for k, etq in (
            ("ssh_fallidos_24h", "SSH fallidos 24 h"), ("ips_baneadas_ahora", "baneadas"),
            ("paquetes_seguridad", "paq. seguridad"), ("disco_max_pct", "disco %"),
            ("tls_dias_min", "TLS días")) if m.get(k) is not None]
        filas.append([host, vigia.NIVELES.get(d.get("status"), d.get("status") or "?")
                      + (" · sin datos recientes" if vigia.sin_datos(d) else ""),
                      "; ".join(("" if h.get("nivel") in ("critico", "atencion") else "[info] ")
                                + str(h.get("texto") or h.get("id")) for h in hall) or "—",
                      " · ".join(datos)])
    csd = r.get("csd")
    if csd:
        hall = csd.get("hallazgos") or []
        avisos += [(h.get("nivel"), "los certificados de sello", h.get("texto") or h.get("id")) for h in hall
                   if h.get("nivel") in ("critico", "atencion")]
        p = csd.get("proximo") or {}
        filas.append(["CSD", vigia.NIVELES.get(csd.get("status"), csd.get("status") or "?")
                      + (" · sin datos recientes" if vigia.sin_datos(csd) else ""),
                      "; ".join(str(h.get("texto") or h.get("id")) for h in hall) or "—",
                      f"{csd.get('certificados', '?')} certificados"
                      + (f" · el próximo vence {p.get('vence')} ({p.get('dias')} días)" if p else "")])
    viejos = [corto(h) for h, d in hosts.items() if vigia.sin_datos(d)]
    n = len(hosts)
    if not avisos:
        voz = f"La seguridad está en orden en {'el servidor' if n == 1 else f'los {n} servidores'}."
    else:
        avisos.sort(key=lambda a: a[0] != "critico")
        criticos = sum(1 for a in avisos if a[0] == "critico")
        por_quien = {}  # agrupados por servidor, críticos primero
        for _, quien, texto in avisos:
            t = re.sub(r"\s+desde \d{4}-\d\d-\d\d\S*", "", sin_parentesis(texto))
            por_quien.setdefault(quien, []).append(t[:1].lower() + t[1:])
        partes = [f"en {quien}, {lista(textos)}" for quien, textos in list(por_quien.items())[:3]]
        voz = (f"Hay {len(avisos)} aviso{'s' if len(avisos) != 1 else ''} de seguridad"
               + ("" if not criticos else f", {criticos} crítico{'s' if criticos != 1 else ''}") + ": "
               + "; ".join(partes)
               + (f"; y en {len(por_quien) - 3} servidores más." if len(por_quien) > 3 else "."))
    if viejos:
        voz += f" No hay datos recientes de {lista(viejos)}."
    return resp(voz, tarjetas=[{"tipo": "tabla", "titulo": "Seguridad por servidor",
                                "columnas": ["Host", "Estado", "Hallazgos", "Datos"], "filas": filas}])


def novedades_servicios(desde: datetime | None, vigia_caido: bool = False) -> tuple[str, list]:
    """Una frase (o "") con las caídas que EMPEZARON desde `desde`, y su tabla.
    Con `vigia_caido` (el hilo de alertas ya sabe que no responde) no se espera el timeout."""
    import vigia
    if not desde or not vigia.configurado() or vigia_caido:
        return "", []
    try:
        r = vigia.incidentes(desde, datetime.now().replace(microsecond=0))
    except vigia.ErrorVigia as err:
        log(f"Resumen del día sin servicios: {err}")
        return "", []
    limite = desde.astimezone()
    nuevos = [i for i in r.get("incidentes", []) if (vigia.leer_hora(i.get("inicio")) or limite) >= limite]
    if not nuevos:
        return "", []
    siguen = [i for i in nuevos if i.get("en_curso")]
    nombres = list(dict.fromkeys(i["monitor"] for i in nuevos))
    voz = (f"Mientras no estabas hubo {len(nuevos)} caída{'s' if len(nuevos) != 1 else ''} "
           + (f"({lista(nombres)})" if len(nombres) <= 3 else f"en {len(nombres)} servicios"))
    voz += (": ya volvió todo." if not siguen else
            f"; {'sigue caído' if len(siguen) == 1 else f'siguen caídos {len(siguen)}'}"
            + (f" {siguen[0]['monitor']}." if len(siguen) == 1 else "."))
    filas = [[vigia.nombre_monitor(i), vigia.hora_corta(vigia.leer_hora(i.get("inicio"))),
              "sigue caído" if i.get("en_curso") else vigia.hora_corta(vigia.leer_hora(i.get("fin"))),
              i.get("duracion") or vigia.duracion(i.get("duracion_s", 0))] for i in nuevos]
    titulo = "Caídas mientras no estabas" + (f" (últimas 20 de {len(filas)})" if len(filas) > 20 else "")
    return voz, [{"tipo": "tabla", "titulo": titulo,
                  "columnas": ["Servicio", "Cayó", "Volvió", "Duración"], "filas": filas[-20:]}]


# ---------- reconocimiento de frases ----------
# ---------- comandos escritos con "/" ----------
# nombre → (argumentos, qué hace); se traducen a la frase que ya entiende atajo() o el motor
COMANDOS = {
    "ayuda": ("", "esta lista"),
    "hora": ("", "la hora"),
    "fecha": ("", "la fecha de hoy"),
    "dia": ("", "resumen del día (agenda, clima y novedades de servicios)"),
    "agenda": ("[mañana]", "lo que tienes hoy o mañana"),
    "clima": ("[ciudad] [mañana|semana]", "el clima"),
    "servicios": ("", "estado de tus servicios"),
    "seguridad": ("", "seguridad de tus servidores"),
    "volumen": ("[0-100|sube|baja]", "volumen de la PC"),
    "silencio": ("", "silencia la PC"),
    "sonido": ("", "quita el silencio"),
    "pausa": ("", "pausa o reanuda lo que suena"),
    "siguiente": ("", "siguiente canción"),
    "anterior": ("", "canción anterior"),
    "musica": ("[qué]", "pone música en YouTube Music"),
    "podcast": ("[cuál]", "último episodio de un podcast"),
    "abrir": ("<app o sitio>", "abre una app o un sitio"),
    "temporizador": ("<n> [min|seg|h]", "temporizador (minutos si no dices nada)"),
    "bloquear": ("", "bloquea la pantalla"),
    "nueva": ("", "empieza una conversación nueva con Claude"),
    "cancelar": ("", "cancela la tarea en marcha"),
}
ALIAS_COMANDOS = {"h": "hora", "help": "ayuda", "comandos": "ayuda", "manana": "agenda manana",
                  "hoy": "agenda", "tiempo": "clima", "vol": "volumen", "mute": "silencio", "play": "pausa",
                  "timer": "temporizador", "alarma": "temporizador", "bloquea": "bloquear", "abre": "abrir",
                  "buenosdias": "dia", "resumen": "dia", "srv": "servicios", "seg": "seguridad"}


def lista_comandos() -> list[list[str]]:
    """[["/clima [ciudad] [mañana|semana]", "el clima"], ...] para /ayuda y las sugerencias."""
    return [[f"/{n}" + (f" {a}" if a else ""), d] for n, (a, d) in COMANDOS.items()]


def ayuda_comandos() -> dict:
    filas = lista_comandos()
    return resp("Estos son los comandos con barra.", tarjetas=[{
        "tipo": "tabla", "titulo": "Comandos", "columnas": ["Comando", "Qué hace"], "filas": filas}])


def expandir_comando(texto: str) -> tuple[str, dict | None]:
    """"/clima monterrey" → ("clima en monterrey", None). Si no es un comando devuelve el
    texto tal cual; para /ayuda o un comando desconocido, (texto, respuesta)."""
    crudo = texto.strip()
    if not crudo.startswith("/"):
        return texto, None
    partes = normalizar(crudo[1:]).split()
    if not partes:
        return texto, ayuda_comandos()
    nombre, args = partes[0], partes[1:]
    if nombre in ALIAS_COMANDOS:
        alias = ALIAS_COMANDOS[nombre].split()
        nombre, args = alias[0], alias[1:] + args
    resto = " ".join(args)
    if nombre == "ayuda":
        return texto, ayuda_comandos()
    if nombre == "agenda":
        return ("que tengo manana" if "manana" in args else "que tengo hoy"), None
    if nombre == "clima":
        cuando = " manana" if "manana" in args else " esta semana" if "semana" in args else ""
        ciudad = " ".join(a for a in args if a not in ("manana", "semana", "hoy"))
        return f"clima{f' en {ciudad}' if ciudad else ''}{cuando}", None
    if nombre == "volumen":
        if not args:
            return "sube el volumen", None
        if re.fullmatch(r"\d{1,3}%?", args[0]):
            return f"pon el volumen al {args[0].rstrip('%')}", None
        if args[0] in ("sube", "+", "mas", "subir"):
            return "sube el volumen", None
        if args[0] in ("baja", "-", "menos", "bajar"):
            return "baja el volumen", None
        return texto, resp("Usa /volumen con un número del 0 al 100, «sube» o «baja».")
    if nombre == "musica":
        return (f"pon musica de {resto}" if resto else "pon musica"), None
    if nombre == "podcast":
        return (f"pon el podcast de {resto}" if resto else "abre podcasts"), None
    if nombre == "abrir":
        if not resto:
            return texto, resp("Dime qué abrir, por ejemplo /abrir gmail.")
        return f"abre {resto}", None
    if nombre == "temporizador":
        m = re.fullmatch(r"(\d+)\s*(s|seg|segundos?|m|min|minutos?|h|hr|horas?)?", resto)
        if not m:
            return texto, resp("Usa /temporizador con un número, por ejemplo /temporizador 10 o /temporizador 30 seg.")
        u = m.group(2) or "min"
        unidad = "segundos" if u.startswith("s") else "horas" if u.startswith("h") else "minutos"
        return f"pon un temporizador de {m.group(1)} {unidad if m.group(1) != '1' else unidad[:-1]}", None
    fijas = {"hora": "que hora es", "fecha": "que dia es hoy", "dia": "buenos dias",
             "servicios": "como estan mis servicios", "seguridad": "como esta la seguridad",
             "silencio": "silencia el sonido", "sonido": "quita el silencio", "pausa": "pausa",
             "siguiente": "siguiente", "anterior": "anterior", "bloquear": "bloquea la pantalla",
             "nueva": "nueva conversación", "cancelar": "cancela la tarea"}
    if nombre in fijas:
        return fijas[nombre], None
    return texto, resp(f"No conozco el comando /{nombre}. Escribe /ayuda para ver la lista.")


def atajo(texto: str, motor, remoto: bool = False) -> dict | None:
    t = normalizar(texto)
    if len(t.split()) > 12:
        return None
    ahora = datetime.now()

    if re.fullmatch(r"(muchas )?gracias|eso es todo|es todo|nada mas|ya es todo|listo gracias|ok gracias", t):
        return resp("De nada.", terminar=True)

    if re.fullmatch(r"(que|dime la|me dices la) hora (es|tienes)?|que horas son|dime la hora", t):
        return resp(f"{'Es' if ahora.hour % 12 == 1 else 'Son'} {hora_hablada(ahora)}.")

    if re.fullmatch(r"que (dia|fecha) es( hoy)?|a que (dia|fecha) estamos( hoy)?|que dia es manana|que fecha es hoy", t):
        return resp(f"Hoy es {fecha_hablada(ahora.date())}.")

    # volumen
    m = re.fullmatch(r"(pon|ajusta|sube|baja|cambia|deja)( el)? volumen (al?|en) (\d{1,3})( ?%| por ciento)?", t)
    if m:
        return resp(f"Volumen al {cambiar_volumen(nivel=int(m.group(4)) / 100)} por ciento.")
    if re.fullmatch(r"(sube(le)?|aumenta|subir)( el| mas el)? (volumen|sonido)( un poco| mas)?|mas volumen|subele", t):
        return resp(f"Volumen al {cambiar_volumen(delta=0.1)} por ciento.")
    if re.fullmatch(r"(baja(le)?|disminuye|bajar)( el| mas el)? (volumen|sonido)( un poco| mas)?|menos volumen|bajale", t):
        return resp(f"Volumen al {cambiar_volumen(delta=-0.1)} por ciento.")
    if re.fullmatch(r"(silencia|mutea|quita)( el)? (sonido|volumen|audio)|silencio total|mutea todo", t):
        volumen().SetMute(1, None)
        return resp("", "🔇 Sonido silenciado", terminar=True)
    if re.fullmatch(r"(quita el silencio|desmutea|activa el sonido|pon sonido|reactiva el sonido)", t):
        volumen().SetMute(0, None)
        return resp("Sonido activado.")

    # música y video (teclas multimedia: funcionan con Spotify, YouTube, etc.)
    if re.fullmatch(r"(pausa|pausar|deten|para|pon pausa a|reanuda|continua|reproduce|dale play a|quita la pausa a)"
                    r"( la| el)? (musica|cancion|video|reproduccion|podcast|episodio)|pausa|play", t):
        tecla(0xB3)  # VK_MEDIA_PLAY_PAUSE
        return resp("", "⏯ Pausa / reproducción", terminar=True)
    if re.fullmatch(r"(siguiente|pasa|salta(te)?|cambia)( la| a la| esta| de)? (cancion|tema|video)( siguiente)?|siguiente|la siguiente", t):
        tecla(0xB0)  # VK_MEDIA_NEXT_TRACK
        return resp("", "⏭ Siguiente", terminar=True)
    if re.fullmatch(r"(pon )?(la )?(cancion|tema) anterior|anterior|regresa( la)? cancion", t):
        tecla(0xB1)  # VK_MEDIA_PREV_TRACK
        return resp("", "⏮ Anterior", terminar=True)

    # temporizador
    m = re.fullmatch(r"(pon|programa|inicia|pon me|ponme|crea)( un| una)? (temporizador|timer|alarma|cronometro)"
                     r" (de|para|en|por) (\d+|[a-z]+( y cinco)?)( y media)? (segundos?|minutos?|horas?)", t)
    if m:
        n = numero(m.group(5))
        if n:
            n += 0.5 if m.group(7) else 0
            minutos = n / 60 if m.group(8).startswith("seg") else n * 60 if m.group(8).startswith("hora") else n
            motor.programar_recordatorio({"texto": f"Se acabó el temporizador de {m.group(5)}"
                                                   f"{m.group(7) or ''} {m.group(8)}.", "en_minutos": minutos,
                                          "temporizador": True})
            fin = ahora + timedelta(minutes=minutos)
            return resp(f"Listo, te aviso a {hora_hablada(fin)}." if minutos >= 5
                        else f"Listo, {m.group(5)}{m.group(7) or ''} {m.group(8)}.")

    # podcasts y música
    m = (re.fullmatch(r"(pon|ponme|reproduce|quiero escuchar)( el)? (ultimo |nuevo )?(episodio|capitulo)"
                      r"( mas reciente| nuevo)? de( el podcast)? (.+)", t)
         or re.fullmatch(r"(pon|ponme|reproduce|quiero escuchar)( el| un)? podcast( de)? (.+)", t))
    if m:
        return reproducir(m.group(m.lastindex), ultimo_episodio=True, remoto=remoto)
    m = (re.fullmatch(r"(pon|ponme|reproduce|quiero escuchar)( la| el| un poco de| algo de)? "
                      r"(musica|cancion|canciones|album|disco)( de)? (.+)", t)
         or re.fullmatch(r"(reproduce|quiero escuchar) (.+)", t))
    if m:
        return reproducir(m.group(m.lastindex), ultimo_episodio=False, remoto=remoto)
    if re.fullmatch(r"(pon|ponme|abre) (musica|un podcast|podcasts|youtube music)", t):
        import webbrowser
        webbrowser.open(SITIOS["podcasts" if "podcast" in t else "youtube music"])
        return resp("Abriendo YouTube Music.", terminar=True)

    # abrir aplicaciones o sitios
    m = re.fullmatch(r"(abre|abrir|abreme|inicia|ejecuta|lanza) (el |la |los |las |mi |un |una )?([a-z0-9 +.]{2,40})", t)
    if m and not re.fullmatch(r"(musica|cancion|temporizador|alarma|volumen|recordatorio).*", m.group(3)):
        return abrir(m.group(3).strip())

    if re.fullmatch(r"bloquea( la)? (pantalla|computadora|compu|pc|equipo|sesion)", t):
        return resp("Bloqueando.", terminar=True, despues=lambda: ctypes.windll.user32.LockWorkStation())

    # clima
    if re.search(r"\b(clima|temperatura|que tiempo hace|como esta el tiempo|va a llover|llovera|hay lluvia|hace (frio|calor))\b", t):
        m = re.search(r"\ben ([a-z ]{3,30}?)( hoy| manana| esta semana| la semana)?$", t)
        ciudad = m.group(1) if m and m.group(1) not in ("la semana", "esta semana") else None
        return clima(ciudad, 1 if re.search(r"\bmanana\b", t) else 0, semana="semana" in t,
                     lluvia=bool(re.search(r"llov|lluvia", t)))

    # agenda y resumen del día
    if re.fullmatch(r"(buenos dias|buen dia)|(dame el |mi )?resumen del dia|como (esta|viene) mi dia", t):
        voz_agenda, filas = agenda(motor, ahora.date())
        saludo = "Buenos días. " if ahora.hour < 12 else ""
        try:
            voz_clima = clima(None, 0)["voz"]
        except Exception as e:
            log(f"Clima no disponible: {e}")
            voz_clima = ""
        # actividad_previa = la ANTERIOR a esta conversación (atender() ya registró la actual)
        voz_servicios, tabla_servicios = novedades_servicios(
            getattr(motor, "actividad_previa", None), getattr(motor, "_vigia_falla_desde", None) is not None)
        return resp(f"{saludo}Hoy es {fecha_hablada(ahora.date())}. {voz_clima} {voz_agenda} {voz_servicios}"
                    .replace("  ", " ").strip(),
                    tarjetas=tabla_agenda(filas, "Hoy") + tabla_servicios)

    # estado de los servicios (vigía)
    if SERVICIOS.fullmatch(t):
        import vigia
        if vigia.configurado():
            return servicios()
    if SEGURIDAD.fullmatch(t):
        import vigia
        if vigia.configurado():
            return seguridad()
    m = re.fullmatch(r"(que|que cosas) tengo( agendado| en (la|mi) agenda| en el calendario| para)* (hoy|manana)"
                     r"( en (la|mi) agenda| en el calendario| agendado)?|(mi )?agenda (de|para) (hoy|manana)|"
                     r"que hay (hoy|manana) en (la|mi) agenda", t)
    if m:
        dia = ahora.date() + timedelta(days=1 if "manana" in t else 0)
        voz_agenda, filas = agenda(motor, dia)
        return resp(voz_agenda, tarjetas=tabla_agenda(filas, "Mañana" if "manana" in t else "Hoy"))

    return None
