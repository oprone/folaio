const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = async (path, opts = {}) => {
  const r = await fetch(path, { ...opts, headers: { "X-Folaio": "1", ...(opts.headers || {}) } });
  if (!r.ok) {
    let msg = await r.text();
    try { msg = JSON.parse(msg).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
};
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
// Replace an element's HTML only when it changed, so buttons stay clickable during refreshes.
const setHTML = (el, html) => { if (el.dataset.html !== html) { el.dataset.html = html; el.innerHTML = html; } };
const pdfLink = (docId, page) => `/api/documents/${docId}/file#page=${page}`;
const gb = (b) => (b / 1e9).toFixed(1) + " GB";
const icon = (name, cls = "sm") => `<svg class="i ${cls}"><use href="#i-${name}"/></svg>`;
const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};

let docs = [];
let status = null;

/* ---------- toasts (instead of pop-up alerts) ---------- */
function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "bad" ? 6000 : 3500);
}

/* ---------- theme ---------- */
function setTheme(choice) {
  document.documentElement.dataset.theme = choice;
  store.set("folaio-theme", choice);
  $$("[data-theme-choice]").forEach((b) => {
    b.classList.toggle("on", b.dataset.themeChoice === choice);
    b.setAttribute("aria-checked", b.dataset.themeChoice === choice);
  });
  $("#theme-select").value = choice;
}
$$("[data-theme-choice]").forEach((b) => (b.onclick = () => setTheme(b.dataset.themeChoice)));
$("#theme-select").onchange = (e) => setTheme(e.target.value);
setTheme(store.get("folaio-theme", "auto"));

/* ---------- navigation ---------- */
function show(view) {
  $$("nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + view));
  if (view === "study") loadStudy();
  if (view === "brains" && status) openBrains();
  if (view === "settings" && status) renderSettings();
  if (view === "ask") (inChat() ? $("#dock-input") : $("#ask-input")).focus();
  if (view === "search") $("#search-input").focus();
}
$$("nav button").forEach((b) => (b.onclick = () => show(b.dataset.view)));
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-goto]");
  if (t) show(t.dataset.goto);
});
document.addEventListener("keydown", (e) => {
  // "/" jumps to the question box (unless you're already typing somewhere)
  if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
    e.preventDefault();
    show("ask");
  }
  if (e.key === "Escape") $$(".overlay:not(.hidden)").forEach((o) => o.id === "pack-dialog" && o.classList.add("hidden"));
});

/* ---------- answers ---------- */
function cites(html, sources) {
  return html.replace(/\[(\d+)\]/g, (m, n) => {
    const s = sources[n - 1];
    return s ? `<a class="cite" href="${pdfLink(s.doc_id, s.page)}" target="_blank" title="Open ${esc(s.doc_name)} at page ${s.page}">p. ${s.page}</a>` : "";
  });
}

function formatText(text, sources) {
  const lines = esc(text).split("\n");
  let html = "", inList = false;
  for (let line of lines) {
    line = cites(line.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>"), sources);
    const item = line.match(/^\s*(?:[-*•]|\d+\.)\s+(.*)/);
    if (item) { if (!inList) { html += `<ul class="points">`; inList = true; } html += `<li><span>${item[1]}</span></li>`; continue; }
    if (inList) { html += "</ul>"; inList = false; }
    if (line.trim()) html += `<p>${line}</p>`;
  }
  return html + (inList ? "</ul>" : "");
}

// An answer card: the book's own sentences (with page chips), then "In simple words" from a Plus brain.
function answerCard(text, sources, done) {
  const [book, simple] = text.split("**In simple words:**");
  const names = [...new Set(sources.map((s) => s.doc_name.replace(/\.pdf$/i, "")))];
  let html = sources.length ? `<div class="label">${icon("library")}From your book · ${esc(names.join(", "))}</div>` : "";
  html += book.trim() ? formatText(book.trim(), sources) : `<div class="thinking"><span></span><span></span><span></span></div>`;
  if (simple !== undefined) {
    html += `<div class="simple"><div class="label">${icon("brains")}In simple words</div>${
      simple.trim() ? esc(simple.trim()) : `<div class="thinking"><span></span><span></span><span></span></div>`}</div>`;
  }
  if (done && sources.length) {
    html += `<div class="answer-actions">
      <button class="ghost quiz-this">${icon("quiz")}Quiz me on this</button>
      <button class="ghost copy-this">${icon("copy")}Copy</button>
      <button class="sources-toggle">${icon("down")}${sources.length} source${sources.length > 1 ? "s" : ""}</button>
    </div>
    <div class="source-list hidden">${sources.map((s) => `<div class="source">
      <a href="${pdfLink(s.doc_id, s.page)}" target="_blank">${esc(s.doc_name)} · page ${s.page}</a><br>${esc(s.text.slice(0, 280))}…</div>`).join("")}</div>`;
  }
  return html;
}

/* ---------- ask ---------- */
const inChat = () => !$("#ask-chat").classList.contains("hidden");

function autoGrow(t) {
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 240) + "px";
}
for (const [form, input] of [["#ask-form", "#ask-input"], ["#dock-form", "#dock-input"]]) {
  const t = $(input);
  t.addEventListener("input", () => autoGrow(t));
  t.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $(form).requestSubmit(); }
  });
  $(form).onsubmit = (e) => {
    e.preventDefault();
    const q = t.value.trim();
    if (!q) return;
    t.value = "";
    autoGrow(t);
    const scope = (form === "#ask-form" ? $("#ask-scope") : $("#dock-scope")).value;
    $("#dock-scope").value = scope;
    ask(q, scope);
  };
}

function newConversation() {
  $("#chat").innerHTML = "";
  $("#ask-chat").classList.add("hidden");
  $("#ask-home").classList.remove("hidden");
  $("#ask-scope").value = $("#dock-scope").value;
  loadAskHome();
  $("#ask-input").focus();
}

