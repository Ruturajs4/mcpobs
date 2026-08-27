import { describe, it, expect, vi } from "vitest";
import { ExportResultCode } from "@opentelemetry/core";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-node";
import { SessionSpanExporter, type DelegateExporter } from "../src/exporter.js";
import { SessionProvider } from "../src/session.js";

function fakeSpan(name: string): ReadableSpan {
  return { name } as unknown as ReadableSpan;
}

function fakeDelegate(): DelegateExporter & { exported: ReadableSpan[][] } {
  const exported: ReadableSpan[][] = [];
  return {
    exported,
    export(spans, cb) {
      exported.push(spans);
      cb({ code: ExportResultCode.SUCCESS });
    },
    async shutdown() {},
  };
}

describe("SessionSpanExporter", () => {
  it("holds spans and reports SUCCESS when no session is available yet, rather than failing the batch", async () => {
    const provider = new SessionProvider({}); // not configured -> current() always null
    const delegate = fakeDelegate();
    const exporter = new SessionSpanExporter({ provider, createDelegate: () => delegate });

    const result = await new Promise((resolve) => {
      exporter.export([fakeSpan("a")], resolve);
    });

    expect(result).toEqual({ code: ExportResultCode.SUCCESS });
    expect(delegate.exported).toHaveLength(0); // never reached the real exporter
  });

  it("drains held spans into the next successful export, prepended", async () => {
    let session: { token: string; endpoint: string; refreshAt: number; expiresAt: number } | null = null;
    const provider = { current: async () => session, invalidate: () => {} } as unknown as SessionProvider;
    const delegate = fakeDelegate();
    const exporter = new SessionSpanExporter({ provider, createDelegate: () => delegate });

    // No session yet -- held.
    await new Promise((resolve) => exporter.export([fakeSpan("first")], resolve));
    await new Promise((resolve) => exporter.export([fakeSpan("second")], resolve));
    expect(delegate.exported).toHaveLength(0);

    // Session becomes available -- next export should carry BOTH held spans
    // plus the new one, in order.
    session = { token: "t", endpoint: "https://example.com", refreshAt: 1e12, expiresAt: 1e12 };
    await new Promise((resolve) => exporter.export([fakeSpan("third")], resolve));

    expect(delegate.exported).toHaveLength(1);
    expect(delegate.exported[0].map((s) => s.name)).toEqual(["first", "second", "third"]);
  });

  it("shuts down the previous delegate when the session endpoint changes", async () => {
    // A session refresh naming a different endpoint (multi-region failover,
    // a rotated collector host) must not leak the old OTLPTraceExporter's
    // underlying HTTP client/keep-alive agent -- the old delegate has to be
    // shut down, not just dropped, when a new one replaces it.
    let session: { token: string; endpoint: string; refreshAt: number; expiresAt: number } | null = {
      token: "t", endpoint: "https://one.example.com", refreshAt: 1e12, expiresAt: 1e12,
    };
    const provider = { current: async () => session, invalidate: () => {} } as unknown as SessionProvider;
    const delegates: DelegateExporter[] = [];
    const exporter = new SessionSpanExporter({
      provider,
      createDelegate: () => {
        const d: DelegateExporter = {
          export: (_spans, cb) => cb({ code: ExportResultCode.SUCCESS }),
          shutdown: vi.fn().mockResolvedValue(undefined),
        };
        delegates.push(d);
        return d;
      },
    });

    await new Promise((resolve) => exporter.export([fakeSpan("a")], resolve));
    expect(delegates).toHaveLength(1);
    expect(delegates[0].shutdown).not.toHaveBeenCalled();

    session = { token: "t2", endpoint: "https://two.example.com", refreshAt: 1e12, expiresAt: 1e12 };
    await new Promise((resolve) => exporter.export([fakeSpan("b")], resolve));

    expect(delegates).toHaveLength(2); // a new delegate for the new endpoint
    expect(delegates[0].shutdown).toHaveBeenCalledTimes(1); // the old one, cleaned up
    expect(delegates[1].shutdown).not.toHaveBeenCalled(); // the new one, still active
  });

  it("does NOT shut down the delegate when the endpoint is unchanged", async () => {
    const provider = {
      current: async () => ({ token: "t", endpoint: "https://example.com", refreshAt: 1e12, expiresAt: 1e12 }),
      invalidate: () => {},
    } as unknown as SessionProvider;
    const shutdown = vi.fn().mockResolvedValue(undefined);
    const delegate: DelegateExporter = {
      export: (_spans, cb) => cb({ code: ExportResultCode.SUCCESS }),
      shutdown,
    };
    const exporter = new SessionSpanExporter({ provider, createDelegate: () => delegate });

    await new Promise((resolve) => exporter.export([fakeSpan("a")], resolve));
    await new Promise((resolve) => exporter.export([fakeSpan("b")], resolve));

    expect(shutdown).not.toHaveBeenCalled();
  });

  it("invalidates the session provider when the underlying export fails", async () => {
    const provider = {
      current: async () => ({ token: "t", endpoint: "https://example.com", refreshAt: 1e12, expiresAt: 1e12 }),
      invalidate: vi.fn(),
    } as unknown as SessionProvider;
    const failing: DelegateExporter = {
      export(_spans, cb) {
        cb({ code: ExportResultCode.FAILED });
      },
      async shutdown() {},
    };
    const exporter = new SessionSpanExporter({ provider, createDelegate: () => failing });

    await new Promise((resolve) => exporter.export([fakeSpan("a")], resolve));
    expect(provider.invalidate).toHaveBeenCalledTimes(1);
  });
});

describe("SessionSpanExporter overflow", () => {
  it("drops the oldest buffered spans once over MAX_PENDING_SPANS (2000)", async () => {
    let session: { token: string; endpoint: string; refreshAt: number; expiresAt: number } | null = null;
    const provider = { current: async () => session, invalidate: () => {} } as unknown as SessionProvider;
    const delegate = fakeDelegate();
    const exporter = new SessionSpanExporter({ provider, createDelegate: () => delegate });

    // 2005 spans held while no session exists -- 5 over the 2000 cap.
    for (let i = 0; i < 2005; i++) {
      await new Promise((resolve) => exporter.export([fakeSpan(`span-${i}`)], resolve));
    }
    session = { token: "t", endpoint: "https://example.com", refreshAt: 1e12, expiresAt: 1e12 };
    await new Promise((resolve) => exporter.export([fakeSpan("final")], resolve));

    const drained = delegate.exported[0].map((s) => s.name);
    expect(drained).toHaveLength(2001); // 2000 held (post-overflow) + "final"
    expect(drained[0]).toBe("span-5"); // the oldest 5 (span-0..span-4) were dropped
    expect(drained).not.toContain("span-0");
  });
});
