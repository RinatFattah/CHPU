"use strict";
const $ = (id) => document.getElementById(id);
// SPEC — ответ /api/params целиком: спецификации ОБОИХ видов обработки и их
// этапы. Форма переключается между видами без похода на сервер.
let SPEC = null, KIND = "mill", JOB = null, ES = null, TIMER = null;

const kindSpec = (k) => SPEC.kinds[k || KIND];
const phases = (k) => SPEC.phases[k || KIND];
const phaseIds = (k) => phases(k).map((p) => p.id);
const cycleIds = (k) => phases(k).filter((p) => p.where === "cycle")
                                 .map((p) => p.id);
const escapeHtml = (s) => String(s).replace(/[&<>]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

// ── вид обработки ────────────────────────────────────────────────────────────
function renderKinds() {
  $("kinds").innerHTML = Object.entries(SPEC.kinds).map(([id, k]) =>
    `<label class="kind ${id === KIND ? "on" : ""}" data-kind="${id}">
       <input type="radio" name="kind" value="${id}" ${id === KIND ? "checked" : ""}>
       <span class="kind-title">${escapeHtml(k.title)}</span>
       <span class="kind-sub">${escapeHtml(k.sub)}</span>
       <span class="kind-about">${escapeHtml(k.about)}</span>
     </label>`).join("");
  $("kinds").querySelectorAll("input[name=kind]").forEach((el) =>
    el.addEventListener("change", () => selectKind(el.value)));
}

function selectKind(kind) {
  KIND = kind;
  $("kinds").querySelectorAll(".kind").forEach((el) =>
    el.classList.toggle("on", el.dataset.kind === kind));
  // Заготовка — только у фрезеровки: у точения прокат подбирается сам, по ряду
  // ГОСТ от габарита детали и припуска на сторону.
  const useStock = kindSpec().stock;
  $("drop-stock").hidden = !useStock;
  if (!useStock) $("stock").value = "";
  renderParams();
}

// ── параметры ────────────────────────────────────────────────────────────────
function toolsHtml(f) {
  const pool = kindSpec().tool_pool || [];
  const on = new Set(f.value || []);
  const rows = pool.map((t) => {
    const tag = t.phi1 !== undefined ? `φ₁ ${t.phi1}°`
              : t.width !== undefined ? `${t.width} мм` : "";
    return `<label class="tool"><input type="checkbox" data-tool="${t.id}"
              ${on.has(t.id) ? "checked" : ""}>
      <span class="tool-id">${t.id}</span>
      <span class="tool-tag">${tag}</span>
      <span class="tool-desc">${escapeHtml(t.desc)}</span></label>`;
  }).join("");
  return `<div class="field wide" id="p_${f.name}">
      <label>${f.label}
        <span class="tool-all"><a href="#" data-all="1">все</a> ·
          <a href="#" data-all="0">снять</a></span></label>
      <div class="hint">${f.hint || ""}</div>
      <div class="tools">${rows}</div></div>`;
}

function fieldHtml(f) {
  if (f.type === "tools") return toolsHtml(f);
  const id = "p_" + f.name;
  let input;
  if (f.type === "bool") {
    input = `<input type="checkbox" id="${id}" ${f.value ? "checked" : ""}>`;
  } else if (f.type === "choice") {
    input = `<select id="${id}">` + f.choices.map(
      (c) => `<option ${c === f.value ? "selected" : ""}>${c}</option>`).join("")
      + "</select>";
  } else if (f.type === "floats") {
    input = `<input type="text" id="${id}" value="${f.value}">`;
  } else {
    input = `<input type="number" id="${id}" value="${f.value}"` +
      (f.min !== undefined ? ` min="${f.min}"` : "") +
      (f.max !== undefined ? ` max="${f.max}"` : "") +
      (f.step !== undefined ? ` step="${f.step}"` : "") + ">";
  }
  const wide = f.type === "floats" ? " wide" : "";
  return `<div class="field${wide}"><label for="${id}">${f.label}</label>${input}` +
    (f.hint ? `<div class="hint">${f.hint}</div>` : "") + "</div>";
}

function renderParams() {
  const spec = kindSpec();
  const open = new Set(["Инструмент", "Черновая обработка"]);
  $("params").innerHTML = spec.groups.map((g) =>
    `<details class="group" ${open.has(g.group) ? "open" : ""}>` +
    `<summary>${g.group}</summary>` +
    g.fields.map(fieldHtml).join("") + "</details>").join("")
    + `<details class="group" open><summary>Петля</summary>`
    + spec.run.map(fieldHtml).join("") + "</details>";
  // «все / снять» у набора инструмента: набор перебирают целыми пачками, и
  // отщёлкивать десяток галочек мышью — лишняя работа.
  $("params").querySelectorAll(".tool-all a").forEach((a) =>
    a.addEventListener("click", (ev) => {
      ev.preventDefault();
      a.closest(".field").querySelectorAll("input[data-tool]").forEach(
        (el) => { el.checked = a.dataset.all === "1"; });
    }));
  $("agent").textContent = "агент: " + SPEC.agent.llm +
    (SPEC.agent.llm_model ? " / " + SPEC.agent.llm_model : "");
}

function collect() {
  const spec = kindSpec();
  const out = {};
  const all = spec.groups.flatMap((g) => g.fields).concat(spec.run);
  for (const f of all) {
    const el = $("p_" + f.name);
    if (!el) continue;
    if (f.type === "tools") {
      out[f.name] = [...el.querySelectorAll("input[data-tool]:checked")]
        .map((x) => x.dataset.tool);
    } else {
      out[f.name] = f.type === "bool" ? el.checked : el.value;
    }
  }
  return out;
}

// ── выбор файлов ─────────────────────────────────────────────────────────────
function wireFile(inputId, nameId, emptyText) {
  const inp = $(inputId), label = inp.closest(".file");
  const show = () => {
    const f = inp.files[0];
    $(nameId).textContent = f ? `${f.name} · ${(f.size / 1048576).toFixed(1)} МБ`
                              : emptyText;
    $(nameId).classList.toggle("empty", !f);
    $("start").disabled = !$("model").files[0];
  };
  inp.addEventListener("change", show);
  ["dragenter", "dragover"].forEach((e) => label.addEventListener(e, (ev) => {
    ev.preventDefault(); label.classList.add("over");
  }));
  ["dragleave", "drop"].forEach((e) => label.addEventListener(e, (ev) => {
    ev.preventDefault(); label.classList.remove("over");
  }));
  label.addEventListener("drop", (ev) => {
    if (ev.dataTransfer.files[0]) { inp.files = ev.dataTransfer.files; show(); }
  });
  show();
}

// ── схема процесса ───────────────────────────────────────────────────────────
function renderPipeline(kind) {
  const p = phases(kind);
  const chip = (x) => `<span class="chip" data-phase="${x.id}">${x.chip}</span>`;
  const row = (list) => list.map(chip).join('<span class="arr">→</span>');
  const once = p.filter((x) => x.where === "once");
  const cyc = p.filter((x) => x.where === "cycle");
  const back = p.find((x) => x.where === "back");
  const exit = p.filter((x) => x.where === "exit");
  $("pipeline").innerHTML =
    `<div class="once">${row(once)}<span class="arr">→</span></div>
     <div class="cycle" id="cycle">
       <div class="cycle-head">итерация <span id="cyc-iter">—</span></div>
       <div class="cycle-row">${row(cyc)}</div>
       ${back ? `<div class="cycle-back" data-phase="${back.id}">
          <span class="back-label">${back.chip}</span></div>` : ""}
     </div>
     <div class="exit"><span class="exit-why">в допуске<br>или лимит</span>
       <span class="arr">→</span>${row(exit)}</div>`;
}

function renderSteps(state) {
  const kind = state.kind || KIND;
  const order = phaseIds(kind), cyc = cycleIds(kind);
  const phase = state.phase, at = order.indexOf(phase);
  const inCycle = cyc.indexOf(phase);
  document.querySelectorAll(".pipeline [data-phase]").forEach((el) => {
    const p = el.dataset.phase, i = order.indexOf(p), c = cyc.indexOf(p);
    el.classList.toggle("now", p === phase);
    // «Пройдено» внутри цикла считается ОТ НАЧАЛА ТЕКУЩЕЙ ИТЕРАЦИИ: на второй
    // итерации генерация снова впереди, а не позади.
    el.classList.toggle("past", c >= 0 && inCycle >= 0 ? c < inCycle : i < at);
  });
  $("cycle").classList.toggle("active", inCycle >= 0);
  $("cyc-iter").textContent = state.iters
    ? `${state.iter} из ${state.iters}` : "—";
}

function addFeed(ev) {
  const d = document.createElement("div");
  d.className = "k-" + ev.kind;
  d.dataset.kind = ev.kind;
  d.innerHTML = `<span class="t">${String(Math.floor(ev.t / 60)).padStart(2, "0")}:`
    + `${String(Math.floor(ev.t % 60)).padStart(2, "0")}</span>`
    + escapeHtml(ev.text || "");
  const feed = $("feed");
  const stick = feed.scrollTop + feed.clientHeight > feed.scrollHeight - 40;
  feed.appendChild(d);
  if (stick) feed.scrollTop = feed.scrollHeight;
}

// ── расхождения ──────────────────────────────────────────────────────────────
// У фрезеровки приёмка — ОБЪЁМ (зарез обязан быть нулём), у точения — ДОПУСК
// ПО РАДИУСУ: объём там зависит от размера детали и идёт как контекст.
const METRICS = {
  mill: {
    head: ["итер.", "недорез, мм³", "зарез, мм³", "вердикт агента"],
    note: "Недорез — металл остался там, где его быть не должно. " +
          "Зарез — срезано лишнее, это брак: восстановить нельзя.",
    row: (m) => `<td class="${m.undercut > 1 ? "bad" : "ok"}">${m.undercut}</td>` +
                `<td class="${m.overcut > 0 ? "bad" : "ok"}">${m.overcut}</td>`,
  },
  lathe: {
    head: ["итер.", "макс. |Δr|, мм", "недорез, мм³", "зарез, мм³",
           "вердикт агента"],
    note: "Приёмка — худшее отклонение по радиусу: с ним сверяется допуск. " +
          "Объёмы даны по существу, без того, что точением не лечится " +
          "(лыски шестигранника, просадка торцов в симуляторе).",
    row: (m) => `<td>${m.dr > 0 ? "+" : ""}${m.dr}</td>` +
                `<td>${m.undercut}</td><td>${m.overcut}</td>`,
  },
};

function renderMetricsHead(kind) {
  const t = METRICS[kind] || METRICS.mill;
  $("metrics-head").innerHTML = "<tr>" +
    t.head.map((h) => `<th>${h}</th>`).join("") + "</tr>";
  $("metrics-note").textContent = t.note;
}

function renderMetrics(state) {
  if (!state.metrics.length) return;
  const t = METRICS[state.kind] || METRICS.mill;
  $("metrics-box").hidden = false;
  const v = {};
  for (const x of state.verdicts) v[x.iter] = x;
  $("metrics").innerHTML = state.metrics.map((m) => {
    const ver = v[m.iter];
    return `<tr><td>${m.iter}</td>${t.row(m)}` +
      `<td>${ver ? escapeHtml(ver.verdict) : "—"}</td></tr>`;
  }).join("");
}

const RESULT_NOTE = {
  mill: "Скачайте <b>_compare.prt</b> и откройте в NX: слой 1 — модель детали, " +
        "слой 2 — то, что реально вырезалось.",
  lathe: "Смотреть глазами: откройте <b>out_full.step</b> и <b>out_part.step</b> " +
         "в одной сцене — это результат обоих установов и модель в одной " +
         "системе координат. Числа по поясам — в <b>out_nxdiff.md</b>.",
};

function renderResult(state) {
  const done = state.status !== "running";
  $("stop").hidden = done;
  $("again").hidden = !done;
  $("phase").classList.toggle("spin", !done);
  if (!done) return;

  const title = { ok: "готово", failed: "не получилось",
                  stopped: "остановлено" }[state.status] || state.status;
  $("phase").textContent = title;
  // Причину показываем ВНУТРИ карточки обработки: блок формы в этот момент
  // скрыт, и раньше сообщение уходило в никуда — оставались слова
  // «не получилось» и пустая строка.
  if (state.status === "failed") {
    $("fail").hidden = false;
    $("fail-why").textContent = state.error || "обработка прервалась";
    $("fail-tail").textContent = (state.tail || []).join("\n");
  }
  if (!state.outputs.length) return;
  $("result").hidden = false;
  $("result-note").innerHTML = RESULT_NOTE[state.kind] || "";
  $("files").innerHTML = state.outputs.map((f) =>
    `<a class="${f.primary ? "primary-file" : ""}" download
        href="/api/jobs/${state.id}/files/${encodeURIComponent(f.name)}">
       <span class="fname">${f.name}</span>
       <span class="ftitle">${f.title}</span>
       <span class="fsize">${(f.size / 1024).toFixed(0)} КБ</span></a>`).join("");
}

function tick(state) {
  $("phase").textContent = state.phase_title;
  $("phase").classList.toggle("spin", state.status === "running");
  $("iter").textContent = state.iters
    ? `итерация ${state.iter} из ${state.iters}` : "";
  renderSteps(state);
}

function startClock(state) {
  clearInterval(TIMER);
  const t0 = Date.now() - state.elapsed * 1000;
  TIMER = setInterval(() => {
    const s = Math.floor((Date.now() - t0) / 1000);
    $("elapsed").textContent =
      `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  }, 1000);
}

function follow(state) {
  JOB = state.id;
  $("setup").hidden = true;
  $("progress").hidden = false;
  $("feed").innerHTML = "";
  $("fail").hidden = true;
  $("form-error").hidden = true;
  // Схема и таблица — по виду обработки ЗАДАЧИ, а не выбранного в форме: при
  // возврате на страницу к уже идущей задаче они могут не совпадать.
  renderPipeline(state.kind);
  renderMetricsHead(state.kind);
  tick(state);
  startClock(state);

  if (ES) ES.close();
  ES = new EventSource(`/api/jobs/${state.id}/events?start=0`);
  ES.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    tick({ ...ev, kind: state.kind });
    addFeed(ev);
    if (ev.kind === "metric" || ev.kind === "verdict")
      fetch(`/api/jobs/${JOB}`).then((r) => r.json()).then(renderMetrics);
  };
  ES.addEventListener("state", (e) => {
    const st = JSON.parse(e.data);
    ES.close(); clearInterval(TIMER);
    renderMetrics(st); renderResult(st);
  });
  ES.onerror = () => {
    // соединение рвётся и когда задача кончилась — добираем состояние опросом
    fetch(`/api/jobs/${JOB}`).then((r) => r.json()).then((st) => {
      if (st.status !== "running") {
        ES.close(); clearInterval(TIMER);
        renderMetrics(st); renderResult(st);
      }
    });
  };
}

// ── запуск ───────────────────────────────────────────────────────────────────
async function start() {
  $("form-error").hidden = true;
  const fd = new FormData();
  fd.append("model", $("model").files[0]);
  fd.append("kind", KIND);
  if (kindSpec().stock && $("stock").files[0])
    fd.append("stock", $("stock").files[0]);
  fd.append("form", JSON.stringify(collect()));
  $("start").disabled = true;
  try {
    const r = await fetch("/api/jobs", { method: "POST", body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "не удалось запустить");
    follow(data);
  } catch (e) {
    $("form-error").hidden = false;
    $("form-error").textContent = e.message;
    $("start").disabled = false;
  }
}

// ── старт страницы ───────────────────────────────────────────────────────────
async function load(keepKind) {
  SPEC = await (await fetch("/api/params")).json();
  if (!keepKind || !SPEC.kinds[KIND]) KIND = SPEC.default;
  renderKinds();
  selectKind(KIND);
  return SPEC;
}

(async function init() {
  await load(false);
  wireFile("model", "model-name", "файл не выбран");
  wireFile("stock", "stock-name", "не выбрана: бокс от габарита детали");

  $("start").addEventListener("click", start);
  $("reset").addEventListener("click", () => load(true));
  $("stop").addEventListener("click", async () => {
    if (JOB) await fetch(`/api/jobs/${JOB}/stop`, { method: "POST" });
  });
  $("again").addEventListener("click", () => {
    $("progress").hidden = true; $("setup").hidden = false;
    $("start").disabled = !$("model").files[0];
    $("result").hidden = true; $("metrics-box").hidden = true;
  });
  $("raw").addEventListener("change", () => {
    $("feed").classList.toggle("raw", $("raw").checked);
  });

  if (SPEC.active && SPEC.active.status === "running") follow(SPEC.active);
})();