async function ask(q, scope) {
  $("#ask-home").classList.add("hidden");
  $("#ask-chat").classList.remove("hidden");
  const chat = $("#chat");
  if (!chat.children.length) {
    chat.insertAdjacentHTML("beforeend",
      `<button class="ghost new-chat" onclick="newConversation()">${icon("plus")}New question</button>`);
  }
  chat.insertAdjacentHTML("beforeend", `<div class="msg user">${esc(q)}</div>`);
  const bot = document.createElement("div");
  bot.className = "msg bot";
  bot.innerHTML = `<img src="brand/logo.svg" alt=""><div class="answer"><div class="thinking"><span></span><span></span><span></span></div></div>`;
  chat.appendChild(bot);
  const card = bot.querySelector(".answer");
  const scroll = () => (chat.scrollTop = chat.scrollHeight);
  scroll();
  $$(".send").forEach((b) => (b.disabled = true));
  $("#dock-input").focus();

  let sources = [], text = "";
  try {
    const r = await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json", "X-Folaio": "1" },
      body: JSON.stringify({ question: q, doc_id: scope ? Number(scope) : null }) });
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const ev = JSON.parse(buf.slice(0, nl));
        buf = buf.slice(nl + 1);
        if (ev.type === "sources") sources = ev.sources;
        if (ev.type === "token") { text += ev.text; card.innerHTML = answerCard(text, sources, false); }
        if (ev.type === "error") text += `\n\n${ev.text}`;
        scroll();
      }
    }
  } catch (err) {
    text += `\n\nSomething went wrong: ${err.message}`;
  }
  card.innerHTML = answerCard(text, sources, true);
  if (!sources.length) card.classList.add("not-found");
  card.querySelector(".copy-this")?.addEventListener("click", () => {
    const plain = text.replace(/\*\*/g, "").replace(/\[(\d+)\]/g, (m, n) => sources[n - 1] ? `(p. ${sources[n - 1].page})` : "");
    navigator.clipboard.writeText(plain).then(() => toast("Copied to clipboard"), () => toast("Couldn't copy", "bad"));
  });
  card.querySelector(".sources-toggle")?.addEventListener("click", () => card.querySelector(".source-list").classList.toggle("hidden"));
  card.querySelector(".quiz-this")?.addEventListener("click", () => quizOnPage(sources[0]));
  scroll();
  $$(".send").forEach((b) => (b.disabled = false));
}

// "Quiz me on this": practise the chapter the answer came from.
async function quizOnPage(src) {
  const [map, where] = await Promise.all([api("/api/map"), api(`/api/section-of/${src.chunk_id}`)]);
  const book = map.find((b) => b.doc_id === (where.doc_id ?? src.doc_id));
  const sec = book?.sections.find((s) => s.index === where.index && s.questions)
    || book?.sections.find((s) => s.page_from <= src.page && src.page <= s.page_to && s.questions);
  if (!sec) { toast("There are no quiz questions for this part of the book yet."); return; }
  quizRange = { doc_id: book.doc_id, section: sec.index, page_from: sec.page_from, page_to: sec.page_to, title: sec.title };
  show("study");
  openTab("quiz");
}

// The start screen: greeting, suggestions from your own books, and today at a glance.
async function loadAskHome() {
  const hour = new Date().getHours();
  const part = hour < 5 ? "Good evening" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  const name = status?.settings?.name;
  $("#greeting").textContent = name ? `${part}, ${name}` : part;

  const books = docs.filter((d) => d.status === "ready" && d.kind !== "paper");
  if (!books.length) {
    setHTML($("#suggest"), "");
    setHTML($("#glance"), `<button class="pill" data-goto="library">${icon("upload")}<b>Add your first book</b> to get started<span class="go">Open Library →</span></button>`);
    return;
  }
  let map = [], plan = null;
  try { [map, plan] = await Promise.all([api("/api/map"), api("/api/plan")]); } catch {}
  const sections = map.flatMap((b) => b.sections.map((s) => ({ ...s, doc_id: b.doc_id })))
    .filter((s) => !/^pages\s/i.test(s.title));
  const topic = (t) => t.replace(/^(chapter|unit|section|lesson)\s*[\dIVX]+\s*[:.\-–]?\s*/i, "").trim();
  const pick = sections.filter((s) => topic(s.title).split(" ").length <= 6);
  const day = new Date().getDate();
  const chips = [];
  if (pick.length) chips.push({ icon: "ask", text: `What is ${topic(pick[day % pick.length].title).toLowerCase()}?`, ask: true });
  if (pick.length > 1) chips.push({ icon: "ask", text: `Explain ${topic(pick[(day + 1) % pick.length].title).toLowerCase()}`, ask: true });
  const weak = sections.filter((s) => s.questions && ["weak", "shaky"].includes(s.status)).sort((a, b) => a.strength - b.strength)[0]
    || sections.find((s) => s.questions && s.status === "new");
  if (weak) chips.push({ icon: "quiz", text: weak.status === "new" ? `Start practising: ${weak.title}` : "Quiz me on my weakest chapter", practise: weak });
  setHTML($("#suggest"), chips.map((c, i) => `<button type="button" data-chip="${i}">${icon(c.icon)}${esc(c.text)}</button>`).join(""));
  $$("#suggest [data-chip]").forEach((b) => (b.onclick = () => {
    const c = chips[Number(b.dataset.chip)];
    if (c.ask) ask(c.text, $("#ask-scope").value);
    else {
      quizRange = { doc_id: c.practise.doc_id, section: c.practise.index, page_from: c.practise.page_from, page_to: c.practise.page_to, title: c.practise.title };
      show("study");
      openTab("quiz");
    }
  }));

  const due = status?.study?.due || 0, streak = plan?.streak || 0;
  setHTML($("#glance"),
    (streak ? `<span class="pill">${icon("flame")}<b>${streak}-day</b> streak</span>` : "") +
    (due ? `<button class="pill" id="glance-due">${icon("calendar")}<b>${due}</b> question${due > 1 ? "s" : ""} due today<span class="go">Start →</span></button>`
         : `<button class="pill" data-goto="study">${icon("study")}Open your study plan<span class="go">→</span></button>`));
  $("#glance-due")?.addEventListener("click", () => { quizRange = null; show("study"); openTab("quiz"); });
}

