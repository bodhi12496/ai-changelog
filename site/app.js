/* AI Changelog front end. No build step, no dependencies. */
(() => {
  "use strict";

  const TYPES = [
    { key: "all", label: "Everything" },
    { key: "paper", label: "Papers", one: "Paper" },
    { key: "repo", label: "Repositories", one: "Repository" },
    { key: "model", label: "Models", one: "Model" },
    { key: "tool", label: "Tools and demos", one: "Tool" },
    { key: "news", label: "News and posts", one: "News" },
    { key: "saved", label: "Saved" },
  ];
  const COLOR_VAR = { paper: "--paper-c", repo: "--repo-c", model: "--model-c", tool: "--tool-c", news: "--news-c" };
  const BATCH = 50;
  const DAY = 86400000;
  const LS = {
    get(k, d) { try { const v = localStorage.getItem("aicl." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("aicl." + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
  };

  const $ = (s) => document.querySelector(s);
  const els = {
    types: $("#types"), range: $("#range"), sort: $("#sort"), unseen: $("#unseen"), source: $("#source"),
    q: $("#q"), list: $("#list"), summary: $("#summary"), sentinel: $("#sentinel"), strip: $("#strip"),
    stripTotal: $("#strip-total"), legend: $("#legend"), health: $("#health-list"), theme: $("#theme"),
    tpl: $("#tpl-entry"), repo: $("#repo-link"),
  };

  const state = {
    type: "all", range: LS.get("range", 3), sort: LS.get("sort", "new"),
    q: "", source: "", day: null, unseen: false,
  };
  let all = [];
  let saved = LS.get("saved", {});
  let previousVisit = LS.get("lastVisit", null);
  let filtered = [];
  let rendered = 0;
  let generatedAt = null;

  // ---------- utils
  const startOfDay = (t) => { const d = new Date(t); d.setHours(0, 0, 0, 0); return d.getTime(); };
  const fmtNum = (n) => n >= 1e6 ? (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M" : n >= 1e3 ? (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k" : String(n);
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  function ago(t) {
    const s = (t - Date.now()) / 1000;
    const abs = Math.abs(s);
    if (abs < 3600) return rtf.format(Math.round(s / 60), "minute");
    if (abs < 86400) return rtf.format(Math.round(s / 3600), "hour");
    return rtf.format(Math.round(s / 86400), "day");
  }
  function dayLabel(t) {
    const today = startOfDay(Date.now());
    const d = new Date(t);
    const long = d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
    if (t === today) return ["Today", long];
    if (t === today - DAY) return ["Yesterday", long];
    return [d.toLocaleDateString(undefined, { weekday: "long" }), d.toLocaleDateString(undefined, { month: "long", day: "numeric" })];
  }

  // When an entry belongs on the timeline: its publish date, unless that is much
  // older than when we first saw it (e.g. an old model that just started trending).
  function prepare(it) {
    const pub = Date.parse(it.published) || Date.now();
    const seen = Date.parse(it.first_seen) || pub;
    it._when = Math.min(pub >= seen - 3 * DAY ? pub : seen, Date.now());
    it._seen = seen;
    it._hay = [it.title, it.ai_summary || "", it.description, it.source, (it.tags || []).join(" "), (it.authors || []).join(" ")].join(" ").toLowerCase();
    return it;
  }

  // ---------- hash state (shareable links)
  function readHash() {
    const p = new URLSearchParams(location.hash.slice(1));
    if (p.has("type") && TYPES.some((t) => t.key === p.get("type"))) state.type = p.get("type");
    if (p.has("q")) state.q = p.get("q");
    if (p.has("source")) state.source = p.get("source");
    if (p.has("range")) state.range = +p.get("range") || state.range;
  }
  function writeHash() {
    const p = new URLSearchParams();
    if (state.type !== "all") p.set("type", state.type);
    if (state.q) p.set("q", state.q);
    if (state.source) p.set("source", state.source);
    const h = p.toString();
    history.replaceState(null, "", h ? "#" + h : location.pathname + location.search);
  }

  // ---------- filtering
  function baseSet() {
    if (state.type === "saved") return Object.values(saved).map(prepare);
    return all;
  }
  function apply() {
    const terms = state.q.toLowerCase().split(/\s+/).filter(Boolean);
    const since = state.type === "saved" ? 0 : Date.now() - state.range * DAY;
    filtered = baseSet().filter((it) => {
      if (state.type !== "all" && state.type !== "saved" && it.type !== state.type) return false;
      if (it._when < since) return false;
      if (state.day != null && startOfDay(it._when) !== state.day) return false;
      if (state.source && it.source !== state.source) return false;
      if (state.unseen && !(previousVisit && it._seen > previousVisit)) return false;
      return terms.every((t) => it._hay.includes(t));
    });

    if (state.sort === "top") {
      // Stars, upvotes and points aren't comparable, so score each item against
      // the 90th percentile of its own type.
      const p90 = {};
      for (const type of Object.keys(COLOR_VAR)) {
        const s = all.filter((i) => i.type === type && i.score).map((i) => i.score).sort((a, b) => a - b);
        p90[type] = s.length ? s[Math.floor(s.length * 0.9)] || 1 : 1;
      }
      filtered.sort((a, b) => (b.score || 0) / p90[b.type] - (a.score || 0) / p90[a.type] || b._when - a._when);
    } else {
      filtered.sort((a, b) => b._when - a._when);
    }
    rendered = 0;
    els.list.textContent = "";
    renderMore();
    renderSummary();
    renderNav();
    writeHash();
  }

  // ---------- rendering
  function entryNode(it) {
    const node = els.tpl.content.firstElementChild.cloneNode(true);
    node.dataset.type = it.type;
    const meta = TYPES.find((t) => t.key === it.type);
    node.querySelector(".kind").textContent = meta ? meta.one : it.type;
    node.querySelector(".src").textContent = it.source;
    const time = node.querySelector("time");
    time.dateTime = new Date(it._when).toISOString();
    time.textContent = ago(it._when);
    time.title = new Date(it._when).toLocaleString();
    if (previousVisit && it._seen > previousVisit) node.querySelector(".fresh").hidden = false;

    const a = node.querySelector(".title a");
    a.href = it.url;
    a.textContent = it.title;
    const desc = node.querySelector(".desc");
    desc.textContent = it.ai_summary || it.description || "";
    if (it.ai_summary) {
      const tag = node.querySelector(".ai");
      tag.hidden = false;
      if (it.description && it.description !== it.ai_summary) tag.title = `Original description: ${it.description}`;
    }
    if (it.authors && it.authors.length) {
      const au = node.querySelector(".authors");
      au.hidden = false;
      au.textContent = it.authors.slice(0, 3).join(", ") + (it.authors.length > 3 ? " and others" : "");
    }

    const tags = node.querySelector(".tags");
    for (const t of (it.tags || []).slice(0, 5)) {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = t;
      b.title = `Search for ${t}`;
      b.addEventListener("click", () => { els.q.value = t; state.q = t; state.day = null; apply(); els.q.focus(); });
      li.append(b);
      tags.append(li);
    }
    if (it.score) {
      const sc = node.querySelector(".score");
      sc.hidden = false;
      sc.textContent = `${fmtNum(it.score)} ${it.score_label || ""}`.trim();
    }
    if (it.discussion && it.discussion !== it.url) {
      const d = node.querySelector(".disc");
      d.hidden = false;
      d.href = it.discussion;
    }
    const save = node.querySelector(".save");
    const setSaved = (on) => { save.setAttribute("aria-pressed", String(on)); save.textContent = on ? "Saved" : "Save"; };
    setSaved(!!saved[it.id]);
    save.addEventListener("click", () => {
      if (saved[it.id]) delete saved[it.id];
      else { const { _hay, _when, _seen, ...plain } = it; saved[it.id] = plain; }
      LS.set("saved", saved);
      setSaved(!!saved[it.id]);
      renderNav();
    });
    return node;
  }

  function renderMore() {
    if (!filtered.length) return renderEmpty();
    const slice = filtered.slice(rendered, rendered + BATCH);
    const grouped = state.sort === "new";
    let section = els.list.lastElementChild;
    for (const it of slice) {
      const key = grouped ? startOfDay(it._when) : "top";
      if (!section || section.dataset.key !== String(key)) {
        section = document.createElement("section");
        section.className = "day";
        section.dataset.key = key;
        const h = document.createElement("h2");
        h.className = "day-head";
        if (grouped) {
          const [main, sub] = dayLabel(key);
          h.textContent = main;
          const small = document.createElement("small");
          small.textContent = sub;
          h.append(small);
        } else {
          h.textContent = "Most popular";
          const small = document.createElement("small");
          small.textContent = state.type === "saved" ? "in your saved list" : `in the last ${rangeText()}`;
          h.append(small);
        }
        section.append(h);
        els.list.append(section);
      }
      section.append(entryNode(it));
    }
    rendered += slice.length;
  }

  function renderEmpty() {
    const box = document.createElement("div");
    box.className = "empty";
    const h = document.createElement("h2");
    const p = document.createElement("p");
    if (!all.length) {
      h.textContent = "No entries yet";
      p.textContent = "The collector hasn't run yet. Open the Actions tab of this repository and run the \"Update feed\" workflow; entries appear here a few minutes later.";
      box.append(h, p);
    } else if (state.type === "saved" && !Object.keys(saved).length) {
      h.textContent = "Nothing saved yet";
      p.textContent = "Select Save on any entry to keep it here. Saved entries stay in this browser even after they leave the main feed.";
      box.append(h, p);
    } else if (state.unseen && !previousVisit) {
      h.textContent = "This is your first visit";
      p.textContent = "From your next visit on, this filter shows only entries that arrived since you were last here.";
      box.append(h, p);
    } else {
      h.textContent = "No matching entries";
      p.textContent = "Try a wider time range, another category, or a shorter search.";
      const b = document.createElement("button");
      b.textContent = "Clear filters";
      b.addEventListener("click", resetFilters);
      box.append(h, p, b);
    }
    els.list.append(box);
  }

  function rangeText() {
    return { 1: "24 hours", 3: "3 days", 7: "week", 30: "month" }[state.range] || `${state.range} days`;
  }

  function renderSummary() {
    const parts = [`${filtered.length.toLocaleString()} ${filtered.length === 1 ? "entry" : "entries"}`];
    if (state.day != null) parts.push(`on ${new Date(state.day).toLocaleDateString(undefined, { month: "long", day: "numeric" })}`);
    else if (state.type !== "saved") parts.push(`from the last ${rangeText()}`);
    let text = parts.join(" ");
    if (generatedAt) text += `. Updated ${ago(generatedAt)}.`;
    els.summary.textContent = text;
    if (state.day != null) {
      const b = document.createElement("button");
      b.className = "save";
      b.style.marginLeft = "8px";
      b.textContent = "Show all days";
      b.addEventListener("click", () => { state.day = null; apply(); renderStrip(); });
      els.summary.append(b);
    }
  }

  function renderNav() {
    const since = Date.now() - state.range * DAY;
    const counts = { all: 0, saved: Object.keys(saved).length };
    for (const it of all) {
      if (it._when < since) continue;
      counts.all++;
      counts[it.type] = (counts[it.type] || 0) + 1;
    }
    els.types.textContent = "";
    for (const t of TYPES) {
      const b = document.createElement("button");
      b.className = "type-btn";
      b.type = "button";
      b.setAttribute("aria-current", String(state.type === t.key));
      if (COLOR_VAR[t.key]) b.style.setProperty("--c", `var(${COLOR_VAR[t.key]})`);
      const sw = document.createElement("span");
      sw.className = "sw";
      if (t.key === "all") sw.style.background = "currentColor";
      if (t.key === "saved") sw.style.background = "transparent", sw.style.boxShadow = "inset 0 0 0 1.5px currentColor";
      const label = document.createElement("span");
      label.textContent = t.label;
      const n = document.createElement("span");
      n.className = "n";
      n.textContent = (counts[t.key] || 0).toLocaleString();
      b.append(sw, label, n);
      b.addEventListener("click", () => { state.type = t.key; apply(); });
      els.types.append(b);
    }
    for (const b of els.range.querySelectorAll("button")) b.setAttribute("aria-checked", String(+b.dataset.range === state.range));
    for (const b of els.sort.querySelectorAll("button")) b.setAttribute("aria-checked", String(b.dataset.sort === state.sort));
  }

  function renderStrip() {
    const today = startOfDay(Date.now());
    const days = [...Array(14)].map((_, i) => today - (13 - i) * DAY);
    const buckets = new Map(days.map((d) => [d, { paper: 0, repo: 0, model: 0, tool: 0, news: 0 }]));
    for (const it of all) {
      const b = buckets.get(startOfDay(it._when));
      if (b && b[it.type] != null) b[it.type]++;
    }
    const totals = days.map((d) => Object.values(buckets.get(d)).reduce((a, b) => a + b, 0));
    const max = Math.max(1, ...totals);
    els.strip.textContent = "";
    days.forEach((d, i) => {
      const btn = document.createElement("button");
      btn.className = "bar";
      btn.type = "button";
      btn.setAttribute("role", "listitem");
      const date = new Date(d);
      btn.dataset.day = i % 2 === 1 || i === 13 ? String(date.getDate()) : "";
      btn.setAttribute("aria-current", String(state.day === d));
      const counts = buckets.get(d);
      const desc = Object.entries(counts).filter(([, n]) => n).map(([k, n]) => `${n} ${k}${n === 1 ? "" : "s"}`).join(", ");
      btn.setAttribute("aria-label", `${date.toLocaleDateString(undefined, { month: "long", day: "numeric" })}: ${totals[i]} entries${desc ? " (" + desc + ")" : ""}. Show this day.`);
      btn.title = `${date.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })}: ${totals[i]} entries`;
      if (!totals[i]) {
        const s = document.createElement("span");
        s.className = "bar-empty";
        btn.append(s);
      }
      for (const [type, n] of Object.entries(counts)) {
        if (!n) continue;
        const s = document.createElement("span");
        s.style.height = `${(n / max) * 100}%`;
        s.style.background = `var(${COLOR_VAR[type]})`;
        btn.append(s);
      }
      btn.addEventListener("click", () => {
        state.day = state.day === d ? null : d;
        if (state.day != null && state.range < 14) { state.range = 30; LS.set("range", 30); }
        if (state.type === "saved") state.type = "all";
        apply();
        renderStrip();
        document.getElementById("feed").focus({ preventScroll: true });
      });
      els.strip.append(btn);
    });
    const sum = totals.reduce((a, b) => a + b, 0);
    els.stripTotal.textContent = `${sum.toLocaleString()} entries in the last 14 days`;
    els.legend.textContent = "";
    for (const t of TYPES.filter((t) => COLOR_VAR[t.key])) {
      const span = document.createElement("span");
      const i = document.createElement("i");
      i.style.background = `var(${COLOR_VAR[t.key]})`;
      span.append(i, t.label.split(" ")[0]);
      els.legend.append(span);
    }
  }

  function renderSources() {
    const names = [...new Set(all.map((i) => i.source))].sort((a, b) => a.localeCompare(b));
    for (const n of names) {
      const o = document.createElement("option");
      o.value = o.textContent = n;
      els.source.append(o);
    }
    els.source.value = state.source;
  }

  function renderHealth(status) {
    els.health.textContent = "";
    if (!status || !status.sources) return;
    for (const [name, s] of Object.entries(status.sources)) {
      const li = document.createElement("li");
      const a = document.createElement("span");
      a.textContent = name;
      const b = document.createElement("span");
      if (s.ok) b.textContent = `${s.count} new`;
      else { b.textContent = /No API key/.test(s.error || "") ? "needs API key" : "failed"; b.className = "bad"; li.title = s.error || ""; }
      li.append(a, b);
      els.health.append(li);
    }
  }

  function resetFilters() {
    Object.assign(state, { type: "all", q: "", source: "", day: null, unseen: false, range: 30 });
    els.q.value = ""; els.source.value = ""; els.unseen.checked = false;
    LS.set("range", 30);
    apply(); renderStrip();
  }

  // ---------- theme
  function applyTheme(t) {
    if (t) document.documentElement.dataset.theme = t;
    else delete document.documentElement.dataset.theme;
  }
  applyTheme(LS.get("theme", null));
  els.theme.addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme
      || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = cur === "dark" ? "light" : "dark";
    LS.set("theme", next);
    applyTheme(next);
  });

  // ---------- events
  let qTimer;
  els.q.addEventListener("input", () => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => { state.q = els.q.value.trim(); apply(); }, 120);
  });
  els.range.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    state.range = +b.dataset.range; state.day = null; LS.set("range", state.range); apply(); renderStrip();
  });
  els.sort.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    state.sort = b.dataset.sort; LS.set("sort", state.sort); apply();
  });
  els.unseen.addEventListener("change", () => { state.unseen = els.unseen.checked; apply(); });
  els.source.addEventListener("change", () => { state.source = els.source.value; apply(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !/input|textarea|select/i.test(document.activeElement.tagName)) { e.preventDefault(); els.q.focus(); }
    if (e.key === "Escape" && document.activeElement === els.q && els.q.value) { els.q.value = ""; state.q = ""; apply(); }
  });
  new IntersectionObserver((entries) => {
    if (entries[0].isIntersecting && rendered && rendered < filtered.length) renderMore();
  }, { rootMargin: "800px" }).observe(els.sentinel);

  // Link to the source repo when hosted on GitHub Pages (user.github.io/repo).
  (function repoLink() {
    const m = location.hostname.match(/^([^.]+)\.github\.io$/);
    if (m) {
      const repo = location.pathname.split("/").filter(Boolean)[0] || `${m[1]}.github.io`;
      els.repo.href = `https://github.com/${m[1]}/${repo}`;
    }
    const mobile = document.getElementById("repo-link-m");
    if (mobile) mobile.href = els.repo.href;
  })();

  // ---------- boot
  async function boot() {
    readHash();
    els.q.value = state.q;
    let data = { items: [] }, status = null;
    try {
      const r = await fetch("data/items.json", { cache: "no-cache" });
      if (r.ok) data = await r.json();
    } catch { /* empty state handles it */ }
    try {
      const r = await fetch("data/status.json", { cache: "no-cache" });
      if (r.ok) status = await r.json();
    } catch { /* optional */ }
    generatedAt = Date.parse(data.generated_at) || null;
    all = (data.items || []).map(prepare);
    renderSources();
    renderHealth(status);
    renderStrip();
    apply();
    // Record this visit (the "New" markers compare against the previous one).
    LS.set("lastVisit", Date.now());
  }
  boot();
})();
