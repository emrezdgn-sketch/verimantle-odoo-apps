# -*- coding: utf-8 -*-
"""The seven read-only tools that surround shortage_check.

Every function here reads and nothing else. The value over a generic "read any
record" connector is that each one answers a question a factory actually asks,
with the fields that decide the answer already picked out - free stock rather
than on-hand, component availability alongside order state, lot genealogy
rather than a lot row.
"""

import math

from odoo.tools import float_round

from .common import (
    envelope,
    error_result,
    product_brief,
    resolve_product,
    resolve_warehouse,
    round_not_above,
    uoms_are_compatible,
)

STOCK_LIMITATIONS = [
    "Free stock only: quantities already reserved for other transfers or "
    "orders are excluded, and incoming supply is not counted.",
    "A snapshot. It stops being true as soon as stock moves.",
]

SEARCH_LIMITATIONS = [
    "Archived products are not searched, so a code that existed once may "
    "return no match.",
    "has_bom reports whether an active bill of materials exists today, not "
    "whether the product has ever been manufactured.",
]

LOT_LIMITATIONS = [
    "Downstream tracing follows stock moves. A lot that left through a route "
    "Odoo did not record will not appear.",
    "Upstream tracing - what this lot was made from - is not included.",
]

BOM_LIMITATIONS = [
    "Requirements only. This says nothing about whether the components are in "
    "stock; use shortage_check for that.",
    "Kits are expanded inline; components with their own manufacturing bill "
    "of materials are listed as components, not expanded further.",
]

ORDER_LIMITATIONS = [
    "components_availability is Odoo's own summary and reflects reservations "
    "against this order, not what could be reserved for it.",
]

BACKLOG_LIMITATIONS = [
    "Draft and cancelled orders are excluded; this is work that is meant to "
    "happen, not everything that has been imagined.",
]

MOVEMENT_LIMITATIONS = [
    "Completed movements only. Planned or reserved moves are not shown.",
]

# Refusals carry limitations too, and they have to be true of a refusal rather
# than copied from the success path. These two say what a refusal establishes,
# which is always less than a reader assumes.
NOT_FOUND_NOTE = (
    "This establishes only that no matching record exists in Odoo right now. "
    "It does not establish that one never existed, that it is not archived, "
    "or that it is not recorded under a different code."
)
BAD_INPUT_NOTE = (
    "The request was refused before any record was read, so nothing here "
    "establishes anything about stock, lots, bills of materials or orders."
)


def _refused(note, scope):
    """Limitations for a refusal: what it did not establish, then the tool's
    standing scope, which is still true of the question that was asked."""
    return [note] + list(scope)


# ----------------------------------------------------------------- helpers


def _quant_rows(env, product, warehouse=None):
    domain = [
        ("product_id", "=", product.id),
        ("location_id.usage", "=", "internal"),
    ]
    if warehouse:
        domain.append(("location_id", "child_of", warehouse.lot_stock_id.id))
    return env["stock.quant"]._read_group(
        domain,
        groupby=["location_id"],
        aggregates=["quantity:sum", "reserved_quantity:sum"],
    )


# ------------------------------------------------------------ product_search


def product_search(env, query, limit=10):
    """Find products by code, barcode or name."""
    limit = max(1, min(int(limit or 10), 50))
    Product = env["product.product"]
    domain = [
        "|", "|",
        ("default_code", "ilike", query),
        ("barcode", "ilike", query),
        ("name", "ilike", query),
    ]
    products = Product.search(domain, limit=limit)

    Bom = env["mrp.bom"]
    matches = []
    for product in products:
        brief = product_brief(product)
        brief["is_storable"] = bool(product.is_storable)
        # Whether it can be manufactured decides which tool the assistant
        # should reach for next, so it is worth the extra lookup here.
        brief["has_bom"] = bool(Bom._bom_find(product).get(product))
        matches.append(brief)

    return envelope(
        asked={"query": query, "limit": limit},
        match_count=len(matches),
        matches=matches,
        truncated=len(matches) == limit,
        limitations=SEARCH_LIMITATIONS + [
            "At most %d rows are returned; `truncated` says whether more "
            "exist." % limit,
        ],
    )


# -------------------------------------------------------------- stock_status