/* ---------- search ---------- */
$("#search-form").onsubmit = async (e) => {
  e.preventDefault();
  const q = $("#search-input").value.trim();
  const box = $("#search-results");
  if (!q) return;
  box.innerHTML = `<p class="muted">Searching…</p>`;
  const hits = await api("/api/search?q=" + encodeURIComponent(q));
  const words = q.toLowerCase().match(/\w{3,}/g) || [];
  const re = words.length ? new RegExp(`\\b(${words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi") : null;
  box.innerHTML = hits.length ? hits.map((h) => `
    <div class="result">
      <div class="where"><a href="${pdfLink(h.doc_id, h.page)}" target="_blank">${esc(h.doc_name)} · page ${h.page}</a></div>
      ${re ? esc(h.text).replace(re, "<mark>$1</mark>") : esc(h.text)}
    </div>`).join("") : `<p class="muted">No matches. Try different words.</p>`;
};

/* ---------- library ---------- */
const drop = $("#drop");
$("#file-input").onchange = (e) => upload(e.target.files);
drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); upload(e.dataTransfer.files); };

async function upload(files) {
  const all = [...files];
  const isPack = (f) => /\.(folaio|herapack)$/i.test(f.name);
  for (const pack of all.filter(isPack)) await openPack(pack);
  const form = new FormData();
  all.filter((f) => !isPack(f)).forEach((f) => form.append("files", f));
  if (![...form.keys()].length) return;
  drop.querySelector("strong").textContent = "Adding…";
  const res = await api("/api/documents", { method: "POST", body: form });
  const skipped = res.filter((r) => r.duplicate || r.error);
  skipped.forEach((r) => toast(`${r.name}: ${r.error || "already in your library"}`, r.error ? "bad" : ""));
  const added = res.length - skipped.length;
  if (added) toast(`Added ${added} file${added > 1 ? "s" : ""}. Folaio is reading ${added > 1 ? "them" : "it"} now.`);
  drop.querySelector("strong").textContent = "Drop PDFs or Subject Packs here";
  $("#file-input").value = "";
  refresh();
}

function docState(d) {
  if (d.status === "error") return `<span class="badge error">Error</span>`;
  if (d.status !== "ready") return `<span class="badge">${{ reading: "Reading…", learning: "Learning…" }[d.status] || "Waiting"}</span>`;
  return `<span class="badge ready">Ready</span>`;
}

function renderDocs() {
  setHTML($("#docs"), docs.length ? docs.map((d) => {
    const pct = d.study_total ? Math.round((100 * d.study_done) / d.study_total) : 0;
    const studying = d.status === "ready" && d.study_status !== "done" && status?.brain.installed;
    return `<div class="doc">
      <span class="file-icon">${icon(d.kind === "paper" ? "paper" : "file", "")}</span>
      <div class="grow">
        <div class="name">${esc(d.name)} ${d.kind === "paper" ? `<span class="badge">${icon("paper")}Past paper</span>` : ""}</div>
        <div class="meta">${d.pages || "?"} page${d.pages === 1 ? "" : "s"} · ${d.kind === "paper" ? "exam paper" : `${d.cards} quiz questions`}
          ${d.error ? ` · <span style="color:var(--bad)">${esc(d.error)}</span>` : ""}
          ${studying ? ` · Folaio Plus is writing notes ${pct}%` : ""}</div>
        ${studying ? `<div class="bar"><div style="width:${pct}%"></div></div>` : ""}
      </div>
      ${docState(d)}
      <a class="ghost" href="/api/documents/${d.id}/file" target="_blank" style="text-decoration:none">Open</a>
      <button class="ghost" onclick="removeDoc(${d.id}, '${esc(d.name).replace(/'/g, "\\'")}')">Remove</button>
    </div>`;
  }).join("") : `<p class="muted">Your library is empty. Add your first PDF above.</p>`);
}

/* ---------- subject packs ---------- */
async function openPack(file) {
  const msg = $("#pack-msg");
  msg.className = "small muted";
  msg.textContent = `Opening ${file.name}…`;
  const form = new FormData();
  form.append("file", file);
  try {
    const r = await api("/api/packs", { method: "POST", body: form });
    msg.className = "small";
    msg.innerHTML = `Opened <b>${esc(r.name)}</b>${r.author ? ` by ${esc(r.author)}` : ""}: ` +
      (r.added.length ? `added ${r.added.length} document${r.added.length > 1 ? "s" : ""}` : "nothing new") +
      (r.skipped.length ? ` (${r.skipped.length} already in your library)` : "") +
      (r.added.length ? ". Folaio is learning it now; quizzes are ready in the Study page." : ".");
  } catch (e) {
    msg.className = "small warn";
    msg.textContent = e.message;
  }
  refresh();
}
$("#pack-input").onchange = (e) => { if (e.target.files[0]) openPack(e.target.files[0]); e.target.value = ""; };

$("#share-pack").onclick = () => {
  const ready = docs.filter((d) => d.status === "ready");
  if (!ready.length) { toast("Add some PDFs first; then you can share them as a Subject Pack."); return; }
  $("#pack-docs").innerHTML = ready.map((d) => `<label><input type="checkbox" value="${d.id}" checked>
    ${esc(d.name)} ${d.kind === "paper" ? `<span class="badge">${icon("paper")}Past paper</span>` : ""}</label>`).join("");
  $("#pack-err").textContent = "";
  $("#pack-dialog").classList.remove("hidden");
  $("#pack-name").focus();
};
$("#pack-cancel").onclick = () => $("#pack-dialog").classList.add("hidden");

$("#pack-form").onsubmit = async (e) => {
  e.preventDefault();
  const ids = [...$$("#pack-docs input:checked")].map((c) => Number(c.value));
  if (!ids.length) { $("#pack-err").textContent = "Choose at least one document."; return; }
  const btn = $("#pack-go");
  btn.disabled = true;
  btn.textContent = "Creating…";
  try {
    const r = await fetch("/api/packs/export", {
      method: "POST", headers: { "Content-Type": "application/json", "X-Folaio": "1" },
      body: JSON.stringify({ doc_ids: ids, name: $("#pack-name").value, author: $("#pack-author").value,
        description: $("#pack-desc").value }),
    });
    if (!r.ok) throw new Error((await r.json()).detail);
    const blob = await r.blob();
    const name = (r.headers.get("content-disposition") || "").match(/filename="?([^"]+)"?/)?.[1] || "Subject Pack.folaio";
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: decodeURIComponent(name) });
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    $("#pack-dialog").classList.add("hidden");
    $("#pack-msg").className = "small";
    $("#pack-msg").textContent = `Saved ${decodeURIComponent(name)} to your Downloads. Share it by USB, email or WhatsApp.`;
  } catch (err) {
    $("#pack-err").textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Create pack";
  }
};

async function removeDoc(id, name) {
  if (!confirm(`Remove "${name}" from Folaio? Its notes and quiz questions will be removed too.`)) return;
  await api(`/api/documents/${id}`, { method: "DELETE" });
  refresh();
}

function fillScopes() {
  const ready = docs.filter((d) => d.status === "ready" && d.kind !== "paper");
  const opts = (all) => (all ? `<option value="">${all}</option>` : "") + ready.map((d) => `<option value="${d.id}">${esc(d.name)}</option>`).join("");
  for (const [sel, all] of [["#ask-scope", "All books"], ["#dock-scope", "All books"], ["#quiz-scope", "All books"], ["#notes-doc", null], ["#chk-scope", "All books"]]) {
    const el = $(sel), cur = el.value;
    el.innerHTML = opts(all);
    if ([...el.options].some((o) => o.value === cur)) el.value = cur;
  }
}

/* ---------- study ---------- */
function openTab(name) {
  $$(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === name));
  $$(".tabpane").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "notes") loadNotes();
  if (name === "map") loadMap();
  if (name === "today") loadToday();
  if (name === "papers") loadPapers();
  if (name === "quiz") nextCard();
}
$$(".tab").forEach((t) => (t.onclick = () => openTab(t.dataset.tab)));
$("#quiz-scope").onchange = () => { quizRange = null; nextCard(); };
$("#notes-doc").onchange = () => loadNotes();

function loadStudy() {
  const s = status?.study || {};
  const acc = s.seen ? Math.round((100 * s.correct) / s.seen) + "%" : "–";
  $("#stats").innerHTML = [["Due now", s.due], ["Mastered", s.mastered], ["Questions", s.total], ["Accuracy", acc]]
    .map(([k, v]) => `<div class="stat"><b>${v ?? 0}</b><span>${k}</span></div>`).join("");
  openTab($(".tab.active").dataset.tab);
}

/* ---------- past papers ---------- */
const paperDrop = $("#paper-drop");
$("#paper-input").onchange = (e) => uploadPapers(e.target.files);
paperDrop.ondragover = (e) => { e.preventDefault(); paperDrop.classList.add("over"); };
paperDrop.ondragleave = () => paperDrop.classList.remove("over");
paperDrop.ondrop = (e) => { e.preventDefault(); paperDrop.classList.remove("over"); uploadPapers(e.dataTransfer.files); };

async function uploadPapers(files) {
  const form = new FormData();
  [...files].forEach((f) => form.append("files", f));
  if (![...form.keys()].length) return;
  paperDrop.querySelector("strong").textContent = "Adding…";
  const res = await api("/api/documents?kind=paper", { method: "POST", body: form });
  const skipped = res.filter((r) => r.duplicate || r.error);
  skipped.forEach((r) => toast(`${r.name}: ${r.error || "already added"}`, r.error ? "bad" : ""));
  paperDrop.querySelector("strong").textContent = "Add past exam papers (PDF)";
  $("#paper-input").value = "";
  setTimeout(loadPapers, 1500);
}

async function loadPapers() {
  const r = await api("/api/papers");
  const box = $("#papers");
  if (!r.papers.length) {
    box.innerHTML = `<p class="muted">No past papers yet. Add a few years of papers to see which topics are examined most.</p>`;
    return;
  }
  const secAttrs = (c) => `data-doc="${c.doc_id}" data-section="${c.index}" data-from="${c.page_from}" data-to="${c.page_to}" data-title="${esc(c.title)}"`;
  const nPapers = r.papers.filter((p) => p.status === "ready").length;
  const maxCount = Math.max(1, ...r.topics.map((t) => t.count));
  let html = "";
  if (!r.books) html += `<div class="banner">Add your textbook in the Library too, so Folaio can link each question to the chapter that answers it.</div>`;
  if (r.topics.length) {
    html += `<div class="plan"><div class="plan-head"><h4>${icon("chart")} Most examined topics</h4>
      <span class="muted small">from ${nPapers} paper${nPapers > 1 ? "s" : ""}</span></div>` +
      r.topics.slice(0, 12).map((t) => `<div class="topic"><span class="dot bg-${t.status}" title="${STATES[t.status]}"></span>
        <div class="grow"><b>${esc(t.title)}</b>
          <div class="muted small">${t.count} question${t.count > 1 ? "s" : ""} · in ${t.papers} of ${nPapers} papers${t.marks ? ` · ${t.marks} marks` : ""}${t.years.length ? ` · ${t.years.join(", ")}` : ""} · you: ${STATES[t.status].toLowerCase()}</div>
          <div class="freq" style="width:${(100 * t.count) / maxCount}%"></div></div>
        ${t.questions ? `<button class="ghost practise" ${secAttrs(t)}>Practise</button>` : ""}</div>`).join("") + `</div>`;
  }
  html += r.papers.map((p) => {
    if (p.status === "error") return `<div class="paper"><p><b>${esc(p.name)}</b> <span class="badge error">Error</span> <span class="small warn">${esc(p.error)}</span></p></div>`;
    if (p.status !== "ready") return `<div class="paper"><p><b>${esc(p.name)}</b> <span class="badge">Reading…</span></p></div>`;
    const linked = p.questions.filter((q) => q.chapter).length;
    return `<details class="paper"><summary>${p.year ? `${p.year} · ` : ""}${esc(p.name)}
        <span class="muted small">· ${p.questions.length} questions · ${linked} linked to your books</span></summary>
      ${p.questions.map((q) => `<div class="pq"><span class="num">${esc(q.number)}</span>
        <div class="grow">${q.context ? `<span class="muted">${esc(q.context)} </span>` : ""}${esc(q.text)}${q.marks ? ` <span class="muted small">[${q.marks}]</span>` : ""}
          <div class="small" style="margin-top:4px">${q.chapter
            ? `<a class="cite" href="${pdfLink(q.chapter.doc_id, q.chapter.page_from)}" target="_blank">${esc(q.chapter.title)} · p.${q.chapter.page_from}</a>`
            : `<span class="muted">Not covered by your books</span>`}</div></div>
        <div class="btns">${q.chapter ? `<button class="ghost answer-this" data-q="${esc(q.text)}" data-doc="${q.chapter.doc_id}">${icon("pen")}Answer this</button>` : ""}</div>
      </div>`).join("")}
    </details>`;
  }).join("");
  setHTML(box, html);
  box.querySelectorAll(".answer-this").forEach((b) => (b.onclick = () => {
    $("#chk-q").value = b.dataset.q;
    $("#chk-a").value = "";
    $("#chk-scope").value = b.dataset.doc;
    $("#chk-result").innerHTML = "";
    openTab("check");
    $("#chk-a").focus();
  }));
}

/* ---------- answer checker ---------- */
$("#check-form").onsubmit = async (e) => {
  e.preventDefault();
  const question = $("#chk-q").value.trim(), answer = $("#chk-a").value.trim(), box = $("#chk-result");
  if (!question || !answer) { box.innerHTML = `<p class="muted">Please write both the question and your answer.</p>`; return; }
  box.innerHTML = `<p class="muted">Checking against your book…</p>`;
  let r;
  try {
    const scope = $("#chk-scope").value;
    r = await post("/api/check", { question, answer, doc_id: scope ? Number(scope) : null });
  } catch (err) { box.innerHTML = `<p class="warn">${esc(err.message)}</p>`; return; }
  if (r.error) { box.innerHTML = `<div class="card"><p>${esc(r.error)}</p></div>`; return; }
  const n = r.points.length, covered = r.points.filter((p) => p.status === "covered").length;
  const partly = r.points.filter((p) => p.status === "partly").length;
  const icon = { covered: "✅", partly: "🟡", missed: "❌" };
  const words = (ws) => ws.map((w) => `<b>${esc(w)}</b>`).join(", ");
  const pct = Math.round(r.score * 100);
  const mood = pct >= 80 ? "Excellent! 🎉" : pct >= 50 ? "Good, but some points are missing." : "Keep going: several key points are missing.";
  box.innerHTML = `<div class="card">
    <div class="verdict"><span class="big">${pct}%</span>
      <div><b>${mood}</b><div class="muted small">Of ${n} key point${n > 1 ? "s" : ""} in your book, you covered ${covered}${partly ? ` fully and ${partly} partly` : ""}.</div></div></div>
    ${r.points.map((p) => {
      const src = r.sources[p.passage - 1];
      const where = src ? ` <a class="cite" href="${pdfLink(src.doc_id, src.page)}" target="_blank">p.${src.page}</a>` : "";
      const hint = p.status === "covered"
        ? (p.missing.length ? `<div class="hint muted">Could also mention: ${words(p.missing)}</div>` : "")
        : `<div class="hint">${p.matched.length ? `You mentioned ${words(p.matched)}. ` : ""}Missing: ${words(p.missing)}</div>`;
      return `<div class="kp"><span class="icon">${icon[p.status]}</span><div>${esc(p.text)}${where}${hint}</div></div>`;
    }).join("")}
    ${r.unsupported.length ? `<div class="unsupported"><b>Double-check:</b> these parts of your answer aren't in your book:
      <ul>${r.unsupported.map((u) => `<li>${esc(u)}</li>`).join("")}</ul></div>` : ""}
  </div>`;
};

/* ---------- today: daily plan ---------- */

async function loadToday() {
  const minutes = Number(store.get("folaio-minutes", 20));
  const plan = await api(`/api/plan?minutes=${minutes}`);
  const box = $("#today");
  if (!plan.exams.length) {
    box.innerHTML = `<p class="muted">Add a PDF in the Library and Folaio will plan your study for each day.</p>`;
    return;
  }
  const secAttrs = (s) => `data-doc="${s.doc_id}" data-section="${s.index}" data-from="${s.page_from}" data-to="${s.page_to}" data-title="${esc(s.title)}"`;
  const task = (t) => {
    if (t.kind === "review") return `<div class="task"><span class="icon">${icon("calendar", "")}</span>
      <div class="grow"><b>Review ${t.count} question${t.count > 1 ? "s" : ""}</b>
        <div class="muted small">Due today so you don't forget them${t.total > t.count ? ` (${t.total - t.count} more can wait)` : ""} · ~${t.minutes} min</div></div>
      <div class="btns"><button class="primary" id="start-review">Start</button></div></div>`;
    const s = t.section;
    if (t.kind === "practise") return `<div class="task"><span class="icon">${icon("target", "")}</span>
      <div class="grow"><b>Strengthen: ${esc(s.title)}</b>
        <div class="muted small">${esc(s.doc)} · ${STATES[s.status]} · ${tileMeta(s)} · ~${t.minutes} min</div></div>
      <div class="btns"><button class="primary practise" ${secAttrs(s)}>Practise</button></div></div>`;
    return `<div class="task"><span class="icon">${icon("library", "")}</span>
      <div class="grow"><b>Learn: ${esc(s.title)}</b>
        <div class="muted small">${esc(s.doc)} · p.${s.page_from}–${s.page_to} · read the summary, then try its ${s.questions} questions · ~${t.minutes} min</div></div>
      <div class="btns"><button class="ghost read-summary" data-doc="${s.doc_id}">Read</button>
        <button class="primary practise" ${secAttrs(s)}>Practise</button></div></div>`;
  };
  const examLine = (e) => {
    if (!e.exam_date) return `No exam date set · ${e.mastered}/${e.chapters} chapters mastered`;
    if (e.days_left < 0) return `Exam was on ${e.exam_date}`;
    const when = e.days_left === 0 ? "Exam today! Good luck 🍀" : `${e.days_left} day${e.days_left > 1 ? "s" : ""} left`;
    const pace = e.new_per_day ? ` · learn about ${e.new_per_day} new chapter${e.new_per_day > 1 ? "s" : ""} a day to cover everything`
      : " · every chapter started: keep reviewing";
    return `${when} · ${e.mastered}/${e.chapters} chapters mastered${e.days_left > 0 ? pace : ""}`;
  };

  box.innerHTML = `
    <div class="today-top">
      <div class="stat"><b>${plan.streak}</b><span>${icon("flame")} day streak</span></div>
      <div class="stat"><b>${plan.today.answered}</b><span>answered today${plan.today.answered ? ` · ${Math.round(100 * plan.today.right / plan.today.answered)}% right` : ""}</span></div>
    </div>
    <div class="plan">
      <div class="plan-head"><h4>Today's plan · about ${plan.planned_minutes} min</h4>
        <label class="small muted">Time today
          <select id="plan-minutes" class="scope">${[10, 20, 30, 45, 60, 90].map((m) =>
            `<option value="${m}" ${m === plan.minutes ? "selected" : ""}>${m} min</option>`).join("")}</select></label></div>
      ${plan.tasks.length ? plan.tasks.map(task).join("")
        : `<div class="task"><span class="icon">${icon("check", "")}</span><div class="grow"><b>All done for today</b>
            <div class="muted small">Nothing is due and every chapter has been started. Come back tomorrow.</div></div></div>`}
    </div>
    <div class="plan">
      <div class="plan-head"><h4>Exams</h4></div>
      ${plan.exams.map((e) => `<div class="exam"><div class="grow"><b>${esc(e.name)}</b>
          <div class="muted small">${examLine(e)}</div></div>
        <input type="date" data-exam="${e.doc_id}" value="${e.exam_date || ""}" title="Exam date"></div>`).join("")}
    </div>`;

  $("#plan-minutes").onchange = (e) => { store.set("folaio-minutes", e.target.value); loadToday(); };
  $("#start-review")?.addEventListener("click", () => { quizRange = null; $("#quiz-scope").value = ""; openTab("quiz"); });
  box.querySelectorAll(".read-summary").forEach((b) => (b.onclick = () => {
    $("#notes-doc").value = b.dataset.doc;
    openTab("notes");
  }));
  box.querySelectorAll("[data-exam]").forEach((inp) => (inp.onchange = async () => {
    await post(`/api/documents/${inp.dataset.exam}/exam`, { date: inp.value || null });
    loadToday();
  }));
}

/* ---------- knowledge map ---------- */
const STATES = {
  mastered: "Mastered", shaky: "Shaky", weak: "Weak", new: "Not studied yet", none: "No questions",
};
let quizRange = null;   // {doc_id, section, page_from, page_to, title} while practising one section

function tileMeta(s) {
  if (s.status === "none") return "no quiz questions";
  if (s.status === "new") return `${s.questions} questions · not started`;
  return `${s.right}/${s.answered} right${s.due ? ` · ${s.due} due` : ""}`;
}

async function loadMap() {
  const map = await api("/api/map");
  const box = $("#map");
  if (!map.length) {
    box.innerHTML = `<p class="muted">Add PDFs in the Library. Each section of your books will appear here, coloured by how well you know it.</p>`;
    return;
  }
  const all = map.flatMap((d) => d.sections.map((s) => ({ ...s, doc_id: d.doc_id, doc: d.name })));
  const rank = { weak: 0, shaky: 1, new: 2 };
  const focus = all.filter((s) => s.status in rank)
    .sort((a, b) => rank[a.status] - rank[b.status] || a.strength - b.strength || b.exam_hits - a.exam_hits).slice(0, 3);
  const attrs = (s) => `data-doc="${s.doc_id}" data-section="${s.index}" data-from="${s.page_from}" data-to="${s.page_to}" data-title="${esc(s.title)}"`;

  let html = "";
  if (focus.length) {
    html += `<div class="focus"><h4>${icon("target")} Focus next</h4>` + focus.map((s) => `
      <div class="focus-item"><span class="dot bg-${s.status}"></span>
        <div class="grow"><b>${esc(s.title)}</b><div class="muted small">${esc(s.doc)} · p.${s.page_from}–${s.page_to} · ${STATES[s.status]}: ${tileMeta(s)}</div></div>
        <button class="ghost practise" ${attrs(s)}>Practise</button></div>`).join("") + `</div>`;
  } else if (all.some((s) => s.status === "mastered")) {
    html += `<div class="focus"><h4>🎉 Everything you've studied is mastered</h4><p class="muted small">Keep answering due questions so it stays that way.</p></div>`;
  }
  html += `<div class="legend">${["mastered", "shaky", "weak", "new"].map((k) => `<span><span class="dot bg-${k}"></span>${STATES[k]}</span>`).join("")}</div>`;
  for (const d of map) {
    const counts = {};
    d.sections.forEach((s) => (counts[s.status] = (counts[s.status] || 0) + 1));
    const n = d.sections.length;
    html += `<div class="map-doc">
      <div class="map-head"><h4>${esc(d.name)}</h4><span class="muted small">${Math.round(d.mastery * 100)}% mastered</span></div>
      <div class="stack">${["mastered", "shaky", "weak", "new", "none"].filter((k) => counts[k])
        .map((k) => `<div class="bg-${k}" style="width:${(100 * counts[k]) / n}%" title="${STATES[k]}: ${counts[k]}"></div>`).join("")}</div>
      <div class="tiles">${d.sections.map((s) => `
        <button class="tile st-${s.status} ${s.questions ? "practise" : ""}" ${attrs({ ...s, doc_id: d.doc_id })} ${s.questions ? "" : "disabled"}
          title="${STATES[s.status]}${s.questions ? ". Click to practise this section" : ""}">
          <span class="tile-title">${esc(s.title)}</span>
          <span class="tile-meta">p.${s.page_from}–${s.page_to} · ${tileMeta(s)}</span>
          ${s.exam_hits ? `<span class="exam-badge">${icon("paper")} in exams ×${s.exam_hits}</span>` : ""}
        </button>`).join("")}</div>
    </div>`;
  }
  setHTML(box, html);
}

