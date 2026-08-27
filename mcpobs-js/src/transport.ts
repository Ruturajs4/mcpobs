/**
 * Which transport the server is running on.
 *
 * Deliberately simpler than the Python SDK's `transport.py`, which has three
 * tiers of precedence (explicit > server.run() argument > ASGI fallback)
 * because Python targets both stdio and streamable-HTTP MCP servers today.
 * The only real-world validation target for this SDK only ever
 * runs stdio: `src/index.ts` constructs a `StdioServerTransport`
 * unconditionally, and its `REMOTE_MCP=true` branch is a stub that logs and
 * returns immediately, implementing no transport at all. Building a
 * streamable-HTTP detection path now would be designing for a target that
 * does not exist yet -- exactly what this project's own decisions.md warns
 * against elsewhere. Add it when a real streamable-HTTP TS target shows up.
 */

const KNOWN_TRANSPORTS = new Set(["stdio", "streamable-http", "sse"]);

let current: string | null = null;
let explicit = false;

export function setTransport(value: string | null | undefined, opts: { explicit?: boolean } = {}): void {
  if (!value) return;
  if (explicit && !opts.explicit) return;
  if (!KNOWN_TRANSPORTS.has(value)) return;
  current = value;
  explicit = explicit || !!opts.explicit;
}

export function getTransport(): string | null {
  return current;
}

/** Test seam: module state is process-wide. */
export function resetTransport(): void {
  current = null;
  explicit = false;
}
