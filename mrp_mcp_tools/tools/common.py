# -*- coding: utf-8 -*-
"""Shared shape for every read-only tool.

Two things every tool here agrees on, because an assistant should not have to
learn a new convention per tool:

  * The same envelope. schema_version, as_of, and - where the answer could be
    mistaken for a guarantee - limitations. A refusal uses the same envelope as
    a success, with an `error` object, so there is one shape to parse.
  * The same timestamp discipline. Everything is a snapshot; as_of says when.
"""

import math
from datetime import datetime, timezone
from fractions import Fraction

from odoo.tools import float_round

SCHEMA_VERSION = "1.0"


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def envelope(limitations, asked=None, **fields):
    """Start a result with the fields every tool carries.

    `limitations` is a required argument, not an option. It used to be one,
    and the result was that seven refusal paths across four tools quietly
    returned an answer with no `limitations` key at all - so a client doing
    the documented `response["limitations"]` raised KeyError on exactly the
    answers where knowing the blind spots matters most. Requiring it here
    turns that omission into a TypeError at the call site, where somebody has
    to write a truthful sentence instead of forgetting one.

    `asked` defaults to an empty dict rather than being absent, for the same
    reason: one shape to parse.
    """
    base = {
        "schema_version": SCHEMA_VERSION,
        "as_of": now_iso(),
        "asked": {} if asked is None else asked,
        "limitations": list(limitations),
        "error": None,
    }
    base.update(fields)
    return base


def error_result(code, message, limitations, asked=None, **fields):
    """A refusal, shaped like a success so callers parse one thing.

    A refusal is still an answer somebody acts on, so it carries the same
    envelope - including limitations that say what this particular refusal
    does and does not establish.
    """
    result = envelope(limitations, asked=asked, **fields)
    result["error"] = {"code": code, "message": message}
    return result


def uom_tree_root(uom):
    """The root of a unit of measure's conversion tree.

    Odoo 19 replaced unit-of-measure *categories* with a hierarchy: `kg` sits
    under `g`, `Ton` under `kg`, `L` under `ml`, `Dozens` under `Units`, and
    `parent_path` records the chain. Two units describe the same physical
    dimension only if they share a root.
    """
    return (uom.parent_path or "").split("/")[0] or str(uom.id)


def uoms_are_compatible(one, other):
    """Whether a quantity in `one` can honestly be expressed in `other`.

    This has to be checked here rather than left to Odoo.
    `uom.uom._compute_quantity` still documents a `raise_if_failure` argument
    that raises "if the conversion is not possible (different UomUom
    category)", but the Odoo 19 implementation never looks: it multiplies by
    one factor and divides by the other whatever the units are. Ask it to turn
    a kilogramme into Units and it answers 1000, with no error. Acting on that
    number would be a buildability verdict derived from invented arithmetic.
    """
    if not one or not other or one == other:
        return True
    return uom_tree_root(one) == uom_tree_root(other)


def round_not_above(value, rounding):
    """Round to a precision, but never return more than the value itself.

    `float_round` divides by the precision before rounding, and past 2**53 that
    division is inexact: `float_round(1e15, precision_rounding=0.01)` returns
    1000000000000001.0, a thousandth of a percent *above* its input. Small
    enough to look like noise, and in the wrong direction - it reports free
    stock that does not exist, which is the one error this product must not
    make. Where rounding would overstate, the exact value is kept instead.
    """
    if not rounding:
        return value
    rounded = float_round(value, precision_rounding=rounding)
    return rounded if rounded <= value else value


def as_decimal(value):
    """The decimal a stored quantity represents, exactly.

    Odoo writes quantities rounded to their unit's precision, so a quant
    holding "1.2 kg" is the decimal 1.2 - but the float in the column is
    1.1999999999999999556, and 2.0 - 0.8 lands somewhere else again. Recovering
    the decimal via repr (which gives the shortest string that round-trips) is
    not a fudge factor: it is reading back the number Odoo meant to store,
    before float representation blurred it.

    Doing this at the edge means the arithmetic below needs no epsilon. The
    alternative - an epsilon-corrected floor, as `float_round` uses - has to
    scale the epsilon with magnitude, and at 1e15 that tolerance grows to tens
    of precision steps, which is the very overstatement being guarded against.
    """
    return Fraction(str(float(value)))


