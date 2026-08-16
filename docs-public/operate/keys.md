# API keys and access

Access is **invite-only**. There is no self-service signup, and no endpoint that
mints a key.

## Scopes

| Scope | Used by | Lives in |
| --- | --- | --- |
| `ingest` | Your MCP server, sending telemetry | Server process, deployment config |
| `read` | The console and the Query API | A browser |
| `admin` | The operator console | Issued out of band |

The three are deliberately separate. An ingest key and a read key identify the
same organisation, but they live in different places — so one being compromised
must not imply the other. The console refuses an ingest key; the ingest endpoint
refuses a read key.

## Storage

Key secrets are stored as **hashes**. The full key is shown once, when it is
issued, and cannot be retrieved afterwards — including by an operator. The
console lists key **prefixes** only, so there is nothing in the listing to leak.

## Revocation

Revoking a key takes effect within the principal cache TTL (30 seconds) in every
running process. There is no restart required.

## Rotation

Issue the new key, deploy it, then revoke the old one. Because ingest and read
keys are separate, rotating one does not interrupt the other.
