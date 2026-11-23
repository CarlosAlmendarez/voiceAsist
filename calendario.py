"""Agenda local del usuario, sin cuentas en la nube: los eventos viven en
`agenda.json` (fuera de git) junto al programa.

Claude la lee como herramienta desde el modo pregunta:
    python calendario.py eventos [--desde hoy|mañana|AAAA-MM-DD] [--dias N] [--buscar texto]

Crear, modificar y borrar solo lo hace el motor (Motor.crear_evento), tras la
confirmación del usuario. Formato de cada evento guardado:
    {"id": "e1a2b", "titulo": "...", "inicio": "AAAA-MM-DD" | "AAAA-MM-DDTHH:MM",
     "fin": opcional (mismo formato; el de todo el día es inclusivo),
     "repetir": opcional diario|semanal|mensual|anual, "hasta": opcional AAAA-MM-DD,
     "lugar", "descripcion", "recordatorio_min", "recordatorio": opcionales, "creado": "..."}"""

import argparse
import json
import os
import sys
import threading
import time
import unicodedata
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
AGENDA = BASE / "agenda.json"
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
REPETIR = {"diario", "semanal", "mensual", "anual"}
CAMPOS = ("titulo", "inicio", "fin", "repetir", "hasta", "lugar", "descripcion", "recordatorio_min", "recordatorio")
_bloqueo = threading.Lock()


# ---------- almacenamiento ----------
def cargar() -> list[dict]:
    try:
        return json.loads(AGENDA.read_text(encoding="utf-8")).get("eventos", [])
    except (OSError, ValueError):
        return []


