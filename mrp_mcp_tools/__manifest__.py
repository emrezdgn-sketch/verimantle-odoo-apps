# -*- coding: utf-8 -*-
{
    # Max 25 characters, no adjectives, no company name - store rule.
    "name": "Manufacturing MCP Tools",
    "version": "19.0.0.1.1",
    "category": "Supply Chain/Manufacturing",
    "summary": "Let AI agents read stock, lots, BOMs and shortages. Read-only.",

    "author": "VeriMantle",
    "website": "https://verimantle.com",
    "support": "support@verimantle.com",

    "license": "LGPL-3",
    "depends": ["mrp", "stock"],
    "data": [
        "security/mcp_tools_security.xml",
        "security/ir.model.access.csv",
    ],
    "images": ["static/description/banner.png"],
    "installable": True,
    "application": False,
    "description": """
Manufacturing MCP Tools
=======================

Serves a Model Context Protocol endpoint from inside Odoo, so an assistant
such as Claude, ChatGPT or Gemini can answer questions about what your factory
can actually do today.

Eight tools, every one of them read-only:

* shortage_check - can N units be built right now, and if not, what is short
* stock_status - on hand, reserved, and genuinely free, by location
* lot_trace - where a lot is and which customers received it
* bom_explode - components for a quantity, kits expanded
* production_order_status - state, progress and component availability
* production_backlog - open orders, priority and date order
* stock_move_history - completed movements for a product or lot
* product_search - find a product, and whether it can be manufactured

What makes the answers different from a generic connector: they count free
stock rather than on-hand stock. Twenty plates on the shelf with six reserved
for another order will build seven brackets, not ten. Every answer also
carries a list of what it did not check, so a snapshot is not relayed as a
promise.

The module writes nothing. The agent connects as a dedicated user holding read
rights and no others: Odoo's own access control denies create, write and delete
to it on every business model, through any protocol. The module's own code is
kept from escalating past that by a static check that scans the tool and
endpoint source on every test run and fails if a sudo() or writing call
appears. Neither control protects against someone with database or server
access.

Install, create an API key, point your assistant at /mcp.
""",
}
