"""Cliente del «vigía»: servicio de solo lectura sobre Uptime Kuma que corre en
otro equipo de la tailnet. Autenticación con la cabecera X-Clave.

Configuración: `vigia_url` en config.json y `vigia_clave` en config.local.json
(fuera de git). Si falta alguna, `configurado()` es False y todo lo de servicios
queda apagado.

Claude lo lee como herramienta desde el modo pregunta:
    python vigia.py estado [--todos]
    python vigia.py alertas
    python vigia.py seguridad
    python vigia.py incidentes [--desde hoy|ayer|AAAA-MM-DD[THH:MM]] [--hasta ...] [--dias N]

Nunca imprime la clave ni la URL del vigía."""

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

BASE = Path(__file__).parent
TIEMPO_ESPERA = 5  # segundos
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
ESTADOS = {"up": "arriba", "down": "caídos", "pending": "pendientes", "maintenance": "en mantenimiento"}
NIVELES = {"ok": "ok", "critico": "crítico", "atencion": "atención", "info": "informativo"}
ORIGENES = {"kuma": "servicio", "salud": "salud", "seguridad": "seguridad"}
# métricas de /api/seguridad → etiqueta legible (las desconocidas se imprimen tal cual)
METRICAS = {
    "ssh_fallidos_1h": "SSH fallidos 1 h", "ssh_fallidos_24h": "SSH fallidos 24 h",
    "ssh_accesos_24h": "accesos SSH 24 h", "ssh_ips_nuevas_24h": "IPs SSH nuevas 24 h",
    "fail2ban_activo": "fail2ban activo", "ips_baneadas_ahora": "IPs baneadas ahora", "bans_24h": "baneos 24 h",
    "web_peticiones_1h": "peticiones web 1 h", "web_4xx_1h": "web 4xx 1 h", "web_5xx_1h": "web 5xx 1 h",
    "disco_max_pct": "disco máx. %", "ram_pct": "RAM %", "swap_pct": "swap %", "carga_por_nucleo": "carga por núcleo",
    "pm2_reinicios_24h": "reinicios pm2 24 h", "pm2_no_online": "pm2 fuera de línea",
    "paquetes_pendientes": "paquetes pendientes", "paquetes_seguridad": "paquetes de seguridad",
    "reinicio_requerido": "reinicio requerido", "tls_dias_min": "días para que venza TLS",
}
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def _config() -> dict:
    cfg = {}
    for nombre in ("config.json", "config.local.json"):
        try:
            cfg.update(json.loads((BASE / nombre).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return cfg


CFG = _config()


class ErrorVigia(Exception):
    """Error con un mensaje apto para decirse en voz alta (sin URL ni clave)."""


def configurado() -> bool:
    return bool(CFG.get("vigia_url") and CFG.get("vigia_clave"))


def _pedir(ruta: str, params: dict | None = None) -> dict:
    if not configurado():
        raise ErrorVigia("El vigía no está configurado")
    url = CFG["vigia_url"].rstrip("/") + ruta + (f"?{urlencode(params)}" if params else "")
    req = urllib.request.Request(url, headers={"X-Clave": CFG["vigia_clave"], "User-Agent": "asistente-voz"})
    try:
        with urllib.request.urlopen(req, timeout=TIEMPO_ESPERA) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ErrorVigia("El vigía rechazó la clave") from None
        raise ErrorVigia(f"El vigía respondió con un error (HTTP {e.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        # sin detalles: el mensaje de urllib puede incluir la URL
        raise ErrorVigia("No pude contactar al vigía") from None


# ---------- API ----------
def estado() -> dict:
    """{generado, total, resumen: {up, down, ...}, monitores: [{id, nombre, grupo, activo,
    critico, status, estado, estado_es, desde, hace, ultimo_chequeo, msg, ping_ms}]}"""
    return _pedir("/api/estado")


def alertas() -> dict:
    """{generado, total, criticas, atencion, alertas: [{id, tipo, origen (kuma|salud|seguridad),
    severidad, motivo?, desde, duracion, texto, ...}]}. Las de Kuma traen monitor_id, monitor,
    grupo, caido_en_silencio, msg; las de seguridad traen nivel, host, grupo, hallazgo,
    vigente, dato_de. `motivo` puede faltar."""
    return _pedir("/api/alertas")


def seguridad() -> dict:
    """{generado, resumen: {critico, atencion, info}, hosts: {host: {status, vigente, ultimo_dato,
    edad_dato, ultimo_intento, sin_respuesta_desde, error, edad_reporte, hallazgos: [{id, nivel,
    texto, desde, hace}], metricas: {...}}}, csd: {status, vigente, ultimo_dato, edad_dato,
    sin_respuesta_desde, error, aviso_dias, certificados, proximo: {rfc, vence, dias},
    sin_certificado: [rfc], hallazgos}, recolector: {intervalo_s, ultimo_ciclo}}"""
    return _pedir("/api/seguridad")


def incidentes(desde: datetime, hasta: datetime) -> dict:
    """{desde, hasta, total_incidentes, en_curso, uptime_pct, por_grupo, por_monitor,
    incidentes: [{monitor_id, monitor, grupo, critico, inicio, fin, en_curso, duracion_s,
    duracion, msg, motivo?}], eventos_seguridad: [{hora, host, fuente, tipo, clave, nivel,
    nivel_anterior, texto}], eventos_salud: [...], nota_uptime}. Fechas sin zona = hora local."""
    return _pedir("/api/incidentes", {"desde": iso(desde), "hasta": iso(hasta)})


# ---------- ayudas ----------
def iso(d: datetime) -> str:
    return (d if d.tzinfo else d.astimezone()).isoformat(timespec="seconds")


def leer_hora(texto: str | None) -> datetime | None:
    """ISO del vigía → hora local con zona (None si no hay)."""
    if not texto:
        return None
    try:
        return datetime.fromisoformat(texto).astimezone()
    except ValueError:
        return None


def hora_corta(d: datetime | None, completa: bool = False) -> str:
    if d is None:
        return "?"
    d = d.astimezone()
    if d.date() == date.today() and not completa:
        return f"hoy {d:%H:%M}"
    return f"{DIAS[d.weekday()]} {d.day} {MESES[d.month - 1]} {d:%H:%M}"


def duracion(segundos: float) -> str:
    s = int(segundos)
    if s < 60:
        return f"{s} s"
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if d:
        return f"{d} d {h} h" if h else f"{d} d"
    if h:
        return f"{h} h {m} min" if m else f"{h} h"
    return f"{m} min"


def no_arriba(e: dict) -> list[dict]:
    """Monitores activos que no están arriba (caídos, pendientes, en mantenimiento...)."""
    return [m for m in e.get("monitores", []) if m.get("activo", True) and m.get("estado") != "up"]


def nombre_monitor(m: dict) -> str:
    return f"{m.get('grupo')} · {m.get('nombre') or m.get('monitor')}" if m.get("grupo") \
        else str(m.get("nombre") or m.get("monitor"))


def nombre_alerta(x: dict) -> str:
    """Nombre corto de lo afectado: el monitor, el host o el texto de la alerta."""
    return str(x.get("monitor") or x.get("host") or x.get("texto") or "alerta")


def fecha(texto: str, fin: bool = False) -> datetime:
    """hoy|ayer|ahora|AAAA-MM-DD[THH:MM] → datetime local sin zona."""
    t = texto.strip().lower()
    hoy = datetime.combine(date.today(), datetime.min.time())
    if t == "ahora":
        return datetime.now().replace(microsecond=0)
    if t == "hoy":
        return hoy
    if t == "ayer":
        return hoy - timedelta(days=1)
    if "t" in t or " " in t:
        return datetime.fromisoformat(texto.strip().replace(" ", "T")[:16])
    return datetime.combine(date.fromisoformat(t[:10]), datetime.min.time())


# ---------- CLI ----------
def cli_estado(todos: bool) -> int:
    e = estado()
    malos = no_arriba(e)
    print(f"Servicios: {e.get('total', len(e.get('monitores', [])))} · "
          + " · ".join(f"{v} {ESTADOS.get(k, k)}" for k, v in (e.get("resumen") or {}).items())
          + f" (revisado {hora_corta(leer_hora(e.get('generado')))})")
    for m in (e.get("monitores", []) if todos else malos):
        partes = [f"- [{m.get('id')}] {nombre_monitor(m)}: {m.get('estado_es') or m.get('estado')}"
                  f" desde {hora_corta(leer_hora(m.get('desde')))} ({m.get('hace', '?')})"]
        if m.get("critico"):
            partes.append("crítico")
        if m.get("estado") != "up" and m.get("msg"):
            partes.append(str(m["msg"])[:120])
        if not m.get("activo", True):
            partes.append("pausado")
        print(" | ".join(partes))
    if not todos and not malos:
        print("(todo arriba)")
    return 0


def cli_alertas() -> int:
    a = alertas()
    lista = a.get("alertas", [])
    print(f"Alertas: {a.get('total', len(lista))} ({a.get('criticas', 0)} críticas, {a.get('atencion', 0)} de atención)")
    for x in lista:
        origen = ORIGENES.get(x.get("origen"), x.get("origen"))
        partes = [f"- [{x.get('severidad')}{' · ' + origen if origen else ''}] {x.get('texto') or nombre_monitor(x)}",
                  f"desde {hora_corta(leer_hora(x.get('desde')))}"]
        if x.get("motivo"):
            partes.append(f"motivo: {str(x['motivo'])[:160]}")
        if x.get("msg") and x.get("msg") != x.get("motivo"):
            partes.append(str(x["msg"])[:120])
        if x.get("vigente") is False:
            partes.append(f"dato no vigente (de {hora_corta(leer_hora(x.get('dato_de')))})")
        print(" | ".join(partes))
    if not lista:
        print("(ninguna)")
    return 0


def cli_incidentes(desde: datetime, hasta: datetime) -> int:
    r = incidentes(desde, hasta)
    lista = r.get("incidentes", [])
    en_periodo = {p.get("monitor_id"): p.get("caido_s") for p in r.get("por_monitor", [])}
    print(f"Incidentes del {hora_corta(desde.astimezone(), True)} al {hora_corta(hasta.astimezone(), True)}: "
          f"{r.get('total_incidentes', len(lista))} ({r.get('en_curso', 0)} en curso) · "
          f"disponibilidad {r.get('uptime_pct', '?')} %")
    for i in lista:
        fin = "sigue caído" if i.get("en_curso") else f"volvió {hora_corta(leer_hora(i.get('fin')))}"
        partes = [f"- {nombre_monitor(i)}: cayó {hora_corta(leer_hora(i.get('inicio')))}, {fin}"
                  f" (duración total {i.get('duracion') or duracion(i.get('duracion_s', 0))}"
                  + (f", en el periodo {duracion(en_periodo[i.get('monitor_id')])}" if en_periodo.get(i.get("monitor_id")) else "")
                  + ")"]
        if i.get("critico"):
            partes.append("crítico")
        if i.get("motivo"):
            partes.append(f"motivo: {str(i['motivo'])[:160]}")
        if i.get("msg") and i.get("msg") != i.get("motivo"):
            partes.append(str(i["msg"])[:120])
        print(" | ".join(partes))
    for g, d in (r.get("por_grupo") or {}).items():
        if d.get("incidentes"):
            print(f"  grupo {g}: {d['incidentes']} incidente{'s' if d['incidentes'] != 1 else ''}, {d.get('monitores_afectados', '?')} de "
                  f"{d.get('monitores', '?')} monitores, caído {d.get('caido', '?')}, disponibilidad {d.get('uptime_pct', '?')} %")
    if not lista:
        print("(ninguno)")
    for clave, titulo in (("eventos_seguridad", "Eventos de seguridad"), ("eventos_salud", "Eventos de salud")):
        eventos = r.get(clave) or []
        if eventos:
            print(f"{titulo}: {len(eventos)}")
        for ev in eventos:
            nivel = NIVELES.get(ev.get("nivel"), ev.get("nivel"))
            antes = f" (antes {NIVELES.get(ev['nivel_anterior'], ev['nivel_anterior'])})" if ev.get("nivel_anterior") else ""
            print(f"- {hora_corta(leer_hora(ev.get('hora')))} {ev.get('host') or ev.get('fuente') or ''}: "
                  f"[{nivel}{antes}] {ev.get('texto') or ev.get('clave')}"
                  + (f" ({ev['tipo']})" if ev.get("tipo") and ev.get("tipo") != "hallazgo" else "")
                  + (f" | motivo: {ev['motivo']}" if ev.get("motivo") else ""))
    return 0


def valor_metrica(v) -> str:
    if isinstance(v, bool):
        return "sí" if v else "no"
    if isinstance(v, list):
        return ", ".join(map(str, v)) or "ninguno"
    return str(v)


def sin_datos(d: dict) -> str:
    """Aviso si el dato de un host (o del CSD) no está al día; "" si lo está."""
    if d.get("vigente") is not False and not d.get("sin_respuesta_desde"):
        return ""
    texto = "SIN DATOS RECIENTES"
    if d.get("sin_respuesta_desde"):
        texto += f" (no responde desde {hora_corta(leer_hora(d['sin_respuesta_desde']))})"
    if d.get("error"):
        texto += f": {str(d['error'])[:120]}"
    return texto


def cli_seguridad() -> int:
    r = seguridad()
    res = r.get("resumen") or {}
    c, i = res.get("critico", 0), res.get("info", 0)
    print(f"Seguridad: {c} crítico{'s' if c != 1 else ''} · {res.get('atencion', 0)} de atención · "
          f"{i} informativo{'s' if i != 1 else ''} (revisado {hora_corta(leer_hora(r.get('generado')))})")
    for host, d in (r.get("hosts") or {}).items():
        partes = [f"- {host}: {NIVELES.get(d.get('status'), d.get('status'))}"
                  + (f" (dato de hace {d['edad_dato']})" if d.get("edad_dato") else ""),
                  sin_datos(d),
                  " · ".join(f"{METRICAS.get(k, k)}: {valor_metrica(v)}"
                             for k, v in (d.get("metricas") or {}).items() if v is not None)]
        print(" | ".join(p for p in partes if p))
        for h in d.get("hallazgos") or []:
            print(f"    · [{NIVELES.get(h.get('nivel'), h.get('nivel'))}] {h.get('texto') or h.get('id')}"
                  f" (desde {hora_corta(leer_hora(h.get('desde')))})"
                  + (f" | motivo: {h['motivo']}" if h.get("motivo") else ""))
    csd = r.get("csd")
    if csd:
        partes = [f"- CSD (certificados de sello digital): {NIVELES.get(csd.get('status'), csd.get('status'))}",
                  sin_datos(csd)]
        if csd.get("certificados") is not None:
            partes.append(f"{csd['certificados']} certificados")
        p = csd.get("proximo") or {}
        if p:
            partes.append(f"el próximo vence {p.get('vence')} ({p.get('rfc')}, en {p.get('dias')} días)")
        if csd.get("sin_certificado"):
            partes.append(f"sin certificado: {', '.join(map(str, csd['sin_certificado']))}")
        print(" | ".join(x for x in partes if x))
        for h in csd.get("hallazgos") or []:
            print(f"    · [{NIVELES.get(h.get('nivel'), h.get('nivel'))}] {h.get('texto') or h.get('id')}")
    if not r.get("hosts") and not csd:
        print("(sin datos)")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Consulta el estado de los servicios en el vigía (solo lectura).")
    sub = p.add_subparsers(dest="orden", required=True)
    e = sub.add_parser("estado", help="servicios que no están arriba")
    e.add_argument("--todos", action="store_true", help="lista también los que están arriba")
    sub.add_parser("alertas", help="alertas activas")
    sub.add_parser("seguridad", help="seguridad de los servidores: hallazgos y métricas por host")
    i = sub.add_parser("incidentes", help="caídas en un rango de fechas")
    i.add_argument("--desde", default="hoy", help="hoy, ayer o AAAA-MM-DD[THH:MM] (hora local)")
    i.add_argument("--hasta", help="ahora (por defecto), hoy, ayer o AAAA-MM-DD[THH:MM]; es exclusivo")
    i.add_argument("--dias", type=int, help="en vez de --hasta: cuántos días a partir de --desde")
    a = p.parse_args()
    try:
        if a.orden == "estado":
            return cli_estado(a.todos)
        if a.orden == "alertas":
            return cli_alertas()
        if a.orden == "seguridad":
            return cli_seguridad()
        desde = fecha(a.desde)
        if a.hasta:
            hasta = fecha(a.hasta)
        elif a.dias:
            hasta = desde + timedelta(days=max(1, min(a.dias, 366)))
        else:
            hasta = datetime.now().replace(microsecond=0)
        hasta = min(hasta, datetime.now().replace(microsecond=0))
        if hasta <= desde:
            print("El rango está vacío: --hasta debe ser posterior a --desde.")
            return 2
        return cli_incidentes(desde, hasta)
    except ErrorVigia as err:
        print(f"{err}.")
        return 1
    except ValueError:
        print("Fecha no válida: usa hoy, ayer o AAAA-MM-DD[THH:MM].")
        return 2


if __name__ == "__main__":
    sys.exit(main())
