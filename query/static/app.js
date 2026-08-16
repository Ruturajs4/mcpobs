/* ===========================================================================
   MCP Observability console.

   Split out of index.html once the drawer and span detail arrived: a single
   file was fine for four tables, not for a debugging surface.

   The organising idea: a trace opens in a RIGHT-HAND DRAWER over the list
   rather than a new page. Debugging is comparison -- you bounce between traces
   -- and navigating away loses the list you were working through.
   =========================================================================== */

const S = { view: "overview", window: 60, trace: null, span: null, kind: "tool" };

/* The API key, held in localStorage. A cookie would ride along automatically on
   every request the browser makes to this origin, which is what makes CSRF
   possible; an explicit header cannot be sent by a form on someone else's
   page. */
const KEY_STORAGE = "mcpobs.key";
const readKey = () => localStorage.getItem(KEY_STORAGE) || "";

async function api(path, signal = null) {
  const sep = path.includes("?") ? "&" : "?";
  const r = await fetch(`/api/v1${path}${sep}window_minutes=${S.window}`, {
    headers: readKey() ? { "x-api-key": readKey() } : {},
    signal,
  });
  if (r.status === 401) {
    // Not an error to render in a panel: it means we are not signed in, and
    // the whole console is unusable until that changes.
    signIn(true);
    throw new Error("unauthorized");
  }
  if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 200)}`);
  return r.json();
}

function signIn(rejected) {
  const existing = readKey();
  document.body.innerHTML = `
    <div class="signin">
      <div class="signin-card">
        <h1>MCP Observability</h1>
        <p>${rejected && existing
          ? "That key was rejected. It may have been revoked, or it may be an ingest key — the console needs a <code>read</code> key."
          : "Sign in with a read-scoped API key."}</p>
        <input id="key-input" type="password" placeholder="mcpo_..." autocomplete="off"
               spellcheck="false" value="">
        <button id="key-go">Continue</button>
        <!-- This page is served to ANYONE who finds the URL, so it says only
             what a legitimate user without a key needs: who to ask. It used to
             print the exact admin CLI invocation, its --org/--scopes flags, and
             the dotfile that dev keys are written to -- a map of the key-issuing
             surface, handed out pre-authentication. None of that helped the
             person actually locked out, because they cannot run it anyway. -->
        <p class="signin-hint">Access is invite-only. If you do not have a key,
          ask the person who administers your workspace.</p>
      </div>
    </div>`;
  const submit = () => {
    const value = document.getElementById("key-input").value.trim();
    if (!value) return;
    localStorage.setItem(KEY_STORAGE, value);
    location.reload();
  };
  document.getElementById("key-go").onclick = submit;
  document.getElementById("key-input").onkeydown = (e) => {
    if (e.key === "Enter") submit();
  };
  document.getElementById("key-input").focus();
}

function signOut() {
  localStorage.removeItem(KEY_STORAGE);
  location.reload();
}

const el = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* Sub-millisecond values print as "<1ms", never "0.00ms". On a coarse clock
   those really are unmeasured, and a precise-looking zero asserts a precision
   we do not have (D27). */
function dur(ms) {
  if (ms === 0) return "0";
  if (ms < 1) return "<1ms";
  if (ms < 1000) return `${ms.toFixed(ms < 10 ? 2 : 1)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}
// A latency the host clock cannot actually resolve, marked where it is READ
// rather than only in a banner at the top of one page (DF-4). A number carrying
// no qualifier in a table is a number someone will quote.
function durq(latency) {
  const text = dur(latency?.p95_ms ?? 0);
  if (!latency?.clock_warning) return text;
  return `<span class="approx" title="${esc(latency.clock_warning)}">~${text}</span>`;
}
const num = (n) => (n ?? 0).toLocaleString();
const ago = (iso) => {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - new Date(iso + "Z").getTime()) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};

/* Category presentation in ONE place: class, label and chart colour must agree,
   and three lookups would eventually disagree. */
const CAT = {
  ok:                { c: "b-ok",      l: "ok",             h: "#22c55e" },
  tool_error:        { c: "b-tool",    l: "tool error",     h: "#f59e0b" },
  server_exception:  { c: "b-server",  l: "exception",      h: "#ef4444" },
  unknown_tool:      { c: "b-unknown", l: "unknown tool",   h: "#a855f7" },
  invalid_arguments: { c: "b-args",    l: "bad args",       h: "#06b6d4" },
  protocol_error:    { c: "b-proto",   l: "protocol",       h: "#ec4899" },
  pending_input:     { c: "b-pending", l: "awaiting input", h: "#9aa1ad" },
  cancelled:         { c: "b-pending", l: "cancelled",      h: "#78716c" },
  unauthorized:      { c: "b-proto",   l: "401 auth",       h: "#f97316" },
  forbidden:         { c: "b-proto",   l: "403 scope",      h: "#eab308" },
  unclassified:      { c: "b-none",    l: "unclassified",   h: "#6b7280" },
};
const badge = (cat) => {
  const m = CAT[cat] || CAT.unclassified;
  return `<span class="badge ${m.c}">${m.l}</span>`;
};

/* How the call reached the server. Its own column rather than a decoration
   because it changes what you check next: a stdio server is spawned per client
   and dies with it, so "restart it" means something different from a long-lived
   shared HTTP server.

   Blank is rendered as an explicit em dash, not an empty cell. Spans normalized
   before mcpobs derived the transport genuinely do not have one, and an empty
   cell reads as a rendering bug rather than as absent data. */
const TRANSPORT = {
  "stdio": { c: "t-stdio", l: "stdio" },
  "streamable-http": { c: "t-http", l: "http" },
  "sse": { c: "t-http", l: "sse" },
};
function transportTag(value) {
  const m = TRANSPORT[value];
  if (!m) return '<span class="mute" title="Recorded only for spans normalized after transport attribution landed">—</span>';
  return `<span class="tag ${m.c}" title="${esc(value)}">${m.l}</span>`;
}

function distBar(bd) {
  const total = Object.values(bd).reduce((a, b) => a + b, 0) || 1;
  const parts = Object.entries(bd).filter(([, v]) => v > 0)
    .map(([k, v]) => `<i style="width:${(v / total) * 100}%;background:${(CAT[k] || CAT.unclassified).h}" title="${k}: ${v}"></i>`)
    .join("");
  return `<div class="dist">${parts}</div>`;
}

function kindOf(span) {
  if (span.mcp_method) return { cls: "k-mcp", bar: "bar-mcp", tag: "MCP" };
  const map = { http: ["k-http", "bar-http", "HTTP"], db: ["k-db", "bar-db", "DB"],
                llm: ["k-llm", "bar-llm", "LLM"],
                messaging: ["k-msg", "bar-msg", "QUEUE"] };
  const [cls, bar, tag] = map[span.downstream_kind] || ["k-int", "bar-int", (span.downstream_kind || "int").toUpperCase()];
  return { cls, bar, tag };
}

/* ===========================================================================
   Views
   =========================================================================== */
