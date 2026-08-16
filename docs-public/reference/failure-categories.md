# Failure categories

The complete list, with what produces each.

| Category | Counts as a failure | Produced when |
| --- | --- | --- |
| `ok` | No | The call succeeded |
| `tool_error` | **Yes** | The tool returned an error result |
| `server_exception` | **Yes** | The tool raised an unhandled exception |
| `unknown_tool` | **Yes** | The requested tool does not exist |
| `invalid_arguments` | **Yes** | Arguments failed validation before the tool ran |
| `protocol_error` | **Yes** | Malformed request; never reached a tool |
| `unclassified` | **Yes** | Not enough information to categorise |
| `pending_input` | No | A multi-round-trip call is awaiting an answer |
| `cancelled` | No | The client gave up |
| `unauthorized` | No | Transport-level 401 |
| `forbidden` | No | Transport-level 403 |

The four "No" rows are explained in
[Failure taxonomy](../concepts/failures.md#three-things-that-are-not-failures) —
each is a deliberate decision, not an omission.

## Classification source

Every span records how it was classified:

- **helper** — the `mcpobs` middleware read the SDK's own result and assigned a
  precise category.
- **span** — no helper present; the category was inferred from the bare span,
  which can only produce the coarse `tool_error`.

The console reports the share that is precise. When it is below 100%, some of
your servers are not running the helper.
