# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Windows-only Spanish voice assistant. A floating orb (pywebview window) listens for a wake word, transcribes the question, sends it to Claude Code via `claude -p`, then speaks the answer and shows it in a side panel with visual "cards". All code, identifiers, comments, log messages and UI text are in Spanish — keep it that way.

## Running

There is no build step, test suite, linter or requirements file. Dependencies live in `.venv` (Python 3.12): pywebview, pystray, keyboard, Pillow, vosk, faster-whisper, sounddevice, numpy, edge-tts, pyttsx3.

- `depurar.bat` — runs `asistente.py` with `python.exe` in a console (UTF-8), so logs are visible. Use this when debugging.
- `iniciar.bat` — runs with `pythonw.exe` (no console); output only goes to `asistente.log`.
- Equivalent: `.venv\Scripts\python.exe asistente.py`
- `inicio-automatico.bat` (or `inicio-automatico.bat quitar`) — creates/removes the Startup-folder shortcut; same `.lnk` the tray's "Iniciar con Windows" toggles.

Requires the `claude` CLI on PATH (or at `~/.local/bin/claude.exe`), Microsoft Edge (for screenshot cards), and a microphone.

## Architecture

Two Python modules plus a static web UI:

- **`motor.py`** — the engine, independent of the window. `Motor.ejecutar()` runs on a daemon thread: loads models, opens the mic stream, and loops over (1) typed questions from `Motor.textos`, (2) manual activation (`activar_manual` event), (3) wake-word detection. Pipeline per question:
  1. **Wake word**: Vosk (`modelos/vosk-model-small-es-*`) with the *full* vocabulary; the wake word is matched in word-level results against `confianza_minima`. Don't switch to a closed grammar — it forced every phrase to match the wake word (see comment in `ejecutar`).
  2. **Recording**: `grabar_pregunta()` does energy-based end-of-speech detection (noise floor from the first 300 ms, `segundos_silencio_fin`, `segundos_max_pregunta`).
  3. **STT**: `Motor.transcribir()` — faster-whisper (`modelo_whisper`, CPU int8, cached in `modelos/`; `HF_HUB_OFFLINE` is set once downloaded), `beam_size=5`, `initial_prompt` = config `vocabulario` + project folder names found by `buscar_proyectos()`. Low-confidence transcriptions (`avg_logprob` below `confianza_minima_transcripcion`) are discarded and the user is asked to repeat.
  4. **Claude**: `correr_claude()` spawns `claude -p ... --output-format stream-json --verbose` and turns tool-use events into live progress. Question mode (`preguntar_a_claude()`) is read-only (`herramientas_permitidas`), runs in `carpeta_proyectos`, appends `SISTEMA` + the known project list, reuses the session via `--resume` within `minutos_contexto`, 300 s kill timer.
  5. **Response contract**: `SISTEMA`/`FORMATO` force JSON `{"voz", "detalle", "tarjetas", "tarea"?}`; `interpretar()` parses it and falls back to splitting plain text. Card types: `captura`, `imagen`, `codigo`, `estado`, `tabla`, `dato`, `fuentes`. Adding a card type means updating `SISTEMA`, `resolver_tarjeta()` (if it needs server-side work) and `construirTarjeta()` in `ui/app.js`.
  6. **Async cards**: `captura` (headless Edge screenshot into `capturas/`, embedded as data URI) and `estado` (HTTP ping) are resolved in a thread pool and pushed later as `tarjeta` events; the item is then saved to `historial.json` (last 100, without `src` blobs).
  7. **Tasks (two-phase)**: question-mode Claude never edits; when the user asks to *do* something it returns `tarea: {descripcion, carpeta, plan}` and asks for confirmation. `pedir_confirmacion()` waits for yes/no by voice (`es_si_o_no`), typed text, or UI buttons (`Motor.confirmaciones` queue). On yes, `lanzar_tarea()` runs a separate background `claude -p` in the project folder with `SISTEMA_TAREA`, `--permission-mode permisos_tarea` (default `auto`), `modelo_tarea`/`esfuerzo_tarea`, `herramientas_bloqueadas_tarea` as `--disallowedTools`, and a `minutos_max_tarea` limit. The main loop keeps listening; the result is queued in `Motor.avisos` and announced when idle, and a summary is prepended to the next question (`nota_contexto`) since the task session can't be resumed from `carpeta_proyectos`. "Cancela la tarea" (`CANCELAR_TAREA`) or the UI button kills it.
  8. **TTS**: edge-tts neural voice → `respuesta.mp3` played via MCI with word-boundary timings sent to the UI for highlighting; falls back to pyttsx3 (Windows SAPI). Mic audio captured while speaking is discarded.

- **`asistente.py`** — the shell: creates the frameless, always-on-top pywebview window, the system tray icon (pystray), the global hotkey (`keyboard`), and the "start with Windows" shortcut. Switches between `orbe` and `panel` modes by resizing/moving the window and clipping it with Win32 regions (ctypes). Note the DPI handling: pywebview uses logical pixels while `webview.screens` and Win32 regions use physical ones (`escala()`).

- **`calendario.py`** — Google Calendar via a service account. Reading: a CLI (`eventos --desde hoy|mañana|AAAA-MM-DD --dias N [--buscar texto]`) using a service account (`google-auth`, `calendar.readonly`). Claude calls it through Bash in question mode: `Motor.sistema()` documents the exact command (`motor.CALENDARIO`, forward-slash paths) and `Motor.herramientas()` adds a matching `Bash(<command> eventos:*)` allow rule, only when `calendarios_google` is configured. Creating: question-mode Claude returns `evento: {titulo, inicio, fin?, repetir?, lugar?, descripcion?, recordatorio_min?}` (never writes itself); after the same yes/no confirmation as tasks, `Motor.crear_evento()` calls `calendario.crear_evento()` (scope `calendar.events`, first id in `calendarios_google`; needs the share permission "Hacer cambios en los eventos"). The service account only sees calendars the user shared with its `client_email`; 403/404 prints an `AVISO` line. Test directly: `.venv\Scripts\python.exe calendario.py eventos --dias 7`.

- **`ui/`** (`index.html`, `app.js`, `app.css`, vendored highlight.js) — plain JS, no framework or bundler.

### Python ↔ UI bridge

- Python → JS: `motor.emitir(evento)` (replaced at startup by `asistente.emitir`) calls `window.app.on(evento)` via `evaluate_js`. Events are dicts keyed by `tipo`: `estado`, `nivel`, `pregunta`, `progreso`, `respuesta`, `palabras`, `tarjeta`, `confirmar`, `confirmado`, `tarea`, `aviso`, `silenciado`, `limpiar`, `abrir_panel`. Handled in the `switch` in `app.js`.
- JS → Python: `window.pywebview.api.<method>` maps to methods of the `Api` class in `asistente.py`.

## Configuration

`config.json` is read once at import (`motor.CFG`) and overlaid with the git-ignored `config.local.json` (personal data: `credenciales_google`, `calendarios_google`); restart after changes. Credentials live in the git-ignored `kys/`. Notable keys: `palabra_activacion`, `confianza_minima`, `carpeta_proyectos` (Claude's working directory for questions and root for project discovery), `herramientas_permitidas` (question mode — keep read-only; edits must go through the confirmed task flow), `vocabulario`, the `*_tarea` keys, `motor_voz`, `atajo`.

## Generated / runtime files

`asistente.log`, `historial.json`, `respuesta.mp3`, `capturas/` (PNGs pruned after 7 days), and `modelos/` (Vosk/Whisper models, Edge headless profile, WebView2 storage) are runtime data — don't edit or search them.