async function viewOverview(signal) {
  const o = await api("/overview", signal);
  const bd = o.failure_breakdown;
  const rate = o.calls ? ((o.errors / o.calls) * 100).toFixed(1) : "0.0";
  const banners = [];
  // The caveat comes from the SERVER, which measured the clock, rather than
  // from a threshold in the browser with the tick hardcoded. The old banner
  // said "~0.75ms" -- a number measured once on one laptop and then frozen into
  // the UI, so it was wrong for every other host and silent when the clock was
  // fine but most calls still floored to zero.
  if (o.latency.clock_warning) banners.push(`<div class="note-bar"><span>&#9888;</span><div>
    <b>Latency percentiles are not reliable on this host.</b>
    ${esc(o.latency.clock_warning)}. Linux hosts are nanosecond-grade and
    unaffected; a server on Windows is not.</div></div>`);
  if (o.classified_ratio < 1) banners.push(`<div class="note-bar"><span>◐</span><div>
    <b>${Math.round(o.classified_ratio * 100)}% of failures are precisely classified.</b>
    The rest come from servers not running the <code>mcpobs</code> helper and report
    only the coarse <em>tool error</em>. Two data qualities, kept distinct.</div></div>`);

  el("content").innerHTML = `
    ${banners.join("")}
    <div class="grid g4">
      <div class="card"><div class="lbl">Tool calls</div><div class="big">${num(o.calls)}</div>
        <div class="sub">${o.servers} server${o.servers === 1 ? "" : "s"} · ${o.tools} tools</div></div>
      <div class="card"><div class="lbl">Error rate</div>
        <div class="big" style="color:${o.errors ? "var(--err)" : "var(--ok)"}">${rate}%</div>
        <div class="sub">${num(o.errors)} failed calls</div></div>
      <div class="card"><div class="lbl">p95 latency</div>
        <div class="big"${o.latency.clock_warning ? ' style="color:var(--text-dim)"' : ""}>${
          o.latency.clock_warning ? "~" : ""}${dur(o.latency.p95_ms)}</div>
        <div class="sub">p50 ${dur(o.latency.p50_ms)} · max ${dur(o.latency.max_ms)}${
          o.latency.clock_tick_ms ? ` · clock ${o.latency.clock_tick_ms.toFixed(3)}ms` : ""}</div></div>
      <div class="card"><div class="lbl">Freshness p95</div><div class="big">${o.freshness_p95_seconds.toFixed(1)}s</div>
        <div class="sub">event time → queryable</div></div>
    </div>
    <div class="panel"><header><h3>Failure breakdown</h3>
      <span class="note">what the raw span alone could never tell you</span></header>
      <table><tbody>${Object.entries(bd).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1])
        .map(([k, v]) => `<tr class="click" data-cat="${k}">
          <td style="width:180px">${badge(k)}</td>
          <td class="num" style="width:70px">${num(v)}</td>
          <td>${distBar({ [k]: v, _rest: Math.max(0, o.calls - v) })}</td></tr>`).join("")}
      </tbody></table></div>`;

  el("foot-fresh").textContent = `${o.freshness_p95_seconds.toFixed(1)}s`;
  el("foot-class").textContent = `${Math.round(o.classified_ratio * 100)}%`;
  bindAll("[data-cat]", (n) => go("errors", { failure_category: n.dataset.cat }));
}

async function viewServers(signal) {
  const rows = await api("/servers", signal);
  el("content").innerHTML = rows.length ? `
    <div class="panel"><header><h3>MCP servers</h3></header><table>
      <thead><tr><th>Server</th><th>Env</th><th class="num">Tools</th><th class="num">Calls</th>
        <th class="num">Errors</th><th style="width:150px">Failures</th><th class="num">p95</th><th>Last seen</th></tr></thead>
      <tbody>${rows.map((s) => `<tr class="click" data-server="${esc(s.server)}">
        <td><strong>${esc(s.server)}</strong> <span class="mute mono">${esc(s.version)}</span></td>
        <td class="dim">${esc(s.environment)}</td><td class="num">${s.tools}</td>
        <td class="num">${num(s.calls)}</td>
        <td class="num" style="color:${s.errors ? "var(--err)" : "inherit"}">${num(s.errors)}</td>
        <td>${distBar(s.failure_breakdown)}</td><td class="num">${durq(s.latency)}</td>
        <td class="dim">${ago(s.last_seen)}</td></tr>`).join("")}</tbody>
    </table></div>` : `<div class="empty">No servers reporting in this window.</div>`;
  bindAll("[data-server]", (n) => go("capabilities", { kind: "tool", server: n.dataset.server }));
}

/* ===========================================================================
   Filter bar
   ===========================================================================
   Composition borrowed from the Untitled UI filter-bar pattern -- Root holding
   a wrapping Content region on the left and a fixed Actions region on the
   right, plus dropdown menus and query-builder rows -- rebuilt in plain JS
   because this console has no React and deliberately no build step.

   Four rules it is built to:

   1. EVERY filter runs in SQL. Narrowing a page after it is fetched means the
      list shows 3 of 80 rows and the next page starts past the 80, so results
      that match are simply never seen. The repository takes the filters and
      builds one WHERE; nothing is filtered in the browser.

   2. THE URL IS THE STATE. Every filter round-trips through the query string,
      so a narrowed view is a link you can paste to a colleague. That is also
      why the advanced rows are `where=field:op:value` triples rather than JSON
      -- a shared link should be readable.

   3. THE BAR SURVIVES A REDRAW. It lives outside #content, so the 30s
      auto-refresh and every list re-render leave the search box, its focus and
      its caret exactly where they were.

   4. AN EMPTY RESULT SAYS WHY. A filtered list that comes back empty names the
      filters responsible and offers to clear them. "No traces" when you forgot
      about a filter set ten minutes ago is how people conclude data is missing.
   =========================================================================== */

function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

function filteredEmpty(noun) {
  const active = activeFilterEntries();
  if (!active.length) return `<div class="empty">No ${noun} in this window.</div>`;
  return `<div class="empty">No ${noun} match the ${active.length} active filter${active.length > 1 ? "s" : ""}.<br>
    <span class="mute" style="font-size:11.5px">${active.map((entry) => esc(entry.text)).join(" · ")}</span><br>
    <button class="btn-ghost" id="empty-clear" type="button" style="margin-top:12px">Clear filters</button></div>`;
}

function bindFilteredEmpty() {
  const button = el("empty-clear");
  if (button) button.onclick = clearGenericFilters;
}

function setCount(shown, capped) {
  const node = el("f-count");
  if (!node) return;
  node.textContent = shown === null ? ""
    : capped ? `first ${shown}` : `${shown} result${shown === 1 ? "" : "s"}`;
}
/* Tools, prompts, resources and protocol methods share one view: they are the
   same question asked of different mcp_methods. `protocol` is the one that was
   missing entirely -- tools/list and server/discover were 38% of stored spans
   with nowhere to appear. */
const KINDS = [
  ["tool", "Tools", "tools/call"],
  ["prompt", "Prompts", "prompts/get"],
  ["resource", "Resources", "resources/read"],
  ["protocol", "Protocol", "tools/list, server/discover, subscriptions/listen …"],
];

/* Sort keys -> the phrase used in the truncation notice. Which rows got cut
   depends entirely on the sort, so the notice has to name it. */
const SORT_LABELS = {
  calls: "most called", errors: "most errors", p95: "slowest p95",
  name: "name", last_seen: "recently used",
};

