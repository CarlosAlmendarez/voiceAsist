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
  seguimiento: "¿Algo más? Te escucho…",
};
const ESTADOS_TAREA = { en_curso: "Trabajando", hecha: "Terminada", error: "Falló", cancelada: "Cancelada" };

let actual = null;        // intercambio en curso
let cfg = { segundos_ocultar: 0, palabra: "asistente" };
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

/* ---------- agenda del día ---------- */
const MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
  "septiembre", "octubre", "noviembre", "diciembre"];
const DIAS_SEM = ["domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado"];
const REPITE = { diario: "cada día", semanal: "cada semana", mensual: "cada mes", anual: "cada año" };
let diaVisto = null;      // "AAAA-MM-DD" que se está viendo; null = hoy
let agendaDatos = null;
let resaltarId = null;    // evento recién creado: se ilumina al pintarlo

const isoLocal = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const deIso = s => { const [y, m, d] = s.slice(0, 10).split("-").map(Number); const [h, mi] = (s.slice(11) || "0:0").split(":").map(Number); return new Date(y, m - 1, d, h || 0, mi || 0); };
const sumarDias = (iso, n) => { const d = deIso(iso); d.setDate(d.getDate() + n); return isoLocal(d); };
const hora = d => d.toLocaleTimeString("es-MX", { hour: "numeric", minute: "2-digit" }).replace(/\s?([ap])\.\s?m\./i, " $1. m.");
function duracion(min) {
  if (min < 1) return "menos de un minuto";
  if (min < 60) return `${Math.round(min)} min`;
  const h = Math.floor(min / 60), m = Math.round(min % 60);
  return m ? `${h} h ${m} min` : `${h} h`;
}

async function cargarAgenda() {
  const a = api();
  if (!a?.agenda) return;
  try { agendaDatos = await a.agenda(diaVisto); } catch { return; }
  pintarAgenda();
}

function estadoItem(it, ahora) {
  if (it.todo_el_dia) return "todo-dia";
  const ini = deIso(it.inicio), fin = it.fin ? deIso(it.fin) : ini;
  if (fin <= ahora && (it.fin || ini < ahora)) return "pasado";
  return ini <= ahora ? "ahora" : "futuro";
}