def guardar(eventos: list[dict]) -> None:
    """Escritura atómica: Claude puede estar leyendo el archivo al mismo tiempo."""
    tmp = AGENDA.with_suffix(".tmp")
    tmp.write_text(json.dumps({"eventos": eventos}, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, AGENDA)


def nuevo_id(existentes: set[str]) -> str:
    n = int(time.time() * 1000)
    while (i := f"e{n % 10**6:06d}") in existentes:
        n += 1
    return i


def validar(ev: dict) -> dict:
    """Normaliza un evento que viene de Claude. Lanza ValueError si no es válido."""
    limpio = {k: ev[k] for k in CAMPOS if ev.get(k) not in (None, "")}
    if not limpio.get("titulo") or not limpio.get("inicio"):
        raise ValueError("el evento necesita título e inicio")
    limpio["titulo"] = str(limpio["titulo"]).strip()
    a = leer_fecha(limpio["inicio"])
    limpio["inicio"] = formato(a)
    if limpio.get("fin"):
        b = leer_fecha(limpio["fin"])
        if isinstance(a, datetime) and not isinstance(b, datetime):
            b = datetime.combine(b, a.time())
        if isinstance(a, datetime) == isinstance(b, datetime) and b < a:
            raise ValueError("el fin es anterior al inicio")
        limpio["fin"] = formato(b) if isinstance(a, datetime) == isinstance(b, datetime) else formato(b.date())
    if limpio.get("repetir") and limpio["repetir"] not in REPETIR:
        raise ValueError(f"repetición no válida: {limpio['repetir']}")
    if limpio.get("hasta"):
        limpio["hasta"] = date.fromisoformat(str(limpio["hasta"])[:10]).isoformat()
    if "recordatorio_min" in limpio:
        limpio["recordatorio_min"] = int(limpio["recordatorio_min"])
    if "recordatorio" in limpio:
        limpio["recordatorio"] = bool(limpio["recordatorio"])
    return limpio


def crear_evento(ev: dict) -> dict:
    """Guarda un evento nuevo y lo devuelve (con su id)."""
    limpio = validar(ev)
    with _bloqueo:
        eventos = cargar()
        limpio = {"id": nuevo_id({e["id"] for e in eventos}), **limpio,
                  "creado": datetime.now().isoformat(timespec="seconds")}
        guardar(eventos + [limpio])
    return limpio


def modificar_evento(eid: str, cambios: dict) -> dict:
    """Cambia campos de un evento (un valor null o "" quita el campo). Devuelve el evento nuevo."""
    with _bloqueo:
        eventos = cargar()
        for i, e in enumerate(eventos):
            if e["id"] == eid:
                mezcla = {**e, **{k: v for k, v in cambios.items() if k in CAMPOS}}
                eventos[i] = {"id": eid, **validar(mezcla), "creado": e.get("creado", "")}
                guardar(eventos)
                return eventos[i]
    raise KeyError(eid)


def borrar_evento(eid: str) -> dict:
    """Borra un evento (con todas sus repeticiones) y lo devuelve."""
    with _bloqueo:
        eventos = cargar()
        for e in eventos:
            if e["id"] == eid:
                guardar([x for x in eventos if x["id"] != eid])
                return e
    raise KeyError(eid)


def buscar_por_id(eid: str) -> dict | None:
    return next((e for e in cargar() if e["id"] == eid), None)


# ---------- fechas ----------
def leer_fecha(texto) -> date | datetime:
    t = str(texto).strip()
    if "T" in t or " " in t:
        d = datetime.fromisoformat(t.replace(" ", "T")[:16])
        return d.replace(tzinfo=None)
    return date.fromisoformat(t[:10])


def formato(d: date | datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M") if isinstance(d, datetime) else d.isoformat()


def fecha(texto: str) -> date:
    t = texto.lower()
    if t == "hoy":
        return date.today()
    if t in ("mañana", "manana"):
        return date.today() + timedelta(days=1)
    return date.fromisoformat(texto)


def sumar(d: date, repetir: str, n: int) -> date | None:
    """La n-ésima repetición de la fecha d (None si ese día no existe, p. ej. 31 de abril)."""
    if repetir == "diario":
        return d + timedelta(days=n)
    if repetir == "semanal":
        return d + timedelta(weeks=n)
    if repetir == "mensual":
        m = d.month - 1 + n
        y, m = d.year + m // 12, m % 12 + 1
        return date(y, m, d.day) if d.day <= monthrange(y, m)[1] else None
    if repetir == "anual":
        y = d.year + n
        return date(y, d.month, d.day) if d.day <= monthrange(y, d.month)[1] else None
    return None


# ---------- consulta ----------
def ocurrencias(ev: dict, desde: date, hasta: date) -> list[dict]:
    """Apariciones del evento que tocan [desde, hasta) como dicts con inicio/fin reales."""
    a = leer_fecha(ev["inicio"])
    todo_dia = not isinstance(a, datetime)
    dia0 = a if todo_dia else a.date()
    if ev.get("fin"):
        b = leer_fecha(ev["fin"])
        duracion = (b - a) if isinstance(b, type(a)) else timedelta(0)
    else:
        duracion = timedelta(0) if todo_dia else timedelta(minutes=15 if ev.get("recordatorio") else 60)
    # días que ocupa (para eventos de varios días que empezaron antes del rango)
    largo = duracion.days if todo_dia else (a + duracion - timedelta(microseconds=1)).date().toordinal() - dia0.toordinal()
    repetir = ev.get("repetir")
    limite = min(hasta, date.fromisoformat(ev["hasta"]) + timedelta(days=1)) if ev.get("hasta") else hasta
    salida = []
    if repetir in ("diario", "semanal"):
        paso = 1 if repetir == "diario" else 7
        n = max(0, (desde.toordinal() - largo - dia0.toordinal()) // paso)
    else:
        n = 0
    while True:
        d = sumar(dia0, repetir, n) if repetir else (dia0 if n == 0 else None)
        if repetir and d is None and repetir in ("mensual", "anual"):
            n += 1
            if n > 2000:
                break
            continue
        if d is None or d >= limite:
            break
        if d + timedelta(days=largo) >= desde:
            ini = d if todo_dia else datetime.combine(d, a.time())
            salida.append({**ev, "inicio_real": ini, "fin_real": ini + duracion, "todo_el_dia": todo_dia})
        n += 1
        if not repetir:
            break
    return salida


def normalizar(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t.lower()) if unicodedata.category(c) != "Mn")


def listar(desde: date, dias: int = 1, buscar: str | None = None) -> list[dict]:
    """Apariciones de eventos en [desde, desde + dias), ordenadas por inicio."""
    hasta = desde + timedelta(days=dias)
    filas = []
    q = normalizar(buscar) if buscar else None
    for ev in cargar():
        if q and q not in normalizar(" ".join(str(ev.get(k, "")) for k in ("titulo", "lugar", "descripcion"))):
            continue
        try:
            filas += ocurrencias(ev, desde, hasta)
        except (ValueError, KeyError):
            continue  # evento dañado: no rompe toda la agenda
    filas.sort(key=lambda o: (o["inicio_real"] if not o["todo_el_dia"]
                              else datetime.combine(o["inicio_real"], datetime.min.time())))
    return filas


def describir(o: dict) -> str:
    ini, fin = o["inicio_real"], o["fin_real"]
    if o["todo_el_dia"]:
        cuando = f"{ini} {DIAS[ini.weekday()]} todo el día" + (f" (hasta {fin})" if fin > ini else "")
    else:
        cuando = f"{ini:%Y-%m-%d} {DIAS[ini.weekday()]} {ini:%H:%M}-{fin:%H:%M}"
    partes = [f"[{o['id']}] {cuando}", o["titulo"]]
    if o.get("repetir"):
        partes.append(f"se repite: {o['repetir']}" + (f" hasta {o['hasta']}" if o.get("hasta") else ""))
    if o.get("lugar"):
        partes.append(f"lugar: {o['lugar']}")
    if o.get("descripcion"):
        nota = " ".join(o["descripcion"].split())  # una línea por evento
        partes.append(f"nota: {nota[:160]}…" if len(nota) > 160 else f"nota: {nota}")
    return " | ".join(partes)


def eventos(desde: date, dias: int, buscar: str | None) -> int:
    filas = listar(desde, dias, buscar)
    fin = desde + timedelta(days=dias - 1)
    print(f"Eventos del {desde} al {fin}" + (f' que coinciden con "{buscar}"' if buscar else "") + ":")
    for o in filas:
        print("- " + describir(o))
    if not filas:
        print("(ninguno)")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Lee la agenda local del usuario (solo lectura).")
    sub = p.add_subparsers(dest="orden", required=True)
    e = sub.add_parser("eventos", help="lista eventos de un rango de días")
    e.add_argument("--desde", default="hoy", help="hoy, mañana o AAAA-MM-DD")
    e.add_argument("--dias", type=int, default=1, help="cuántos días a partir de --desde")
    e.add_argument("--buscar", help="texto a buscar en título, lugar o nota")
    a = p.parse_args()
    return eventos(fecha(a.desde), max(1, min(a.dias, 366)), a.buscar)


if __name__ == "__main__":
    sys.exit(main())