async function viewCapabilities(signal) {
  const kind = S.kind || "tool";
  const p = capParams();
  p.set("kind", kind);
  const page = await api(`/capabilities?${p}`, signal);
  const rows = page.items;
  setCount(rows.length, page.truncated);
  const tabs = KINDS.map(([k, label]) =>
    `<button class="tab ${k === kind ? "on" : ""}" data-kind="${k}">${label}</button>`).join("");
  const meta = KINDS.find(([k]) => k === kind);

  /* Capabilities are aggregated by name, so there is no time cursor to page
     through -- the bound is "the top N by whatever you sorted on". When it
     bites, SAY SO: a table that silently stops at 200 is one where somebody
     concludes a tool is not being called. The sort is named because which rows
     were dropped depends entirely on it. */
  const cut = page.truncated ? `<div class="note-bar"><span>&#9888;</span><div>
    <b>Showing the first ${num(page.cap)}.</b> More ${esc(meta[1].toLowerCase())}
    matched than fit in one table. These are the top ${num(page.cap)} by
    <em>${esc(SORT_LABELS[valuesFromUrl().sort || "calls"])}</em> — narrow with search,
    server or “at least N calls” to see the rest.</div></div>` : "";

  el("content").innerHTML = `
    <div class="tabs">${tabs}<span class="note mono">${esc(meta[2])}</span></div>
    ${cut}
    ${rows.length ? `<div class="panel"><table>
      <thead><tr><th>${meta[1].replace(/s$/, "")}</th><th>Server</th><th class="num">Calls</th>
        <th class="num">Errors</th><th style="width:150px">Failures</th><th class="num">p50</th>
        <th class="num">p95</th><th>Dominant failure</th><th>Last seen</th></tr></thead>
      <tbody>${rows.map((r) => {
        const worst = Object.entries(r.failure_breakdown)
          .filter(([k, v]) => v > 0 && k !== "ok" && k !== "pending_input").sort((a, b) => b[1] - a[1])[0];
        return `<tr class="click" data-item="${esc(r.name)}" data-method="${esc(r.method)}">
          <td><strong>${esc(r.name)}</strong></td><td class="dim">${esc(r.server)}</td>
          <td class="num">${num(r.calls)}</td>
          <td class="num" style="color:${r.errors ? "var(--err)" : "inherit"}">${num(r.errors)}</td>
          <td>${distBar(r.failure_breakdown)}</td>
          <td class="num">${dur(r.latency.p50_ms)}</td><td class="num">${durq(r.latency)}</td>
          <td>${worst ? badge(worst[0]) : '<span class="mute">—</span>'}</td>
          <td class="dim">${ago(r.last_seen)}</td></tr>`;
      }).join("")}</tbody></table></div>`
    : filteredEmpty(meta[1].toLowerCase())}`;

  bindFilteredEmpty();
  // The kind tabs reset the filters: "Server is X" carried from Tools to
  // Protocol looks like the tab is broken when X exposes no protocol spans.
  bindAll("[data-kind]", (n) => go("capabilities", { kind: n.dataset.kind }));
  bindAll("[data-item]", (n) => {
    // A capability reached through Servers is already scoped to that server.
    // Keep that shared identity filter when drilling into its traces; capability-
    // only controls (sort, minimum calls, error toggle) deliberately do not carry.
    const server = new URLSearchParams(location.search).get("server");
    go("traces", { tool: n.dataset.item, server });
  });
}

function renderTraceList(items, heading, note, noun = "traces") {
  el("content").innerHTML = items.length ? `
    <div class="panel"><header><h3>${esc(heading)}</h3><span class="note">${esc(note)}</span></header><table>
      <thead><tr><th>Trace</th><th>Tool</th><th>Method</th><th>Status</th>
        <th>Transport</th>
        <th class="num">Spans</th><th class="num">Duration</th><th>When</th></tr></thead>
      <tbody>${items.map((t) => `<tr class="click" data-trace="${esc(t.trace_id)}">
        <td class="mono dim">${esc(t.trace_id.slice(0, 16))}</td>
        <td><strong>${esc(t.tool || "—")}</strong></td>
        <td class="mono dim">${esc(t.mcp_method)}</td>
        <td>${badge(t.failure_category || "ok")}</td>
        <td>${transportTag(t.transport)}</td>
        <td class="num">${t.span_count}</td><td class="num">${dur(t.duration_ms)}</td>
        <td class="dim">${ago(t.start_time)}</td></tr>`).join("")}</tbody>
    </table></div>` : filteredEmpty(noun);
  bindFilteredEmpty();
  bindAll("[data-trace]", (n) => openTrace(n.dataset.trace));
}

/* ===========================================================================
   The drawer: trace waterfall + full span detail, over the list
   =========================================================================== */
let drawerController = null;

async function openTrace(traceId, spanId = null) {
  drawerController?.abort();
  const controller = new AbortController();
  drawerController = controller;
  S.trace = traceId;
  S.span = spanId;
  pushUrl();
  document.querySelectorAll("[data-trace]").forEach((n) =>
    n.classList.toggle("sel", n.dataset.trace === traceId));

  const shell = el("drawer");
  shell.classList.add("open");
  el("drawer-body").innerHTML = '<div class="spin">Loading…</div>';

  let t;
  try {
    t = await api(`/traces/${traceId}`, controller.signal);
  } catch (e) {
    if (e.name === "AbortError") return;
    el("drawer-body").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  }
  if (controller.signal.aborted || S.trace !== traceId) return;
  S.traceData = t;
  renderDrawer(t, spanId || t.root_span_id);
}

