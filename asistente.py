"""Asistente de voz con interfaz: panel lateral fijo con las actividades del día y
la conversación (progreso en vivo y tarjetas visuales); se puede plegar a un orbe."""

import ctypes
import json
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from ctypes import wintypes
from datetime import date
from pathlib import Path

import keyboard
import pystray
import webview
from PIL import Image, ImageDraw, ImageFilter

import motor
from motor import BASE, CFG, Motor, log

TITULO = "Asistente de voz"
ORBE = 104
PANEL_ANCHO = CFG.get("ancho_panel", 400)
PANEL_FIJO = CFG.get("panel_fijo", True)  # reserva su franja de pantalla, como la barra de tareas
MARGEN = 16
BARRA_TAREAS = 48
INICIO = Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/Asistente de voz.lnk"

user32, gdi32, shell32 = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.shell32
ventana: webview.Window
m = Motor()
estado_ui = {"modo": CFG.get("modo_inicial", "panel"), "visible": True, "barra": 0, "encima": True}  # barra = hwnd registrado


class APPBARDATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uCallbackMessage", wintypes.UINT),
                ("uEdge", wintypes.UINT), ("rc", wintypes.RECT), ("lParam", wintypes.LPARAM)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


shell32.SHAppBarMessage.argtypes = [wintypes.DWORD, ctypes.POINTER(APPBARDATA)]
shell32.SHAppBarMessage.restype = ctypes.c_size_t
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.GetForegroundWindow.restype = wintypes.HWND
ABM_NEW, ABM_REMOVE, ABM_QUERYPOS, ABM_SETPOS, ABE_RIGHT = 0, 1, 2, 3, 2
BARRA_ANTERIOR = BASE / "modelos" / "panel-barra.txt"  # hwnd de la franja reservada, por si hay un cierre brusco


def emitir(evento: dict) -> None:
    try:
        ventana.evaluate_js(f"window.app && app.on({json.dumps(evento, ensure_ascii=False)})")
    except Exception as e:
        log(f"No se pudo enviar a la interfaz: {e}")


def hwnd() -> int:
    return user32.FindWindowW(None, TITULO)


def escala() -> float:
    """Escala de Windows (1.25 = 125 %). pywebview mueve y redimensiona en
    píxeles lógicos, pero webview.screens y las regiones Win32 usan físicos."""
    h = hwnd()
    return (user32.GetDpiForWindow(h) / 96) if h else 1.0


def recortar(radio: int) -> None:
    """Da forma redondeada a la ventana (Windows 10 no redondea ventanas sin marco)."""
    h = hwnd()
    if h:
        r = (ctypes.c_long * 4)()
        user32.GetWindowRect(h, r)
        radio = int(radio * escala())
        region = gdi32.CreateRoundRectRgn(0, 0, r[2] - r[0] + 1, r[3] - r[1] + 1, radio, radio)
        user32.SetWindowRgn(h, region, True)


def ocultar_de_barra_de_tareas() -> None:
    h = hwnd()
    if h:
        estilo = user32.GetWindowLongW(h, -20)  # GWL_EXSTYLE
        user32.SetWindowLongW(h, -20, (estilo | 0x80) & ~0x40000)  # TOOLWINDOW, sin APPWINDOW
        # sin esto Windows dibuja un marco blanco de 1 px alrededor de la ventana recortada
        desactivado = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(h, 2, ctypes.byref(desactivado), 4)


def pantalla() -> tuple[int, int]:
    s, e = webview.screens[0], escala()
    return int(s.width / e), int(s.height / e)


def monitor(h: int) -> MONITORINFO:
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(user32.MonitorFromWindow(h, 2), ctypes.byref(info))  # 2 = el más cercano
    return info


def barra(h: int, mensaje: int, rc: wintypes.RECT | None = None) -> APPBARDATA:
    d = APPBARDATA(cbSize=ctypes.sizeof(APPBARDATA), hWnd=h, uEdge=ABE_RIGHT)
    if rc:
        d.rc = rc
    shell32.SHAppBarMessage(mensaje, ctypes.byref(d))
    return d


def soltar_barra() -> None:
    """Devuelve a Windows la franja reservada por el panel."""
    if estado_ui["barra"]:
        barra(estado_ui["barra"], ABM_REMOVE)
        estado_ui["barra"] = 0
        BARRA_ANTERIOR.unlink(missing_ok=True)


def limpiar_barra_huerfana() -> None:
    """Si el asistente se cerró a la fuerza, Windows sigue reservando la franja de la
    ventana muerta: se libera con el identificador que quedó apuntado."""
    try:
        viejo = int(BARRA_ANTERIOR.read_text())
    except (OSError, ValueError):
        return
    if viejo and not user32.IsWindow(viejo):
        barra(viejo, ABM_REMOVE)
        log("Liberé el espacio que dejó reservado un cierre anterior.")
    BARRA_ANTERIOR.unlink(missing_ok=True)


