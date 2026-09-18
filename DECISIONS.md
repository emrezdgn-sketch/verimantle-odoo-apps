# DECISIONS.md

Stable decisions only — things a future session should not re-open or
re-debate without new contradicting evidence. Append, don't rewrite history.
Keep entries short.

---

**2026-09-18 — Pricing for `mrp_mcp_governance` is closed.**
€129, one-time, per Odoo major version. Not a subscription. No
per-database enforcement is implemented or claimed.

**2026-09-18 — Pre-publication review for `mrp_mcp_tools` is cleared.**
All internal review steps required before listing on Odoo Apps are
complete. Do not re-open this without a concrete new reason.

**2026-09-18 — Publisher metadata values are final.**
`AUTHOR_DISPLAY_NAME=VeriMantle`, `WEBSITE_URL=https://verimantle.com`,
`SUPPORT_EMAIL_OR_URL=support@verimantle.com`. Applied to both manifests.

**2026-09-18 — Odoo Apps re-uploads require a version bump.**
apps.odoo.com does not treat a re-uploaded zip with an unchanged
`version` string as a new release — the description/asset changes inside
it don't go live. Always bump the patch segment of `version` in
`__manifest__.py` before rebuilding the upload zip, even for a
storefront-only (HTML/CSS) change.

**2026-09-18 — Never build the Odoo Apps upload zip with PowerShell
`Compress-Archive`.** It writes backslash entry names; non-Windows hosts
(including the Apps store's own unpacking) silently drop whole folders.
Use a plain `zip -rX` (or equivalent) from the module's parent directory.

**2026-09-18 — Live Odoo Apps rendering is authoritative over local preview.**
The storefront page runs inside apps.odoo.com's own Bootstrap-loaded page
chrome. A fix that looks correct in an isolated local render can still
break on the live listing (seen with `.card`/`.grid`/`.lead` colliding with
Bootstrap's own classes). Verify storefront fixes against the live URL,
not a local file open.

**2026-09-18 — `verimantle-odoo-apps` is the publication tree, not the
working repo.** It holds only the two shippable module directories plus
this memory system. Additional internal project documentation is
maintained outside this repository and should not be duplicated here.
