"""Asistente de voz con interfaz: orbe flotante que se abre en un panel lateral
con la respuesta, el progreso en vivo y tarjetas visuales."""

import ctypes
import json
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import keyboard
import pystray
import webview
from PIL import Image, ImageDraw, ImageFilter

import motor
from motor import BASE, CFG, Motor, log

TITULO = "Asistente de voz"
ORBE = 104
PANEL_ANCHO = 440
MARGEN = 16
BARRA_TAREAS = 48
INICIO = Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/Asistente de voz.lnk"

user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
ventana: webview.Window
m = Motor()
estado_ui = {"modo": "orbe", "visible": True}


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


def poner_modo(modo: str) -> None:
    ancho_p, alto_p = pantalla()
    if modo == "panel":
        alto = alto_p - BARRA_TAREAS - 2 * MARGEN
        ventana.resize(PANEL_ANCHO, alto)
        ventana.move(ancho_p - PANEL_ANCHO - MARGEN, MARGEN)
        recortar(28)
        threading.Timer(0.3, recortar, (28,)).start()  # por si el tamaño se aplica tarde
    else:
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

    def historial(self):
        return [{"i": i, "fecha": h["fecha"], "pregunta": h["pregunta"]}
                for i, h in enumerate(motor.cargar_historial())][::-1]

    def ver_historial(self, i):
        return motor.item_historial(int(i))

    def abrir(self, url):
        if motor.es_http(url):
            webbrowser.open(url)

    def config(self):
        return {"segundos_ocultar": CFG.get("segundos_ocultar", 60),
                "palabra": CFG["palabra_activacion"], "atajo": CFG.get("atajo", "ctrl+alt+a")}


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
    (ventana.show if estado_ui["visible"] else ventana.hide)()


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
    poner_modo("orbe")
    try:
        keyboard.add_hotkey(CFG.get("atajo", "ctrl+alt+a"), abrir_panel)
    except Exception as e:
        log(f"No se pudo registrar el atajo de teclado: {e}")
    threading.Thread(target=m.ejecutar, daemon=True).start()


if __name__ == "__main__":
    motor.emitir = emitir
    ventana = webview.create_window(
        TITULO, str(BASE / "ui" / "index.html"), js_api=Api(),
        width=ORBE, height=ORBE, min_size=(ORBE, ORBE), frameless=True, on_top=True, resizable=False,
        background_color="#0b0e14", easy_drag=False,
    )
    ventana.events.loaded += al_cargar
    icono.run_detached()
    webview.start(private_mode=False, storage_path=str(BASE / "modelos" / "webview"))
    icono.stop()
