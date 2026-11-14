# DatasetPlaza MCP

**Read-only dataset routing for agents.** It does not fetch data, hold data, or
resell access. It answers one question: *which published source can answer this,
how do I reach it, and is the platform actually entitled to claim that?*

The catalogue behind it holds 37,847 dataset cards — China National Bureau of
Statistics annual and provincial tables, Hong Kong open data, World Bank,
Guizhou portals and more. Each card carries a capability profile, a locator with
its indicator list, an evidence provenance, an access level and a
machine-readable next step. The agent then connects to the source itself.

## Why this exists

Search engines and LLM answers happily return a plausible-looking dataset that
cannot actually answer the question. This server is built around the opposite
promise: **say "no" honestly**.

- `NO_MATCH` + `shelf_gap` — no capability statement on the shelf can take this.
- `ACCESS_RESTRICTED` + `access_gap` — a route exists but requires registration,
  authorisation or payment; the platform never pays or borrows credentials.
- `OK` — only when a candidate is genuinely verified with no named gap.

Evidence provenance is explicit: `declared` (source-side measured statement),
`source_mapped` (translated from the publisher's own classification tree),
`inferred` (guessed from a name by us). Only `declared` enters `verified_axes`
and can unlock `complete`. `CONTRACT.md` is the full contract, including the
evaluation baseline and the known limits.

## What this repository contains

Code and contract only. **No catalogue.** The index is ~313 MB, which exceeds
GitHub's per-file limit and is not something a git history should carry. The
data snapshot is distributed separately; see [Data](#data).

## Install

No third-party dependencies. Python 3.10+.

```bash
pip install datasetplaza-mcp
# or, one-shot:
uvx datasetplaza-mcp --data /absolute/path/to/catalogue
```

## Data

`--data` expects a directory that holds `listings/listings.json`, plus a thin
index under `listings/` that is derived from it. Put the catalogue there and
build the index once:

```bash
python scripts/build_shelf.py --no-rows
```

Then point the server at that directory root:

```bash
datasetplaza-mcp --data /absolute/path/to/catalogue
```

## MCP client configuration

```json
{
  "mcpServers": {
    "datasetplaza": {
      "command": "uvx",
      "args": ["datasetplaza-mcp", "--data", "/absolute/path/to/catalogue"]
    }
  }
}
```

Transport is stdio JSON-RPC 2.0. A remote Streamable HTTP gateway
(`python -m bdsp.gateway`) is available for self-hosting; it is anonymous,
read-only by default, IP rate-limited and binds to 127.0.0.1. Add
`--strict-readonly` to remove the single write tool from the public surface.

## Tools (13)

`dp_map`, `dp_catalog`, `dp_discover`, `dp_index`, `dp_card`, `dp_compare`,
`dp_hub`, `dp_hub_route`, `dp_pitfalls`, `dp_report_pitfall`, `dp_usage`,
`dp_forms`, `dp_audit`.

Every tool carries `annotations.readOnlyHint`. Twelve are read-only;
`dp_report_pitfall` appends a "pitfall" note (it never modifies or deletes) and
is rate-limited to 5 writes per day per caller on the public gateway.

## Boundaries

- Leads are **not** proof of existence and contain **no values**.
- An undeclared licence is not permission to redistribute.
- Metadata goes stale. Cards reflect what was measured at collection time;
  verify against the source before relying on an access level or an endpoint.
- The catalogue metadata remains subject to each publisher's own terms. The code
  licence does not cover the data.

## Licence

Apache-2.0. The patent grant and the termination clause are the reason this
project is not MIT; see `LICENSE`.