function renderDrawer(t, selectedId) {
  const total = Math.max(t.duration_ms, 0.001);

  const rows = t.spans.map((s) => {
    const k = kindOf(s);
    const left = Math.min(99, (s.offset_ms / total) * 100);
    // Floor the width so a sub-millisecond span stays visible. A bar you cannot
    // see reads as "did not happen", which is worse than imprecise.
    const width = Math.max(0.6, Math.min(100 - left, (s.duration_ms / total) * 100));
    // Self-time as a solid inner segment: the difference between "this is slow"
    // and "this waits on something slow", which duration alone cannot show.
    const selfPct = s.duration_ms ? Math.max(2, (s.self_ms / s.duration_ms) * 100) : 100;
    const failed = s.status === "ERROR";
    const dot = failed ? "d-err" : s.status === "OK" ? "d-ok" : "d-none";

    return `<div class="wf-row ${s.span_id === selectedId ? "sel" : ""}" data-span="${esc(s.span_id)}">
      <div class="wf-name" style="padding-left:${s.depth * 18}px">
        ${s.depth ? '<span class="rail"></span>' : ""}
        <span class="dot ${dot}"></span>
        <span class="wf-kind ${k.cls}">${k.tag}</span>
        <span class="txt" title="${esc(s.name)}">${esc(s.name)}</span>
        ${s.downstream_detail ? `<span class="fact">${esc(s.downstream_detail)}</span>` : ""}
        ${s.span_id === t.root_span_id ? '<span class="mute mono rootmark">ROOT</span>' : ""}
      </div>
      <div class="num dim">${dur(s.duration_ms)}</div>
      <div class="wf-track">
        <div class="wf-bar ${failed ? "bar-err" : k.bar}" style="left:${left}%;width:${width}%">
          <i class="self" style="width:${selfPct}%"></i>
          ${width > 9 ? `<span>${dur(s.duration_ms)}</span>` : ""}
        </div>
      </div>
    </div>`;
  }).join("");

  el("drawer-body").innerHTML = `
    <div class="dr-head">
      <div class="dr-title">
        <strong>${esc(t.tool || t.mcp_method || "Trace")}</strong>
        ${badge(t.failure_category || "ok")}
        <span class="mono mute">${esc(t.trace_id)}</span>
      </div>
      <div class="kv">
        <div><span class="k">Server</span><span class="v">${esc(t.server || "—")}</span></div>
        <div><span class="k">Method</span><span class="v">${esc(t.mcp_method || "—")}</span></div>
        <div><span class="k">Duration</span><span class="v">${dur(t.duration_ms)}</span></div>
        <div><span class="k">Spans</span><span class="v">${t.span_count}</span></div>
        <div><span class="k">Errors</span><span class="v" style="color:${t.error_count ? "var(--err)" : "inherit"}">${t.error_count}</span></div>
        <div><span class="k">Started</span><span class="v">${esc(t.start_time.replace("T", " ").slice(0, 19))}</span></div>
      </div>
    </div>
    ${t.truncated ? `<div class="note-bar"><span>&#9888;</span><div>
      <b>Showing the first ${num(t.span_cap)} spans.</b> This trace has more.
      They are the earliest ones, so the waterfall reads forwards from the start
      of the call &mdash; but the tail is missing, and a span deeper in the trace
      may have no visible parent here.</div></div>` : ""}
    ${t.detail_omitted ? `<div class="note-bar"><span>&#8505;</span><div>
      <b>Span detail is loaded on click for a trace this large.</b>
      Sending it for all ${num(t.span_count)} spans up front would be most of the
      response; one request per span you actually open is
      cheaper.</div></div>` : ""}
    <div class="wf-head">
      <div>Span</div><div class="num">Duration</div>
      <div class="axis"><span>0</span><span>${dur(total * 0.25)}</span><span>${dur(total * 0.5)}</span>
        <span>${dur(total * 0.75)}</span><span>${dur(total)}</span></div>
    </div>
    <div class="wf">${rows}</div>
    <div id="span-detail"></div>`;

  bindAll("[data-span]", (n) => {
    S.span = n.dataset.span;
    pushUrl();
    renderDrawer(S.traceData, S.span);
    el("span-detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
  showSpanDetail(t, selectedId);
}

/* Resolve one span's detail, from the bulk map or from the server.

   Large traces omit the bulk map for payload size. The first version of that
   cap traded the map for NOTHING -- no span in such a trace could be inspected,
   including the ones on screen, which is a loss of capability rather than an
   optimisation. One request per click restores it, and the result is cached so
   clicking back and forth costs one round trip per span.

   The bulk map is still used when present: on an ordinary trace this makes no
   request at all. */
const spanDetailCache = new Map();

async function showSpanDetail(t, spanId) {
  if (!spanId) { renderSpanDetail(null); return; }

  const inline = t.detail?.[spanId];
  if (inline) { renderSpanDetail(inline); return; }

  const key = `${t.trace_id}/${spanId}`;
  if (spanDetailCache.has(key)) { renderSpanDetail(spanDetailCache.get(key)); return; }

  el("span-detail").innerHTML = '<div class="spin">Loading span…</div>';
  try {
    const d = await api(`/traces/${encodeURIComponent(t.trace_id)}/spans/${encodeURIComponent(spanId)}`);
    spanDetailCache.set(key, d);
    // The selection may have moved while this was in flight; rendering the
    // stale one would silently show the wrong span's fields.
    if (S.span === spanId || !S.span) renderSpanDetail(d);
  } catch (e) {
    el("span-detail").innerHTML =
      `<div class="empty" style="padding:24px">Could not load this span.<br>
       <span class="mono" style="font-size:11px">${esc(e.message)}</span></div>`;
  }
}

/* Everything we hold about one span. Nothing omitted for being uninteresting:
   the console previously showed 17 of 55 columns, and the dropped ones were
   exactly what you need when something is wrong. */
function renderSpanDetail(d) {
  if (!d) {
    // Blank is ambiguous between "nothing selected" and "detail suppressed for
    // size". Only the second needs explaining, and leaving it blank makes the
    // panel look broken on exactly the traces that are hardest to debug.
    el("span-detail").innerHTML = "";
    return;
  }

  const row = (k, v, cls = "") => (v === null || v === undefined || v === "" || v === -1)
    ? "" : `<div class="f"><span class="fk">${k}</span><span class="fv ${cls}">${esc(v)}</span></div>`;

  const group = (title, body, extra = "") =>
    body.trim() ? `<div class="grp"><h4>${title}${extra}</h4><div class="fields">${body}</div></div>` : "";

  const map = (title, obj, note = "") => {
    const keys = Object.keys(obj || {});
    if (!keys.length) return "";
    return `<div class="grp"><h4>${title} <span class="mute">${keys.length}</span>
      ${note ? `<span class="note">${note}</span>` : ""}</h4>
      <div class="fields mono">${keys.sort().map((k) =>
        `<div class="f"><span class="fk">${esc(k)}</span><span class="fv">${esc(obj[k])}</span></div>`).join("")}</div></div>`;
  };

  // The error text, given its own block: it is the single thing an operator
  // opens a failing span to read, and burying it in a field list would be
  // pretending it is as important as `transport`.
  const errorBlock = d.failure_detail ? `
    <div class="err-block">
      <div class="err-head">Error detail
        <span class="note">captured from the tool result · off via capture_error_detail=False</span></div>
      <pre>${esc(d.failure_detail)}</pre>
    </div>` : "";

  // Request and response, high in the panel: it is the first thing anyone
  // opens a span to read. When capture is off, say so explicitly with the flag
  // that turns it on -- an empty space just looks broken.
  const io = (label, text, size) => text
    ? `<div class="io"><div class="io-head">${label}
         <span class="note mono">${size ? `${size} chars` : ""}${
           size && text.length < size ? " · truncated" : ""}</span></div>
       <pre>${esc(text)}</pre></div>`
    : "";

  // Request/Response means something different per span kind, and rendering
  // only the MCP shape is why a downstream call looked empty. An HTTP span's
  // request IS its URL; a DB span's request IS its statement.
  let reqLabel = "Request", respLabel = "Response";
  let request = d.input_preview, response = d.output_preview;
  let reqSize = d.input_size, respSize = d.output_size;
  let footnote = "redacted and truncated at capture";

  if (!d.mcp_method) {
    reqSize = respSize = null;
    if (d.downstream_kind === "http") {
      reqLabel = "Request"; respLabel = "Response";
      // The request line, then the body under it. Showing the body alone would
      // lose which URL produced it; showing the URL alone was the old D60 gap.
      const reqLine = [d.http_method, d.http_url || d.http_host].filter(Boolean).join(" ");
      const respLine = d.http_status_code ? `HTTP ${d.http_status_code}` : "";
      request = [reqLine, d.http_request_headers, d.http_request_body]
        .filter(Boolean).join("\n\n");
      // No response body, and the reason is worth stating rather than leaving a
      // blank: the OTel client span ends when the transport returns, and httpx
      // reads the body after that.
      response = [respLine, d.http_response_headers].filter(Boolean).join("\n\n");
      footnote = d.http_request_headers
        ? "captured by instrument_httpx() \u00b7 redacted and truncated, credential headers never read \u00b7 no response body: the client span ends before httpx reads one"
        : "request body and headers not captured \u2014 call instrument_httpx() in your server";
    } else if (d.downstream_kind === "db") {
      reqLabel = "Statement"; respLabel = "";
      request = d.db_statement;
      response = "";
      footnote = "literals redacted; placeholders preserved";
    } else if (d.downstream_kind === "messaging") {
      /* Classified correctly since U6 and rendered as a grey tag with nothing
         in it, because no messaging attribute was ever promoted to a column.
         A publish's "request" IS its destination and operation. */
      reqLabel = "Publish"; respLabel = "";
      request = [d.messaging_operation, d.messaging_system, d.messaging_destination]
        .filter(Boolean).join(" · ");
      response = "";
      footnote = "message bodies are not captured — a queue payload is the "
        + "customer's data, and nothing here opts into it";
    } else if (d.downstream_kind === "llm") {
      reqLabel = "Request"; respLabel = "Response";
      request = [d.gen_ai_system, d.gen_ai_model,
        d.gen_ai_input_tokens ? `${d.gen_ai_input_tokens} input tokens` : ""]
        .filter(Boolean).join(" · ");
      response = d.gen_ai_output_tokens ? `${d.gen_ai_output_tokens} output tokens` : "";
      footnote = "prompt and completion are not recorded by the LLM instrumentation";
    }
  }

  const payloadBlock = (request || response)
    ? `<div class="grp"><h4>${d.mcp_method ? "Request / Response" : "Downstream call"}
         <span class="note">${esc(footnote)}</span></h4>
       ${io(reqLabel, request, reqSize)}
       ${respLabel ? io(respLabel, response, respSize) : ""}</div>`
    : (d.mcp_method === "tools/call"
      ? `<div class="grp"><h4>Request / Response</h4>
         <div class="io-off">Not captured. Payload capture is off by default —
         it records every argument and every result, not just failures.
         Enable with <code>instrument(mcp, capture_payloads=True)</code>.</div></div>`
      : "");

  el("span-detail").innerHTML = `
    <div class="detail">
      <div class="detail-head">
        <strong>${esc(d.name)}</strong>
        ${d.failure_category ? badge(d.failure_category) : ""}
        <span class="mono mute">${esc(d.span_id)}</span>
      </div>
      ${errorBlock}
      ${payloadBlock}
      ${group("Timing", [
        row("duration", dur(d.duration_ms)),
        row("self time", dur(d.self_ms)),
        row("offset", dur(d.offset_ms)),
        row("% of trace", `${d.pct_of_trace}%`),
        row("started", d.start_time.replace("T", " ").slice(0, 23)),
        row("latency eligible", d.is_latency_eligible ? "yes" : "no — stream or interim round"),
      ].join(""))}
      ${group("Status", [
        row("status", d.status, d.status === "ERROR" ? "bad" : ""),
        row("status message", d.status_message),
        row("category", d.failure_category),
        row("classified by", d.failure_kind_source === "helper"
          ? "mcpobs helper (precise)" : d.failure_kind_source === "span" ? "raw span (coarse)" : ""),
        row("classifier version", d.classifier_version || ""),
        row("error.type", d.error_type),
        row("rpc status", d.rpc_status_code),
      ].join(""))}
      ${group("MCP", [
        row("method", d.mcp_method), row("tool", d.tool), row("prompt", d.prompt),
        row("resource", d.resource_uri), row("operation", d.gen_ai_operation),
        row("protocol", d.protocol_version), row("jsonrpc id", d.jsonrpc_request_id),
        row("result type", d.result_type), row("transport", d.transport),
        row("session", d.session_id),
        // Self-reported by the client and unverified, exactly as the spec warns.
        // Labelled so nobody reads it as an identity the server authenticated.
        row("client", [d.client_name, d.client_version].filter(Boolean).join(" ")
          + (d.client_name ? " · self-reported" : "")),
        row("mrtr in", d.mrtr_state_in), row("mrtr out", d.mrtr_state_out),
      ].join(""))}
      ${group("Downstream", [
        row("kind", d.downstream_kind),
        row("http", [d.http_method, d.http_status_code, d.http_host].filter(Boolean).join(" ")),
        row("db", [d.db_system, d.db_operation, d.db_collection].filter(Boolean).join(" ")),
        row("queue", [d.messaging_system, d.messaging_operation, d.messaging_destination]
          .filter(Boolean).join(" ")),
        row("llm", [d.gen_ai_system, d.gen_ai_model].filter(Boolean).join(" ")),
        row("tokens", d.gen_ai_input_tokens || d.gen_ai_output_tokens
          ? `${d.gen_ai_input_tokens ?? 0} in → ${d.gen_ai_output_tokens ?? 0} out` : ""),
      ].join(""))}
      ${group("Service", [
        row("name", d.service_name), row("version", d.service_version),
        row("environment", d.environment), row("instance", d.service_instance),
      ].join(""))}
      ${map("Span attributes", d.span_attributes, "raw, as the SDK emitted them")}
      ${map("Resource attributes", d.resource_attributes)}
      ${group("Provenance", [
        row("normalization", `v${d.normalization_version}`),
        row("kafka", `partition ${d.kafka_partition} · offset ${d.kafka_offset}`),
        row("ingested", d.ingested_at ? d.ingested_at.replace("T", " ").slice(0, 19) : ""),
        row("freshness", dur(d.freshness_ms)),
      ].join(""), '<span class="note">which message produced this row, and which code wrote it</span>')}
    </div>`;
}

function closeDrawer() {
  drawerController?.abort();
  drawerController = null;
  el("drawer").classList.remove("open");
  S.trace = null; S.span = null;
  document.querySelectorAll(".sel").forEach((n) => n.classList.remove("sel"));
  pushUrl();
}

/* ===========================================================================
   Router
   =========================================================================== */
const VIEWS = {
  overview:     { t: "Overview",     f: viewOverview },
  servers:      { t: "Servers",      f: viewServers },
  capabilities: { t: "Capabilities", f: viewCapabilities },
  traces:       { t: "Traces",       f: viewTraces },
  errors:       { t: "Errors",       f: viewErrors },
};

function bindAll(sel, fn) {
  // The event is passed through: a chip's remove button sits inside a row that
  // has its own click handler, so it needs stopPropagation.
  document.querySelectorAll(sel).forEach((n) => (n.onclick = (ev) => fn(n, ev)));
}

/* The URL carries the filters, so a narrowed view is a link. The parameter
   names are EXACTLY the API's, so the address bar and the request agree and
   there is no third naming scheme to keep in step. */
function pushUrl() {
  const q = new URLSearchParams(location.search);
  q.set("view", S.view);
  q.set("w", S.window);
  if (S.view === "capabilities") q.set("kind", S.kind);
  else q.delete("kind");
  if (S.trace) q.set("trace", S.trace);
  else q.delete("trace");
  if (S.span) q.set("span", S.span); else q.delete("span");
  history.replaceState(null, "", `?${q}`);
}

/** Navigate, resetting filters unless the caller passes some.
 *
 *  Filters reset on navigation because they belong to the list you set them
 *  on. Carrying "Server is X" from Tools into Errors gives an empty error list
 *  for a reason that is no longer on screen -- and an empty error view is the
 *  single most dangerous thing this console can show incorrectly.
 *
 *  Drill-through is the exception: clicking a tool means "traces for this
 *  tool", so it lands with exactly that filter set and visible as a chip.
 */
function go(view, params = {}) {
  closeFilterPanel();
  pageCursors = [];
  invalidateFilterCatalog();
  advancedRows = null;
  S.view = view;
  const { tool, server, failure_category, kind } = params;
  if (kind) S.kind = kind;
  closeDrawerSilently();
  const q = new URLSearchParams({ view, w: String(S.window) });
  if (view === "capabilities") q.set("kind", S.kind);
  for (const [key, value] of Object.entries({ tool, server, failure_category })) {
    if (value) q.set(key, value);
  }
  history.replaceState(null, "", `?${q}`);
  renderFilterBar();
  render();
}
function closeDrawerSilently() {
  drawerController?.abort();
  drawerController = null;
  el("drawer").classList.remove("open");
  S.trace = null;
  S.span = null;
}

/* What the content pane is currently showing. A REFRESH of the same thing must
   not look like a navigation to a different one.
 *
 * Filters are deliberately NOT part of this key. Narrowing a list is a refine,
 * not a navigation: blanking the pane to a spinner on every keystroke of a
 * debounced search is the flicker we just spent a commit removing, in a place
 * where it would fire far more often. */
const viewKey = () => [S.view, S.kind, S.window].join("|");
let renderedKey = null;
let renderController = null;

async function render() {
  renderController?.abort();
  const controller = new AbortController();
  renderController = controller;
  const v = VIEWS[S.view] || VIEWS.overview;
  el("title").textContent = S.view === "capabilities"
    ? (KINDS.find(([k]) => k === S.kind)?.[1] ?? "Capabilities") : v.t;
  document.querySelectorAll("#nav a").forEach((a) =>
    a.classList.toggle("on", a.dataset.view === S.view && (!a.dataset.kind || a.dataset.kind === S.kind)));

  /* THE FLICKER. This blanked the pane to a spinner on EVERY render, including
     the auto-refresh -- so a dashboard nobody had touched went white and
     repainted four times a minute. A spinner answers "your click did
     something"; on a background refresh there was no click, so it answered a
     question nobody asked and destroyed the thing being read to do it.

     Shown only when the view is actually CHANGING, or on first load. */
  const refreshing = renderedKey === viewKey() && el("content").children.length > 0;
  if (!refreshing) el("content").innerHTML = '<div class="spin">Loading…</div>';

  /* `.content` is the scroll container, so replacing its innerHTML snaps back
     to the top. On a navigation that is right; on a refresh it yanks the page
     out from under someone reading row forty. */
  const scrollTop = refreshing ? el("content").scrollTop : 0;

  try {
    if (filterView() && (!filterCatalog || filterCatalogView !== filterView()
        || filterCatalogWindow !== S.window)) {
      loadOptions()
        .then((catalog) => { if (catalog && !controller.signal.aborted) renderFilterBar(); })
        .catch(() => { if (!controller.signal.aborted) el("filters").innerHTML = ""; });
    }
    await v.f(controller.signal);
    if (controller.signal.aborted) return;
    if (scrollTop) el("content").scrollTop = scrollTop;
    renderedKey = viewKey();
    // Stamped AFTER the fetch succeeds. Stamping before it would report
    // "updated just now" over data that failed to load -- a staleness
    // indicator that lies is worse than none, because it is believed.
    lastRendered = Date.now();
    el("health-dot").style.background = "var(--ok)";
    if (S.trace) openTrace(S.trace, S.span);
  } catch (e) {
    if (e.name === "AbortError") return;
    el("health-dot").style.background = "var(--err)";
    // A failed BACKGROUND refresh keeps what is on screen rather than replacing
    // good data with an error. The health dot going red is the signal; wiping
    // the pane would punish the reader for a transient nobody triggered.
    if (!refreshing) {
      el("content").innerHTML =
        `<div class="empty">Could not load.<br><span class="mono" style="font-size:11px">${esc(e.message)}</span></div>`;
    }
  }
}

document.querySelectorAll("#nav a").forEach((a) =>
  (a.onclick = () => go(a.dataset.view, a.dataset.kind ? { kind: a.dataset.kind } : {})));

el("range").onclick = (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  S.window = +b.dataset.m;
  document.querySelectorAll("#range button").forEach((x) => x.classList.toggle("on", x === b));
  pageCursors = [];
  advancedRows = null;
  invalidateFilterCatalog();
  pushUrl();
  renderFilterBar();
  render();
};

el("drawer-close").onclick = closeDrawer;
el("scrim").onclick = closeDrawer;
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (filterPanelOpen) closeFilterPanel();
  else closeDrawer();
});

/* ===========================================================================
   Auto-refresh
   ===========================================================================
   Three rules, each removing waste that was real:

   1. NOT WHILE A TAB IS HIDDEN. The old timer refetched 80 trace summaries and
      re-ran the rollup for tabs nobody was looking at -- which is most open
      tabs, most of the time. This is the single biggest saving and costs one
      event listener.

   2. NOT WHILE A DRAWER IS OPEN. Redrawing under someone mid-investigation is
      hostile, and it discards the span they had selected.

   3. ONLY ON MONITORING VIEWS. Overview and Servers answer "is it healthy
      NOW". Traces and Errors are investigation surfaces -- the ground should
      not move while you read them, and a list that reorders under the cursor
      is worse than a stale one.

   The interval stays well clear of the ~5s ingest floor: refreshing faster than
   data can arrive only makes the page flicker with identical numbers. At 30s it
   matches the admin console, and the footer's live timestamp ticks every second
   regardless, so the age of what you are reading is never in doubt between
   refreshes. */
const REFRESH_MS = 30000;
const LIVE_VIEWS = new Set(["overview", "servers"]);
let refreshTimer = null;
let lastRendered = 0;

function shouldRefresh() {
  return document.visibilityState === "visible" && !S.trace && LIVE_VIEWS.has(S.view);
}

function startAutoRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(() => { if (shouldRefresh()) render(); }, REFRESH_MS);

  // Coming back to a hidden tab, refresh ONCE immediately rather than waiting
  // out the interval -- otherwise the first thing a returning user sees is a
  // full interval stale while claiming to be live. This matters more at 30s
  // than it did at 15s.
  document.addEventListener("visibilitychange", () => {
    if (shouldRefresh() && Date.now() - lastRendered > REFRESH_MS) render();
  });

  // Staleness is SHOWN, not assumed. A number with no timestamp beside it is a
  // number someone will quote as current -- and on the views that no longer
  // auto-refresh, it is the only thing telling them it is not.
  setInterval(() => {
    const stamp = el("foot-updated");
    const dot = el("live-dot");
    if (!stamp || !lastRendered) return;
    const seconds = Math.round((Date.now() - lastRendered) / 1000);
    const age = seconds < 5 ? "just now" : seconds < 60 ? `${seconds}s ago`
      : `${Math.floor(seconds / 60)}m ago`;

    /* Says WHY it is not updating, not just that it is not. "paused" alone
       reads as a fault; the reason turns it into something the reader can act
       on -- or correctly ignore. */
    if (shouldRefresh()) {
      stamp.textContent = `Live · ${age}`;
      // Derived from the constant rather than written out, so the tooltip
      // cannot drift from the interval it describes.
      stamp.title = `This view refreshes automatically every ${REFRESH_MS / 1000} seconds.`;
      dot?.classList.remove("paused");
    } else {
      const why = document.visibilityState !== "visible" ? "tab in background"
        : S.trace ? "trace open"
        : "click Refresh to update";
      stamp.textContent = `Paused · ${age}`;
      stamp.title = `Not refreshing: ${why}.`;
      dot?.classList.add("paused");
    }
  }, 1000);
}

