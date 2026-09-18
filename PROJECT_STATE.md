# PROJECT_STATE.md — VeriMantle

Compact, authoritative snapshot. If this contradicts the code or git history,
the repository wins — update this file, don't trust memory over it.

## What this is

**VeriMantle** — two Odoo 19 modules sold under one brand, positioned as
"Manufacturing AI Governance for Odoo" (deliberately not "another Odoo MCP
connector").

| Module | Technical name | Licence | Price |
| --- | --- | --- | --- |
| Free layer | `mrp_mcp_tools` | LGPL-3 | Free |
| Paid layer | `mrp_mcp_governance` | OPL-1 | €129, one-time, per Odoo major version |

- **`mrp_mcp_tools`** serves eight **read-only** Manufacturing MCP tools from
  inside Odoo (`shortage_check`, `stock_status`, `lot_trace`, `bom_explode`,
  `production_order_status`, `production_backlog`, `stock_move_history`,
  `product_search`). The agent user has no create/write/delete rights on any
  business model, through any protocol.
- **`mrp_mcp_governance`** depends on `mrp_mcp_tools` and adds the approval
  boundary: an agent proposes, a person approves, state is revalidated at
  apply time, everything is logged. Requires `mrp`, `stock`, `mail`.

## Architecture

- Odoo 19, two installable, non-`application` modules.
- `mrp_mcp_tools/controllers/mcp.py` serves the MCP endpoint (`/mcp`).
- `mrp_mcp_tools/tools/` holds the eight read-only tool implementations plus
  a static scanner that fails the test run if a `sudo()` or writing call
  appears in tool/endpoint source.
- `mrp_mcp_governance/models/` + `views/mcp_proposal_views.xml` implement the
  proposal → approval → apply flow (Governance > Proposals in the Odoo UI).

## Repository structure (this repo)

This repo is the **publication tree** — only what ships to GitHub and to the
Odoo Apps upload:

```
mrp_mcp_tools/        # free module, LGPL-3
mrp_mcp_governance/   # paid module, OPL-1
```

Additional internal project documentation is maintained outside this
repository.

## Publication status

- **`mrp_mcp_tools` is live on the Odoo Apps store**:
  `https://apps.odoo.com/apps/modules/19.0/mrp_mcp_tools`.
- Pre-publication review for `mrp_mcp_tools` is complete. Publisher metadata
  (`author`, `website`, `support`) is finalized in the manifest as
  `VeriMantle` / `https://verimantle.com` / `support@verimantle.com`.
- `mrp_mcp_governance` publication status on the Apps store: not yet
  confirmed live — check the store page before assuming.

## Latest release/version baseline

- `mrp_mcp_tools` version: `19.0.0.1.1` (bumped from `19.0.0.1.0` on
  2026-09-18 specifically so apps.odoo.com would register a re-upload as a
  new release rather than a duplicate).
- `mrp_mcp_governance` version: `19.0.0.1.0` (unchanged).
- Last commit on `19.0`: `0ac0e63` — "fix: resolve Bootstrap class collision
  in MCP Tools storefront description, bump version". Branch is up to date
  with `origin/19.0`.
- A release gate and Odoo test suite back this baseline. Additional internal
  project documentation is maintained outside this repository — don't
  restate specific counts here, since they go stale.

## Known product boundaries — do not misrepresent

- `mrp_mcp_tools` writes nothing, ever, through any protocol.
- Pricing for `mrp_mcp_governance` is a one-time €129 per Odoo major version —
  **not** a subscription, no per-database enforcement implemented or claimed.
- Any technical claim used in marketing copy should trace back to actual
  test/source evidence rather than intuition — don't make the product sound
  more capable than the shipped code. Additional internal project
  documentation is maintained outside this repository.

## GitHub / Odoo Apps publication structure

- Hosted on GitHub, branch `19.0` (matches the Odoo series the modules
  target).
- Odoo Apps: modules are uploaded manually via the publisher's "My Apps"
  dashboard on apps.odoo.com, using a locally built upload zip. There is no
  CI/automated push to the Apps store — every upload is a manual,
  human-approved action.
