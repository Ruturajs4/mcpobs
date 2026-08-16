/* ===========================================================================
   MCP Observability console.

   Split out of index.html once the drawer and span detail arrived: a single
   file was fine for four tables, not for a debugging surface.

   The organising idea: a trace opens in a RIGHT-HAND DRAWER over the list
   rather than a new page. Debugging is comparison -- you bounce between traces
   -- and navigating away loses the list you were working through.
   =========================================================================== */

const S = { view: "overview", window: 60, trace: null, span: null, kind: "tool", tool: null };

/* The API key, held in localStorage. A cookie would ride along automatically on
   every request the browser makes to this origin, which is what makes CSRF
   possible; an explicit header cannot be sent by a form on someone else's
   page. */
const KEY_STORAGE = "mcpobs.key";
const readKey = () => localStorage.getItem(KEY_STORAGE) || "";

async function api(path) {
  const sep = path.includes("?") ? "&" : "?";
  const r = await fetch(`/api/v1${path}${sep}window_minutes=${S.window}`, {
    headers: readKey() ? { "x-api-key": readKey() } : {},
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
        <p class="signin-hint">Invite-only. An operator issues keys with
          <code>python scripts/admin.py key --org &lt;org&gt; --scopes read</code>.
          Locally, <code>make devkeys</code> writes one to
          <code>.mcpobs-keys.env</code>.</p>
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
async function viewOverview() {
  const o = await api("/overview");
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

  el("foot-fresh").textContent = `freshness ${o.freshness_p95_seconds.toFixed(1)}s`;
  el("foot-class").textContent = `classified ${Math.round(o.classified_ratio * 100)}%`;
  bindAll("[data-cat]", (n) => go("errors", { cat: n.dataset.cat }));
}

async function viewServers() {
  const rows = await api("/servers");
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

async function viewCapabilities() {
  const kind = S.kind || "tool";
  const rows = await api(`/capabilities?kind=${kind}${S.server ? `&server=${encodeURIComponent(S.server)}` : ""}`);
  const tabs = KINDS.map(([k, label]) =>
    `<button class="tab ${k === kind ? "on" : ""}" data-kind="${k}">${label}</button>`).join("");
  const meta = KINDS.find(([k]) => k === kind);

  el("content").innerHTML = `
    <div class="tabs">${tabs}<span class="note mono">${esc(meta[2])}</span></div>
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
    : `<div class="empty">No ${meta[1].toLowerCase()} in this window.</div>`}`;

  bindAll("[data-kind]", (n) => go("capabilities", { kind: n.dataset.kind, server: S.server }));
  bindAll("[data-item]", (n) => go("traces", { tool: n.dataset.item }));
}

async function viewTraces() {
  const q = S.tool ? `/traces?tool=${encodeURIComponent(S.tool)}&limit=80` : "/traces?limit=80";
  renderTraceList((await api(q)).items, S.tool ? `Traces · ${S.tool}` : "Recent traces",
    "newest first · click a row to open it alongside");
}

async function viewErrors() {
  const q = S.cat ? `/errors?failure_category=${encodeURIComponent(S.cat)}&limit=80` : "/errors?limit=80";
  renderTraceList((await api(q)).items, S.cat ? `Failing · ${CAT[S.cat]?.l ?? S.cat}` : "Failing traces",
    "awaiting-input rounds are not failures and are excluded");
}

function renderTraceList(items, heading, note) {
  el("content").innerHTML = items.length ? `
    <div class="panel"><header><h3>${esc(heading)}</h3><span class="note">${esc(note)}</span></header><table>
      <thead><tr><th>Trace</th><th>Tool</th><th>Method</th><th>Status</th>
        <th class="num">Spans</th><th class="num">Duration</th><th>When</th></tr></thead>
      <tbody>${items.map((t) => `<tr class="click" data-trace="${esc(t.trace_id)}">
        <td class="mono dim">${esc(t.trace_id.slice(0, 16))}</td>
        <td><strong>${esc(t.tool || "—")}</strong></td>
        <td class="mono dim">${esc(t.mcp_method)}</td>
        <td>${badge(t.failure_category || "ok")}</td>
        <td class="num">${t.span_count}</td><td class="num">${dur(t.duration_ms)}</td>
        <td class="dim">${ago(t.start_time)}</td></tr>`).join("")}</tbody>
    </table></div>` : `<div class="empty">Nothing here in this window.</div>`;
  bindAll("[data-trace]", (n) => openTrace(n.dataset.trace));
}

/* ===========================================================================
   The drawer: trace waterfall + full span detail, over the list
   =========================================================================== */
async function openTrace(traceId, spanId = null) {
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
    t = await api(`/traces/${traceId}`);
  } catch (e) {
    el("drawer-body").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  }
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
  renderSpanDetail(t.detail[selectedId]);
}

/* Everything we hold about one span. Nothing omitted for being uninteresting:
   the console previously showed 17 of 55 columns, and the dropped ones were
   exactly what you need when something is wrong. */
function renderSpanDetail(d) {
  if (!d) { el("span-detail").innerHTML = ""; return; }

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
  document.querySelectorAll(sel).forEach((n) => (n.onclick = () => fn(n)));
}

function pushUrl() {
  const q = new URLSearchParams({ view: S.view, w: S.window });
  if (S.kind && S.view === "capabilities") q.set("kind", S.kind);
  if (S.server) q.set("server", S.server);
  if (S.tool) q.set("tool", S.tool);
  if (S.cat) q.set("cat", S.cat);
  if (S.trace) q.set("trace", S.trace);
  if (S.span) q.set("span", S.span);
  history.replaceState(null, "", `?${q}`);
}

function go(view, params = {}) {
  S.view = view;
  S.tool = null; S.cat = null; S.server = null;
  Object.assign(S, params);
  closeDrawerSilently();
  pushUrl();
  render();
}
function closeDrawerSilently() { el("drawer").classList.remove("open"); S.trace = null; S.span = null; }

/* What the content pane is currently showing. A REFRESH of the same thing must
   not look like a navigation to a different one. */
const viewKey = () =>
  [S.view, S.kind, S.server, S.tool, S.cat, S.window].join("|");
let renderedKey = null;

async function render() {
  const v = VIEWS[S.view] || VIEWS.overview;
  el("title").textContent = S.view === "capabilities"
    ? (KINDS.find(([k]) => k === S.kind)?.[1] ?? "Capabilities") : v.t;
  document.querySelectorAll("#nav a").forEach((a) =>
    a.classList.toggle("on", a.dataset.view === S.view && (!a.dataset.kind || a.dataset.kind === S.kind)));

  /* THE FLICKER. This blanked the pane to a spinner on EVERY render, including
     the 15s auto-refresh -- so a dashboard nobody had touched went white and
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
    await v.f();
    if (scrollTop) el("content").scrollTop = scrollTop;
    renderedKey = viewKey();
    // Stamped AFTER the fetch succeeds. Stamping before it would report
    // "updated just now" over data that failed to load -- a staleness
    // indicator that lies is worse than none, because it is believed.
    lastRendered = Date.now();
    el("health-dot").style.background = "var(--ok)";
    if (S.trace) openTrace(S.trace, S.span);
  } catch (e) {
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
  pushUrl();
  render();
};

el("drawer-close").onclick = closeDrawer;
el("scrim").onclick = closeDrawer;
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

/* ===========================================================================
   Auto-refresh
   ===========================================================================
   Three rules, each removing waste that was real:

   1. NOT WHILE A TAB IS HIDDEN. Every 15s the old timer refetched 80 trace
      summaries and re-ran the rollup for tabs nobody was looking at -- which is
      most open tabs, most of the time. This is the single biggest saving and
      costs one event listener.

   2. NOT WHILE A DRAWER IS OPEN. Redrawing under someone mid-investigation is
      hostile, and it discards the span they had selected.

   3. ONLY ON MONITORING VIEWS. Overview and Servers answer "is it healthy
      NOW". Traces and Errors are investigation surfaces -- the ground should
      not move while you read them, and a list that reorders under the cursor
      is worse than a stale one.

   The interval stays slower than the ~5s ingest floor: refreshing faster than
   data can arrive only makes the page flicker with identical numbers. */
const REFRESH_MS = 15000;
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
  // out the interval -- otherwise the first thing a returning user sees is up
  // to 15s stale while claiming to be live.
  document.addEventListener("visibilitychange", () => {
    if (shouldRefresh() && Date.now() - lastRendered > REFRESH_MS) render();
  });

  // Staleness is SHOWN, not assumed. A number with no timestamp beside it is a
  // number someone will quote as current -- and on the views that no longer
  // auto-refresh, it is the only thing telling them it is not.
  setInterval(() => {
    const stamp = el("foot-updated");
    if (!stamp || !lastRendered) return;
    const seconds = Math.round((Date.now() - lastRendered) / 1000);
    const live = shouldRefresh() ? "" : " · paused";
    stamp.textContent = seconds < 5 ? "updated just now" : `updated ${seconds}s ago${live}`;
  }, 1000);
}

(function boot() {
  // No key, no console. Checked before anything renders, so a signed-out user
  // sees the sign-in form rather than a dashboard that flashes empty panels and
  // then replaces itself.
  if (!readKey()) { signIn(false); return; }

  const p = new URLSearchParams(location.search);
  S.view = p.get("view") || "overview";
  S.kind = p.get("kind") || "tool";
  S.server = p.get("server"); S.tool = p.get("tool"); S.cat = p.get("cat");
  S.trace = p.get("trace"); S.span = p.get("span");
  if (p.get("w")) {
    S.window = +p.get("w");
    document.querySelectorAll("#range button").forEach((b) =>
      b.classList.toggle("on", +b.dataset.m === S.window));
  }
  render();
  startAutoRefresh();

  const out = document.getElementById("sign-out");
  if (out) out.onclick = signOut;
  const refresh = el("refresh-now");
  if (refresh) refresh.onclick = () => render();
})();