/* ===========================================================================
   API-described filter panel and cursor pagination

   The earlier filter-bar experiment was intentionally replaced rather than
   extended: the browser now knows only control *kinds*. Labels, fields,
   sections, help text and options come from /filters. Adding a select, search,
   toggle or number filter is a single query/filters.py entry.
   =========================================================================== */
let filterCatalog = null;
let filterCatalogView = null;
let filterCatalogWindow = null;
let filterPanelOpen = false;
let pageCursors = [];
let catalogController = null;
let catalogGeneration = 0;
let advancedRows = null;
let filterReturnFocus = null;

function filterView() {
  return ["traces", "errors", "capabilities"].includes(S.view) ? S.view : null;
}

async function loadOptions() {
  const view = filterView();
  if (!view) return null;
  if (filterCatalog && filterCatalogView === view && filterCatalogWindow === S.window) return filterCatalog;
  catalogController?.abort();
  const controller = new AbortController();
  catalogController = controller;
  const generation = ++catalogGeneration;
  const windowAtStart = S.window;
  const catalog = await api(`/filters?view=${encodeURIComponent(view)}`, controller.signal);
  if (controller.signal.aborted || generation !== catalogGeneration
      || view !== filterView() || windowAtStart !== S.window) return null;
  filterCatalog = catalog;
  filterCatalogView = view;
  filterCatalogWindow = windowAtStart;
  return filterCatalog;
}

