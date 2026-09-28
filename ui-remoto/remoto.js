"use strict";

const $ = (s, r = document) => r.querySelector(s);
const conv = $("#conversacion");
const audio = $("#audio");
const ESTADOS = { reposo: "Listo", escuchando: "Escuchando… suelta o toca para enviar", pensando: "Pensando…" };

let novedad = 0;          // último evento del buzón ya visto
let actual = null;        // intercambio en curso
let grabadora = null, microfono = null, inicioToque = 0, modoToque = false;
let leerEnVoz = true;
try { leerEnVoz = localStorage.getItem("leerEnVoz") !== "0"; } catch {}

/* ---------- utilidades ---------- */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function dominio(url) { try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return ""; } }
function el(html) { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; }
function bajar() { conv.scrollTop = conv.scrollHeight; }
function mdLigero(texto) {
  let html = "", enLista = false;
  for (const l of esc(texto).split(/\n/)) {
    const item = l.match(/^\s*[-•*]\s+(.*)/);
    if (item) { if (!enLista) { html += "<ul>"; enLista = true; } html += `<li>${item[1]}</li>`; continue; }
    if (enLista) { html += "</ul>"; enLista = false; }
    if (l.trim()) html += `<p>${l}</p>`;
  }
  if (enLista) html += "</ul>";
  return html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`([^`]+)`/g, "<code>$1</code>");
}
function estado(e, texto) {
  document.body.dataset.estado = e;
  $("#estado").textContent = texto || ESTADOS[e] || e;
}

async function api(ruta, opciones = {}) {
  const r = await fetch(ruta, { credentials: "same-origin", ...opciones });
  if (r.status === 401) throw new Error("Sin acceso: abre el enlace con la clave de nuevo.");
  if (!r.ok) throw new Error(`Error ${r.status}`);
  return r.json();
}

/* ---------- voz ---------- */
// iOS solo deja reproducir audio tras un toque: se "desbloquea" el elemento en el primero
let audioDesbloqueado = false;
function silencioWav() {  // WAV válido de 0.05 s en silencio (8 kHz, 8 bits, mono)
  const n = 400, b = new Uint8Array(44 + n), v = new DataView(b.buffer);
  const txt = (o, s) => [...s].forEach((c, i) => b[o + i] = c.charCodeAt(0));
  txt(0, "RIFF"); v.setUint32(4, 36 + n, true); txt(8, "WAVEfmt "); v.setUint32(16, 16, true);
  v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, 8000, true); v.setUint32(28, 8000, true);
  v.setUint16(32, 1, true); v.setUint16(34, 8, true); txt(36, "data"); v.setUint32(40, n, true);
  b.fill(128, 44);
  return "data:audio/wav;base64," + btoa(String.fromCharCode(...b));
}
function desbloquearAudio() {
  if (audioDesbloqueado) return;
  audioDesbloqueado = true;
  audio.src = silencioWav();
  audio.play().catch(() => { audioDesbloqueado = false; });
}
function hablar(texto) {
  if (!leerEnVoz || !texto) return;
  audio.src = "/api/voz?t=" + encodeURIComponent(texto);
  audio.play().catch(() => {});
}
$("#btn-voz").setAttribute("aria-pressed", String(leerEnVoz));
$("#btn-voz").onclick = () => {
  leerEnVoz = !leerEnVoz;
  $("#btn-voz").setAttribute("aria-pressed", String(leerEnVoz));
  try { localStorage.setItem("leerEnVoz", leerEnVoz ? "1" : "0"); } catch {}
  if (!leerEnVoz) audio.pause();
};

/* ---------- conversación ---------- */
function nuevoIntercambio(pregunta, aviso = false) {
  $("#bienvenida")?.remove();
  const nodo = el(`<section class="intercambio${aviso ? " aviso" : ""}">
      <div class="pregunta">${esc(pregunta)}</div><div class="pasos" hidden></div></section>`);
  conv.appendChild(nodo);
  bajar();
  return nodo;
}

function mostrar(nodo, r) {
  $(".pasos", nodo)?.remove();
  if (r.voz) nodo.appendChild(el(`<p class="voz">${esc(r.voz)}</p>`));
  if (r.detalle) nodo.appendChild(el(`<div class="detalle">${mdLigero(r.detalle)}</div>`));
  (r.tarjetas || []).forEach(t => nodo.appendChild(tarjeta(t)));
  if (r.abrir) nodo.appendChild(el(`<a class="btn" href="${esc(r.abrir)}" target="_blank" rel="noopener">▶ Escuchar en YouTube Music</a>`));
  if (r.confirmar) {
    const c = el(`<div class="confirmar" data-id="${esc(r.confirmar.id)}"><span>${esc(r.confirmar.pregunta)}</span>
        <button class="btn si">Sí, hazlo</button><button class="btn no">No</button></div>`);
    $(".si", c).onclick = () => confirmar(true);
    $(".no", c).onclick = () => confirmar(false);
    nodo.appendChild(c);
  }
  bajar();
}

function manejar(r, nodo) {
  if (r.limpiar) { conv.innerHTML = ""; nodo = nuevoIntercambio(r.texto || "…"); }
  if (r.texto && nodo) $(".pregunta", nodo).textContent = r.texto;
  if (r.confirmado) {
    const c = document.querySelector(`.confirmar[data-id="${CSS.escape(r.confirmado.id)}"]`);
    if (c) c.outerHTML = `<div class="confirmar hecho">${r.confirmado.valor ? "Confirmado." : "Descartado."}</div>`;
  }
  mostrar(nodo, r);
  hablar(r.voz);
}

async function enviar(peticion, pregunta) {
  audio.pause();
  actual = nuevoIntercambio(pregunta);
  const pasos = $(".pasos", actual);
  pasos.hidden = false;
  pasos.textContent = "Pensando…";
  estado("pensando");
  try {
    manejar(await peticion(), actual);
  } catch (e) {
    mostrar(actual, { voz: e.message });
  } finally {
    estado("reposo");
  }
}

const preguntar = texto => enviar(() => api("/api/preguntar", {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ texto }),
}), texto);

function confirmar(valor) {
  enviar(() => api("/api/confirmar", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ valor }),
  }), valor ? "Sí" : "No");
}

/* ---------- tarjetas ---------- */
function tarjeta(t) {
  const n = construirTarjeta(t);
  n.dataset.id = t.id || "";
  return n;
}
function construirTarjeta(t) {
  const titulo = esc(t.titulo || t.nombre || "");
  switch (t.tipo) {
    case "captura":
      return el(`<a class="tarjeta" href="${esc(t.url)}" target="_blank" rel="noopener">
          <div class="cab"><b>${titulo || esc(dominio(t.url))}</b></div>
          ${t.pendiente ? `<div class="cargando">Capturando ${esc(dominio(t.url))}…</div>`
            : t.src ? `<img src="${t.src}" alt="">` : `<div class="fallo">${esc(t.error || "Sin captura")}</div>`}</a>`);
    case "imagen":
      return el(`<a class="tarjeta" href="${esc(t.url)}" target="_blank" rel="noopener">
          ${titulo ? `<div class="cab"><b>${titulo}</b></div>` : ""}<img src="${esc(t.url)}" alt="" referrerpolicy="no-referrer"></a>`);
    case "codigo":
      return el(`<div class="tarjeta"><div class="cab"><b>${esc(t.archivo || "código")}</b></div><pre><code>${esc(t.codigo)}</code></pre></div>`);
    case "estado": {
      const clase = t.pendiente ? "" : t.ok ? "ok" : "mal";
      const info = t.pendiente ? "Comprobando…" : t.ok ? `En línea · ${t.ms} ms` : "Sin respuesta";
      return el(`<div class="tarjeta"><div class="estado-fila"><span class="punto ${clase}"></span>
          <div><div>${titulo || esc(dominio(t.url))}</div><div class="fd">${info}</div></div></div></div>`);
    }
    case "tabla": {
      const cols = (t.columnas || []).map(c => `<th>${esc(c)}</th>`).join("");
      const filas = (t.filas || []).map(f => `<tr>${f.map(c => `<td>${esc(c)}</td>`).join("")}</tr>`).join("");
      return el(`<div class="tarjeta">${titulo ? `<div class="cab"><b>${titulo}</b></div>` : ""}
          <div class="tabla-cont"><table><thead><tr>${cols}</tr></thead><tbody>${filas}</tbody></table></div></div>`);
    }
    case "dato":
      return el(`<div class="tarjeta dato"><div class="titulo-dato">${titulo}</div><div class="valor">${esc(t.valor)}</div>
          ${t.nota ? `<div class="nota">${esc(t.nota)}</div>` : ""}</div>`);
    case "fuentes": {
      const n = el(`<div class="tarjeta"><div class="cab"><b>Enlaces</b></div><ul class="fuentes"></ul></div>`);
      (t.enlaces || []).forEach(e => $(".fuentes", n).appendChild(el(`<li><a href="${esc(e.url)}" target="_blank" rel="noopener">
          <span>${esc(e.titulo || dominio(e.url))}</span><span class="fd">${esc(dominio(e.url))}</span></a></li>`)));
      return n;
    }
    default:
      return el(`<div class="tarjeta"><div class="fallo">${esc(JSON.stringify(t))}</div></div>`);
  }
}

/* ---------- novedades: progreso, tarjetas pendientes, tareas y recordatorios ---------- */
async function revisarNovedades(inicial = false) {
  try {
    const r = await api(`/api/novedades?desde=${novedad}`);
    if (!inicial) {
      for (const e of r.eventos) {
        if (e.tipo === "progreso" && actual && document.body.dataset.estado === "pensando") {
          const pasos = $(".pasos", actual);
          if (pasos) pasos.textContent = e.texto;
        } else if (e.tipo === "tarjeta") {
          const viejo = document.querySelector(`.tarjeta[data-id="${CSS.escape(e.tarjeta.id)}"]`);
          if (viejo) viejo.replaceWith(tarjeta(e.tarjeta));
        } else if (e.tipo === "aviso") {
          const nodo = nuevoIntercambio(e.texto, true);
          $(".pasos", nodo).remove();
          mostrar(nodo, e);
          if (document.body.dataset.estado === "reposo") hablar(e.voz);
        }
      }
    }
    novedad = r.n;
  } catch {}
}
setInterval(() => { if (!document.hidden) revisarNovedades(); }, 1500);
document.addEventListener("visibilitychange", () => { if (!document.hidden) revisarNovedades(); });
revisarNovedades(true);

/* ---------- hablar: mantener pulsado, o tocar para empezar y otra vez para enviar ---------- */
async function empezarGrabacion() {
  desbloquearAudio();
  audio.pause();
  try {
    microfono = microfono || await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch {
    actual = nuevoIntercambio("🎤");
    mostrar(actual, { voz: "Necesito permiso para usar el micrófono." });
    return false;
  }
  const tipo = ["audio/mp4", "audio/webm;codecs=opus", "audio/webm"].find(t => MediaRecorder.isTypeSupported(t)) || "";
  const partes = [];
  grabadora = new MediaRecorder(microfono, tipo ? { mimeType: tipo } : {});
  grabadora.ondataavailable = e => e.data.size && partes.push(e.data);
  grabadora.onstop = () => {
    const blob = new Blob(partes, { type: grabadora.mimeType || tipo });
    grabadora = null;
    enviar(() => api("/api/audio", { method: "POST", headers: { "Content-Type": blob.type }, body: blob }), "🎤 …");
  };
  grabadora.start();
  estado("escuchando");
  return true;
}
function terminarGrabacion() {
  if (grabadora && grabadora.state === "recording") grabadora.stop();
  modoToque = false;
}

const boton = $("#hablar");
boton.addEventListener("pointerdown", async e => {
  e.preventDefault();
  if (document.body.dataset.estado === "pensando") return;
  if (grabadora) { terminarGrabacion(); return; }  // segundo toque en modo toque
  inicioToque = Date.now();
  await empezarGrabacion();
});
boton.addEventListener("pointerup", () => {
  if (!grabadora) return;
  if (Date.now() - inicioToque < 400 && !modoToque) { modoToque = true; return; }  // toque corto: sigue grabando
  if (!modoToque) terminarGrabacion();
});
boton.addEventListener("pointercancel", terminarGrabacion);

$("#form").onsubmit = e => {
  e.preventDefault();
  const texto = $("#entrada").value.trim();
  if (!texto) return;
  $("#entrada").value = "";
  $("#entrada").blur();
  desbloquearAudio();
  preguntar(texto);
};
