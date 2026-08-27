/**
 * Session tokens for client-launched (stdio) servers, ADR-011.
 *
 * A near-verbatim port of the Python SDK's `mcpobs/session.py` -- the state
 * machine, every constant, and the response contract are unchanged. Ported
 * rather than redesigned because the logic is pure and already
 * battle-tested: `time.monotonic()` becomes `performance.now()` (also
 * monotonic in Node, via `perf_hooks`), `requests` becomes `fetch`.
 */

import { performance } from "node:perf_hooks";

/** Refresh at 75% of the token's life. */
const REFRESH_AT = 0.75;
/** Jitter only ever SUBTRACTS from the refresh point, never adds -- a fleet
 * of processes started at the same moment must not refresh in lockstep and
 * flood the customer's session endpoint, and refreshing LATE is always safe
 * (the token is still valid); refreshing EARLY is not more correct, so there
 * is no reason to jitter upward. */
const JITTER = 0.1;
const MIN_RETRY_SECONDS = 5;
const MAX_RETRY_SECONDS = 300;
const FETCH_TIMEOUT_MS = 10_000;

function diag(message: string): void {
  // stderr only. On stdio, stdout IS the JSON-RPC protocol channel.
  process.stderr.write(`[mcpobs] ${message}\n`);
}

export interface Session {
  token: string;
  endpoint: string;
  refreshAt: number; // performance.now()-relative deadline
  expiresAt: number; // performance.now()-relative deadline
}

export type SessionHeaders = Record<string, string> | (() => Record<string, string>);

export interface SessionProviderOptions {
  endpoint?: string;
  headers?: SessionHeaders;
  fetchImpl?: typeof fetch;
}

function checkEndpoint(endpoint: string): void {
  const url = new URL(endpoint);
  const isLocal = ["localhost", "127.0.0.1", "::1"].includes(url.hostname);
  if (url.protocol === "https:") return;
  if (url.protocol === "http:" && isLocal) return;
  throw new Error(
    `session endpoint must be https://, or http:// with a localhost hostname (got ${endpoint})`,
  );
}

export class SessionProvider {
  private endpoint: string;
  private headers: SessionHeaders;
  private fetchImpl: typeof fetch;
  private session: Session | null = null;
  private failures = 0;
  private nextAttempt = 0;

  constructor(opts: SessionProviderOptions = {}) {
    this.endpoint = opts.endpoint ?? process.env.MCPOBS_SESSION_ENDPOINT ?? "";
    this.headers = opts.headers ?? parseHeadersEnv(process.env.MCPOBS_SESSION_HEADERS);
    this.fetchImpl = opts.fetchImpl ?? fetch;
    if (this.endpoint) checkEndpoint(this.endpoint);
  }

  get configured(): boolean {
    return !!this.endpoint;
  }

  resolvedHeaders(): Record<string, string> {
    if (typeof this.headers === "function") {
      try {
        return { ...this.headers() };
      } catch (err) {
        diag(`session header callback failed: ${(err as Error).message}`);
        return {};
      }
    }
    return { ...this.headers };
  }

  /** The token to export with, refreshing if it is time. Never throws.
   * Returning null means "no telemetry right now"; callers buffer rather
   * than fail. */
  async current(): Promise<Session | null> {
    const now = performance.now();
    const session = this.session;

    if (session && now < session.refreshAt) return session;

    if (now < this.nextAttempt) {
      // Backing off. Keep serving the old token if it still has life left --
      // a failed refresh is not a reason to discard working credentials.
      return session && now < session.expiresAt ? session : null;
    }

    const fetched = await this.tryFetch();
    if (fetched) {
      this.session = fetched;
      return fetched;
    }
    return session && now < session.expiresAt ? session : null;
  }

  /** Drop the current token, e.g. after the server rejected it as expired.
   * Does NOT fetch -- the caller is on a failure path, and a synchronous
   * fetch there would turn one rejected export into a stalled exporter. */
  invalidate(): void {
    this.session = null;
  }

  private async tryFetch(): Promise<Session | null> {
    if (!this.endpoint) return null;
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
      let response: Response;
      try {
        response = await this.fetchImpl(this.endpoint, {
          method: "GET",
          headers: this.resolvedHeaders(),
          signal: controller.signal,
        });
      } finally {
        clearTimeout(timeout);
      }
      if (!response.ok) {
        throw new Error(`session endpoint returned ${response.status}`);
      }
      const body = (await response.json()) as {
        token?: string;
        expires_in?: number;
        endpoint?: string;
      };
      if (!body.token || !body.expires_in || body.expires_in <= 0) {
        throw new Error("session endpoint response missing token or expires_in");
      }
      const now = performance.now();
      const ttlMs = body.expires_in * 1000;
      const spread = 1 - Math.random() * JITTER;
      const session: Session = {
        token: body.token,
        endpoint: body.endpoint ?? this.endpoint,
        refreshAt: now + ttlMs * REFRESH_AT * spread,
        expiresAt: now + ttlMs,
      };
      if (this.failures > 0) {
        diag(`session endpoint recovered after ${this.failures} failure(s)`);
      }
      this.failures = 0;
      this.nextAttempt = 0;
      return session;
    } catch (err) {
      this.failures += 1;
      const delaySeconds = Math.min(
        MAX_RETRY_SECONDS,
        MIN_RETRY_SECONDS * 2 ** Math.min(this.failures, 6),
      );
      this.nextAttempt = performance.now() + delaySeconds * 1000;
      const message = `session endpoint unreachable (${(err as Error).message}); telemetry paused, retrying`;
      if (this.failures === 1) {
        diag(message);
      }
      return null;
    }
  }
}

function parseHeadersEnv(raw: string | undefined): Record<string, string> {
  if (!raw) return {};
  const out: Record<string, string> = {};
  for (const pair of raw.split(",")) {
    const [key, ...rest] = pair.split("=");
    if (key && rest.length) out[key.trim()] = rest.join("=").trim();
  }
  return out;
}

let defaultProvider: SessionProvider | null = null;

export function configure(opts: SessionProviderOptions): void {
  defaultProvider = new SessionProvider(opts);
}

export function defaultSessionProvider(): SessionProvider {
  if (!defaultProvider) defaultProvider = new SessionProvider();
  return defaultProvider;
}

export function resetDefaultSessionProvider(): void {
  defaultProvider = null;
}
