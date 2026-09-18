# -*- coding: utf-8 -*-
"""The tool catalogue: what the assistant is offered, and what runs.

Definitions live next to the code they call so the two cannot drift. The
controller only dispatches; adding a tool means adding an entry here.

Descriptions are written for a model rather than for documentation. Each one
says when to reach for the tool, and - where the answer could be mistaken for a
guarantee - what it does not cover, because that is what stops a snapshot being
relayed as a promise.
"""

from . import readonly
from .shortage import shortage_check

# Every tool in this module reads and nothing else, so they share annotations.
READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


def _schema(properties, required):
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_PRODUCT_CODE = {
    "type": "string",
    "description": "Internal reference, barcode, or an unambiguous name.",
}
_WAREHOUSE_CODE = {
    "type": "string",
    "description": "Optional warehouse code such as 'WH'. Omit to cover every internal location in the company.",
}
_LIMIT = {
    "type": "integer",
    "minimum": 1,
    "description": "Maximum rows to return.",
}


TOOLS = [
    {
        "name": "shortage_check",
        "title": "Check whether a product can be built now",
        "description": (
            "Answer whether N units of a manufactured product can be built from "
            "stock that is free right now, and if not, which components are short "
            "and how many units are possible. Counts only unreserved stock, so it "
            "reflects what can actually start today rather than what is on the "
            "shelf. Does NOT consider incoming purchase orders, unreserved "
            "competing demand, work-center capacity, or lot constraints - the "
            "answer carries a 'limitations' list saying so, which should be "
            "passed on rather than dropped."
        ),
        "inputSchema": _schema({
            "product_code": _PRODUCT_CODE,
            "quantity": {"type": "number", "exclusiveMinimum": 0,
                         "description": "How many finished units to test."},
            "warehouse_code": _WAREHOUSE_CODE,
            "bom_code": {"type": "string",
                         "description": "Optional. Force a specific bill of materials by its code."},
        }, ["product_code", "quantity"]),
        "handler": lambda env, a: shortage_check(
            env,
            product_code=a.get("product_code"),
            quantity=a.get("quantity"),
            warehouse_code=a.get("warehouse_code"),
            bom_code=a.get("bom_code"),
        ),
    },
    {
        "name": "product_search",
        "title": "Find a product",
        "description": (
            "Look up products by code, barcode or name when the exact internal "
            "reference is not known. Reports whether each match is storable and "
            "whether it has a bill of materials, which decides whether it can be "
            "manufactured or only bought."
        ),
        "inputSchema": _schema({
            "query": {"type": "string", "description": "Text to search for."},
            "limit": _LIMIT,
        }, ["query"]),
        "handler": lambda env, a: readonly.product_search(
            env, query=a.get("query"), limit=a.get("limit", 10)
        ),
    },
    {
        "name": "stock_status",
        "title": "On-hand, reserved and free stock",
        "description": (
            "How much of a product exists, how much is already committed to other "
            "work, and how much is genuinely free, broken down by location. Use "
            "this when the question is about one product rather than about "
            "building something. Incoming supply is not counted."
        ),
        "inputSchema": _schema({
            "product_code": _PRODUCT_CODE,
            "warehouse_code": _WAREHOUSE_CODE,
        }, ["product_code"]),
        "handler": lambda env, a: readonly.stock_status(
            env, product_code=a.get("product_code"),
            warehouse_code=a.get("warehouse_code"),
        ),
    },
    {
        "name": "lot_trace",
        "title": "Trace a lot or serial number",
        "description": (
            "Where a lot or serial number currently sits and which customers "
            "received it, following stock moves through production so a lot "
            "consumed into an assembly still resolves to the customers that "
            "assembly shipped to. This is the recall question. Upstream tracing "
            "(what the lot was made from) is not included."
        ),
        "inputSchema": _schema({
            "lot_name": {"type": "string", "description": "The exact lot or serial number."},
            "product_code": dict(_PRODUCT_CODE, description=(
                "Optional. Needed only when the same lot name exists for more "
                "than one product.")),
        }, ["lot_name"]),
        "handler": lambda env, a: readonly.lot_trace(
            env, lot_name=a.get("lot_name"), product_code=a.get("product_code")
        ),
    },
    {
        "name": "bom_explode",
        "title": "Components needed for a quantity",
        "description": (
            "What a quantity of a product is made of, with kits expanded and a "
            "component reachable by two paths counted once. Requirements only - "
            "it says nothing about availability, so use shortage_check when the "
            "question is whether the work can start."
        ),
        "inputSchema": _schema({
            "product_code": _PRODUCT_CODE,
            "quantity": {"type": "number", "exclusiveMinimum": 0,
                         "description": "Finished quantity. Defaults to 1."},
            "bom_code": {"type": "string", "description": "Optional specific bill of materials."},
        }, ["product_code"]),
        "handler": lambda env, a: readonly.bom_explode(
            env, product_code=a.get("product_code"),
            quantity=a.get("quantity", 1.0), bom_code=a.get("bom_code"),
        ),
    },
    {
        "name": "production_order_status",
        "title": "Status of one manufacturing order",
        "description": (
            "State, quantities produced against ordered, dates, and Odoo's own "
            "component-availability summary for a single manufacturing order, "
            "plus its component lines."
        ),
        "inputSchema": _schema({
            "order_name": {"type": "string", "description": "Order reference, e.g. 'WH/MO/00007'."},
        }, ["order_name"]),
        "handler": lambda env, a: readonly.production_order_status(
            env, order_name=a.get("order_name")
        ),
    },
    {
        "name": "production_backlog",
        "title": "Open manufacturing orders",
        "description": (
            "Manufacturing orders that are confirmed, in progress or ready to "
            "close, highest priority and soonest first, each with its component "
            "availability. Use this for 'what should we look at today'."
        ),
        "inputSchema": _schema({
            "warehouse_code": _WAREHOUSE_CODE,
            "limit": _LIMIT,
        }, []),
        "handler": lambda env, a: readonly.production_backlog(
            env, warehouse_code=a.get("warehouse_code"), limit=a.get("limit", 20)
        ),
    },
    {
        "name": "stock_move_history",
        "title": "Recent stock movements",
        "description": (
            "Completed movements for a product or a lot, most recent first, with "
            "source and destination. Use it to explain how a quantity got to "
            "where it is. Planned and reserved moves are not shown."
        ),
        "inputSchema": _schema({
            "product_code": _PRODUCT_CODE,
            "lot_name": {"type": "string", "description": "Restrict to one lot or serial number."},
            "limit": _LIMIT,
        }, []),
        "handler": lambda env, a: readonly.stock_move_history(
            env, product_code=a.get("product_code"),
            lot_name=a.get("lot_name"), limit=a.get("limit", 20),
        ),
    },
]

BY_NAME = {tool["name"]: tool for tool in TOOLS}


def definitions():
    """The catalogue as tools/list should send it - handlers stripped."""
    return [
        {
            "name": tool["name"],
            "title": tool["title"],
            "description": tool["description"],
            "inputSchema": tool["inputSchema"],
            "annotations": READ_ONLY,
        }
        for tool in TOOLS
    ]
