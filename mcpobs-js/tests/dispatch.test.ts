/**
 * The most important test in this package: a REAL @modelcontextprotocol/sdk
 * McpServer, connected to a real Client over InMemoryTransport, with
 * patchDispatch() installed BEFORE the server is constructed -- exactly the
 * ordering the whole design depends on. Not a mock of the SDK's dispatch
 * behavior; the actual thing.
 */

import { describe, it, expect, beforeAll, afterEach } from "vitest";
import { z } from "zod";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { patchDispatch, SpanAttributes } from "../src/dispatch.js";
import { FailureKind, FailureKindSource } from "../src/classifier.js";
import { setTransport, resetTransport } from "../src/transport.js";

let exporter: InMemorySpanExporter;
const dispatchOptions: Parameters<typeof patchDispatch>[0] = {
  tracer: undefined as never,
  captureErrorDetail: true,
  capturePayloads: true,
};

beforeAll(() => {
  // patchDispatch MUST run before any McpServer in this file is
  // constructed -- this beforeAll IS that ordering requirement, made
  // structural rather than a comment.
  exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const tracer = provider.getTracer("test");
  dispatchOptions.tracer = tracer;
  patchDispatch(dispatchOptions);
  setTransport("stdio", { explicit: true });
});

afterEach(() => {
  exporter.reset();
  dispatchOptions.capturePayloads = true;
});

async function connectedPair(configure: (server: McpServer) => void) {
  const server = new McpServer({ name: "test-server", version: "1.0.0" });
  configure(server);
  const client = new Client({ name: "test-client", version: "1.0.0" });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await Promise.all([server.connect(serverTransport), client.connect(clientTransport)]);
  return { server, client };
}

function attrs(span: { attributes: Record<string, unknown> }) {
  return span.attributes;
}

// Every connectedPair() now produces an `initialize` span too, not just the
// tool/prompt call under test (patchDispatch instruments every method, not
// an allowlisted subset -- see dispatch.ts's module docstring). Tests that
// used to grab getFinishedSpans()[0] as "the one span" need to say which
// method they mean instead.
function byMethod(spans: readonly { attributes: Record<string, unknown> }[], method: string) {
  return spans.find((s) => attrs(s)[SpanAttributes.MCP_METHOD] === method);
}