def floor_ratio_to_precision(numerator, denominator, rounding):
    """The largest multiple of `rounding` that does not exceed numerator/denominator.

    This is the contract for `max_buildable`, and it is computed in exact
    rational arithmetic rather than by dividing two floats and calling
    `float_round(..., 'DOWN')`, because at large magnitudes *both* of those
    steps can round upward:

      * the division itself: 1000000000000001.0 / 3.0 is stored as
        333333333333333.7, which is above the true 333333333333333.67;
      * `float_round`, which divides by the precision before flooring, and
        past 2**53 returns a value above its own input.

    Either one reports buildable quantity that the stock does not support.
    Fractions make the division and the flooring exact; the final conversion
    back to float is the only lossy step left, and `nextafter` pulls it down
    if it lands above the exact answer.
    """
    numerator = Fraction(numerator)
    denominator = Fraction(denominator)
    if denominator <= 0 or numerator <= 0:
        return 0.0

    ratio = numerator / denominator
    floored = ratio
    step = Fraction(str(rounding)) if rounding else Fraction(0)
    if step > 0:
        floored = (ratio // step) * step

    result = float(floored)
    # Converting back to float is the one lossy step left. Ordinarily the
    # error is a single unit in the last place - 3.33 has no exact float, and
    # the nearest one sits a fraction above it - which is representation, not
    # optimism, and pulling it down would emit 3.3299999999999996 for an
    # answer everyone agrees is 3.33.
    #
    # It stops being representation once the number is large enough that the
    # gap between adjacent floats exceeds the precision step itself: around
    # 1e15 the grid is 0.125 wide against a 0.01 step, so the conversion can
    # land whole steps above the true ratio. There, being exactly right is not
    # available and not overstating is, so take the float below.
    if Fraction(result) > ratio and math.ulp(result) > float(step):
        result = math.nextafter(result, 0.0)
    return result


def resolve_product(env, product_code):
    """Find exactly one product, or explain why that was not possible.

    Tried in order of how sure the match is: internal reference, barcode, then
    partial matches. An ambiguous term is refused rather than guessed - picking
    the first of several products silently is how an assistant ends up
    answering confidently about the wrong part.

    :returns: (recordset, None) or (empty recordset, (code, message))
    """
    Product = env["product.product"]
    for domain in (
        [("default_code", "=", product_code)],
        [("barcode", "=", product_code)],
        [("default_code", "ilike", product_code)],
        [("name", "ilike", product_code)],
    ):
        hits = Product.search(domain, limit=5)
        if len(hits) == 1:
            return hits, None
        if len(hits) > 1:
            names = ", ".join(h.default_code or h.name for h in hits)
            return Product.browse(), (
                "ambiguous_product",
                "'%s' matches several products: %s. Use the exact internal "
                "reference." % (product_code, names),
            )
    return Product.browse(), (
        "product_not_found",
        "No product matches '%s'." % product_code,
    )


def resolve_warehouse(env, warehouse_code):
    """:returns: (recordset_or_None, None) or (None, (code, message))"""
    if not warehouse_code:
        return None, None
    warehouse = env["stock.warehouse"].search(
        [("code", "=", warehouse_code)], limit=1
    )
    if not warehouse:
        return None, (
            "warehouse_not_found",
            "No warehouse with code '%s'." % warehouse_code,
        )
    return warehouse, None


def product_brief(product):
    """The identification block repeated across tools."""
    return {
        "product_code": product.default_code or "",
        "product_name": product.name,
        "product_id": product.id,
        "uom": product.uom_id.name,
        "tracking": product.tracking or "none",
    }
