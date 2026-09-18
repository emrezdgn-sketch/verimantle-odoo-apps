# -*- coding: utf-8 -*-
"""shortage_check - can I build N of this product right now?

Read-only. This module never writes to any business model; it only reads
stock.quant and explodes a bill of materials. Keeping it that way is the
whole point of the free tier, so do not add a create/write call here - a
write belongs in an mcp.proposal record instead.

The result is documented by schemas/shortage_check.schema.json; the two
must be changed together.
"""

import math
from fractions import Fraction

from odoo.tools import float_compare, float_round

from .common import (
    SCHEMA_VERSION,
    as_decimal,
    floor_ratio_to_precision,
    now_iso,
    resolve_product,
    round_not_above,
    uoms_are_compatible,
)

VERDICT_CAN_BUILD = "can_build"
VERDICT_PARTIAL = "partial"
VERDICT_BLOCKED = "blocked"

# Stated on every answer. These are the questions a stock snapshot cannot
# answer, and the agent is expected to repeat them rather than quietly imply
# that a "can_build" verdict is a commitment.
LIMITATIONS = [
    "Incoming supply is ignored: confirmed purchase orders and manufacturing "
    "orders that have not yet been received do not count towards availability.",
    "Competing demand is only counted once it has reserved stock. Draft or "
    "unreserved orders for the same components are invisible here.",
    "Work-center capacity, routing time and lead times are not considered - "
    "this answers a material question only.",
    "Lot and serial constraints are not evaluated. A component may total "
    "enough while no single lot satisfies a requirement.",
    "Components that have their own manufacturing bill of materials are "
    "reported as components to be stocked, not exploded further.",
]


def _blocked(code, message, asked=None):
    """Shape a refusal in the same schema as a successful answer."""
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": now_iso(),
        "asked": asked or {},
        "verdict": VERDICT_BLOCKED,
        "max_buildable": 0.0,
        "limiting_components": [],
        "components": [],
        "notes": [],
        "limitations": LIMITATIONS,
        "error": {"code": code, "message": message},
    }


def _stock_by_location(env, product, warehouse):
    """Free stock for one product, broken down by internal location.

    available = quantity - reserved_quantity, floored at zero. This is the
    only quantity that decides whether work can start today; qty_available
    would overstate it by counting stock already committed elsewhere.
    """
    domain = [
        ("product_id", "=", product.id),
        ("location_id.usage", "=", "internal"),
    ]
    if warehouse:
        domain.append(("location_id", "child_of", warehouse.lot_stock_id.id))

    groups = env["stock.quant"]._read_group(
        domain,
        groupby=["location_id"],
        aggregates=["quantity:sum", "reserved_quantity:sum"],
    )

    rounding = product.uom_id.rounding
    locations = []
    total_on_hand = 0.0
    total_reserved = 0.0

    for location, on_hand, reserved in groups:
        on_hand = on_hand or 0.0
        reserved = reserved or 0.0
        available = round_not_above(max(0.0, on_hand - reserved), rounding)
        total_on_hand += on_hand
        total_reserved += reserved
        if float_compare(available, 0.0, precision_rounding=rounding) > 0:
            locations.append({
                "location": location.complete_name,
                "location_id": location.id,
                "on_hand_qty": float_round(on_hand, precision_rounding=rounding),
                "reserved_qty": float_round(reserved, precision_rounding=rounding),
                "available_qty": available,
            })

    locations.sort(key=lambda row: row["available_qty"], reverse=True)
    total_available = round_not_above(
        max(0.0, total_on_hand - total_reserved), rounding)
    return {
        "on_hand_qty": float_round(total_on_hand, precision_rounding=rounding),
        "reserved_qty": float_round(total_reserved, precision_rounding=rounding),
        "available_qty": total_available,
        "locations": locations,
    }


