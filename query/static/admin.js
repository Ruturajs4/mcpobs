/* ===========================================================================
   Operator console.

   A SEPARATE page from the customer console, not a mode of it. A toggle inside
   the customer console would be one rendering bug away from showing a customer
   everyone else's tenants; two pages with two credentials have no code path
   from one to the other.

   Its key is stored under a DIFFERENT localStorage name too, so signing into
   one does not silently sign you into the other.
   =========================================================================== */

const S = { view: "tenants", window: 1440 };

const KEY_STORAGE = "mcpobs.admin.key";
const readKey = () => localStorage.getItem(KEY_STORAGE) || "";

const el = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const num = (n) => (n ?? 0).toLocaleString();

/* 0 means unlimited in control/quota.py, and it must not render as "0" -- that
   reads as "nothing allowed", the exact opposite of what it means. */
const limit = (n) => (n === 0 ? '<span class="unl">unlimited</span>' : num(n));

/* Two databases, two timestamp shapes. ClickHouse DateTime64 serialises NAIVE
   (`2026-08-16T07:39:04`) and needs a `Z` to be read as UTC; Postgres
   TIMESTAMPTZ already carries an offset (`...+00:00`), and appending `Z` to
   that makes an invalid Date -- which rendered every audit row as "NaNd ago".
   The customer console never hit this because it only reads ClickHouse. */
const ago = (iso) => {
  if (!iso) return "—";
  const hasZone = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso);
  const s = Math.max(0, (Date.now() - new Date(hasZone ? iso : iso + "Z").getTime()) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};