function invalidateFilterCatalog() {
  catalogController?.abort();
  catalogController = null;
  catalogGeneration += 1;
  filterCatalog = null;
  filterCatalogView = null;
  filterCatalogWindow = null;
}

function valuesFromUrl() {
  const p = new URLSearchParams(location.search);
  const values = {};
  for (const group of filterCatalog?.groups || []) {
    for (const spec of group.filters) {
      if (p.has(spec.key)) values[spec.key] = p.get(spec.key);
    }
  }
  return values;
}

function genericParams() {
  const p = new URLSearchParams(location.search);
  for (const key of ["view", "w", "kind", "trace", "span", "cursor"]) p.delete(key);
  return p;
}

function traceParams() { return genericParams(); }
function capParams() { return genericParams(); }

function renderFilterBar() {
  const host = el("filters");
  const view = filterView();
  if (!view) { host.innerHTML = ""; return; }
  if (!filterCatalog || filterCatalogView !== view) {
    host.innerHTML = '<div class="filter-loading">Loading filters…</div>';
    return;
  }
  const values = valuesFromUrl();
  const active = activeFilterEntries(values);
  const search = allFilterSpecs().find((spec) => spec.kind === "search" && spec.pinned);
  host.innerHTML = `<div class="filter-summary">
    ${search ? `<input id="generic-search" type="search" value="${esc(values[search.key] || "")}" placeholder="${esc(search.placeholder || search.label)}" aria-label="${esc(search.label)}">` : ""}
    <button class="btn-ghost ${active.length ? "on" : ""}" id="open-filters" type="button" aria-expanded="${filterPanelOpen}">
      Filters${active.length ? ` (${active.length})` : ""}
    </button>
    ${active.map((entry) => `<span class="chip">${esc(entry.text)}<button type="button" data-clear-filter="${esc(entry.key)}" aria-label="Clear ${esc(entry.text)}">&times;</button></span>`).join("")}
    ${active.length ? '<button class="chip-clear" id="clear-filters" type="button">Clear all</button>' : ""}
  </div>`;
  el("open-filters").onclick = openFilterPanel;
  el("clear-filters")?.addEventListener("click", clearGenericFilters);
  document.querySelectorAll("[data-clear-filter]").forEach((node) => node.onclick = () => setGenericFilter(node.dataset.clearFilter, ""));
  const input = el("generic-search");
  if (input) input.oninput = debounce(() => setGenericFilter(search.key, input.value.trim()), 300);
  if (filterPanelOpen) renderFilterPanel();
}

