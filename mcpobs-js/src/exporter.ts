/**
 * Session-aware span export, ADR-011.
 *
 * SIMPLER than the Python SDK's `mcpobs/exporter.py`, and deliberately not a
 * mechanical port -- checked the actual JS OTLP exporter's config type
 * (`node_modules/@opentelemetry/otlp-exporter-base/.../legacy-base-configuration.d.ts`)
 * before assuming a subclass was needed: it already accepts
 * `headers?: Record<string,string> | (() => Promise<Record<string,string>>)`,
 * with an async factory as a documented, first-class usage. Python's
 * exporter had to subclass `OTLPSpanExporter` and mutate a persistent
 * `requests.Session`'s headers dict per export because `requests` has no
 * equivalent hook -- that workaround does not need to be carried over here.
 *
 * What still needs building: the buffer/drain behavior for spans created
 * BEFORE a session token is ever minted (server just started, first
 * `SessionProvider.current()` hasn't resolved yet). The OTel SDK has no
 * concept of "hold this and retry once auth exists" -- exporting with no
 * `x-api-key` would just 401 and the spans would be lost. `MAX_PENDING_SPANS`
 * and the drop-oldest-on-overflow behavior are the same numbers as Python's
 * exporter.py, since the underlying trade (bounded memory vs. losing the
 * earliest spans from a cold start) is identical either way.
 */

import type { ExportResult } from "@opentelemetry/core";
import { ExportResultCode } from "@opentelemetry/core";
import type { ReadableSpan, SpanExporter } from "@opentelemetry/sdk-trace-node";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import type { SessionProvider } from "./session.js";

const MAX_PENDING_SPANS = 2_000;

function diag(message: string): void {
  process.stderr.write(`[mcpobs] ${message}\n`);
}

/** Minimal shape SessionSpanExporter actually depends on -- lets tests
 * inject a fake delegate exporter without making real network calls. */
export interface DelegateExporter {
  export(spans: ReadableSpan[], resultCallback: (result: ExportResult) => void): void;
  shutdown(): Promise<void>;
  forceFlush?(): Promise<void>;
}

export interface SessionSpanExporterOptions {
  provider: SessionProvider;
  /** Path appended to the session's own endpoint, or a fixed fallback if the
   * session response never names one. */
  tracesPath?: string;
  /** Test seam: defaults to constructing a real OTLPTraceExporter. */
  createDelegate?: (url: string, headersFactory: () => Promise<Record<string, string>>) => DelegateExporter;
}

export class SessionSpanExporter implements SpanExporter {
  private readonly provider: SessionProvider;
  private readonly tracesPath: string;
  private readonly createDelegate: NonNullable<SessionSpanExporterOptions["createDelegate"]>;
  private delegate: DelegateExporter | null = null;
  private delegateUrl: string | null = null;
  private pending: ReadableSpan[] = [];
  private droppedWhileHeld = 0;

  constructor(opts: SessionSpanExporterOptions) {
    this.provider = opts.provider;
    this.tracesPath = opts.tracesPath ?? "/v1/traces";
    this.createDelegate =
      opts.createDelegate ?? ((url, headers) => new OTLPTraceExporter({ url, headers }));
  }

  export(spans: ReadableSpan[], resultCallback: (result: ExportResult) => void): void {
    void this.doExport(spans, resultCallback);
  }

  private async doExport(
    spans: ReadableSpan[],
    resultCallback: (result: ExportResult) => void,
  ): Promise<void> {
    const session = await this.provider.current();
    if (!session) {
      this.hold(spans);
      // SUCCESS, not an error: BatchSpanProcessor must not retry-storm a
      // customer's session endpoint just because it hasn't minted a token
      // yet. The spans are not lost -- they drain on the next successful
      // export, same contract as Python's exporter.
      resultCallback({ code: ExportResultCode.SUCCESS });
      return;
    }

    const toSend = this.drain(spans);
    const exporter = this.exporterFor(session.endpoint);
    exporter.export(toSend, (result) => {
      if (result.code !== ExportResultCode.SUCCESS) {
        this.provider.invalidate();
      }
      resultCallback(result);
    });
  }

  private hold(spans: ReadableSpan[]): void {
    this.pending.push(...spans);
    const overflow = this.pending.length - MAX_PENDING_SPANS;
    if (overflow > 0) {
      this.pending.splice(0, overflow);
      this.droppedWhileHeld += overflow;
      diag(`session not yet available: dropped ${overflow} oldest buffered span(s)`);
    }
  }

  private drain(spans: ReadableSpan[]): ReadableSpan[] {
    if (this.pending.length === 0 && this.droppedWhileHeld === 0) return spans;
    const held = this.pending;
    this.pending = [];
    if (this.droppedWhileHeld > 0) {
      diag(`resuming export after buffering; ${this.droppedWhileHeld} span(s) were dropped while held`);
      this.droppedWhileHeld = 0;
    }
    return [...held, ...spans];
  }

  private exporterFor(endpoint: string): DelegateExporter {
    const url = `${endpoint.replace(/\/$/, "")}${this.tracesPath}`;
    if (this.delegate && this.delegateUrl === url) return this.delegate;
    // A session refresh naming a different endpoint (multi-region failover,
    // a rotated collector host) replaces the delegate -- the OLD one must be
    // shut down, not dropped, or its underlying HTTP client/keep-alive
    // agent leaks for the rest of this long-running process. Fire-and-forget:
    // shutdown is cleanup for an exporter nothing references anymore, not
    // something the CURRENT export (about to use the new delegate) should
    // wait on.
    const previous = this.delegate;
    if (previous) void previous.shutdown().catch(() => {});
    this.delegate = this.createDelegate(url, async () => {
      const session = await this.provider.current();
      const headers: Record<string, string> = {};
      if (session) headers["x-api-key"] = session.token;
      return headers;
    });
    this.delegateUrl = url;
    return this.delegate;
  }

  async shutdown(): Promise<void> {
    await this.delegate?.shutdown();
  }

  async forceFlush(): Promise<void> {
    if (this.pending.length > 0) {
      const session = await this.provider.current();
      if (session) {
        const toSend = this.drain([]);
        await new Promise<void>((resolve) => {
          this.exporterFor(session.endpoint).export(toSend, () => resolve());
        });
      }
    }
    await this.delegate?.forceFlush?.();
  }
}
