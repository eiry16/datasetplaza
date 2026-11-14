mcp-name: io.github.eiry16/datasetplaza

# DatasetPlaza MCP

**Read-only dataset routing for agents.** It does not fetch data, hold data, or
resell access. It answers one question: *which published source can answer this,
how do I reach it, and is the platform actually entitled to claim that?*

DatasetPlaza is an index of 37,847 dataset cards (China National Bureau of
Statistics annual/provincial tables, Hong Kong open data, World Bank, Guizhou
portals and more). Each card carries a capability profile, a locator with the
indicator list, an evidence provenance, an access level and a machine-readable
next step. The agent then connects to the source itself.

## Why this exists

Search engines and LLM answers happily return a plausible-looking dataset that
cannot actually answer the question. This server is built around the opposite
promise: **say "no" honestly**. It distinguishes:

- `NO_MATCH` + `shelf_gap` — no capability statement on the shelf can take this.
- `ACCESS_RESTRICTED` + `access_gap` — a route exists but requires registration,
  authorisation or payment; the platform never pays or borrows credentials.
- `OK` — only when a candidate is genuinely verified with no named gap.

Evidence provenance is explicit: `declared` (source-side measured statement),
`source_mapped` (translated from the publisher's own classification tree),
`inferred` (guessed from a name by us). Only `declared` enters `verified_axes`
and can unlock `complete`. See `CONTRACT.md` in the repository for the full
contract and the evaluation baseline.

## Install

No third-party dependencies. Python 3.10+.

```bash
pip install datasetplaza-mcp
# or, one-shot:
uvx datasetplaza-mcp --data /path/to/DatasetPlaza
```

The wheel deliberately contains **no data** (the catalogue is 313 MB). Point
`--data` at a directory holding `listings/listings.json`, then build the thin
index once:

```bash
python scripts/build_shelf.py --no-rows
```

The repository ships code and contract only — the catalogue snapshot is
distributed separately, so `--data` must point at your own copy.

## MCP client configuration

```json
{
  "mcpServers": {
    "datasetplaza": {
      "command": "uvx",
      "args": ["datasetplaza-mcp", "--data", "/absolute/path/to/DatasetPlaza"]
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

Copyright 2026 eiry. Licensed under Apache-2.0. The catalogue metadata it
points to remains subject to each publisher's own terms; the code licence
does not cover the data. Repository: https://github.com/eiry16/datasetplaza