def acoplar() -> None:
    """Pega el panel al borde derecho, de arriba abajo del área de trabajo. Con
    panel_fijo lo registra como barra de escritorio (AppBar): Windows encoge el área
    de trabajo y las ventanas maximizadas ya no quedan debajo del panel."""
    h = hwnd()
    if not h:
        return
    ancho = int(PANEL_ANCHO * escala())
    if PANEL_FIJO and not estado_ui["barra"]:
        t = monitor(h).rcWork  # medido antes de reservar nada
        estado_ui["zona"] = (t.top, t.right, t.bottom)
        barra(h, ABM_NEW)
        estado_ui["barra"] = h
        BARRA_ANTERIOR.write_text(str(h))
    if estado_ui["barra"]:
        arriba, derecha, abajo = estado_ui["zona"]
        rc = barra(h, ABM_QUERYPOS, wintypes.RECT(derecha - ancho, arriba, derecha, abajo)).rc
        rc.left = rc.right - ancho
        rc = barra(h, ABM_SETPOS, rc).rc
    else:
        t = monitor(h).rcWork
        rc = wintypes.RECT(t.right - ancho, t.top, t.right, t.bottom)
    user32.SetWindowRgn(h, None, True)  # rectangular: va pegado al borde
    user32.MoveWindow(h, rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top, True)


