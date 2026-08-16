# Filtering and search

Every list view has a **Filters** panel on the right. Filters live in the URL,
so a narrowed view is a link you can paste to a colleague.

## The controls

| Control | Applies to |
| --- | --- |
| Search | Trace id, tool, server, method — case-insensitive substring |
| Server / Tool / Method | Exact match, from values present in the window |
| Status | Succeeded or failed |
| Category | A specific failure category |
| Duration | Slower than / faster than, in milliseconds |

Dropdowns are populated from **your data in the selected window**, so they never
offer a server that stopped reporting or omit one that appeared an hour ago.

## Advanced conditions

For anything the standard controls cannot express, add a condition:

```
Method   contains   list
Server   is not     staging
```

Conditions are combined with AND. Empty rows are ignored while you type.

## Active filters are always visible

Applied filters appear as chips above the list, each removable on its own. If a
filtered list comes back empty it tells you which filters are responsible and
offers to clear them — "no results" with no explanation is how people conclude
their data is missing.

## Pagination

Traces and Errors paginate with **Previous** / **Next**. Changing any filter
resets to the first page.

Capability tables (Tools, Prompts, Resources, Protocol) are not paginated —
they aggregate by name, so there is no time axis to page along. They show the
top 200 by whatever you sorted on, and say so when there are more.