async function api(path, options = {}) {
  const sep = path.includes("?") ? "&" : "?";
  const r = await fetch(`/api/v1/admin${path}${sep}window_minutes=${S.window}`, {
    ...options,
    headers: { "x-api-key": readKey(), "content-type": "application/json", ...(options.headers || {}) },
  });
  if (r.status === 401) { signIn(true); throw new Error("unauthorized"); }
  if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 200)}`);
  return r.json();
}

function signIn(rejected) {
  document.body.innerHTML = `
    <div class="signin">
      <div class="signin-card">
        <h1>Operator console</h1>
        <p>${rejected
          ? "That key was rejected. The operator console needs an <code>admin</code>-scoped key — a <code>read</code> key will not do, because this page spans every tenant."
          : "Sign in with an admin-scoped API key."}</p>
        <input id="key-input" type="password" placeholder="mcpo_..." autocomplete="off"
               spellcheck="false">
        <button id="key-go">Continue</button>
        <p class="signin-hint">Admin keys are issued only from the database side:
          <code>python scripts/admin.py key --org &lt;org&gt; --scopes admin</code>.
          There is no endpoint that mints one.</p>
      </div>
    </div>`;
  const submit = () => {
    const value = el("key-input").value.trim();
    if (!value) return;
    localStorage.setItem(KEY_STORAGE, value);
    location.reload();
  };
  el("key-go").onclick = submit;
  el("key-input").onkeydown = (e) => { if (e.key === "Enter") submit(); };
  el("key-input").focus();
}

/* ===========================================================================
   Views
   =========================================================================== */
async function viewTenants() {
  const o = await api("/overview");
  const banners = [];

  /* Telemetry under a tenant with no org row should be impossible now that the
     gateway resolves tenancy from an authenticated key. Surfaced loudly rather
     than filtered out: if it appears, either a key outlived its org or
     something bypassed the gateway, and both are worth a page. */
  if (o.orphaned) banners.push(`<div class="note-bar"><span>&#9888;</span><div>
    <b>${o.orphaned} tenant(s) have telemetry but no organisation.</b>
    That should be impossible while the gateway is the only way in — either a
    key outlived its org, or something reached the Collector directly.</div></div>`);

  const soft = o.tenants.filter((t) => t.soft_quota_spans > 0);
  if (soft.length) banners.push(`<div class="note-bar"><span>&#9888;</span><div>
    <b>${soft.length} tenant(s) crossed a soft quota threshold.</b>
    ${soft.map((t) => esc(t.tenant)).join(", ")} — raise the limit before
    they are refused, or leave it and they will be.</div></div>`);

  el("content").innerHTML = `
    ${banners.join("")}
    <div class="grid g4">
      <div class="card"><div class="lbl">Tenants</div><div class="big">${o.tenants.length}</div>
        <div class="sub">${o.never_onboarded} never sent a span</div></div>
      <div class="card"><div class="lbl">Spans</div><div class="big">${num(o.total_spans)}</div>
        <div class="sub">across every tenant</div></div>
      <div class="card"><div class="lbl">Errors</div>
        <div class="big" style="color:${o.total_errors ? "var(--err)" : "var(--ok)"}">${num(o.total_errors)}</div>
        <div class="sub">${o.total_spans ? ((o.total_errors / o.total_spans) * 100).toFixed(1) : "0.0"}% of spans</div></div>
      <div class="card"><div class="lbl">Freshness p95</div>
        <div class="big">${o.pipeline.freshness_p95_seconds.toFixed(1)}s</div>
        <div class="sub">${num(o.pipeline.dead_letters_24h)} dead letters / 24h</div></div>
    </div>

    <div class="panel"><header><h3>Tenants</h3>
      <span class="note">volume from ClickHouse, identity from Postgres — joined here, because neither database can see the other</span></header>
      <table>
        <thead><tr>
          <th>Tenant</th><th>Plan</th><th class="num">Spans</th><th class="num">Errors</th>
          <th class="num">Servers</th><th>Limits (min / day)</th><th>Last seen</th><th></th>
        </tr></thead>
        <tbody>${o.tenants.map(tenantRow).join("")}</tbody>
      </table></div>`;

  bindQuotaEditors();
}

function tenantRow(t) {
  const flags = [];
  if (t.orphaned) flags.push('<span class="flag flag-orphan">no org</span>');
  if (!t.onboarded) flags.push('<span class="flag flag-quiet">never sent</span>');
  if (t.soft_quota_spans) flags.push(`<span class="flag flag-soft">soft quota</span>`);
  if (t.limit_overridden) flags.push('<span class="flag flag-override">override</span>');

  return `<tr>
    <td><strong>${esc(t.tenant)}</strong>
      ${t.name && t.name !== t.tenant ? `<span class="mute"> ${esc(t.name)}</span>` : ""}
      <div class="mute mono" style="font-size:11px">
        ${t.projects} project(s) · ${t.users} user(s) · ${t.active_keys} key(s)${
          t.open_invites ? ` · ${t.open_invites} open invite(s)` : ""}</div>
      ${flags.join(" ")}</td>
    <td>${esc(t.plan)}</td>
    <td class="num">${num(t.spans)}</td>
    <td class="num" ${t.errors ? 'style="color:var(--err)"' : ""}>${num(t.errors)}</td>
    <td class="num">${num(t.servers)}</td>
    <td class="limit">${limit(t.limit_minute)} / ${limit(t.limit_day)}</td>
    <td class="${t.last_seen ? "" : "stale"}">${ago(t.last_seen)}</td>
    <td><div class="quota-edit">
      <input id="qm-${esc(t.tenant)}" placeholder="min" value="${t.limit_overridden ? t.limit_minute : ""}">
      <button data-quota="${esc(t.tenant)}">set</button>
      <button data-quota-clear="${esc(t.tenant)}">plan</button>
    </div></td>
  </tr>`;
}

function bindQuotaEditors() {
  /* One of the two emergency levers Architecture §8 names for a whale flooding
     ingest, so it lives where the operator is already looking rather than in a
     CLI they would have to go and find. */
  document.querySelectorAll("[data-quota]").forEach((b) => {
    b.onclick = async () => {
      const tenant = b.dataset.quota;
      const raw = el(`qm-${tenant}`).value.trim();
      if (raw === "") return;
      await api(`/tenants/${encodeURIComponent(tenant)}/quota`, {
        method: "POST",
        body: JSON.stringify({ per_minute: Number(raw), per_day: null }),
      });
      render();
    };
  });
  document.querySelectorAll("[data-quota-clear]").forEach((b) => {
    b.onclick = async () => {
      await api(`/tenants/${encodeURIComponent(b.dataset.quotaClear)}/quota`, {
        method: "POST",
        body: JSON.stringify({ per_minute: null, per_day: null }),
      });
      render();
    };
  });
}

async function viewPipeline() {
  const p = await api("/pipeline");
  const versions = Object.entries(p.normalization_versions);

  /* Several live versions is not an error, but it changes what an aggregate
     means: argMax resolution is exactly what hides the difference (D24). */
  const drift = versions.length > 1
    ? `<div class="note-bar"><span>&#9888;</span><div>
        <b>${versions.length} normalization versions are live.</b>
        A deploy is rolling out or a replay is in flight. Aggregates resolve to
        the latest version per span, so numbers are correct — but a
        comparison across this window spans two definitions.</div></div>`
    : "";

  el("content").innerHTML = `
    ${drift}
    <div class="grid g4">
      <div class="card"><div class="lbl">Freshness p50</div>
        <div class="big">${p.freshness_p50_seconds.toFixed(1)}s</div>
        <div class="sub">event time → queryable</div></div>
      <div class="card"><div class="lbl">Freshness p95</div>
        <div class="big">${p.freshness_p95_seconds.toFixed(1)}s</div>
        <div class="sub">fixed 15m window, never the selected range</div></div>
      <div class="card"><div class="lbl">Spans (15m)</div>
        <div class="big">${num(p.spans_recent)}</div>
        <div class="sub">is anything arriving at all?</div></div>
      <div class="card"><div class="lbl">Dead letters</div>
        <div class="big" style="color:${p.dead_letters_24h ? "var(--warn)" : "var(--ok)"}">${num(p.dead_letters_24h)}</div>
        <div class="sub">last 24h</div></div>
    </div>

    <div class="panel"><header><h3>Dead letters by reason</h3>
      <span class="note">nothing is dropped silently — a message we cannot decode is stored, not discarded</span></header>
      ${Object.keys(p.dead_letter_reasons).length ? `<table><tbody>
        ${Object.entries(p.dead_letter_reasons).map(([r, n]) =>
          `<tr><td>${esc(r)}</td><td class="num">${num(n)}</td></tr>`).join("")}
      </tbody></table>` : '<div class="io-off">No dead letters in 24h.</div>'}</div>

    <div class="panel"><header><h3>Normalization versions in play</h3>
      <span class="note">more than one means a deploy or replay is in flight</span></header>
      <table><tbody>${versions.map(([v, n]) =>
        `<tr><td class="mono">v${esc(v)}</td><td class="num">${num(n)} spans</td></tr>`).join("")}
      </tbody></table></div>`;
}

async function viewKeys() {
  const keys = await api("/keys");
  el("content").innerHTML = `
    <div class="panel"><header><h3>API keys</h3>
      <span class="note">prefixes only — the secret is never stored, so there is nothing here to leak even to you</span></header>
      <table>
        <thead><tr><th>Prefix</th><th>Tenant</th><th>Project</th><th>Scopes</th>
          <th>Last used</th><th>Status</th><th></th></tr></thead>
        <tbody>${keys.map((k) => `<tr>
          <td class="mono">${esc(k.prefix)}${k.name ? `<div class="mute" style="font-size:11px">${esc(k.name)}</div>` : ""}</td>
          <td>${esc(k.tenant)}</td><td>${esc(k.project)}</td>
          <td class="mono" style="font-size:12px">${esc(k.scopes)}</td>
          <td class="${k.last_used_at ? "" : "stale"}">${k.last_used_at ? ago(k.last_used_at) : "never"}</td>
          <td>${k.revoked_at
            ? '<span class="flag flag-orphan">revoked</span>'
            : '<span class="flag flag-quiet">active</span>'}</td>
          <td>${k.revoked_at ? "" :
            `<button class="danger" data-revoke="${esc(k.prefix)}">revoke</button>`}</td>
        </tr>`).join("")}</tbody>
      </table></div>`;

  document.querySelectorAll("[data-revoke]").forEach((b) => {
    b.onclick = async () => {
      // Irreversible, so it asks. Revoking the wrong key takes a customer's
      // ingest down until somebody notices and issues another.
      if (!window.confirm(`Revoke key ${b.dataset.revoke}? This cannot be undone.`)) return;
      await api(`/keys/${encodeURIComponent(b.dataset.revoke)}/revoke`, { method: "POST" });
      render();
    };
  });
}

async function viewInvites() {
  const invites = await api("/invites");
  el("content").innerHTML = `
    <div class="panel"><header><h3>Invites</h3>
      <span class="note">invite-only, so this IS the signup queue</span></header>
      ${invites.length ? `<table>
        <thead><tr><th>Email</th><th>Tenant</th><th>Role</th><th>Created</th>
          <th>Status</th></tr></thead>
        <tbody>${invites.map((i) => `<tr>
          <td>${esc(i.email)}</td><td>${esc(i.tenant)}</td><td>${esc(i.role)}</td>
          <td>${ago(i.created_at)}</td>
          <td>${i.accepted_at
            ? `<span class="flag flag-quiet">accepted ${ago(i.accepted_at)}</span>`
            : new Date(i.expires_at) < new Date()
              ? '<span class="flag flag-orphan">expired</span>'
              : '<span class="flag flag-soft">pending</span>'}</td>
        </tr>`).join("")}</tbody></table>`
        : '<div class="io-off">No invites yet.</div>'}</div>`;
}

async function viewAudit() {
  const rows = await api("/audit?limit=300");
  el("content").innerHTML = `
    <div class="panel"><header><h3>Operator actions</h3>
      <span class="note">written in the SAME transaction as the change, so an action and its
        record land together or not at all &middot; refused attempts included</span></header>
      ${rows.length ? `<table>
        <thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th>
          <th>Change</th><th>From</th></tr></thead>
        <tbody>${rows.map((r) => `<tr>
          <td>${ago(r.at)}</td>
          <td class="mono">${esc(r.actor_prefix || "—")}
            <div class="mute" style="font-size:11px">${esc(r.actor_source)}${
              r.actor_org ? ` · ${esc(r.actor_org)}` : ""}</div></td>
          <td>${r.outcome === "denied"
            ? `<span class="flag flag-orphan">${esc(r.action)}</span>`
            : `<span class="flag flag-quiet">${esc(r.action)}</span>`}</td>
          <td class="mono">${esc(r.target || "—")}</td>
          <td class="mono" style="font-size:12px">${esc(JSON.stringify(r.detail)).slice(0, 120)}</td>
          <td class="mute mono" style="font-size:11px">${esc(r.source_ip || "—")}</td>
        </tr>`).join("")}</tbody></table>`
        : `<div class="io-off">No operator actions recorded yet. Reads are not
           audited — the console refreshes every 30s, so logging them would bury
           the mutations under thousands of rows meaning "a tab was open".</div>`}
    </div>`;
}

const VIEWS = {
  tenants: [viewTenants, "Tenants"],
  pipeline: [viewPipeline, "Pipeline"],
  keys: [viewKeys, "API keys"],
  invites: [viewInvites, "Invites"],
  audit: [viewAudit, "Audit"],
};

let adminRenderedKey = null;

async function render() {
  const [fn, title] = VIEWS[S.view] || VIEWS.tenants;
  el("title").textContent = title;
  // Same reasoning as the customer console: a 30s background refresh must not
  // reset the scroll position of a table somebody is reading.
  const key = `${S.view}|${S.window}`;
  const refreshing = adminRenderedKey === key;
  const scrollTop = refreshing ? el("content").scrollTop : 0;
  try {
    await fn();
    if (scrollTop) el("content").scrollTop = scrollTop;
    adminRenderedKey = key;
    const health = await api("/pipeline");
    // Labels live in the markup now, so these carry the value alone.
    el("foot-spans").textContent = `${num(health.spans_recent)} / 15m`;
    el("foot-fresh").textContent = `${health.freshness_p95_seconds.toFixed(1)}s`;
  } catch (e) {
    if (e.message !== "unauthorized") {
      el("content").innerHTML = `<div class="note-bar"><span>&#9888;</span><div>
        <b>Could not load.</b> ${esc(e.message)}</div></div>`;
    }
  }
}

(function boot() {
  if (!readKey()) { signIn(false); return; }

  document.querySelectorAll("nav a").forEach((a) => {
    a.onclick = () => {
      document.querySelectorAll("nav a").forEach((x) => x.classList.remove("on"));
      a.classList.add("on");
      S.view = a.dataset.view;
      render();
    };
  });
  document.querySelectorAll("#range button").forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll("#range button").forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      S.window = +b.dataset.m;
      render();
    };
  });
  el("sign-out").onclick = () => {
    localStorage.removeItem(KEY_STORAGE);
    location.reload();
  };
  render();
  // Same rule as the customer console: a hidden tab refetches nothing. This one
  // polls unconditionally otherwise -- it has no drawer to protect, and every
  // view on it is a monitoring view.
  setInterval(() => {
    if (document.visibilityState === "visible") render();
  }, 30000);
})();
