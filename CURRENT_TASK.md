# CURRENT_TASK.md

This file holds exactly **one** active task. When the task changes, replace
this whole file — don't append to it or keep old tasks around.

## Active task: fix live mobile rendering of the `mrp_mcp_tools` storefront

- `mrp_mcp_tools` is **live** on Odoo Apps
  (`https://apps.odoo.com/apps/modules/19.0/mrp_mcp_tools`).
- A prior fix (commit `89f38d2`, then `0ac0e63`) renamed classes that
  collided with Bootstrap (`.lead`/`.grid`/`.card`/`table.stack` →
  `.vm-lead`/`.vm-grid`/`.vm-card`/`table.mcp-table`) and moved to a
  mobile-first CSS approach. It is committed, pushed, and already reflected
  in the uploaded `19.0.0.1.1` package.
- **Despite that fix, real Odoo Apps mobile rendering is still broken**:
  - The "eight tools" section remains cramped.
  - The "Built so the answers stay honest" cards break or disappear.
- **Live Odoo Apps rendering is authoritative.** A local Chrome/browser
  preview of the same HTML is not sufficient evidence that a fix worked —
  the host page's Bootstrap/CSS environment on apps.odoo.com is what matters,
  and it has already fooled a previous fix once.
- Next step: further **storefront-only HTML/CSS correction** to
  `mrp_mcp_tools/static/description/index.html` (and its inline `<style>`),
  verified against the actual live listing page, not a local render.

## Explicitly out of scope for this task

- No changes to Python source, business logic, or tests in either module.
- No changes to `__manifest__.py` in either module.
- No commit, no push, without explicit user approval first.
- Do not touch `mrp_mcp_governance` — this task is `mrp_mcp_tools` storefront
  only.
