# -*- coding: utf-8 -*-
#
# Price: DECIDED at 129.00 EUR, one-time, per Odoo major version. See
# docs/publication/PRICING_DECISION.md for the market evidence. In short: the
# AI-agent governance niche on Odoo 19 prices at EUR 85-172, the closest
# functional competitor is EUR 149, and EUR 129 is an absorbed price point in
# this store.
#
{
    # Max 25 characters, no adjectives, no company name - store rule.
    "name": "MCP Agent Governance",
    "version": "19.0.0.1.0",
    "category": "Supply Chain/Manufacturing",
    "summary": "AI agents propose, people approve. Approval queue and audit log.",

    "author": "VeriMantle",
    "website": "https://verimantle.com",
    "support": "support@verimantle.com",

    "price": 129.00,
    "currency": "EUR",

    "license": "OPL-1",
    "depends": ["mrp", "stock", "mail", "mrp_mcp_tools"],
    "data": [
        "security/mcp_security.xml",
        "security/ir.model.access.csv",
        "data/mcp_data.xml",
        "views/mcp_proposal_views.xml",
    ],
    "images": ["static/description/banner.png"],
    "installable": True,
    "application": False,
    "description": """
MCP Agent Governance
====================

AI agents are starting to create invoices, edit contacts and move stock inside
Odoo. This module answers the question that follows: who approved that.

It does not let an agent write and then record what happened. The agent cannot
write at all. A sensitive action produces a proposal record, and a person turns
that proposal into a real document under their own access rights - if they may
not create a manufacturing order by hand, approving one does not create it
either.

* Proposals instead of writes. Nothing reaches a business model until a person
  acts on it.
* Separation of duties. Whoever requested a proposal cannot approve it, and the
  agent identity can never approve anything.
* Validated on arrival. An archived bill of materials, a zero quantity or a
  missing warehouse is refused when the proposal is made, so a reviewer is
  never shown something that would fail.
* Proposals expire. Stock moves; a four-hour-old "this is buildable" is not a
  fact any more.
* An append-only log of every tool call, reads included. Competing products
  record writes; the question an auditor actually asks is what the agent looked
  at before it suggested that.

For regulated manufacturing the distinction matters: rollback is not a remedy
once a lot has shipped. "It never wrote" is a different sentence from "we undid
it".

Requires Manufacturing MCP Tools, which provides the agent identity and the
read-only tools.
""",
}
