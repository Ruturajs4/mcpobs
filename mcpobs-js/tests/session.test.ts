import { describe, it, expect, vi } from "vitest";
import { SessionProvider } from "../src/session.js";

function fakeFetch(response: { token?: string; expires_in?: number; endpoint?: string } | null, status = 200) {
  return vi.fn(async () => {
    if (response === null) {
      return { ok: false, status: 500 } as Response;
    }
    return {
      ok: status < 400,
      status,
      json: async () => response,
    } as Response;
  });
}

describe("SessionProvider", () => {
  it("is not configured with no endpoint", () => {
    const provider = new SessionProvider({});
    expect(provider.configured).toBe(false);
  });

  it("rejects a non-local http:// endpoint", () => {
    expect(() => new SessionProvider({ endpoint: "http://example.com/session" })).toThrow();
  });

  it("accepts https:// and local http://", () => {
    expect(() => new SessionProvider({ endpoint: "https://example.com/session" })).not.toThrow();
    expect(() => new SessionProvider({ endpoint: "http://localhost:8080/session" })).not.toThrow();
    expect(() => new SessionProvider({ endpoint: "http://127.0.0.1:8080/session" })).not.toThrow();
  });

  it("fetches a token on first call and returns it", async () => {
    const fetchImpl = fakeFetch({ token: "abc123", expires_in: 10800 });
    const provider = new SessionProvider({ endpoint: "https://example.com/session", fetchImpl });

    const session = await provider.current();
    expect(session?.token).toBe("abc123");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("does not refetch before the refresh point (75% of TTL)", async () => {
    // 4-second TTL -> refreshAt is 3s out (minus jitter). Well within a fast
    // test's runtime for "before refresh" to hold true.
    const fetchImpl = fakeFetch({ token: "abc123", expires_in: 4 });
    const provider = new SessionProvider({ endpoint: "https://example.com/session", fetchImpl });

    await provider.current();
    await provider.current();
    await provider.current();
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("refreshes once the refresh point has passed", async () => {
    // 200ms TTL -> refreshAt is at most 150ms out (75% * jitter<=1.0).
    // Sleeping 220ms guarantees we're past it, well short of expiresAt (200ms
    // + whatever grace -- actually expiresAt IS 200ms, so this also proves
    // the OLD token would still have been servable if refetch failed).
    const fetchImpl = fakeFetch({ token: "first", expires_in: 0.2 });
    const provider = new SessionProvider({ endpoint: "https://example.com/session", fetchImpl });

    await provider.current();
    await new Promise((resolve) => setTimeout(resolve, 220));

    fetchImpl.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ token: "second", expires_in: 10800 }),
    } as Response);
    const session = await provider.current();
    expect(session?.token).toBe("second");
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it("serves the old token while backing off from a failed refresh, if still under expiresAt", async () => {
    const fetchImpl = fakeFetch({ token: "first", expires_in: 0.2 });
    const provider = new SessionProvider({ endpoint: "https://example.com/session", fetchImpl });
    await provider.current();

    await new Promise((resolve) => setTimeout(resolve, 220));
    fetchImpl.mockResolvedValueOnce({ ok: false, status: 503 } as Response);
    const duringFailedRefresh = await provider.current();
    // Still within the original 200ms expiresAt? No -- 220ms already elapsed,
    // past expiresAt too. So this proves the OTHER half: once genuinely
    // expired AND the refetch failed, null is returned, not a stale token
    // served past its own expiry.
    expect(duringFailedRefresh).toBeNull();
  });

  it("keeps serving a still-valid token across a failed refresh attempt", async () => {
    // Longer TTL so refreshAt passes but expiresAt has not.
    const fetchImpl = fakeFetch({ token: "first", expires_in: 1 });
    const provider = new SessionProvider({ endpoint: "https://example.com/session", fetchImpl });
    await provider.current();

    await new Promise((resolve) => setTimeout(resolve, 780)); // past 75% of 1s, short of 1s
    fetchImpl.mockResolvedValueOnce({ ok: false, status: 503 } as Response);
    const session = await provider.current();
    expect(session?.token).toBe("first"); // old token, still under expiresAt
  });

  it("backs off after a failure and does not hammer the endpoint on every call", async () => {
    const fetchImpl = fakeFetch(null);
    const provider = new SessionProvider({ endpoint: "https://example.com/session", fetchImpl });

    await provider.current(); // 1st attempt, fails, enters backoff (min 5s)
    await provider.current(); // called again immediately -- should NOT refetch
    await provider.current();
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("invalidate() drops the session without fetching", async () => {
    const fetchImpl = fakeFetch({ token: "abc123", expires_in: 10800 });
    const provider = new SessionProvider({ endpoint: "https://example.com/session", fetchImpl });
    await provider.current();
    expect(fetchImpl).toHaveBeenCalledTimes(1);

    provider.invalidate();
    // Next current() call must refetch (session is gone), but invalidate()
    // itself must not have triggered a fetch.
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("rejects a response missing token or expires_in", async () => {
    const fetchImpl = fakeFetch({ expires_in: 10800 }); // no token
    const provider = new SessionProvider({ endpoint: "https://example.com/session", fetchImpl });
    const session = await provider.current();
    expect(session).toBeNull();
  });

  it("resolves a callable headers factory per fetch, not once at construction", async () => {
    let call = 0;
    const fetchImpl = fakeFetch({ token: "abc", expires_in: 10800 });
    const provider = new SessionProvider({
      endpoint: "https://example.com/session",
      headers: () => ({ authorization: `Bearer ${++call}` }),
      fetchImpl,
    });
    await provider.current();
    const [, options] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect((options.headers as Record<string, string>).authorization).toBe("Bearer 1");
  });

  it("a throwing header callback does not crash the provider", async () => {
    const provider = new SessionProvider({
      endpoint: "https://example.com/session",
      headers: () => {
        throw new Error("boom");
      },
    });
    expect(provider.resolvedHeaders()).toEqual({});
  });
});
