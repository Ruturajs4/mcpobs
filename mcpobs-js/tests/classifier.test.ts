import { describe, it, expect } from "vitest";
import { classify, classifyResult, firstText, FailureKind, FailureKindSource } from "../src/classifier.js";

describe("classify", () => {
  it("clean success is ok, source clean", () => {
    const result = classify("", false);
    expect(result).toEqual({ kind: FailureKind.OK, source: FailureKindSource.CLEAN });
  });

  it("ok even with leftover text if isError is not set", () => {
    const result = classify("some non-error text", undefined);
    expect(result.kind).toBe(FailureKind.OK);
  });

  it("matches the SDK's exact 'not found' string for unknown tool", () => {
    // Verbatim string McpServer's own dispatcher generates -- read directly
    // from node_modules/@modelcontextprotocol/sdk/dist/esm/server/mcp.js,
    // not guessed.
    const result = classify("Tool doesNotExist not found", true);
    expect(result).toEqual({ kind: FailureKind.UNKNOWN_TOOL, source: FailureKindSource.SDK_DISPATCH });
  });

  it("matches the SDK's 'disabled' string for unknown tool too", () => {
    const result = classify("Tool someTool disabled", true);
    expect(result.kind).toBe(FailureKind.UNKNOWN_TOOL);
  });

  it("matches the SDK's exact invalid-arguments prefix", () => {
    const result = classify(
      "Input validation error: Invalid arguments for tool fetchScreenshots: Required",
      true,
    );
    expect(result).toEqual({
      kind: FailureKind.INVALID_ARGUMENTS,
      source: FailureKindSource.SDK_DISPATCH,
    });
  });

  it("a downstream server's own ad-hoc text falls to tool_error, not a guess", () => {
    // Shape modeled on a real downstream MCP server's ad-hoc error formatter --
    // deliberately NOT pattern-matched further, per the confirmed taxonomy-scope decision.
    const result = classify(
      "Error during fetching automate screenshots: Request failed with status code 401",
      true,
    );
    expect(result).toEqual({ kind: FailureKind.TOOL_ERROR, source: FailureKindSource.IS_ERROR_FLAG });
  });

  it("isError true with no text at all is unclassified, not a silent ok", () => {
    const result = classify("", true);
    expect(result).toEqual({ kind: FailureKind.UNCLASSIFIED, source: FailureKindSource.UNCLASSIFIED });
  });

  it("a real downstream tool_error message must not accidentally match unknown_tool/invalid_arguments", () => {
    // Regression guard: the two SDK patterns are narrow ("Tool X not found",
    // "Input validation error: ...") specifically so ordinary application
    // errors mentioning the word "tool" don't get misclassified as SDK-level.
    const result = classify("The requested tool configuration is invalid for this device", true);
    expect(result.kind).toBe(FailureKind.TOOL_ERROR);
  });
});

describe("firstText", () => {
  it("extracts the first text block from a CallToolResult shape", () => {
    const result = { content: [{ type: "text", text: "hello" }], isError: false };
    expect(firstText(result)).toBe("hello");
  });

  it("skips non-text content blocks to find the first text one", () => {
    const result = {
      content: [
        { type: "image", data: "base64...", mimeType: "image/png" },
        { type: "text", text: "the actual message" },
      ],
    };
    expect(firstText(result)).toBe("the actual message");
  });

  it("returns empty string for a result with no text content", () => {
    expect(firstText({ content: [] })).toBe("");
    expect(firstText(undefined)).toBe("");
  });

  it("reads .message off a real thrown Error", () => {
    expect(firstText(new Error("boom"))).toBe("boom");
  });
});

describe("classifyResult", () => {
  it("classifies a real CallToolResult end to end", () => {
    const result = { content: [{ type: "text", text: "Tool xyz not found" }], isError: true };
    expect(classifyResult(result)).toEqual({
      kind: FailureKind.UNKNOWN_TOOL,
      source: FailureKindSource.SDK_DISPATCH,
    });
  });

  it("classifies a thrown exception (the UrlElicitationRequired escape hatch case)", () => {
    const err = new Error("some thrown error, not a CallToolResult");
    expect(classifyResult(undefined, err).kind).toBe(FailureKind.TOOL_ERROR);
  });
});