def stock_status(env, product_code, warehouse_code=None):
    """On-hand, reserved and free stock for one product, by location."""
    asked = {"product_code": product_code, "warehouse_code": warehouse_code}

    product, err = resolve_product(env, product_code)
    if err:
        return error_result(err[0], err[1], _refused(NOT_FOUND_NOTE, STOCK_LIMITATIONS),
                            asked=asked)

    warehouse, werr = resolve_warehouse(env, warehouse_code)
    if werr:
        return error_result(werr[0], werr[1], _refused(NOT_FOUND_NOTE, STOCK_LIMITATIONS),
                            asked=asked)

    rounding = product.uom_id.rounding
    locations, on_hand, reserved = [], 0.0, 0.0
    for location, qty, res in _quant_rows(env, product, warehouse):
        qty, res = qty or 0.0, res or 0.0
        on_hand += qty
        reserved += res
        locations.append({
            "location": location.complete_name,
            "location_id": location.id,
            "on_hand_qty": float_round(qty, precision_rounding=rounding),
            "reserved_qty": float_round(res, precision_rounding=rounding),
            "available_qty": round_not_above(max(0.0, qty - res), rounding),
        })
    locations.sort(key=lambda row: row["available_qty"], reverse=True)

    return envelope(
        asked=dict(product_brief(product),
                   warehouse_code=warehouse.code if warehouse else None),
        on_hand_qty=float_round(on_hand, precision_rounding=rounding),
        reserved_qty=float_round(reserved, precision_rounding=rounding),
        available_qty=round_not_above(max(0.0, on_hand - reserved), rounding),
        locations=locations,
        limitations=STOCK_LIMITATIONS,
    )


# ----------------------------------------------------------------- lot_trace


def lot_trace(env, lot_name, product_code=None):
    """Where a lot is, and who received it.

    Odoo computes the downstream side itself, following moves through
    production, so a lot consumed into an assembly still resolves to the
    customers that assembly shipped to. That is the question a recall asks.
    """
    asked = {"lot_name": lot_name, "product_code": product_code}
    domain = [("name", "=", lot_name)]
    if product_code:
        product, err = resolve_product(env, product_code)
        if err:
            return error_result(err[0], err[1],
                                _refused(NOT_FOUND_NOTE, LOT_LIMITATIONS),
                                asked=asked)
        domain.append(("product_id", "=", product.id))

    lots = env["stock.lot"].search(domain, limit=5)
    if not lots:
        return error_result("lot_not_found",
                            "No lot or serial number '%s'." % lot_name,
                            _refused(NOT_FOUND_NOTE, LOT_LIMITATIONS),
                            asked=asked)
    if len(lots) > 1:
        return error_result(
            "ambiguous_lot",
            "'%s' exists for several products; pass product_code to "
            "disambiguate." % lot_name,
            _refused(
                "This establishes that the label is not unique, and nothing "
                "about any one of the lots carrying it. Guessing which was "
                "meant is how a recall traces the wrong part.",
                LOT_LIMITATIONS),
            asked=asked,
        )

    lot = lots
    rounding = lot.product_id.uom_id.rounding
    quants = [
        {
            "location": quant.location_id.complete_name,
            "quantity": float_round(quant.quantity, precision_rounding=rounding),
            "reserved_qty": float_round(quant.reserved_quantity, precision_rounding=rounding),
            "usage": quant.location_id.usage,
        }
        for quant in lot.quant_ids
        if quant.quantity
    ]

    return envelope(
        asked={"lot_name": lot_name},
        lot={
            "name": lot.name,
            "ref": lot.ref or None,
            "product_code": lot.product_id.default_code or "",
            "product_name": lot.product_id.name,
            "on_hand_qty": float_round(lot.product_qty, precision_rounding=rounding),
            "current_location": lot.location_id.complete_name if lot.location_id else None,
        },
        quants=quants,
        delivered_to=[partner.display_name for partner in lot.partner_ids],
        delivery_count=len(lot.delivery_ids),
        limitations=LOT_LIMITATIONS,
    )


# --------------------------------------------------------------- bom_explode