document.addEventListener("click", (e) => {
  const t = e.target.closest(".practise");
  if (!t || t.disabled) return;
  quizRange = { doc_id: Number(t.dataset.doc), section: Number(t.dataset.section),
    page_from: Number(t.dataset.from), page_to: Number(t.dataset.to), title: t.dataset.title };
  openTab("quiz");
});

async function nextCard() {
  const banner = $("#practising");
  let url = "/api/quiz/next";
  if (quizRange) {
    url += `?doc_id=${quizRange.doc_id}&section=${quizRange.section}`;
    banner.innerHTML = `Practising <b>${esc(quizRange.title)}</b> (p.${quizRange.page_from}–${quizRange.page_to})
      <button class="link" id="stop-practice">Back to all due questions</button>`;
    banner.classList.remove("hidden");
    $("#stop-practice").onclick = () => { quizRange = null; nextCard(); };
  } else {
    banner.classList.add("hidden");
    const scope = $("#quiz-scope").value;
    if (scope) url += "?doc_id=" + scope;
  }
  const card = await api(url);
  const box = $("#card");
  if (!card) {
    box.innerHTML = status?.study.total
      ? `<p><b>All done for now 🎉</b></p><p class="muted">You've answered every question that's due. Folaio will bring questions back at the right time so you don't forget.</p>`
      : `<p class="muted">No quiz questions yet. Folaio makes them automatically after you add PDFs.</p>`;
    return;
  }
  box.innerHTML = `<div class="muted small">${esc(card.doc_name)} · page ${card.page}</div>
    <div class="q">${esc(card.question)}</div>
    ${card.choices.map((c, i) => `<button class="choice" data-i="${i}">${String.fromCharCode(65 + i)}. ${esc(c)}</button>`).join("")}
    <div id="after"></div>`;
  box.querySelectorAll(".choice").forEach((b) => (b.onclick = async () => {
    const pick = Number(b.dataset.i), ok = pick === card.answer;
    box.querySelectorAll(".choice").forEach((x) => {
      x.disabled = true;
      if (Number(x.dataset.i) === card.answer) x.classList.add("right");
    });
    if (!ok) b.classList.add("wrong");
    await post(`/api/quiz/${card.id}`, { correct: ok });
    $("#after").innerHTML = `<div class="explain"><b>${ok ? "✅ Correct!" : "❌ Not quite."}</b> ${esc(card.explanation)}
      <a class="cite" href="${pdfLink(card.doc_id, card.page)}" target="_blank">p.${card.page}</a></div>
      <p><button class="primary" id="next">Next question →</button></p>`;
    $("#next").onclick = async () => { await refresh(); loadStudy(); };
  }));
}