def shortage_check(env, product_code, quantity, warehouse_code=None, bom_code=None):
    """Evaluate whether `quantity` units of `product_code` can be built now.

    :param env: an Odoo environment whose user has read access only.
    :param product_code: internal reference, barcode or unambiguous name.
    :param quantity: finished-product quantity to test, greater than zero.
    :param warehouse_code: optional; restrict stock to one warehouse.
    :param bom_code: optional; force a specific bill of materials by its code.
    :returns: dict matching schemas/shortage_check.schema.json
    """
    notes = []

    try:
        quantity = float(quantity)
    except (TypeError, ValueError):
        return _blocked("invalid_quantity", "Quantity must be a number.")
    # NaN survives float() and compares False against every threshold, so it
    # reaches the unit-of-measure conversion and dies there as a ValueError -
    # escaping the envelope entirely. JSON parsers accept the bare NaN literal,
    # so this is reachable from any client.
    if not math.isfinite(quantity):
        return _blocked("invalid_quantity",
                        "Quantity must be a finite number (NaN and infinity "
                        "are not quantities).")
    if quantity <= 0:
        return _blocked("invalid_quantity", "Quantity must be greater than zero.")

    product, err = resolve_product(env, product_code)
    if err:
        return _blocked(err[0], err[1])

    warehouse = None
    if warehouse_code:
        warehouse = env["stock.warehouse"].search(
            [("code", "=", warehouse_code)], limit=1
        )
        if not warehouse:
            return _blocked(
                "warehouse_not_found", "No warehouse with code '%s'." % warehouse_code
            )

    if bom_code:
        # active_test=False on purpose. A plain search hides archived records,
        # so an archived bill of materials would come back as "no such code" -
        # and the caller would go looking for a typo instead of discovering
        # that the bill exists but was withdrawn from use. Those are different
        # problems and the error codes promise to tell them apart.
        bom = env["mrp.bom"].with_context(active_test=False).search(
            [("code", "=", bom_code)], limit=1
        )
        if not bom:
            return _blocked("no_bom", "No bill of materials with code '%s'." % bom_code)
        if not bom.active:
            return _blocked(
                "inactive_bom",
                "Bill of materials '%s' exists but is archived, so it cannot be "
                "used for new production." % bom_code,
            )
    else:
        bom = env["mrp.bom"]._bom_find(product).get(product)
        if not bom:
            return _blocked(
                "no_bom",
                "%s has no active bill of materials, so it cannot be "
                "manufactured." % (product.default_code or product.name),
            )

    asked = {
        "product_code": product.default_code or "",
        "product_name": product.name,
        "product_id": product.id,
        "quantity": quantity,
        "uom": product.uom_id.name,
        "warehouse_code": warehouse.code if warehouse else None,
        "warehouse_name": warehouse.name if warehouse else None,
        "bom_code": bom.code or None,
        "bom_id": bom.id,
        "bom_type": bom.type,
    }

    if not warehouse:
        notes.append(
            "No warehouse was specified, so stock was summed across every "
            "internal location in the company."
        )
    if bom.type == "phantom":
        notes.append(
            "This bill of materials is a kit; its components were expanded "
            "inline instead of producing a separate manufacturing order."
        )

    # explode() takes a MULTIPLIER of the bill of materials, not a finished
    # quantity. Getting this wrong silently scales every component, so the
    # conversion is done explicitly: express the request in the bill's own
    # unit of measure, then divide by the quantity that bill produces.
    qty_in_bom_uom = product.uom_id._compute_quantity(quantity, bom.product_uom_id)
    factor = qty_in_bom_uom / (bom.product_qty or 1.0)

    try:
        _boms_done, lines_done = bom.explode(product, factor)
    except RecursionError:
        return _blocked(
            "bom_recursion",
            "The bill of materials references itself and could not be exploded.",
            asked,
        )

    # A component can appear on more than one exploded line - required
    # directly and again through a kit, or by two different sub-assemblies.
    # Requirements must be summed per product BEFORE any availability check.
    # Checking line by line compares each line against the full free stock and
    # promises the same units twice, which turns a real shortage into a
    # confident "can_build".
    aggregated = {}
    for bom_line, line_data in lines_done:
        component = bom_line.product_id
        required = line_data.get("qty", 0.0) or 0.0
        # explode() reports each line in the LINE's unit of measure, which is
        # not always the unit the component is stocked in - a line may ask for
        # 500 g of something held in kilogrammes. Availability below comes from
        # stock.quant in the component's own unit, so the requirement has to be
        # converted before the two are ever compared. Without this the answer
        # is wrong by the conversion factor: a thousandfold for g against kg.
        line_uom = bom_line.product_uom_id
        if line_uom and line_uom != component.uom_id:
            if not uoms_are_compatible(line_uom, component.uom_id):
                # Odoo permits this state and converts it anyway - see
                # uoms_are_compatible(). An answer built on it would be a
                # buildability verdict derived from invented arithmetic, which
                # is worse than no answer.
                return _blocked(
                    "incompatible_uom",
                    "Bill of materials line for %s is written in %s, but the "
                    "component is stocked in %s, and those measure different "
                    "things. Correct the line's unit of measure; this cannot "
                    "be answered by converting between them."
                    % (component.default_code or component.name,
                       line_uom.name, component.uom_id.name),
                    asked,
                )
            # round=False on purpose. The default rounds the converted figure
            # UP to the component's precision, once per line, so a component
            # reached by several lines accumulates that inflation. Converting
            # exactly and letting the final comparison carry the precision is
            # both simpler and closer to the physical quantity.
            required = line_uom._compute_quantity(
                required, component.uom_id, round=False)
        # A positive requirement counts even when it is finer than the
        # component's display precision. Dropping it here would silently
        # remove a component from the answer - and a component nobody is told
        # about cannot be the one that blocks the order.
        if required <= 0:
            continue
        entry = aggregated.setdefault(
            component, {"required": 0.0, "level": None, "line_count": 0}
        )
        entry["required"] += required
        entry["line_count"] += 1
        level = line_data.get("level", 1) or 1
        entry["level"] = level if entry["level"] is None else min(entry["level"], level)

    merged = [c for c, e in aggregated.items() if e["line_count"] > 1]
    if merged:
        notes.append(
            "%s appear on more than one line of the exploded bill of "
            "materials; their requirements were summed before checking "
            "stock." % ", ".join((c.default_code or c.name) for c in merged[:5])
        )

    components = []
    max_buildable = None

    for component, entry in aggregated.items():
        required = entry["required"]
        rounding = component.uom_id.rounding

        stock = _stock_by_location(env, component, warehouse)
        available = stock["available_qty"]
        shortage = max(
            0.0, float_round(required - available, precision_rounding=rounding)
        )
        per_unit = required / quantity

        if required > 0:
            # available / (required / quantity), without forming the
            # intermediate per-unit float: that division is one more place a
            # result can round upward. floor_ratio_to_precision() does the
            # whole thing exactly and guarantees it never exceeds the ratio.
            buildable = floor_ratio_to_precision(
                as_decimal(available) * as_decimal(quantity),
                as_decimal(required),
                product.uom_id.rounding,
            )
        else:
            buildable = quantity

        components.append({
            "product_code": component.default_code or "",
            "product_name": component.name,
            "product_id": component.id,
            "level": entry["level"] or 1,
            "uom": component.uom_id.name,
            "required_qty": float_round(required, precision_rounding=rounding),
            "required_per_unit": per_unit,
            "on_hand_qty": stock["on_hand_qty"],
            "reserved_qty": stock["reserved_qty"],
            "available_qty": available,
            "shortage_qty": shortage,
            "max_buildable_from_this": buildable,
            "tracking": component.tracking or "none",
            "is_limiting": False,
            "has_own_bom": bool(env["mrp.bom"]._bom_find(component).get(component)),
            "locations": stock["locations"],
        })

        max_buildable = buildable if max_buildable is None else min(max_buildable, buildable)

    if not components:
        return _blocked(
            "no_bom",
            "The bill of materials explodes to no component lines.",
            asked,
        )

    max_buildable = max(0.0, max_buildable or 0.0)

    limiting = [
        c["product_code"] or c["product_name"]
        for c in components
        if float_compare(
            c["max_buildable_from_this"], max_buildable,
            precision_rounding=product.uom_id.rounding
        ) == 0
        and float_compare(
            max_buildable, quantity, precision_rounding=product.uom_id.rounding
        ) < 0
    ]
    for comp in components:
        comp["is_limiting"] = (comp["product_code"] or comp["product_name"]) in limiting

    if float_compare(max_buildable, quantity, precision_rounding=product.uom_id.rounding) >= 0:
        verdict = VERDICT_CAN_BUILD
    elif float_compare(max_buildable, 0.0, precision_rounding=product.uom_id.rounding) > 0:
        verdict = VERDICT_PARTIAL
    else:
        verdict = VERDICT_BLOCKED

    # Scattered stock is a common false alarm: the total is fine, but it sits
    # in warehouses that cannot serve one order. Say so rather than letting the
    # agent guess.
    scattered = [c["product_code"] for c in components if len(c["locations"]) > 1]
    if scattered and not warehouse:
        notes.append(
            "Free stock for %s is spread across more than one location; "
            "re-run with a warehouse code to see whether a single site can "
            "cover the order." % ", ".join(scattered[:5])
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": now_iso(),
        "asked": asked,
        "verdict": verdict,
        "max_buildable": max_buildable,
        "limiting_components": limiting,
        "components": components,
        "notes": notes,
        "limitations": LIMITATIONS,
        "error": None,
    }