function pintarAgenda() {
  if (!agendaDatos) return;
  const { dia, hoy, items } = agendaDatos;
  const ahora = new Date();
  if (!diaVisto && hoy !== isoLocal(ahora)) { cargarAgenda(); return; }  // pasó la medianoche
  const esHoy = dia === hoy, d = deIso(dia);
  $("#dia-nombre").textContent = esHoy ? "Hoy" : dia === sumarDias(hoy, 1) ? "Mañana"
    : dia === sumarDias(hoy, -1) ? "Ayer" : DIAS_SEM[d.getDay()].replace(/^./, c => c.toUpperCase());
  $("#dia-fecha").textContent = `${esHoy || dia === sumarDias(hoy, 1) || dia === sumarDias(hoy, -1) ? DIAS_SEM[d.getDay()] + " " : ""}${d.getDate()} de ${MESES[d.getMonth()]}`;
  $("#dia-titulo").classList.toggle("otro", !esHoy);
  $("#dia-cuenta").textContent = items.length ? `${items.length} ${items.length === 1 ? "actividad" : "actividades"}` : "";

  const lista = $("#dia-lista");
  lista.innerHTML = "";
  if (!items.length) {
    lista.appendChild(el(`<li class="vacio">${esHoy ? "Día libre." : "Nada agendado."} Di «agrega comida a las 3» para anotar algo.</li>`));
  }
  let marcaPuesta = !esHoy;
  for (const it of items) {
    const est = esHoy ? estadoItem(it, ahora) : deIso(dia) < deIso(hoy) ? "pasado" : "futuro";
    if (!marcaPuesta && (est === "futuro" || est === "ahora")) {
      lista.appendChild(el(`<li class="marca-ahora"><span>${hora(ahora)}</span></li>`));
      marcaPuesta = true;
    }
    lista.appendChild(itemAgenda(it, est, ahora));
  }
  if (!marcaPuesta && items.some(i => !i.todo_el_dia)) lista.appendChild(el(`<li class="marca-ahora"><span>${hora(ahora)}</span></li>`));

  // aviso de lo que sigue (solo hoy)
  const banda = $("#dia-siguiente");
  const enCurso = esHoy && items.find(i => estadoItem(i, ahora) === "ahora");
  const prox = esHoy && items.find(i => estadoItem(i, ahora) === "futuro");
  if (enCurso && enCurso.fin) {
    banda.innerHTML = `<b>Ahora</b> ${esc(enCurso.titulo)} <span>· termina en ${duracion((deIso(enCurso.fin) - ahora) / 60000)}</span>`;
  } else if (prox) {
    banda.innerHTML = `<b>Sigue</b> ${esc(prox.titulo)} <span>· en ${duracion((deIso(prox.inicio) - ahora) / 60000)}</span>`;
  }
  banda.hidden = !(enCurso?.fin || prox);

  if (resaltarId) {
    const n = lista.querySelector(`[data-id="${CSS.escape(resaltarId)}"]`);
    if (n) { n.classList.add("nuevo"); n.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
    resaltarId = null;
  }
}

function itemAgenda(it, est, ahora) {
  let cuando = `<div class="hora todo">Todo<br>el día</div>`, meta = [];
  if (!it.todo_el_dia) {
    const ini = deIso(it.inicio), [, h, ap] = hora(ini).match(/^(\S+)\s*(.*)$/);
    cuando = `<div class="hora"><b>${esc(h)}</b><span>${esc(ap)}</span></div>`;
    if (it.fin && it.tipo === "evento") meta.push(`${hora(ini)} – ${hora(deIso(it.fin))}`);
  }
  if (it.tipo === "recordatorio") meta.push("Recordatorio");
  if (it.tipo === "voz") meta.push("Aviso por voz");
  if (it.lugar) meta.push(esc(it.lugar));
  if (it.repetir) meta.push(`↻ ${REPITE[it.repetir] || it.repetir}`);
  let progreso = "";
  if (est === "ahora" && it.fin) {
    const p = (ahora - deIso(it.inicio)) / (deIso(it.fin) - deIso(it.inicio));
    progreso = `<i class="progreso" style="--p:${Math.min(1, Math.max(0, p)).toFixed(3)}"></i>`;
  }
  const n = el(`<li class="item ${est} ${it.tipo}" data-id="${esc(it.id)}">
      ${cuando}
      <div class="linea"><i></i></div>
      <div class="info">
        <div class="t">${esc(it.titulo)}</div>
        ${meta.length ? `<div class="m">${meta.map(x => `<span>${x}</span>`).join("")}</div>` : ""}
        ${it.nota ? `<div class="nota">${esc(it.nota)}</div>` : ""}
        ${progreso}
      </div>
      <div class="acciones">
        <button class="quitar" title="Quitar de la agenda"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
      </div>
    </li>`);
  $(".quitar", n).onclick = () => {
    const acc = $(".acciones", n);
    const extra = it.repetir ? " (todas las repeticiones)" : "";
    acc.innerHTML = `<span class="seguro">¿Quitar${extra}?</span><button class="btn si">Sí</button><button class="btn no">No</button>`;
    n.classList.add("confirmando");
    $(".si", acc).onclick = () => api()?.borrar(it.id, it.tipo);
    $(".no", acc).onclick = () => pintarAgenda();
  };
  return n;
}

function plegarAgenda(valor) {
  $("#dia").classList.toggle("plegado", valor);
  $("#dia-plegar").title = valor ? "Mostrar la agenda" : "Plegar la agenda";
  try { localStorage.setItem("agendaPlegada", valor ? "1" : ""); } catch {}
}

/* ---------- servicios (vigía) ---------- */
let serviciosDatos = null;

async function cargarServicios() {
  const a = api();
  if (!a?.alertas) return;
  try { serviciosDatos = await a.alertas(); } catch { return; }
  pintarServicios();
}

function haceTexto(d) {
  const min = (Date.now() - d) / 60000;
  if (min < 60) return `hace ${Math.max(1, Math.round(min))} min`;
  if (min < 1440) return `hace ${Math.floor(min / 60)} h`;
  return `hace ${Math.floor(min / 1440)} d`;
}

function desdeTexto(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const dia = isoLocal(d) === isoLocal(new Date()) ? "" : `${d.getDate()} ${MESES[d.getMonth()].slice(0, 3)}, `;
  return `desde ${dia}${hora(d)} (${haceTexto(d)})`;
}

const ORIGENES = { kuma: "servicio", salud: "salud", seguridad: "seguridad" };

// el vigía manda "host · seguridad: qué pasa"; con la etiqueta de origen sobra el " · seguridad"
function textoAlerta(a) {
  const t = a.texto || a.monitor || "Alerta";
  return a.origen ? t.replace(` · ${a.origen}:`, ":") : t;
}

function pintarServicios() {
  const s = serviciosDatos, sec = $("#servicios");
  sec.hidden = !s?.configurado;
  if (sec.hidden) return;
  const activas = s.alertas || [];
  const criticas = activas.filter(a => a.severidad === "critico" && !a.reconocida);  // rojo solo sin «Visto»
  const n = activas.length;
  let texto, nivel;
  if (s.total == null) {
    [texto, nivel] = [s.sin_respuesta ? "Servicios · el vigía no responde" : "Servicios · revisando…", s.sin_respuesta ? "atencion" : ""];
  } else if (!n && !s.no_arriba) {
    [texto, nivel] = [`${s.total} servicios · todo en orden`, "ok"];
  } else if (!n) {
    [texto, nivel] = [`${s.total} servicios · ${s.no_arriba} sin responder`, "atencion"];
  } else {
    texto = `${s.total} servicios · ${n} ${n === 1 ? "problema" : "problemas"}`
      + (criticas.length ? ` (${criticas.length} ${criticas.length === 1 ? "crítico" : "críticos"})` : "");
    nivel = criticas.length ? "critico" : "atencion";
  }
  if (s.sin_respuesta && s.total != null) texto += " · sin respuesta del vigía";
  $("#srv-resumen").textContent = texto;
  $("#srv-resumen").title = s.revisado ? `Revisado a las ${hora(new Date(s.revisado))}` : "";
  sec.dataset.nivel = nivel;

  // plegada: solo quedan a la vista las críticas sin «Visto» (fijas hasta pulsarlo)
  const plegada = sec.classList.contains("plegado");
  const lista = $("#srv-lista");
  lista.innerHTML = "";
  for (const a of activas) {
    if (a.reconocida || (plegada && a.severidad !== "critico")) continue;
    const silencio = a.caido_en_silencio && !/silencio/i.test(a.texto || "") ? "caído en silencio" : "";
    const meta = [desdeTexto(a.desde), silencio, a.msg && a.msg !== a.motivo ? a.msg : ""].filter(Boolean);
    const origen = ORIGENES[a.origen] || a.origen || "";
    const li = el(`<li class="alerta ${a.severidad === "critico" ? "critico" : "atencion"}">
        <i class="marca"></i>
        <div class="info">
          <div class="t">${origen ? `<span class="origen" data-origen="${esc(a.origen)}">${esc(origen)}</span>` : ""}${esc(textoAlerta(a))}</div>
          ${a.motivo ? `<div class="motivo">${esc(a.motivo)}</div>` : ""}
          ${meta.length ? `<div class="m">${meta.map(x => `<span>${esc(x)}</span>`).join("")}</div>` : ""}
        </div>
        <button class="btn visto" title="Marcar como vista">Visto</button>
      </li>`);
    $(".visto", li).onclick = async () => {
      li.classList.add("saliendo");
      try { await api()?.reconocer(a.id); } catch {}
      cargarServicios();
    };
    lista.appendChild(li);
  }
  const vistas = activas.filter(a => a.reconocida).length;
  if (vistas && !plegada) lista.appendChild(el(`<li class="vacio">${vistas} ${vistas === 1 ? "alerta vista" : "alertas vistas"} sigue${vistas === 1 ? "" : "n"} activa${vistas === 1 ? "" : "s"}.</li>`));
}

function plegarServicios(valor) {
  $("#servicios").classList.toggle("plegado", valor);
  $("#srv-plegar").title = valor ? "Mostrar los servicios" : "Plegar servicios";
  try { localStorage.setItem("serviciosPlegados", valor ? "1" : ""); } catch {}
  pintarServicios();
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
      case "agenda":
        if (e.dia) diaVisto = agendaDatos && e.dia === agendaDatos.hoy ? null : e.dia;
        resaltarId = e.resaltar || null;
        cargarAgenda();
        break;
      case "alerta":
        if (e.accion === "nueva" && e.alerta?.severidad === "critico") abrirPanel();
        cargarServicios();
        break;
      case "modo":
        document.body.classList.remove("orbe", "panel");
        document.body.classList.add(e.modo);
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
$("#dia-ant").onclick = () => { diaVisto = sumarDias(agendaDatos?.dia || isoLocal(new Date()), -1); cargarAgenda(); };
$("#dia-sig").onclick = () => { diaVisto = sumarDias(agendaDatos?.dia || isoLocal(new Date()), 1); cargarAgenda(); };
$("#dia-titulo").onclick = () => { diaVisto = null; cargarAgenda(); };
$("#dia-plegar").onclick = () => plegarAgenda(!$("#dia").classList.contains("plegado"));
try { plegarAgenda(!!localStorage.getItem("agendaPlegada")); } catch {}
$("#srv-plegar").onclick = () => plegarServicios(!$("#servicios").classList.contains("plegado"));
try { plegarServicios(!!localStorage.getItem("serviciosPlegados")); } catch {}
setInterval(cargarServicios, 60000);  // resumen al día aunque no cambie ninguna alerta
setInterval(pintarAgenda, 30000);   // estados "ahora"/"pasado" y la marca de la hora
setInterval(cargarAgenda, 300000);  // por si se editó agenda.json a mano
document.addEventListener("keydown", e => { if (e.key === "Escape" && cfg.segundos_ocultar) cerrarPanel(); });
$("#panel").addEventListener("mouseenter", () => { ratonEncima = true; });
$("#panel").addEventListener("mouseleave", () => { ratonEncima = false; });

window.addEventListener("pywebviewready", async () => {
  cfg = { ...cfg, ...(await api().config()) };
  if (cfg.modo) app.on({ tipo: "modo", modo: cfg.modo });
  $("#palabra").textContent = `«${cfg.palabra}»`;
  ESTADOS.reposo = `Di «${cfg.palabra}» para hablarme`;
  cargarAgenda();
  cargarServicios();
  try { sugerirComandos($("#entrada"), await api().comandos()); } catch {}
});

// sugerencias de comandos al escribir "/" (la lista viene de acciones.COMANDOS)
function sugerirComandos(entrada, comandos) {
  const lista = document.createElement("datalist");
  lista.id = "comandos";
  for (const [uso, que] of comandos) {
    const nombre = uso.split(" ")[0];
    const op = document.createElement("option");
    op.value = nombre + (uso !== nombre ? " " : "");
    op.label = `${uso.slice(nombre.length).trim()} — ${que}`.replace(/^ — /, "");
    lista.appendChild(op);
  }
  document.body.appendChild(lista);
  // solo con "/" al principio: si no, sugeriría comandos al escribir preguntas normales
  entrada.addEventListener("input", () => {
    if (entrada.value.startsWith("/")) entrada.setAttribute("list", "comandos");
    else entrada.removeAttribute("list");
  });
}
