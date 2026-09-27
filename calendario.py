"""Lectura del Google Calendar del usuario con una cuenta de servicio (solo lectura).

Claude lo ejecuta como herramienta desde el modo pregunta:
    python calendario.py eventos [--desde hoy|mañana|AAAA-MM-DD] [--dias N] [--buscar texto]

La cuenta de servicio solo ve los calendarios que el usuario haya compartido con
su correo (client_email del JSON de credenciales)."""

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

BASE = Path(__file__).parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
if (BASE / "config.local.json").exists():  # datos personales, fuera de git
    CFG.update(json.loads((BASE / "config.local.json").read_text(encoding="utf-8")))
API = "https://www.googleapis.com/calendar/v3"
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def credenciales() -> Path:
    ruta = CFG.get("credenciales_google") or ""
    if ruta and (BASE / ruta).exists():
        return BASE / ruta
    return next((BASE / "kys").glob("*.json"))  # el primer JSON de la carpeta kys


def sesion() -> AuthorizedSession:
    cred = service_account.Credentials.from_service_account_file(
        str(credenciales()), scopes=["https://www.googleapis.com/auth/calendar.readonly"])
    return AuthorizedSession(cred)


def fecha(texto: str) -> date:
    t = texto.lower()
    if t == "hoy":
        return date.today()
    if t in ("mañana", "manana"):
        return date.today() + timedelta(days=1)
    return date.fromisoformat(texto)


def formatear(ev: dict, calendario: str, varios: bool) -> str:
    ini, fin = ev["start"], ev["end"]
    if "date" in ini:  # evento de día completo (end es exclusivo)
        d = date.fromisoformat(ini["date"])
        dias = (date.fromisoformat(fin["date"]) - d).days
        cuando = f"{d} {DIAS[d.weekday()]} todo el día" + (f" ({dias} días)" if dias > 1 else "")
    else:
        a = datetime.fromisoformat(ini["dateTime"]).astimezone()
        b = datetime.fromisoformat(fin["dateTime"]).astimezone()
        cuando = f"{a:%Y-%m-%d} {DIAS[a.weekday()]} {a:%H:%M}-{b:%H:%M}"
    partes = [cuando, ev.get("summary") or "(sin título)"]
    if ev.get("location"):
        partes.append(f"lugar: {ev['location']}")
    if ev.get("hangoutLink"):
        partes.append(f"videollamada: {ev['hangoutLink']}")
    if varios:
        partes.append(f"calendario: {calendario}")
    return " | ".join(partes)


def eventos(desde: date, dias: int, buscar: str | None) -> int:
    s = sesion()
    ids = CFG.get("calendarios_google") or []
    zona = datetime.now().astimezone().tzinfo
    params = {
        "timeMin": datetime.combine(desde, time.min, zona).isoformat(),
        "timeMax": datetime.combine(desde + timedelta(days=dias), time.min, zona).isoformat(),
        "singleEvents": "true", "orderBy": "startTime", "maxResults": 250,
    }
    if buscar:
        params["q"] = buscar
    filas, errores = [], []
    for cid in ids:
        r = s.get(f"{API}/calendars/{cid}/events", params=params, timeout=20)
        if r.status_code in (403, 404):
            errores.append(f"Sin acceso al calendario {cid}: compártelo con la cuenta de servicio "
                           f"(Configuración del calendario > Compartir con personas específicas).")
            continue
        r.raise_for_status()
        for ev in r.json().get("items", []):
            if ev.get("status") != "cancelled":
                clave = ev["start"].get("dateTime") or ev["start"].get("date")
                filas.append((clave, formatear(ev, cid, len(ids) > 1)))
    fin = desde + timedelta(days=dias - 1)
    print(f"Eventos del {desde} al {fin}" + (f' que coinciden con "{buscar}"' if buscar else "") + ":")
    for _, linea in sorted(filas):
        print("- " + linea)
    if not filas:
        print("(ninguno)")
    for e in errores:
        print("AVISO: " + e)
    return 1 if errores and not filas else 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Lee el Google Calendar del usuario (solo lectura).")
    sub = p.add_subparsers(dest="orden", required=True)
    e = sub.add_parser("eventos", help="lista eventos de un rango de días")
    e.add_argument("--desde", default="hoy", help="hoy, mañana o AAAA-MM-DD")
    e.add_argument("--dias", type=int, default=1, help="cuántos días a partir de --desde")
    e.add_argument("--buscar", help="texto a buscar en título, descripción, lugar o invitados")
    a = p.parse_args()
    return eventos(fecha(a.desde), max(1, min(a.dias, 366)), a.buscar)


if __name__ == "__main__":
    sys.exit(main())