describe("patchDispatch, against a real connected client/server", () => {
  it("a successful tool call produces a span classified ok", async () => {
    const { client } = await connectedPair((server) => {
      server.tool("echo", { message: z.string() }, async ({ message }) => ({
        content: [{ type: "text", text: message }],
      }));
    });

    const result = await client.callTool({ name: "echo", arguments: { message: "hi" } });
    expect(result.isError).not.toBe(true);

    // initialize (the client handshake) AND tools/call, both instrumented --
    // not the allowlisted-methods-only span count this test used to expect.
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(2);
    const span = byMethod(spans, "tools/call")!;
    expect(attrs(span)[SpanAttributes.TOOL_NAME]).toBe("echo");
    expect(attrs(span)[SpanAttributes.FAILURE_KIND]).toBe(FailureKind.OK);
    expect(attrs(span)[SpanAttributes.TRANSPORT]).toBe("stdio");
  });

  it("calling an unregistered tool is classified unknown_tool BEFORE any handler runs", async () => {
    let handlerRan = false;
    const { client } = await connectedPair((server) => {
      server.tool("realTool", async () => {
        handlerRan = true;
        return { content: [{ type: "text", text: "ok" }] };
      });
    });

    const result = await client.callTool({ name: "doesNotExist", arguments: {} });
    expect(result.isError).toBe(true);
    expect(handlerRan).toBe(false);

    const span = byMethod(exporter.getFinishedSpans(), "tools/call")!;
    expect(attrs(span)[SpanAttributes.FAILURE_KIND]).toBe(FailureKind.UNKNOWN_TOOL);
    expect(attrs(span)[SpanAttributes.FAILURE_KIND_SOURCE]).toBe(FailureKindSource.SDK_DISPATCH);
  });

  it("bad arguments (schema validation failure) is classified invalid_arguments, handler never runs", async () => {
    let handlerRan = false;
    const { client } = await connectedPair((server) => {
      server.tool("needsNumber", { count: z.number() }, async () => {
        handlerRan = true;
        return { content: [{ type: "text", text: "ok" }] };
      });
    });

    const result = await client.callTool({ name: "needsNumber", arguments: { count: "not a number" } });
    expect(result.isError).toBe(true);
    expect(handlerRan).toBe(false);

    const span = byMethod(exporter.getFinishedSpans(), "tools/call")!;
    expect(attrs(span)[SpanAttributes.FAILURE_KIND]).toBe(FailureKind.INVALID_ARGUMENTS);
  });

  it("a tool's own isError:true result is tool_error, not misclassified as SDK-level", async () => {
    const { client } = await connectedPair((server) => {
      server.tool("failing", async () => ({
        content: [{ type: "text", text: "Error during fetching automate screenshots: 401" }],
        isError: true,
      }));
    });

    await client.callTool({ name: "failing", arguments: {} });

    const span = byMethod(exporter.getFinishedSpans(), "tools/call")!;
    expect(attrs(span)[SpanAttributes.FAILURE_KIND]).toBe(FailureKind.TOOL_ERROR);
    expect(attrs(span)[SpanAttributes.FAILURE_KIND_SOURCE]).toBe(FailureKindSource.IS_ERROR_FLAG);
  });

  it("captures failure detail text on a failing span, truncated, and omits it on success", async () => {
    const { client } = await connectedPair((server) => {
      server.tool("failing", async () => ({
        content: [{ type: "text", text: "some real failure text" }],
        isError: true,
      }));
      server.tool("ok", async () => ({ content: [{ type: "text", text: "fine" }] }));
    });

    await client.callTool({ name: "failing", arguments: {} });
    await client.callTool({ name: "ok", arguments: {} });

    const spans = exporter.getFinishedSpans();
    const failing = spans.find((s) => attrs(s)[SpanAttributes.TOOL_NAME] === "failing")!;
    const ok = spans.find((s) => attrs(s)[SpanAttributes.TOOL_NAME] === "ok")!;
    expect(attrs(failing)[SpanAttributes.FAILURE_DETAIL]).toBe("some real failure text");
    expect(attrs(ok)[SpanAttributes.FAILURE_DETAIL]).toBeUndefined();
  });

  it("captures redacted JSON-RPC request and response payloads when enabled", async () => {
    const { client } = await connectedPair((server) => {
      server.tool(
        "secretEcho",
        { message: z.string(), access_key: z.string() },
        async ({ message }) => ({
          content: [{ type: "text", text: `${message} Bearer abcdefghijklmnop` }],
        }),
      );
    });

    await client.callTool({
      name: "secretEcho",
      arguments: { message: "hello", access_key: "must-not-leak" },
    });

    const span = byMethod(exporter.getFinishedSpans(), "tools/call")!;
    const attributes = attrs(span);
    const request = String(attributes[SpanAttributes.REQUEST]);
    const response = String(attributes[SpanAttributes.RESPONSE]);

    expect(request).toContain('"method":"tools/call"');
    expect(request).toContain('"name":"secretEcho"');
    expect(request).toContain('"access_key":"[redacted]"');
    expect(request).not.toContain("must-not-leak");
    // Whole match ("Bearer <token>") is replaced, not just the token --
    // matches mcpobs/http.py's SENSITIVE_VALUES exactly, no capture group.
    expect(response).toContain("[redacted]");
    expect(response).not.toContain("Bearer abcdefghijklmnop");
    expect(response).not.toContain("abcdefghijklmnop");
    expect(attributes[SpanAttributes.REQUEST_SIZE]).toBe(request.length);
    expect(attributes[SpanAttributes.RESPONSE_SIZE]).toBe(response.length);
  });

  it("omits request and response payloads when capture is disabled", async () => {
    dispatchOptions.capturePayloads = false;
    const { client } = await connectedPair((server) => {
      server.tool("echoWithoutCapture", async () => ({
        content: [{ type: "text", text: "private result" }],
      }));
    });

    await client.callTool({ name: "echoWithoutCapture", arguments: { value: "private input" } });

    const attributes = attrs(byMethod(exporter.getFinishedSpans(), "tools/call")!);
    expect(attributes[SpanAttributes.REQUEST]).toBeUndefined();
    expect(attributes[SpanAttributes.RESPONSE]).toBeUndefined();
    expect(attributes[SpanAttributes.REQUEST_SIZE]).toBeUndefined();
    expect(attributes[SpanAttributes.RESPONSE_SIZE]).toBeUndefined();
  });

  it("prompts/get is instrumented the same way as tools/call", async () => {
    const { client } = await connectedPair((server) => {
      server.prompt("greeting", async () => ({
        messages: [{ role: "assistant", content: { type: "text", text: "hello" } }],
      }));
    });

    await client.getPrompt({ name: "greeting" });

    const span = byMethod(exporter.getFinishedSpans(), "prompts/get")!;
    expect(attrs(span)[SpanAttributes.PROMPT_NAME]).toBe("greeting");
  });

  it("stamps network.transport from whatever was set, not hardcoded", async () => {
    resetTransport();
    setTransport("streamable-http", { explicit: true });
    const { client } = await connectedPair((server) => {
      server.tool("t", async () => ({ content: [{ type: "text", text: "x" }] }));
    });
    await client.callTool({ name: "t", arguments: {} });
    const span = byMethod(exporter.getFinishedSpans(), "tools/call")!;
    expect(attrs(span)[SpanAttributes.TRANSPORT]).toBe("streamable-http");
    setTransport("stdio", { explicit: true }); // restore for subsequent tests
  });

  it("methods with no allowlist entry are instrumented too -- tools/list, initialize", async () => {
    // The actual bug report this change responds to: tools/list (and every
    // other protocol method) was previously invisible entirely, not just
    // unclassified. patchDispatch instruments every method now (module
    // docstring), not a hardcoded subset -- this proves it for the two
    // methods a real MCP session always exercises.
    const { client } = await connectedPair((server) => {
      server.tool("t", async () => ({ content: [{ type: "text", text: "x" }] }));
    });
    await client.listTools();

    const spans = exporter.getFinishedSpans();
    const init = byMethod(spans, "initialize");
    const list = byMethod(spans, "tools/list");
    expect(init).toBeDefined();
    expect(attrs(init!)[SpanAttributes.FAILURE_KIND]).toBe(FailureKind.OK);
    expect(list).toBeDefined();
    expect(attrs(list!)[SpanAttributes.FAILURE_KIND]).toBe(FailureKind.OK);
    // No tool/prompt name for a method that never had one -- not an empty
    // string sitting in a span attribute where a real value should be.
    expect(attrs(list!)[SpanAttributes.TOOL_NAME]).toBeUndefined();
  });

  it("resources/read is instrumented with the resource's uri, not a tool/prompt name", async () => {
    const { client } = await connectedPair((server) => {
      server.resource("doc", "file:///doc.txt", async () => ({
        contents: [{ uri: "file:///doc.txt", text: "hello" }],
      }));
    });
    await client.readResource({ uri: "file:///doc.txt" });

    const span = byMethod(exporter.getFinishedSpans(), "resources/read")!;
    expect(attrs(span)[SpanAttributes.RESOURCE_URI]).toBe("file:///doc.txt");
  });
});
