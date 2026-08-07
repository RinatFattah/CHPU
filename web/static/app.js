"use strict";
const $ = (id) => document.getElementById(id);
// Порядок этапов. Внимание: это НЕ прямая — «apply» (правки агента) возвращает
// в «generate», и цикл повторяется до «в допуске» либо до лимита итераций.
const PHASES = ["prepare", "convert", "generate", "simulate", "diff", "llm",
                "apply", "compare", "done"];
const CYCLE = ["generate", "simulate", "diff", "llm", "apply"];
let SPEC = null, JOB = null, ES = null, TIMER = null;

// ── параметры ────────────────────────────────────────────────────────────────
function fieldHtml(f) {
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

function renderParams(spec) {
  SPEC = spec;
  const open = new Set(["Инструмент", "Черновая обработка"]);
  $("params").innerHTML = spec.params.groups.map((g, i) =>
    `<details class="group" ${open.has(g.group) ? "open" : ""}>` +
    `<summary>${g.group}</summary>` +
    g.fields.map(fieldHtml).join("") + "</details>").join("")
    + `<details class="group" open><summary>Петля</summary>`
    + spec.params.run.map(fieldHtml).join("") + "</details>";
  $("agent").textContent = "агент: " + spec.agent.llm +
    (spec.agent.llm_model ? " / " + spec.agent.llm_model : "");
}

function collect() {
  const out = {};
  const all = SPEC.params.groups.flatMap((g) => g.fields).concat(SPEC.params.run);
  for (const f of all) {
    const el = $("p_" + f.name);
    if (!el) continue;
    out[f.name] = f.type === "bool" ? el.checked : el.value;
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

// ── прогресс ─────────────────────────────────────────────────────────────────
function renderSteps(state) {
  const phase = state.phase, at = PHASES.indexOf(phase);
  const inCycle = CYCLE.indexOf(phase);
  document.querySelectorAll(".pipeline [data-phase]").forEach((el) => {
    const p = el.dataset.phase, i = PHASES.indexOf(p), c = CYCLE.indexOf(p);
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
  const showRaw = $("raw").checked;
  if (ev.kind === "log" && !showRaw) return;
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

const escapeHtml = (s) => s.replace(/[&<>]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

function renderMetrics(state) {
  if (!state.metrics.length) return;
  $("metrics-box").hidden = false;
  const v = {};
  for (const x of state.verdicts) v[x.iter] = x;
  $("metrics").innerHTML = state.metrics.map((m) => {
    const ver = v[m.iter];
    return `<tr><td>${m.iter}</td>` +
      `<td class="${m.undercut > 1 ? "bad" : "ok"}">${m.undercut}</td>` +
      `<td class="${m.overcut > 0 ? "bad" : "ok"}">${m.overcut}</td>` +
      `<td>${ver ? escapeHtml(ver.verdict) : "—"}</td></tr>`;
  }).join("");
}

function renderResult(state) {
  const done = state.status !== "running";
  $("stop").hidden = done;
  $("again").hidden = !done;
  $("phase").classList.toggle("spin", !done);
  if (!done) return;

  const title = { ok: "готово", failed: "не получилось",
                  stopped: "остановлено" }[state.status] || state.status;
  $("phase").textContent = title;
  if (state.error) {
    $("form-error").hidden = false;
    $("form-error").textContent = state.error;
  }
  if (!state.outputs.length) return;
  $("result").hidden = false;
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
  document.querySelector("main").classList.add("solo");
  $("feed").innerHTML = "";
  $("form-error").hidden = true;
  tick(state);
  startClock(state);

  if (ES) ES.close();
  ES = new EventSource(`/api/jobs/${state.id}/events?start=0`);
  ES.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    tick(ev);
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
  if ($("stock").files[0]) fd.append("stock", $("stock").files[0]);
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
(async function init() {
  const spec = await (await fetch("/api/params")).json();
  renderParams(spec);
  wireFile("model", "model-name", "файл не выбран");
  wireFile("stock", "stock-name", "не выбрана: бокс от габарита детали");

  $("start").addEventListener("click", start);
  $("reset").addEventListener("click", async () => {
    renderParams(await (await fetch("/api/params")).json());
  });
  $("stop").addEventListener("click", async () => {
    if (JOB) await fetch(`/api/jobs/${JOB}/stop`, { method: "POST" });
  });
  $("again").addEventListener("click", () => {
    $("progress").hidden = true; $("setup").hidden = false;
    document.querySelector("main").classList.remove("solo");
    $("start").disabled = !$("model").files[0];
    $("result").hidden = true; $("metrics-box").hidden = true;
  });
  $("raw").addEventListener("change", () => {
    document.querySelectorAll("#feed div[data-kind=log]").forEach(
      (d) => { d.hidden = !$("raw").checked; });
  });

  if (spec.active && spec.active.status === "running") follow(spec.active);
})();
