"use strict";

const $ = (s, r = document) => r.querySelector(s);
const api = () => window.pywebview && window.pywebview.api;
const conv = $("#conversacion");

const ESTADOS = {
  cargando: "Cargando modelos…",
  reposo: "Di «asistente» para hablarme",
  escuchando: "Escuchando…",
  transcribiendo: "Entendiendo…",
  pensando: "Pensando…",
  hablando: "Respondiendo",
  confirmando: "¿Lo hago? Di «sí» o «no»",
};
const ESTADOS_TAREA = { en_curso: "Trabajando", hecha: "Terminada", error: "Falló", cancelada: "Cancelada" };

let actual = null;        // intercambio en curso
let cfg = { segundos_ocultar: 60, palabra: "asistente" };
let temporizadorOcultar = null;
let temporizadoresVoz = [];
let ratonEncima = false;

/* ---------- utilidades ---------- */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function dominio(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return ""; }
}
function mdLigero(texto) {
  const lineas = esc(texto).split(/\n/);
  let html = "", enLista = false;
  for (const l of lineas) {
    const item = l.match(/^\s*[-•*]\s+(.*)/);
    if (item) { if (!enLista) { html += "<ul>"; enLista = true; } html += `<li>${item[1]}</li>`; continue; }
    if (enLista) { html += "</ul>"; enLista = false; }
    if (l.trim()) html += `<p>${l}</p>`;
  }
  if (enLista) html += "</ul>";
  return html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`([^`]+)`/g, "<code>$1</code>");
}
function bajar() { conv.scrollTop = conv.scrollHeight; }
function el(html) { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; }

/* ---------- modo orbe / panel ---------- */
function abrirPanel() {
  clearTimeout(temporizadorOcultar);
  if (document.body.classList.contains("panel")) return;
  document.body.classList.replace("orbe", "panel");
  api()?.modo("panel");
}
function cerrarPanel() {
  clearTimeout(temporizadorOcultar);
  $("#historial").hidden = true;
  api()?.modo("orbe");
  document.body.classList.replace("panel", "orbe");
}
function programarOcultar() {
  clearTimeout(temporizadorOcultar);
  if (!cfg.segundos_ocultar) return;
  temporizadorOcultar = setTimeout(() => {
    const ocupado = ratonEncima || document.activeElement === $("#entrada") && $("#entrada").value
      || document.body.dataset.estado !== "reposo" || !$("#historial").hidden;
    ocupado ? programarOcultar() : cerrarPanel();
  }, cfg.segundos_ocultar * 1000);
}

/* ---------- intercambios ---------- */
function nuevoIntercambio(pregunta, historico) {
  $("#bienvenida")?.remove();
  const nodo = el(`<section class="intercambio">
      ${historico ? `<div class="historico">${esc(historico)}</div>` : ""}
      <div class="pregunta">${esc(pregunta)}</div>
      <ul class="pasos"></ul>
    </section>`);
  conv.appendChild(nodo);
  bajar();
  return nodo;
}

function agregarPaso(texto) {
  if (!actual) actual = nuevoIntercambio("…");
  const lista = $(".pasos", actual);
  lista.querySelectorAll("li:not(.hecho)").forEach(li => li.classList.add("hecho"));
  lista.appendChild(el(`<li>${esc(texto)}</li>`));
  bajar();
}

function cerrarPasos(nodo) {
  const lista = $(".pasos", nodo);
  lista.querySelectorAll("li").forEach(li => li.classList.add("hecho"));
  const n = lista.children.length;
  if (n <= 1) { lista.remove(); return; }
  lista.classList.add("plegado");
  lista.lastElementChild.dataset.mas = `  · ${n} pasos`;
  lista.onclick = () => lista.classList.toggle("plegado");
}

function mostrarRespuesta(nodo, r) {
  cerrarPasos(nodo);
  const tokens = String(r.voz).split(/(\s+)/).map(t => /^\s+$/.test(t) ? t : `<span class="w">${esc(t)}</span>`).join("");
  nodo.appendChild(el(`<p class="voz">${tokens}</p>`));
  if (r.detalle) nodo.appendChild(el(`<div class="detalle">${mdLigero(r.detalle)}</div>`));
  if (r.tarjetas?.length) {
    const cont = el(`<div class="tarjetas"></div>`);
    r.tarjetas.forEach(t => cont.appendChild(tarjeta(t)));
    nodo.appendChild(cont);
  }
  nodo.querySelectorAll(".confirmar").forEach(c => nodo.appendChild(c));  // la pregunta de confirmación, al final
  // deja a la vista el inicio del intercambio (la frase que se está leyendo), no el final
  conv.scrollTop = nodo.offsetTop - conv.offsetTop - 12;
}

/* ---------- palabras iluminadas al ritmo de la voz ---------- */
const norm = s => s.toLowerCase().normalize("NFD").replace(/[^\p{L}\p{N}]/gu, "");

function iluminar(palabras) {
  temporizadoresVoz.forEach(clearTimeout);
  temporizadoresVoz = [];
  const voz = actual && [...actual.querySelectorAll(".voz")].pop();
  if (!voz) return;
  const spans = [...voz.querySelectorAll(".w")];
  if (!palabras.length) { voz.classList.add("completa"); return; }
  let j = 0;
  for (const p of palabras) {
    const objetivo = norm(p.w);
    if (!objetivo) continue;
    let k = j;
    while (k < spans.length && k < j + 5) {
      const s = norm(spans[k].textContent);
      if (s && (s.startsWith(objetivo) || objetivo.startsWith(s))) break;
      k++;
    }
    if (k >= spans.length || k >= j + 5) continue;
    const hasta = k;
    temporizadoresVoz.push(setTimeout(() => spans.slice(0, hasta + 1).forEach(s => s.classList.add("on")), p.t));
    j = k + 1;
  }
  const fin = palabras[palabras.length - 1].t + 900;
  temporizadoresVoz.push(setTimeout(() => voz.classList.add("completa"), fin));
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
    case "captura": {
      const cuerpo = t.pendiente ? `<div class="cargando">Capturando ${esc(dominio(t.url))}…</div>`
        : t.src ? `<img src="${t.src}" alt="">`
        : `<div class="fallo">${esc(t.error || "Sin captura")}</div>`;
      const n = el(`<figure class="tarjeta captura clic">
          <div class="cab"><b>${titulo || esc(dominio(t.url))}</b><span class="dom">${esc(dominio(t.url))} ↗</span></div>
          ${cuerpo}</figure>`);
      n.onclick = () => api()?.abrir(t.url);
      return n;
    }
    case "imagen": {
      const n = el(`<figure class="tarjeta imagen clic">
          ${titulo ? `<div class="cab"><b>${titulo}</b><span class="dom">${esc(dominio(t.url))}</span></div>` : ""}
          <img src="${esc(t.url)}" alt="" referrerpolicy="no-referrer"></figure>`);
      $("img", n).onerror = e => e.target.replaceWith(el(`<div class="fallo">No se pudo cargar la imagen.</div>`));
      n.onclick = () => api()?.abrir(t.url);
      return n;
    }
    case "codigo": {
      const n = el(`<div class="tarjeta codigo">
          <div class="cab"><b>${esc(t.archivo || "código")}</b><span class="dom">${t.desde ? `línea ${esc(t.desde)}` : ""}</span></div>
          <pre><code class="language-${esc(t.lenguaje || "plaintext")}">${esc(t.codigo)}</code></pre></div>`);
      try { hljs.highlightElement($("code", n)); } catch {}
      return n;
    }
    case "estado": {
      const clase = t.pendiente ? "espera" : t.ok ? "ok" : "mal";
      const info = t.pendiente ? "Comprobando…"
        : t.ok ? `En línea · ${t.ms} ms · HTTP ${t.codigo}`
        : `Sin respuesta${t.codigo ? ` · HTTP ${t.codigo}` : t.detalle ? ` · ${esc(t.detalle)}` : ""}`;
      const n = el(`<div class="tarjeta clic"><div class="estado-fila">
          <span class="punto ${clase}"></span>
          <div><div class="nombre">${titulo || esc(dominio(t.url))}</div><div class="info">${info}</div></div>
        </div></div>`);
      n.onclick = () => api()?.abrir(t.url);
      return n;
    }
    case "tabla": {
      const cols = (t.columnas || []).map(c => `<th>${esc(c)}</th>`).join("");
      const filas = (t.filas || []).map(f => `<tr>${f.map(c => `<td>${esc(c)}</td>`).join("")}</tr>`).join("");
      return el(`<div class="tarjeta">${titulo ? `<div class="cab"><b>${titulo}</b></div>` : ""}
          <div class="tabla-cont"><table><thead><tr>${cols}</tr></thead><tbody>${filas}</tbody></table></div></div>`);
    }
    case "dato":
      return el(`<div class="tarjeta dato">
          <div class="titulo-dato">${titulo}</div><div class="valor">${esc(t.valor)}</div>
          ${t.nota ? `<div class="nota">${esc(t.nota)}</div>` : ""}</div>`);
    case "fuentes": {
      const n = el(`<div class="tarjeta"><div class="cab"><b>Fuentes</b></div><ul class="fuentes"></ul></div>`);
      (t.enlaces || []).forEach(e => {
        const li = el(`<li><a><span class="ft">${esc(e.titulo || dominio(e.url))}</span><span class="fd">${esc(dominio(e.url))}</span></a></li>`);
        li.onclick = () => api()?.abrir(e.url);
        $(".fuentes", n).appendChild(li);
      });
      return n;
    }
    default:
      return el(`<div class="tarjeta"><div class="fallo">${esc(JSON.stringify(t))}</div></div>`);
  }
}

/* ---------- tareas ---------- */
function pedirConfirmacion(e) {
  if (!actual) actual = nuevoIntercambio("…");
  const n = el(`<div class="confirmar" data-id="${esc(e.id)}">
      <span>${esc(e.pregunta)}</span>
      <button class="btn si">Sí, hazlo</button><button class="btn no">No</button></div>`);
  $(".si", n).onclick = () => api()?.confirmar(true);
  $(".no", n).onclick = () => api()?.confirmar(false);
  actual.appendChild(n);
  bajar();
}

function actualizarTarea(e) {
  let n = document.querySelector(`.tarea[data-id="${CSS.escape(e.id)}"]`);
  if (!n) {
    if (!actual) actual = nuevoIntercambio("…");
    n = el(`<div class="tarjeta tarea" data-id="${esc(e.id)}">
        <div class="cab"><b>${esc(e.descripcion || "Tarea")}</b><span class="dom estado-tarea"></span>
          <button class="btn no cancelar">Cancelar</button></div>
        <ul class="pasos"></ul></div>`);
    $(".cancelar", n).onclick = () => api()?.cancelar_tarea(e.id);
    actual.appendChild(n);
    bajar();
  }
  n.dataset.estado = e.estado;
  $(".estado-tarea", n).textContent = ESTADOS_TAREA[e.estado] || e.estado;
  const lista = $(".pasos", n);
  if (e.paso) {
    lista.querySelectorAll("li:not(.hecho)").forEach(li => li.classList.add("hecho"));
    lista.appendChild(el(`<li>${esc(e.paso)}</li>`));
    while (lista.children.length > 4) lista.firstElementChild.remove();  // solo los últimos pasos
  }
  if (e.estado !== "en_curso") lista.querySelectorAll("li").forEach(li => li.classList.add("hecho"));
  document.body.classList.toggle("trabajando", !!document.querySelector('.tarea[data-estado="en_curso"]'));
}

/* ---------- historial ---------- */
async function abrirHistorial() {
  const lista = $("#hist-lista");
  lista.innerHTML = "";
  const items = (await api()?.historial()) || [];
  if (!items.length) lista.appendChild(el(`<li class="vacio">Todavía no hay preguntas.</li>`));
  items.forEach(h => {
    const li = el(`<li><span class="hp">${esc(h.pregunta)}</span><span class="hf">${esc(h.fecha)}</span></li>`);
    li.onclick = async () => {
      const item = await api().ver_historial(h.i);
      if (!item) return;
      $("#historial").hidden = true;
      actual = nuevoIntercambio(item.pregunta, item.fecha);
      mostrarRespuesta(actual, item);
      actual.querySelector(".voz")?.classList.add("completa");
    };
    lista.appendChild(li);
  });
  $("#historial").hidden = false;
}

/* ---------- eventos del motor ---------- */
window.app = {
  on(e) {
    switch (e.tipo) {
      case "estado":
        document.body.dataset.estado = e.estado;
        $("#estado-texto").textContent = document.body.classList.contains("silenciado") && e.estado === "reposo"
          ? "Micrófono silenciado" : ESTADOS[e.estado] || e.estado;
        if (["escuchando", "pensando", "transcribiendo", "confirmando"].includes(e.estado)) abrirPanel();
        if (["escuchando", "confirmando"].includes(e.estado)) document.documentElement.style.setProperty("--nivel", 0);
        if (e.estado === "reposo" && document.body.classList.contains("panel")) programarOcultar();
        break;
      case "nivel":
        document.documentElement.style.setProperty("--nivel", e.valor.toFixed(3));
        break;
      case "pregunta":
        abrirPanel();
        actual = nuevoIntercambio(e.texto);
        break;
      case "progreso":
        agregarPaso(e.texto);
        break;
      case "respuesta":
        if (!actual) actual = nuevoIntercambio("…");
        mostrarRespuesta(actual, e);
        break;
      case "palabras":
        iluminar(e.palabras);
        break;
      case "tarjeta": {
        const viejo = document.querySelector(`.tarjeta[data-id="${CSS.escape(e.tarjeta.id)}"]`);
        if (viejo) viejo.replaceWith(tarjeta(e.tarjeta));
        break;
      }
      case "confirmar":
        pedirConfirmacion(e);
        break;
      case "confirmado": {
        const n = document.querySelector(`.confirmar[data-id="${CSS.escape(e.id)}"]`);
        if (n) n.outerHTML = `<div class="confirmar hecho">${e.valor ? "Confirmado." : "Descartado."}</div>`;
        break;
      }
      case "tarea":
        actualizarTarea(e);
        break;
      case "aviso":
        abrirPanel();
        actual = nuevoIntercambio(e.texto);
        actual.classList.add("aviso");
        programarOcultar();
        break;
      case "silenciado":
        document.body.classList.toggle("silenciado", e.valor);
        $("#estado-texto").textContent = e.valor ? "Micrófono silenciado" : ESTADOS[document.body.dataset.estado];
        break;
      case "limpiar":
        conv.innerHTML = "";
        actual = null;
        break;
      case "abrir_panel":
        abrirPanel();
        setTimeout(() => $("#entrada").focus(), 150);
        programarOcultar();
        break;
    }
  },
};

/* ---------- controles ---------- */
$("#orbe-solo").onclick = () => { abrirPanel(); programarOcultar(); };
$("#orb-mini").onclick = () => api()?.escuchar();
$("#btn-cerrar").onclick = cerrarPanel;
$("#btn-nueva").onclick = () => api()?.nueva();
$("#btn-historial").onclick = () => $("#historial").hidden ? abrirHistorial() : ($("#historial").hidden = true);
$("#btn-hist-cerrar").onclick = () => ($("#historial").hidden = true);
$("#btn-mic").onclick = () => api()?.silenciar(!document.body.classList.contains("silenciado"));
$("#btn-detener").onclick = () => api()?.detener();
$("#form").onsubmit = ev => {
  ev.preventDefault();
  const texto = $("#entrada").value.trim();
  if (!texto) return;
  $("#entrada").value = "";
  api()?.preguntar(texto);
};
document.addEventListener("keydown", e => { if (e.key === "Escape") cerrarPanel(); });
$("#panel").addEventListener("mouseenter", () => { ratonEncima = true; });
$("#panel").addEventListener("mouseleave", () => { ratonEncima = false; });

window.addEventListener("pywebviewready", async () => {
  cfg = { ...cfg, ...(await api().config()) };
  $("#palabra").textContent = `«${cfg.palabra}»`;
  ESTADOS.reposo = `Di «${cfg.palabra}» para hablarme`;
});
