"""Cliente del «vigía»: servicio de solo lectura sobre Uptime Kuma que corre en
otro equipo de la tailnet. Autenticación con la cabecera X-Clave.

Configuración: `vigia_url` en config.json y `vigia_clave` en config.local.json
(fuera de git). Si falta alguna, `configurado()` es False y todo lo de servicios
queda apagado.

Claude lo lee como herramienta desde el modo pregunta:
    python vigia.py estado [--todos]
    python vigia.py alertas
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
    """{generado, total, criticas, atencion, alertas: [{id, tipo, severidad, monitor_id,
    monitor, grupo, desde, duracion, caido_en_silencio, msg, texto}]}"""
    return _pedir("/api/alertas")


def incidentes(desde: datetime, hasta: datetime) -> dict:
    """{desde, hasta, total_incidentes, en_curso, uptime_pct, por_grupo, por_monitor,
    incidentes: [{monitor_id, monitor, grupo, critico, inicio, fin, en_curso, duracion_s,
    duracion, msg}]}. Fechas sin zona = hora local."""
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
        print(f"- [{x.get('severidad')}] {x.get('texto') or nombre_monitor(x)} | desde {hora_corta(leer_hora(x.get('desde')))}"
              + (f" | {str(x['msg'])[:120]}" if x.get("msg") else ""))
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
        if i.get("msg"):
            partes.append(str(i["msg"])[:120])
        print(" | ".join(partes))
    for g, d in (r.get("por_grupo") or {}).items():
        if d.get("incidentes"):
            print(f"  grupo {g}: {d['incidentes']} incidente{'s' if d['incidentes'] != 1 else ''}, {d.get('monitores_afectados', '?')} de "
                  f"{d.get('monitores', '?')} monitores, caído {d.get('caido', '?')}, disponibilidad {d.get('uptime_pct', '?')} %")
    if not lista:
        print("(ninguno)")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Consulta el estado de los servicios en el vigía (solo lectura).")
    sub = p.add_subparsers(dest="orden", required=True)
    e = sub.add_parser("estado", help="servicios que no están arriba")
    e.add_argument("--todos", action="store_true", help="lista también los que están arriba")
    sub.add_parser("alertas", help="alertas activas")
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