function allFilterSpecs() { return (filterCatalog?.groups || []).flatMap((group) => group.filters); }
function readAdvancedRows() {
  return new URLSearchParams(location.search).getAll("where").map((raw) => {
    const [field, op, ...value] = raw.split(":");
    return { field, op: op || "is", value: value.join(":") };
  });
}

function activeFilterEntries(values = valuesFromUrl()) {
  const entries = allFilterSpecs()
    .filter((spec) => spec.show_chip && values[spec.key] && values[spec.key] !== "false")
    .map((spec) => ({ ...spec, text: displayFilter(spec, values[spec.key]) }));
  const fields = new Map((filterCatalog?.advanced?.fields || []).map((field) => [field.value, field.label]));
  const operators = new Map((filterCatalog?.advanced?.operators || []).map((op) => [op.value, op.label]));
  readAdvancedRows().forEach((row, index) => {
    if (fields.has(row.field) && operators.has(row.op) && row.value.trim()) {
      entries.push({
        key: `where:${index}`,
        text: `${fields.get(row.field)} ${operators.get(row.op)} ${row.value}`,
      });
    }
  });
  return entries;
}
function displayFilter(spec, value) {
  if (spec.kind === "toggle") return spec.label;
  const option = spec.options?.find((item) => item.value === value);
  return spec.kind === "search" ? `matching “${value}”` : `${spec.label}: ${option?.label || value}`;
}

function renderFilterPanel() {
  let panel = el("filter-panel");
  if (!panel) {
    panel = document.createElement("aside");
    panel.id = "filter-panel";
    panel.className = "filter-panel";
    panel.setAttribute("aria-label", "Filters");
    panel.setAttribute("aria-hidden", "true");
    panel.inert = true;
    document.body.append(panel);
  }
  if (advancedRows === null) advancedRows = readAdvancedRows();
  const values = valuesFromUrl();
  panel.innerHTML = `<div class="filter-panel-head"><div><strong>Filters</strong><span>Refine this ${esc(filterCatalog.view)} view</span></div><button id="close-filter-panel" type="button" aria-label="Close filters">×</button></div>
    <div class="filter-panel-body">${filterCatalog.groups.map((group) => `<section class="filter-group"><h4>${esc(group.name)}</h4>${group.filters.map((spec) => genericControl(spec, values[spec.key])).join("")}</section>`).join("")}</div>
    <div class="filter-panel-foot"><button class="chip-clear" id="panel-clear" type="button">Clear all</button><button class="btn-ghost" id="panel-done" type="button">Done</button></div>`;
  panel.querySelector(".filter-panel-body").insertAdjacentHTML("beforeend", advancedPanel());
  panel.inert = false;
  panel.setAttribute("aria-hidden", "false");
  panel.classList.add("open");
  el("close-filter-panel").onclick = closeFilterPanel;
  el("panel-done").onclick = closeFilterPanel;
  el("panel-clear").onclick = clearGenericFilters;
  panel.querySelectorAll("[data-generic-filter]").forEach((node) => {
    const spec = allFilterSpecs().find((item) => item.key === node.dataset.genericFilter);
    node.addEventListener("change", () => setGenericFilter(spec.key, spec.kind === "toggle" ? String(node.checked) : node.value));
  });
  bindAdvancedPanel(panel);
}

function genericControl(spec, value) {
  const help = spec.help ? `<small>${esc(spec.help)}</small>` : "";
  if (spec.kind === "toggle") return `<label class="generic-toggle"><input data-generic-filter="${esc(spec.key)}" type="checkbox" ${value === "true" ? "checked" : ""}><span>${esc(spec.label)}</span>${help}</label>`;
  if (spec.kind === "select") return `<label class="generic-control"><span>${esc(spec.label)}</span><select data-generic-filter="${esc(spec.key)}"><option value="">Any ${esc(spec.label.toLowerCase())}</option>${(spec.options || []).filter((option) => option.value !== "").map((option) => `<option value="${esc(option.value)}" ${option.value === value ? "selected" : ""}>${esc(option.label)}</option>`).join("")}</select>${help}</label>`;
  const type = spec.kind === "number" ? "number" : "search";
  const bounds = spec.kind === "number"
    ? ` min="${esc(spec.minimum)}"${spec.maximum == null ? "" : ` max="${esc(spec.maximum)}"`}` : "";
  return `<label class="generic-control"><span>${esc(spec.label)}</span><input data-generic-filter="${esc(spec.key)}" type="${type}"${bounds} value="${esc(value || "")}" placeholder="${esc(spec.placeholder || "")}">${help}</label>`;
}