async function loadNotes() {
  const id = $("#notes-doc").value;
  const box = $("#notes");
  if (!id) { box.innerHTML = `<p class="muted">Add a PDF to see its summaries.</p>`; return; }
  const secs = await api(`/api/documents/${id}/summary`);
  box.innerHTML = secs.length ? secs.map((s) => `<div class="section">
      <div class="pages"><a class="cite" href="${pdfLink(id, s.page_from)}" target="_blank">Pages ${s.page_from}–${s.page_to}</a></div>${esc(s.summary)}</div>`).join("")
    : `<p class="muted">Summaries for this document are still being made.</p>`;
}

/* ---------- settings ---------- */
const pct = (dl) => (dl.total ? Math.round((100 * dl.done) / dl.total) : 0);
const ago = (t) => { const m = Math.round((Date.now() / 1000 - t) / 60); return m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} days ago`; };

function renderSettings() {
  $("#data-dir").textContent = status.data_dir;
  const name = $("#your-name");
  if (document.activeElement !== name) name.value = status.settings.name || "";
}
$("#your-name").onchange = async (e) => {
  await post("/api/settings", { name: e.target.value.trim().slice(0, 40) });
  toast("Saved");
  refresh();
};

let newBrains = [];   // "new" brains shown during this visit to the Brains page
async function openBrains() {
  newBrains = status?.brain?.new || [];
  renderBrains();
  if (newBrains.length) { await post("/api/brains/seen", {}); refresh(); }
}

function renderBrains() {
  const b = status.brain, hw = status.hardware, dl = b.download, mind = status.mind;
  const fresh = b.options.filter((o) => newBrains.includes(o.key));
  $("#new-brain").hidden = !fresh.length;
  $("#new-brain").innerHTML = fresh.length ? `<b>✨ New brain${fresh.length > 1 ? "s" : ""} available:</b> ` +
    fresh.map((o) => `<b>${esc(o.name)}</b> (${esc(o.blurb)})`).join(", ") + ". Download it below if you'd like to try it." : "";
  $("#engine-missing").hidden = b.engine !== false;   // only when the server says it is missing
  $("#hw").textContent = `This computer: ${hw.os} · ${hw.ram_gb} GB RAM · ${hw.cpu_cores} CPU cores · ${hw.disk_free_gb} GB free disk space`;
  $("#brain-folder").textContent = b.folder + (b.folder_is_default ? "  (default)" : "");
  $("#folder-reset").hidden = b.folder_is_default;
  $("#folder-missing").hidden = b.folder_ok;
  $("#core-now").innerHTML = mind.ready
    ? `Knows ${mind.words.toLocaleString()} different words from your library` +
      (mind.accuracy != null ? ` · recognises ${Math.round(mind.accuracy * 100)}% of sentences it has never seen` : "") +
      ` · last learned ${ago(mind.learned_at)}`
    : "Add PDFs to the Library and Folaio Core will learn them.";

  const anyDownloaded = b.options.some((o) => o.installed);
  const off = `<div class="brain ${!b.installed ? "in-use" : ""}">
      <div class="grow" style="flex:1"><b>Off</b> ${!b.installed ? `<span class="badge ready">In use</span>` : ""}
        <div class="muted small">Use Folaio Core only. Answers are the exact sentences from your documents.</div></div>
      <div class="actions-col">${b.installed ? `<button class="ghost" onclick="useBrain('off')">Use this</button>` : ""}</div>
    </div>`;

  setHTML($("#brains"), off + b.options.map((o) => {
    const busy = dl.active && dl.key === o.key;
    const fitsDisk = hw.disk_free_gb * 1e9 > o.size_bytes * 1.1;
    let state = o.active ? `<span class="badge ready">In use</span>`
      : o.installed ? `<span class="badge">Downloaded</span>`
      : busy ? `<span class="badge">${dl.verifying ? "Checking the download…" : `Downloading ${pct(dl)}%`}</span>` : `<span class="badge">Not downloaded</span>`;
    if (o.key === b.recommended) state += ` <span class="badge">⭐ Best for this computer</span>`;
    if (newBrains.includes(o.key)) state += ` <span class="badge" style="background:var(--accent-soft);color:var(--accent-text)">✨ New</span>`;
    const buttons = o.installed
      ? (o.active ? "" : `<button class="ghost" onclick="useBrain('${o.key}')">Use this</button>`) +
        `<button class="ghost" onclick="removeBrain('${o.key}', '${esc(o.name)}')">Remove</button>`
      : busy ? ""
      : `<button class="${o.key === b.recommended ? "primary" : "ghost"}" onclick="getBrain('${o.key}')"
           ${dl.active || !fitsDisk ? "disabled" : ""}>Download</button>`;
    const needs = o.min_ram_gb || 2;
    return `<div class="brain ${o.active ? "in-use" : ""}">
      <div class="grow" style="flex:1"><b>${esc(o.name)}</b> ${state}
        <div class="muted small">${esc(o.blurb)}</div>
        <div class="facts">
          <span>Model: <b>${esc(o.label)}</b></span>
          <span>Download: <b>${gb(o.size_bytes)}</b></span>
          <span>Needs: <b>${needs} GB RAM</b> ${o.fits ? "✓" : `<span class="warn">(this computer has ${hw.ram_gb} GB)</span>`}</span>
          <span>License: <b>${esc(o.license)}</b></span>
        </div>
        ${!fitsDisk && !o.installed ? `<div class="small warn">Not enough free disk space.</div>` : ""}
        ${busy ? `<div class="bar"><div style="width:${pct(dl)}%"></div></div>` : ""}
        ${dl.error && dl.key === o.key ? `<div class="small ${dl.active ? "muted" : "warn"}">${esc(dl.error)}</div>` : ""}
      </div>
      <div class="actions-col">${buttons}</div>
    </div>`;
  }).join(""));
}

async function pickBrainFolder() {
  const msg = $("#folder-msg");
  msg.className = "small muted";
  msg.textContent = "A folder window has opened. It may be behind your browser.";
  let path;
  try {
    const r = await post("/api/brain-folder/pick", {});
    if (r.cancelled) { msg.textContent = ""; return; }
    path = r.unsupported ? prompt("Type the full path of the folder for Folaio's brains:") : r.path;
  } catch (e) { msg.textContent = e.message; return; }
  if (path) moveBrains(path);
  else msg.textContent = "";
}

async function moveBrains(path) {
  const msg = $("#folder-msg"), have = status.brain.options.filter((o) => o.installed);
  if (have.length && !confirm(`Move your downloaded brains (${have.map((o) => o.name).join(", ")}) to:\n${path || "the default folder"}?`)) return;
  msg.className = "small muted";
  msg.textContent = have.length ? "Moving brains… this can take a minute." : "Saving…";
  try {
    await post("/api/brain-folder", { path });
    msg.textContent = "✅ Done.";
  } catch (e) {
    msg.className = "small warn";
    msg.textContent = e.message;
  }
  refresh();
}

async function useBrain(key) {
  await post(`/api/brain/${key}/use`, {});
  refresh();
}

async function removeBrain(key, name) {
  if (!confirm(`Remove the ${name} brain from this computer? You can download it again later.`)) return;
  await api(`/api/brain/${key}`, { method: "DELETE" });
  refresh();
}

async function getBrain(key) {
  await post(`/api/brain/${key}`, {});
  refresh();
}

/* ---------- first-launch welcome ---------- */
let welcomeOpen = false;

function showWelcome() {
  const b = status.brain;
  welcomeOpen = true;
  $("#welcome-body").innerHTML = `<h1>Welcome to Folaio 👋</h1>
    <p>Folaio is an AI that learns from <b>your</b> PDFs, right here on this computer. No internet, no account.</p>
    <p>Add a textbook or your notes, and within seconds you can search it, ask questions and practise with quizzes.</p>
    <div class="actions"><button class="primary" id="w-start">Add my first PDF</button></div>
    <div class="welcome-plus" ${b.engine === false ? "hidden" : ""}>
      <p class="small"><b>Optional:</b> add a tiny <b>Folaio Plus</b> brain so Folaio can also explain answers in simple words.
        It's downloaded once and then works offline.</p>
      <div class="plus-choices">${b.options.map((o) => `
        <button class="ghost plus-choice" data-key="${o.key}">
          <b>${esc(o.name)}</b> · ${gb(o.size_bytes)}${o.key === b.recommended ? " ⭐" : ""}<br><span class="muted small">${esc(o.blurb)}</span>
        </button>`).join("")}</div>
      <div id="w-progress" class="small muted"></div>
    </div>`;
  $("#welcome").classList.remove("hidden");
  $("#w-start").onclick = () => {
    post("/api/settings", { welcomed: true });
    welcomeOpen = false;
    $("#welcome").classList.add("hidden");
    show("library");
  };
  $$(".plus-choice").forEach((btn) => (btn.onclick = () => getBrain(btn.dataset.key)));
  renderWelcomeProgress();
}

function renderWelcomeProgress() {
  if (!welcomeOpen) return;
  const b = status.brain, dl = b.download;
  const busy = dl.active || !!b.installed;
  $$(".plus-choice").forEach((btn) => (btn.disabled = busy));
  $("#w-progress").innerHTML = b.installed ? `✅ ${esc(b.installed.name)} brain is ready.`
    : dl.active ? `Downloading… ${pct(dl)}% (you can start adding PDFs meanwhile)<div class="bar"><div style="width:${pct(dl)}%"></div></div>`
    : dl.error ? `<span style="color:var(--bad)">Download stopped: ${esc(dl.error)}. Click a brain to try again.</span>` : "";
}

/* ---------- polling ---------- */
let welcomeChecked = false;
let askHomeKey = "";
async function refresh() {
  try {
    [status, docs] = await Promise.all([api("/api/status"), api("/api/documents")]);
  } catch {
    setHTML($("#activity"), `<span class="dot" style="background:var(--bad)"></span><span>Folaio isn't running</span>`);
    return;
  }
  const b = status.brain, busy = status.activity !== "idle";
  const line = busy ? esc(status.activity)
    : b.download.active ? `Downloading a Plus brain · ${pct(b.download)}%`
    : `Folaio Core${b.installed ? " + Plus" : ""} · ready`;
  setHTML($("#activity"), `<span class="dot ${busy || b.download.active ? "busy" : ""}"></span><span>${line}</span>`);
  $("#brains-new").classList.toggle("hidden", !(b.new || []).length);
  const due = status.study.due || 0;
  $("#due-count").textContent = due > 99 ? "99+" : due;
  $("#due-count").classList.toggle("hidden", !due);
  $("#inbox-hint").textContent = `Tip: PDFs dropped into ${status.inbox} are added automatically.`;
  renderDocs();
  fillScopes();
  if ($("#view-settings").classList.contains("active")) renderSettings();
  if ($("#view-brains").classList.contains("active")) renderBrains();
  // Refresh the Ask start screen when something it shows has changed.
  const key = JSON.stringify([docs.map((d) => d.id + d.status), due, status.settings.name, status.mind.learned_at]);
  if (key !== askHomeKey && !inChat()) { askHomeKey = key; loadAskHome(); }
  renderWelcomeProgress();
  if (!welcomeChecked) {
    welcomeChecked = true;
    if (!status.settings.welcomed && !docs.length) showWelcome();
  }
}

setInterval(refresh, 2500);