def vigilar_pantalla_completa() -> None:
    """Si otra app ocupa toda la pantalla (video, juego, presentación), el panel deja
    de estar siempre encima para no taparla; vuelve al salir de pantalla completa."""
    while True:
        time.sleep(1.5)
        try:
            h = hwnd()
            fg = user32.GetForegroundWindow()
            if not h or not fg or fg == h:
                continue
            clase = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(fg, clase, 64)
            r, mon = wintypes.RECT(), monitor(fg).rcMonitor
            user32.GetWindowRect(fg, ctypes.byref(r))
            completa = clase.value not in ("Progman", "WorkerW", "Shell_TrayWnd") and \
                r.left <= mon.left and r.top <= mon.top and r.right >= mon.right and r.bottom >= mon.bottom
            if completa == estado_ui["encima"]:
                estado_ui["encima"] = not completa
                # -1 = HWND_TOPMOST, -2 = HWND_NOTOPMOST; sin mover, cambiar tamaño ni activar
                user32.SetWindowPos(h, -2 if completa else -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        except Exception as e:
            log(f"Vigilancia de pantalla completa: {e}")


def poner_modo(modo: str) -> None:
    ancho_p, alto_p = pantalla()
    if modo == "panel":
        acoplar()
        threading.Timer(0.3, acoplar).start()  # por si el tamaño se aplica tarde
    else:
        soltar_barra()
        ventana.move(ancho_p - ORBE - MARGEN, alto_p - BARRA_TAREAS - ORBE - MARGEN)
        ventana.resize(ORBE, ORBE)
        recortar(ORBE)
        threading.Timer(0.3, recortar, (ORBE,)).start()
    estado_ui["modo"] = modo


class Api:
    """Funciones que la interfaz (JavaScript) puede llamar."""

    def modo(self, modo):
        poner_modo(modo)

    def escuchar(self):
        m.activar_manual.set()

    def preguntar(self, texto):
        m.preguntar_texto(texto)

    def silenciar(self, valor):
        m.silenciar(bool(valor))
        icono.update_menu()

    def detener(self):
        m.detener_voz.set()

    def nueva(self):
        m.nueva_conversacion()

    def confirmar(self, valor):
        m.confirmar(bool(valor))

    def cancelar_tarea(self, tid):
        m.cancelar_tarea(tid)

    def historial(self):
        return [{"i": i, "fecha": h["fecha"], "pregunta": h["pregunta"]}
                for i, h in enumerate(motor.cargar_historial())][::-1]

    def ver_historial(self, i):
        return motor.item_historial(int(i))

    def abrir(self, url):
        if motor.es_http(url):
            webbrowser.open(url)

    def config(self):
        return {"segundos_ocultar": CFG.get("segundos_ocultar", 0),
                "palabra": CFG["palabra_activacion"], "atajo": CFG.get("atajo", "ctrl+alt+a"),
                "modo": estado_ui["modo"]}

    def agenda(self, dia=None):
        """Eventos y recordatorios de un día (AAAA-MM-DD; hoy por defecto) para el panel."""
        import calendario
        d = date.fromisoformat(dia) if dia else date.today()
        items = []
        for o in calendario.listar(d, 1):
            ini, fin = o["inicio_real"], o["fin_real"]
            items.append({"id": o["id"], "tipo": "recordatorio" if o.get("recordatorio") else "evento",
                          "titulo": o["titulo"], "todo_el_dia": o["todo_el_dia"],
                          "inicio": None if o["todo_el_dia"] else ini.strftime("%Y-%m-%dT%H:%M"),
                          "fin": None if o["todo_el_dia"] else fin.strftime("%Y-%m-%dT%H:%M"),
                          "lugar": o.get("lugar", ""), "nota": o.get("descripcion", ""),
                          "repetir": o.get("repetir", "")})
        for r in m.recordatorios:
            if r["cuando"][:10] == d.isoformat():
                items.append({"id": r["id"], "tipo": "voz", "titulo": r["texto"], "todo_el_dia": False,
                              "inicio": r["cuando"][:16], "fin": None, "lugar": "", "nota": "", "repetir": ""})
        items.sort(key=lambda i: (not i["todo_el_dia"], i["inicio"] or ""))
        return {"dia": d.isoformat(), "hoy": date.today().isoformat(), "items": items}

    def alertas(self):
        """Resumen de servicios y alertas activas del vigía (configurado: False si no hay vigía)."""
        return m.estado_alertas()

    def reconocer(self, alerta_id):
        """Botón «Visto» del panel."""
        return m.reconocer_alerta(str(alerta_id))

    def comandos(self):
        """Sugerencias al escribir "/" en la caja de texto: [[comando, descripción], ...]."""
        import acciones
        return acciones.lista_comandos()

    def borrar(self, item_id, tipo):
        """Quitar desde el panel (la interfaz pide confirmar antes)."""
        if tipo == "voz":
            m.cancelar_recordatorios([item_id])
        else:
            import calendario
            try:
                ev = calendario.borrar_evento(item_id)
                log(f"Agenda: borrado desde el panel {ev['titulo']} [{item_id}]")
            except KeyError:
                pass
        emitir({"tipo": "agenda"})


# ---------- bandeja del sistema ----------
def dibujar_icono() -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    brillo = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(brillo).ellipse((6, 6, 58, 58), fill=(125, 160, 255, 150))
    img.alpha_composite(brillo.filter(ImageFilter.GaussianBlur(4)))
    d = ImageDraw.Draw(img)
    d.ellipse((12, 12, 52, 52), fill=(110, 140, 250, 255))
    d.ellipse((20, 18, 38, 34), fill=(190, 225, 255, 255))
    return img


def alternar_visible(*_):
    estado_ui["visible"] = not estado_ui["visible"]
    if estado_ui["visible"]:
        ventana.show()
        if estado_ui["modo"] == "panel":
            acoplar()
    else:
        soltar_barra()  # oculto no debe seguir quitando espacio a las demás ventanas
        ventana.hide()


def abrir_panel(*_):
    if not estado_ui["visible"]:
        alternar_visible()
    emitir({"tipo": "abrir_panel"})


def alternar_inicio(*_):
    if INICIO.exists():
        INICIO.unlink()
        return
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{INICIO}');"
          f"$s.TargetPath='{pythonw}';$s.Arguments='\"{BASE / 'asistente.py'}\"';"
          f"$s.WorkingDirectory='{BASE}';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=0x08000000)


def salir(*_):
    soltar_barra()
    icono.stop()
    ventana.destroy()


icono = pystray.Icon("asistente", dibujar_icono(), TITULO, menu=pystray.Menu(
    pystray.MenuItem("Abrir panel", abrir_panel, default=True),
    pystray.MenuItem("Mostrar u ocultar", alternar_visible),
    pystray.MenuItem("Silenciar micrófono", lambda *_: Api().silenciar(not m.silenciado),
                     checked=lambda _: m.silenciado),
    pystray.MenuItem("Iniciar con Windows", alternar_inicio, checked=lambda _: INICIO.exists()),
    pystray.Menu.SEPARATOR,
    pystray.MenuItem("Salir", salir),
))


def al_cargar():
    ocultar_de_barra_de_tareas()
    limpiar_barra_huerfana()
    poner_modo(CFG.get("modo_inicial", "panel"))
    threading.Thread(target=vigilar_pantalla_completa, daemon=True).start()
    try:
        keyboard.add_hotkey(CFG.get("atajo", "ctrl+alt+a"), abrir_panel)
    except Exception as e:
        log(f"No se pudo registrar el atajo de teclado: {e}")
    threading.Thread(target=m.ejecutar, daemon=True).start()


if __name__ == "__main__":
    motor.emitir = emitir
    # WebView2 guarda en caché index.html/app.js: tras actualizar el asistente mostraría la interfaz vieja
    shutil.rmtree(BASE / "modelos" / "webview" / "EBWebView" / "Default" / "Cache", ignore_errors=True)
    ventana = webview.create_window(
        TITULO, str(BASE / "ui" / "index.html"), js_api=Api(),
        width=ORBE, height=ORBE, min_size=(ORBE, ORBE), frameless=True, on_top=True, resizable=False,
        background_color="#0b0e14", easy_drag=False,
    )
    ventana.events.loaded += al_cargar
    icono.run_detached()
    try:
        webview.start(private_mode=False, storage_path=str(BASE / "modelos" / "webview"))
    finally:
        soltar_barra()  # devuelve el espacio reservado aunque algo falle
        icono.stop()