def bom_explode(env, product_code, quantity=1.0, bom_code=None):
    """Components needed for a quantity, kits expanded, no stock involved."""
    asked = {"product_code": product_code, "quantity": quantity,
             "bom_code": bom_code}
    bad_input = _refused(BAD_INPUT_NOTE, BOM_LIMITATIONS)

    try:
        quantity = float(quantity)
    except (TypeError, ValueError):
        return error_result("invalid_quantity", "Quantity must be a number.",
                            bad_input, asked=asked)
    if not math.isfinite(quantity):
        return error_result("invalid_quantity",
                            "Quantity must be a finite number (NaN and "
                            "infinity are not quantities).",
                            bad_input, asked=asked)
    if quantity <= 0:
        return error_result("invalid_quantity",
                            "Quantity must be greater than zero.",
                            bad_input, asked=asked)

    not_found = _refused(NOT_FOUND_NOTE, BOM_LIMITATIONS)

    product, err = resolve_product(env, product_code)
    if err:
        return error_result(err[0], err[1], not_found, asked=asked)

    if bom_code:
        bom = env["mrp.bom"].with_context(active_test=False).search(
            [("code", "=", bom_code)], limit=1
        )
        if not bom:
            return error_result("no_bom", "No bill of materials '%s'." % bom_code,
                                not_found, asked=asked)
        if not bom.active:
            return error_result(
                "inactive_bom",
                "Bill of materials '%s' exists but is archived." % bom_code,
                _refused(
                    "The bill of materials exists but is archived, so its "
                    "component list was deliberately not read: an archived "
                    "bill is not released for use.",
                    BOM_LIMITATIONS),
                asked=asked,
            )
    else:
        bom = env["mrp.bom"]._bom_find(product).get(product)
        if not bom:
            return error_result(
                "no_bom",
                "%s has no active bill of materials." % (product.default_code or product.name),
                _refused(
                    "This establishes that the product has no active bill of "
                    "materials today, which usually means it is purchased "
                    "rather than manufactured.",
                    BOM_LIMITATIONS),
                asked=asked,
            )

    factor = product.uom_id._compute_quantity(quantity, bom.product_uom_id) / (
        bom.product_qty or 1.0
    )
    _boms, lines = bom.explode(product, factor)

    # Same accumulation rule as shortage_check: a component reachable by two
    # paths is one requirement, not two.
    totals = {}
    for bom_line, data in lines:
        qty = data.get("qty", 0.0) or 0.0
        if qty <= 0:
            continue
        component = bom_line.product_id
        # explode() reports each line in the LINE's unit of measure, which is
        # not always the unit the component is stocked in - a line may ask for
        # 500 g of something held in kilogrammes. Summing those raw, or
        # comparing them against stock, is wrong by the conversion factor.
        line_uom = bom_line.product_uom_id
        if line_uom and line_uom != component.uom_id:
            if not uoms_are_compatible(line_uom, component.uom_id):
                # shortage_check refuses this state for the same reason. The
                # two tools must agree on whether the question is answerable.
                return error_result(
                    "incompatible_uom",
                    "Bill of materials line for %s is written in %s, but the "
                    "component is stocked in %s, and those measure different "
                    "things. Correct the line's unit of measure; this cannot "
                    "be answered by converting between them."
                    % (component.default_code or component.name,
                       line_uom.name, component.uom_id.name),
                    _refused(
                        "No component list was produced. The bill of materials "
                        "names a unit of measure that cannot be converted to "
                        "the one the component is stocked in, and guessing a "
                        "conversion would produce a plausible wrong answer.",
                        BOM_LIMITATIONS),
                    asked=asked,
                )
            # round=False: convert exactly and let the final figures carry the
            # precision, rather than rounding once per line.
            qty = line_uom._compute_quantity(qty, component.uom_id, round=False)
        totals[component] = totals.setdefault(component, 0.0) + qty

    components = [
        dict(product_brief(component),
             required_qty=float_round(qty, precision_rounding=component.uom_id.rounding),
             required_per_unit=qty / quantity)
        for component, qty in totals.items()
    ]
    components.sort(key=lambda row: row["required_qty"], reverse=True)

    return envelope(
        asked=dict(product_brief(product), quantity=quantity,
                   bom_code=bom.code or None, bom_type=bom.type),
        component_count=len(components),
        components=components,
        limitations=BOM_LIMITATIONS,
    )


# --------------------------------------------- production_order_status


