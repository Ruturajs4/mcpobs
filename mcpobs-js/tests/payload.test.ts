import { describe, it, expect } from "vitest";
import { capturePayload, capturePreview } from "../src/payload.js";

describe("capturePayload", () => {
  it("redacts a value under a secret-shaped key", () => {
    const result = capturePayload({ apiKey: "sk_live_abc123", name: "fine" });
    expect(result.preview).toContain('"apiKey":"[redacted]"');
    expect(result.preview).toContain('"name":"fine"');
    expect(result.preview).not.toContain("sk_live_abc123");
  });

  it("redacts a Bearer token embedded in an otherwise legitimate string", () => {
    // Matches mcpobs/http.py's SENSITIVE_VALUES exactly: the whole match
    // ("Bearer <token>") is replaced, not just the token -- there is no
    // capture group to preserve the "Bearer " prefix.
    const result = capturePayload({ header: "Bearer abc.def.ghi" });
    expect(result.preview).toContain('"header":"[redacted]"');
    expect(result.preview).not.toContain("abc.def.ghi");
  });

  it("redacts a token in a query string", () => {
    const result = capturePayload({ url: "https://api.example.com/x?api_key=SECRET&other=1" });
    expect(result.preview).toContain("api_key=[redacted]");
    expect(result.preview).toContain("other=1");
    expect(result.preview).not.toContain("SECRET");
  });

  it("truncates previews over MAX_PREVIEW and reports the pre-truncation size", () => {
    const big = "x".repeat(3_000);
    const result = capturePayload({ data: big });
    expect(result.preview.endsWith("...[truncated]")).toBe(true);
    expect(result.preview.length).toBeLessThan(result.size);
  });

  it("preserves a real Error's message when nested inside a result wrapper (dispatch.ts's actual shape)", () => {
    // This is the exact shape dispatch.ts's exception path builds:
    // {jsonrpc, id, result: <thrown Error>}. Regression guard for the bug
    // where Error's message/stack are non-enumerable, so a naive
    // JSON.stringify(err) silently produced "{}".
    const err = new Error("a real failure message");
    const result = capturePayload({ jsonrpc: "2.0", id: 1, result: err });
    expect(result.preview).toContain("a real failure message");
    expect(result.preview).not.toBe('{"jsonrpc":"2.0","id":1,"result":{}}');
  });

  it("preserves a real Error's message when passed directly, not just nested", () => {
    const err = new TypeError("bad input");
    const result = capturePayload(err);
    expect(result.preview).toContain("bad input");
    expect(result.preview).toContain("TypeError");
  });

  it("never includes the stack trace, deliberately", () => {
    const err = new Error("boom");
    const result = capturePayload({ result: err });
    expect(result.preview).not.toContain("at ");
    expect(result.preview).not.toMatch(/\.ts:\d+/);
  });

  it("does not throw on a value that cannot be serialized (circular reference)", () => {
    const circular: Record<string, unknown> = { name: "x" };
    circular.self = circular;
    expect(() => capturePayload(circular)).not.toThrow();
    const result = capturePayload(circular);
    expect(result).toEqual({ preview: "", size: 0 });
  });
});

describe("capturePreview", () => {
  it("is a thin wrapper returning just the preview string", () => {
    expect(capturePreview({ name: "fine" })).toBe('{"name":"fine"}');
  });
});
