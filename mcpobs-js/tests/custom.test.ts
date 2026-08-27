/**
 * setCustomAttribute must reach the same span dispatch.ts already created
 * for the call -- tested against a REAL McpServer/Client pair, same pattern
 * as dispatch.test.ts, because the mechanism it depends on (the tool handler
 * running inside `context.with(trace.setSpan(...))`) is dispatch.ts's own
 * behavior, not something worth re-mocking here.
 *
 * Uses NodeTracerProvider + `.register()`, NOT dispatch.test.ts's
 * BasicTracerProvider -- deliberately, and measured, not copied by habit.
 * `trace.getActiveSpan()` (what setCustomAttribute depends on) reads
 * `context.active()`, which only reflects a `context.with(...)` call's
 * pushed context when a REAL context manager is registered; without one,
 * OTel JS's default no-op manager still calls the callback but never
 * actually threads the context through, so `context.active()` inside the
 * handler stays at ROOT_CONTEXT and `getActiveSpan()` returns `undefined`.
 * `NodeTracerProvider.register()` sets up the AsyncLocalStorage-based context
 * manager as a side effect -- exactly what production `instrument()` uses
 * (index.ts), so this test exercises the real mechanism rather than a lighter
 * substitute that happens not to need it. dispatch.test.ts's own tests never
 * hit this gap because they read span attributes off the closure variable
 * `patchDispatch` already holds, never through `context.active()`.
 */

import { describe, it, expect, beforeAll, afterEach } from "vitest";
import { z } from "zod";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { patchDispatch } from "../src/dispatch.js";
import { setTransport } from "../src/transport.js";
import { setCustomAttribute, CUSTOM_ATTRIBUTE_PREFIX } from "../src/custom.js";

let exporter: InMemorySpanExporter;

beforeAll(() => {
  exporter = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  provider.register();
  const tracer = provider.getTracer("test");
  patchDispatch({ tracer, captureErrorDetail: true, capturePayloads: false });
  setTransport("stdio", { explicit: true });
});

afterEach(() => {
  exporter.reset();
});

async function connectedPair(configure: (server: McpServer) => void) {
  const server = new McpServer({ name: "test-server", version: "1.0.0" });
  configure(server);
  const client = new Client({ name: "test-client", version: "1.0.0" });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await Promise.all([server.connect(serverTransport), client.connect(clientTransport)]);
  return { server, client };
}

describe("setCustomAttribute", () => {
  it("lands on the call's own span, namespaced under mcpobs.custom.", async () => {
    const { client } = await connectedPair((server) => {
      server.tool("attachesCustomAttribute", { requestId: z.string() }, async ({ requestId }) => {
        setCustomAttribute("request_id", requestId);
        return { content: [{ type: "text", text: "ok" }] };
      });
    });

    await client.callTool({ name: "attachesCustomAttribute", arguments: { requestId: "test-abc-123" } });

    // patchDispatch now instruments every method, not an allowlisted subset
    // (dispatch.ts's module docstring) -- getFinishedSpans()[0] is
    // `initialize` here, not the tool call this test cares about.
    const span = exporter.getFinishedSpans().find((s) => s.name.includes("attachesCustomAttribute"))!;
    expect(span.attributes[`${CUSTOM_ATTRIBUTE_PREFIX}request_id`]).toBe("test-abc-123");
    // Never the raw key -- collision-proofing (with mcpobs.failure.kind and
    // friends) is the entire point of the namespace prefix.
    expect(span.attributes["request_id"]).toBeUndefined();
  });

  it("outside any tool handler, is a silent no-op", () => {
    expect(() => setCustomAttribute("request_id", "no-span-here")).not.toThrow();
  });

  it("never throws, even if the active span's own setAttribute does", async () => {
    // Matches mcpobs/custom.py's (Python) explicit "never raises" guarantee
    // -- a non-standard Tracer/Span implementation throwing here must not
    // propagate into the customer's request path, since this is called from
    // inside their own tool handler.
    const { trace, context } = await import("@opentelemetry/api");
    const throwingSpan = {
      setAttribute: () => {
        throw new Error("boom");
      },
    } as unknown as import("@opentelemetry/api").Span;

    await context.with(trace.setSpan(context.active(), throwingSpan), async () => {
      expect(() => setCustomAttribute("request_id", "x")).not.toThrow();
    });
  });
});