function advancedPanel() {
  const advanced = filterCatalog?.advanced;
  if (!advanced?.fields?.length) return "";
  const rows = advancedRows.map((row, index) => `<div class="generic-advanced-row" data-advanced-row="${index}">
    <select data-advanced-part="field" aria-label="Filter field">${advanced.fields.map((field) => `<option value="${esc(field.value)}" ${field.value === row.field ? "selected" : ""}>${esc(field.label)}</option>`).join("")}</select>
    <select data-advanced-part="op" aria-label="Filter operator">${advanced.operators.map((operator) => `<option value="${esc(operator.value)}" ${operator.value === row.op ? "selected" : ""}>${esc(operator.label)}</option>`).join("")}</select>
    <input data-advanced-part="value" value="${esc(row.value)}" placeholder="Value" aria-label="Filter value">
    <button type="button" data-remove-advanced="${index}" aria-label="Remove condition">&times;</button>
  </div>`).join("");
  return `<section class="filter-group generic-advanced"><h4>Advanced</h4>
    ${rows || '<p class="generic-empty">Add a field condition when the standard controls are not specific enough.</p>'}
    <button class="btn-ghost" id="add-advanced" type="button" ${advancedRows.length >= advanced.max ? "disabled" : ""}>Add condition</button>
  </section>`;
}

function bindAdvancedPanel(panel) {
  panel.querySelectorAll("[data-advanced-row]").forEach((row) => {
    const index = Number(row.dataset.advancedRow);
    const targetRow = advancedRows[index];
    row.querySelectorAll("[data-advanced-part]").forEach((node) => {
      const update = () => {
        if (!advancedRows?.includes(targetRow)) return;
        targetRow[node.dataset.advancedPart] = node.value;
        commitAdvancedRows();
      };
      node.addEventListener(node.tagName === "INPUT" ? "input" : "change",
        node.tagName === "INPUT" ? debounce(update, 300) : update);
    });
  });
  panel.querySelectorAll("[data-remove-advanced]").forEach((button) => {
    button.onclick = () => {
      advancedRows.splice(Number(button.dataset.removeAdvanced), 1);
      commitAdvancedRows();
      renderFilterPanel();
    };
  });
  el("add-advanced")?.addEventListener("click", () => {
    const field = filterCatalog.advanced.fields[0]?.value;
    const op = filterCatalog.advanced.operators[0]?.value;
    if (!field || !op || advancedRows.length >= filterCatalog.advanced.max) return;
    advancedRows.push({ field, op, value: "" });
    renderFilterPanel();
    el("filter-panel").querySelector("[data-advanced-row]:last-of-type input")?.focus();
  });
}

function commitAdvancedRows() {
  const p = new URLSearchParams(location.search);
  p.delete("where");
  for (const row of advancedRows) {
    if (row.field && row.op && row.value.trim()) {
      p.append("where", `${row.field}:${row.op}:${row.value.trim()}`);
    }
  }
  p.delete("cursor");
  history.replaceState(null, "", `?${p}`);
  pageCursors = [];
  renderFilterBar();
  render();
}

function openFilterPanel() {
  filterReturnFocus = document.activeElement;
  filterPanelOpen = true;
  renderFilterPanel();
  el("close-filter-panel")?.focus();
}

function closeFilterPanel() {
  filterPanelOpen = false;
  const panel = el("filter-panel");
  panel?.classList.remove("open");
  if (panel) {
    panel.inert = true;
    panel.setAttribute("aria-hidden", "true");
  }
  renderFilterBar();
  /* `isConnected` alone is too weak a guard: document.body is always connected,
     so whenever the panel was opened without focus landing on a real control --
     a mouse click in browsers that do not focus buttons, or any programmatic
     open -- the captured "previous focus" WAS the body, the guard passed, and
     focus was restored to nothing. Verified: the focus call fired, with BODY as
     its target.

     Restoring focus to the body is never what anyone wanted. Fall back to the
     trigger unless the captured element is a genuinely focusable control. */
  const restorable =
    filterReturnFocus?.isConnected &&
    filterReturnFocus !== document.body &&
    filterReturnFocus !== document.documentElement &&
    typeof filterReturnFocus.focus === "function";
  const target = restorable ? filterReturnFocus : el("open-filters");
  target?.focus();
  filterReturnFocus = null;
}

function setGenericFilter(key, value) {
  const p = new URLSearchParams(location.search);
  if (key.startsWith("where:")) {
    const rows = readAdvancedRows();
    rows.splice(Number(key.slice(6)), 1);
    advancedRows = rows;
    p.delete("where");
    rows.forEach((row) => p.append("where", `${row.field}:${row.op}:${row.value}`));
  } else if (!value || value === "false") p.delete(key); else p.set(key, value);
  p.delete("cursor");
  history.replaceState(null, "", `?${p}`);
  pageCursors = [];
  renderFilterBar();
  render();
}

function clearGenericFilters() {
  const p = new URLSearchParams(location.search);
  allFilterSpecs().forEach((spec) => p.delete(spec.key));
  p.delete("where"); p.delete("cursor");
  advancedRows = [];
  history.replaceState(null, "", `?${p}`);
  pageCursors = [];
  renderFilterBar();
  render();
}

function pagination(page) {
  if (!page.next_cursor && !pageCursors.length) return "";
  return `<nav class="pagination" aria-label="Trace pages"><button class="btn-ghost" id="page-prev" type="button" ${pageCursors.length ? "" : "disabled"}>Previous</button><span>Page ${pageCursors.length + 1}</span><button class="btn-ghost" id="page-next" type="button" ${page.next_cursor ? "" : "disabled"}>Next</button></nav>`;
}

function bindPagination(page) {
  el("page-prev")?.addEventListener("click", () => { pageCursors.pop(); render(); });
  el("page-next")?.addEventListener("click", () => { if (page.next_cursor) { pageCursors.push(page.next_cursor); render(); } });
}

async function viewTraces(signal) {
  const p = traceParams(); p.set("limit", "80");
  if (pageCursors.at(-1)) p.set("cursor", pageCursors.at(-1));
  const page = await api(`/traces?${p}`, signal);
  setCount(page.items.length, !!page.next_cursor);
  renderTraceList(page.items, "Recent traces", "newest first · click a row to open it alongside", "traces");
  el("content").insertAdjacentHTML("beforeend", pagination(page)); bindPagination(page);
}

async function viewErrors(signal) {
  const p = traceParams(); p.set("limit", "80");
  if (pageCursors.at(-1)) p.set("cursor", pageCursors.at(-1));
  const page = await api(`/errors?${p}`, signal);
  setCount(page.items.length, !!page.next_cursor);
  renderTraceList(page.items, "Failing traces", "awaiting-input rounds are not failures and are excluded", "failing traces");
  el("content").insertAdjacentHTML("beforeend", pagination(page)); bindPagination(page);
}

function restoreRoute() {
  const p = new URLSearchParams(location.search);
  S.view = p.get("view") || "overview";
  S.kind = p.get("kind") || "tool";
  S.trace = p.get("trace");
  S.span = p.get("span");
  const windowMinutes = Number(p.get("w"));
  if (windowMinutes > 0) S.window = windowMinutes;
  document.querySelectorAll("#range button").forEach((button) =>
    button.classList.toggle("on", Number(button.dataset.m) === S.window));
}

window.addEventListener("popstate", () => {
  closeFilterPanel();
  closeDrawerSilently();
  pageCursors = [];
  advancedRows = null;
  invalidateFilterCatalog();
  restoreRoute();
  renderFilterBar();
  render();
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Tab" || !filterPanelOpen) return;
  const panel = el("filter-panel");
  const focusable = [...panel.querySelectorAll("button:not(:disabled), input:not(:disabled), select:not(:disabled)")];
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

(function boot() {
  if (!readKey()) { signIn(false); return; }
  restoreRoute();
  renderFilterBar();
  render();
  startAutoRefresh();
  el("sign-out").onclick = signOut;
  el("refresh-now").onclick = () => render();
})();