def _production_brief(order):
    rounding = order.product_uom_id.rounding
    return {
        "name": order.name,
        "state": order.state,
        "product_code": order.product_id.default_code or "",
        "product_name": order.product_id.name,
        "quantity": float_round(order.product_qty, precision_rounding=rounding),
        "produced_qty": float_round(order.qty_produced, precision_rounding=rounding),
        "uom": order.product_uom_id.name,
        "date_start": order.date_start and order.date_start.isoformat() or None,
        "date_finished": order.date_finished and order.date_finished.isoformat() or None,
        "bom_code": order.bom_id.code or None,
        "components_availability": order.components_availability or None,
        "priority": order.priority,
    }


def production_order_status(env, order_name):
    """One manufacturing order, with the component-availability line Odoo keeps."""
    asked = {"order_name": order_name}
    order = env["mrp.production"].search([("name", "=", order_name)], limit=1)
    if not order:
        return error_result(
            "order_not_found", "No manufacturing order named '%s'." % order_name,
            _refused(NOT_FOUND_NOTE, ORDER_LIMITATIONS), asked=asked,
        )

    brief = _production_brief(order)
    brief["components"] = [
        {
            "product_code": move.product_id.default_code or "",
            "product_name": move.product_id.name,
            "required_qty": float_round(
                move.product_uom_qty, precision_rounding=move.product_uom.rounding
            ),
            "uom": move.product_uom.name,
            "state": move.state,
        }
        for move in order.move_raw_ids
    ]

    return envelope(
        asked={"order_name": order_name},
        order=brief,
        limitations=ORDER_LIMITATIONS,
    )


# ------------------------------------------------------- production_backlog


def production_backlog(env, warehouse_code=None, limit=20):
    """Open manufacturing orders, soonest first."""
    limit = max(1, min(int(limit or 20), 100))
    warehouse, werr = resolve_warehouse(env, warehouse_code)
    if werr:
        return error_result(werr[0], werr[1],
                            _refused(NOT_FOUND_NOTE, BACKLOG_LIMITATIONS),
                            asked={"warehouse_code": warehouse_code,
                                   "limit": limit})

    domain = [("state", "in", ("confirmed", "progress", "to_close"))]
    if warehouse:
        domain.append(("picking_type_id.warehouse_id", "=", warehouse.id))

    orders = env["mrp.production"].search(
        domain, order="priority desc, date_start asc", limit=limit
    )

    return envelope(
        asked={"warehouse_code": warehouse.code if warehouse else None, "limit": limit},
        order_count=len(orders),
        orders=[_production_brief(order) for order in orders],
        truncated=len(orders) == limit,
        limitations=BACKLOG_LIMITATIONS,
    )


# ------------------------------------------------------ stock_move_history


def stock_move_history(env, product_code=None, lot_name=None, limit=20):
    """Recent completed stock movements for a product or a lot."""
    asked = {"product_code": product_code, "lot_name": lot_name, "limit": limit}

    if not product_code and not lot_name:
        return error_result(
            "invalid_params", "Give at least one of product_code or lot_name.",
            _refused(
                "No filter was given, so no movements were read. Returning "
                "every movement would fill an assistant's context with rows "
                "nobody asked for rather than answer a question.",
                MOVEMENT_LIMITATIONS),
            asked=asked,
        )

    limit = max(1, min(int(limit or 20), 100))
    asked["limit"] = limit
    domain = [("state", "=", "done")]

    if product_code:
        product, err = resolve_product(env, product_code)
        if err:
            return error_result(err[0], err[1],
                                _refused(NOT_FOUND_NOTE, MOVEMENT_LIMITATIONS),
                                asked=asked)
        domain.append(("product_id", "=", product.id))
    if lot_name:
        domain.append(("lot_id.name", "=", lot_name))

    lines = env["stock.move.line"].search(domain, order="date desc", limit=limit)

    movements = [
        {
            "date": line.date and line.date.isoformat() or None,
            "product_code": line.product_id.default_code or "",
            "quantity": float_round(
                line.quantity, precision_rounding=line.product_uom_id.rounding
            ),
            "uom": line.product_uom_id.name,
            "lot": line.lot_id.name or line.lot_name or None,
            "from_location": line.location_id.complete_name,
            "to_location": line.location_dest_id.complete_name,
            "reference": line.reference or None,
        }
        for line in lines
    ]

    return envelope(
        asked={"product_code": product_code, "lot_name": lot_name, "limit": limit},
        movement_count=len(movements),
        movements=movements,
        truncated=len(movements) == limit,
        limitations=MOVEMENT_LIMITATIONS,
    )
