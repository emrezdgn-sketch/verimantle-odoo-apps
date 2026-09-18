# -*- coding: utf-8 -*-
"""Manufacturing semantics: the same physical situation must get the same answer.

The cross-unit defect that prompted this file - a 500 g requirement compared
against kilogrammes of stock as though it were 500 kg - was not found by any
example-based test, because every example used one unit of measure throughout.
It was found the moment two units met in one bill of materials.

So this file does not test examples. It tests invariants that must hold across
every way the same physical state can be written down:

  SEM-1  UoM equivalence. Equivalent physical quantities expressed in
         compatible units produce equivalent feasibility conclusions.
  SEM-2  Convert before aggregate. Requirements in different compatible units
         are normalised before demand is summed.
  SEM-3  Free stock remains authoritative. Reservations stay excluded
         regardless of how either side is expressed.
  SEM-4  No optimistic rounding. max_buildable never exceeds exact physical
         buildability.
  SEM-5  Cross-tool consistency. Two tools describing the same physical fact
         do not contradict each other.

THE ORACLE. Expected values are not taken from the implementation. They are
computed here in exact rational arithmetic (fractions.Fraction) from the
`factor` column of the unit records themselves, normalising every quantity to
its unit tree's base before any comparison. That is deliberately a different
route from the production path, which converts through
`uom.uom._compute_quantity`, rounds with `float_round`, and aggregates in an
order of its own - and it is precisely those three steps, not the physics, that
the original defect lived in. Sharing the `factor` column is unavoidable: it is
the physical fact. Sharing the arithmetic would defeat the purpose.
"""

import itertools
import math
from fractions import Fraction

from odoo.tests import TransactionCase, tagged

from ..tools import readonly
from ..tools.shortage import shortage_check


# --------------------------------------------------------------------------
# the oracle - exact rational arithmetic, independent of the implementation
# --------------------------------------------------------------------------

def exact(value):
    """A Fraction that is exactly the decimal a human wrote, not the float."""
    return Fraction(str(value))


def to_base(quantity, uom):
    """Normalise a quantity into its unit tree's base unit, exactly.

    Odoo stores `factor` as the multiple of the tree's base unit: grams are the
    base of the mass tree, so kg has factor 1000 and Ton 1000000.
    """
    return exact(quantity) * exact(uom.factor)


def from_base(quantity_in_base, uom):
    return quantity_in_base / exact(uom.factor)


def floor_to(value, precision):
    """Round DOWN to a precision step, exactly - the documented contract for
    max_buildable, computed without float_round."""
    step = exact(precision)
    if step == 0:
        return value
    return Fraction(math.floor(value / step)) * step


def uom_root(uom):
    """The root of a unit's tree. Two units are physically comparable only if
    they share one; Odoo 19 no longer refuses when they do not."""
    return (uom.parent_path or "").split("/")[0] or str(uom.id)


@tagged("post_install", "-at_install")
class SemanticsCase(TransactionCase):
    """Fixtures built from a physical description, in whatever units are asked."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.unit = cls.env.ref("uom.product_uom_unit")
        cls.dozen = cls.env.ref("uom.product_uom_dozen")
        cls.gram = cls.env.ref("uom.product_uom_gram")
        cls.kg = cls.env.ref("uom.product_uom_kgm")
        cls.ton = cls.env.ref("uom.product_uom_ton")
        cls.litre = cls.env.ref("uom.product_uom_litre")
        cls.ml = cls.env["uom.uom"].search([("name", "=", "ml")], limit=1)

        cls.warehouse = cls.env["stock.warehouse"].search([], limit=1)
        cls.stock_location = cls.warehouse.lot_stock_id
        cls.warehouse_2 = cls.env["stock.warehouse"].create({
            "name": "Semantics Site B", "code": "SMB"})
        cls.customer_location = cls.env.ref("stock.stock_location_customers")

        cls._seq = itertools.count(1)

    # ---------------------------------------------------------------- fixture

    def _code(self, prefix):
        return "SEM-%s-%d" % (prefix, next(self._seq))

    def _product(self, uom, prefix="P", tracking="none"):
        code = self._code(prefix)
        return self.env["product.product"].create({
            "name": "Semantics %s" % code,
            "default_code": code,
            "is_storable": True,
            "uom_id": uom.id,
            "tracking": tracking,
        })

    def _bom(self, finished, lines, bom_type="normal", bom_qty=1.0):
        """lines: iterable of (component, quantity, line_uom or None)."""
        return self.env["mrp.bom"].create({
            "product_tmpl_id": finished.product_tmpl_id.id,
            "product_qty": bom_qty,
            "type": bom_type,
            "code": self._code("BOM"),
            "bom_line_ids": [
                (0, 0, {
                    "product_id": component.id,
                    "product_qty": quantity,
                    "product_uom_id": (line_uom or component.uom_id).id,
                })
                for component, quantity, line_uom in lines
            ],
        })

    def _stock(self, product, quantity, reserved=0.0, location=None):
        location = location or self.stock_location
        self.env["stock.quant"]._update_available_quantity(
            product, location, quantity)
        if reserved:
            quant = self.env["stock.quant"].search([
                ("product_id", "=", product.id),
                ("location_id", "=", location.id)], limit=1)
            quant.sudo().write({"reserved_quantity": reserved})

    def _stored(self, product, warehouse=None):
        """What Odoo actually holds, read back from the quants.

        Requested and stored are not the same thing: `_update_available_quantity`
        rounds to the unit's precision on write, so asking for 2.7495 kg stores
        2.75, asking for 0.002 Ton stores nothing at all, and even 1e15 comes
        back as 1000000000000001.0. An oracle fed the requested figure would
        be testing the fixture rather than the tool. These are the numbers the
        tool is entitled to reason about.

        :returns: (on_hand, reserved, free) as exact Fractions
        """
        domain = [("product_id", "=", product.id),
                  ("location_id.usage", "=", "internal")]
        if warehouse:
            domain.append(("location_id", "child_of", warehouse.lot_stock_id.id))
        on_hand = Fraction(0)
        reserved = Fraction(0)
        for quant in self.env["stock.quant"].search(domain):
            on_hand += exact(quant.quantity)
            reserved += exact(quant.reserved_quantity)
        return on_hand, reserved, max(Fraction(0), on_hand - reserved)

    def _component(self, result, code):
        for entry in result["components"]:
            if entry["product_code"] == code:
                return entry
        self.fail("%s not in %s" % (
            code, [c["product_code"] for c in result["components"]]))

    # ----------------------------------------------------------------- oracle

    def _expected_max_buildable(self, finished, demands, supplies):
        """demands: {component: [(qty, line_uom), ...]} per one finished unit.
           supplies: {component: free quantity in the component's own uom}

        Returns the exact rational answer, then floored to the finished
        product's precision - the documented contract, computed independently.
        """
        ratios = []
        for component, lines in demands.items():
            per_unit_base = sum(
                (to_base(qty, line_uom or component.uom_id) for qty, line_uom in lines),
                Fraction(0),
            )
            if per_unit_base == 0:
                continue
            free_base = to_base(supplies.get(component, 0.0), component.uom_id)
            ratios.append(free_base / per_unit_base)
        if not ratios:
            return None
        return floor_to(min(ratios), finished.uom_id.rounding)

    def _assert_not_optimistic(self, result, demands, supplies, label=""):
        """SEM-4, checked against the oracle rather than against the code."""
        for component, lines in demands.items():
            per_unit_base = sum(
                (to_base(qty, line_uom or component.uom_id) for qty, line_uom in lines),
                Fraction(0),
            )
            if per_unit_base == 0:
                continue
            free_base = to_base(supplies.get(component, 0.0), component.uom_id)
            true_ratio = free_base / per_unit_base
            # Tolerance is one unit in the last place of the answer itself -
            # the error of writing a decimal as a float, nothing more. It is
            # roughly 1e-16 relative, so it cannot hide an optimistic result:
            # the defect this guards against overstated by whole precision
            # steps. Anything larger than a representation step is a defect.
            tolerance = Fraction(math.ulp(float(result["max_buildable"] or 0.0)))
            self.assertLessEqual(
                exact(result["max_buildable"]), true_ratio + tolerance,
                "%s: max_buildable %r exceeds what %s physically supports (%s)"
                % (label, result["max_buildable"], component.default_code,
                   float(true_ratio)),
            )


# ==========================================================================
# WORKSTREAM A - UoM equivalence (SEM-1)
# ==========================================================================

@tagged("post_install", "-at_install")
class TestUomEquivalence(SemanticsCase):

    def _build_case(self, component_uom, line_uom, component_qty, free_qty):
        finished = self._product(self.unit, "FG")
        component = self._product(component_uom, "CM")
        self._bom(finished, [(component, component_qty, line_uom)])
        self._stock(component, free_qty)
        return finished, component

    def test_uom1_mass_kg_and_gram_are_the_same_situation(self):
        """UOM-1. 0.5 kg needed from 2 kg free, and 500 g needed from 2000 g
        free, are one physical situation written two ways. Four units, both."""
        cases = [
            ("kg/kg", self.kg, self.kg, 0.5, 2.0),
            ("g/g", self.gram, self.gram, 500.0, 2000.0),
            ("kg stock, g line", self.kg, self.gram, 500.0, 2.0),
            ("g stock, kg line", self.gram, self.kg, 0.5, 2000.0),
            # Ton is written at its own scale: Odoo stores a quant rounded to
            # the unit's precision, so 0.002 Ton would be stored as zero and
            # the fixture would describe stock that does not exist.
            ("ton stock, kg line", self.ton, self.kg, 500.0, 2.0),
        ]
        answers = {}
        for label, component_uom, line_uom, qty, free in cases:
            with self.subTest(case=label):
                finished, component = self._build_case(
                    component_uom, line_uom, qty, free)
                result = shortage_check(self.env, finished.default_code, 4.0)

                demands = {component: [(qty, line_uom)]}
                supplies = {component: free}
                expected = self._expected_max_buildable(
                    finished, demands, supplies)

                self.assertEqual(
                    exact(result["max_buildable"]), expected,
                    "%s: expected %s" % (label, float(expected)))
                self.assertEqual(result["verdict"], "can_build", label)
                self.assertEqual(result["limiting_components"], [], label)
                self._assert_not_optimistic(result, demands, supplies, label)
                answers[label] = result["max_buildable"]

        self.assertEqual(
            len(set(answers.values())), 1,
            "SEM-1 violated: the same physical state gave %r" % (answers,))

    def test_uom1_volume_litre_and_millilitre(self):
        cases = [
            ("L/L", self.litre, self.litre, 0.25, 1.0),
            ("ml/ml", self.ml, self.ml, 250.0, 1000.0),
            ("L stock, ml line", self.litre, self.ml, 250.0, 1.0),
            ("ml stock, L line", self.ml, self.litre, 0.25, 1000.0),
        ]
        answers = set()
        for label, component_uom, line_uom, qty, free in cases:
            with self.subTest(case=label):
                finished, component = self._build_case(
                    component_uom, line_uom, qty, free)
                result = shortage_check(self.env, finished.default_code, 4.0)

                demands = {component: [(qty, line_uom)]}
                supplies = {component: free}
                self.assertEqual(
                    exact(result["max_buildable"]),
                    self._expected_max_buildable(finished, demands, supplies),
                    label)
                self._assert_not_optimistic(result, demands, supplies, label)
                answers.add(result["max_buildable"])

        self.assertEqual(len(answers), 1, "SEM-1 violated: %r" % (answers,))

    def test_uom1_countable_units_and_dozens(self):
        """Dozens is a compatible unit of Units - factor 12."""
        cases = [
            ("Units/Units", self.unit, self.unit, 24.0, 96.0),
            ("Units stock, Dozens line", self.unit, self.dozen, 2.0, 96.0),
            ("Dozens stock, Units line", self.dozen, self.unit, 24.0, 8.0),
            ("Dozens/Dozens", self.dozen, self.dozen, 2.0, 8.0),
        ]
        answers = set()
        for label, component_uom, line_uom, qty, free in cases:
            with self.subTest(case=label):
                finished, component = self._build_case(
                    component_uom, line_uom, qty, free)
                result = shortage_check(self.env, finished.default_code, 4.0)

                demands = {component: [(qty, line_uom)]}
                supplies = {component: free}
                self.assertEqual(
                    exact(result["max_buildable"]),
                    self._expected_max_buildable(finished, demands, supplies),
                    label)
                self._assert_not_optimistic(result, demands, supplies, label)
                answers.add(result["max_buildable"])

        self.assertEqual(len(answers), 1, "SEM-1 violated: %r" % (answers,))

    def test_uom1_shortage_verdict_travels_with_the_units(self):
        """The same deficit must read as the same deficit in either unit."""
        for label, component_uom, line_uom, qty, free in [
            ("kg", self.kg, self.kg, 0.5, 1.0),
            ("g", self.gram, self.gram, 500.0, 1000.0),
            ("mixed", self.kg, self.gram, 500.0, 1.0),
        ]:
            with self.subTest(case=label):
                finished, component = self._build_case(
                    component_uom, line_uom, qty, free)
                result = shortage_check(self.env, finished.default_code, 4.0)

                self.assertEqual(result["verdict"], "partial", label)
                self.assertEqual(result["max_buildable"], 2.0, label)
                self.assertEqual(
                    result["limiting_components"], [component.default_code], label)
                entry = self._component(result, component.default_code)
                self.assertTrue(entry["is_limiting"], label)
                # Reported in the component's own unit, and short by half of it.
                self.assertEqual(entry["uom"], component_uom.name, label)
                self.assertEqual(
                    to_base(entry["shortage_qty"], component_uom),
                    to_base(1.0, self.kg), label)


# ==========================================================================
# WORKSTREAM B - BOM depth x UoM (SEM-2)
# ==========================================================================

@tagged("post_install", "-at_install")
class TestNestedBomUom(SemanticsCase):

    def test_sem2_direct_and_nested_demand_is_converted_then_summed(self):
        """The case from the brief. 0.5 kg directly plus 250 g through a kit is
        0.75 kg per unit - never 250.5, and never two separate promises against
        the same stock."""
        finished = self._product(self.unit, "FG")
        part = self._product(self.kg, "X")
        kit = self._product(self.unit, "KIT")

        self._bom(kit, [(part, 250.0, self.gram)], bom_type="phantom")
        self._bom(finished, [(part, 0.5, self.kg), (kit, 1.0, None)])
        self._stock(part, 3.0)          # 3 kg

        result = shortage_check(self.env, finished.default_code, 4.0)
        entry = self._component(result, part.default_code)

        self.assertEqual(
            exact(entry["required_per_unit"]), exact(0.75),
            "0.5 kg + 250 g must be 0.75 kg")
        self.assertEqual(exact(entry["required_qty"]), exact(3.0))
        self.assertEqual(result["max_buildable"], 4.0)
        self.assertEqual(
            [c["product_code"] for c in result["components"]].count(
                part.default_code), 1,
            "one line per physical component")

        self._assert_not_optimistic(
            result, {part: [(0.5, self.kg), (250.0, self.gram)]}, {part: 3.0})

    def test_sem2_three_levels_with_a_unit_change_at_each(self):
        """Finished -> B in kg -> subassembly C -> D in g. Depth must not lose
        a conversion, and a component with its own bill of materials is a leaf
        this tool reports rather than explodes."""
        finished = self._product(self.unit, "FG")
        b = self._product(self.kg, "B")
        c = self._product(self.unit, "C")
        d = self._product(self.gram, "D")

        self._bom(c, [(d, 40.0, self.gram)], bom_type="phantom")
        self._bom(finished, [(b, 1500.0, self.gram), (c, 2.0, None)])
        self._stock(b, 6.0)      # 6 kg, 1.5 kg per unit -> 4
        self._stock(d, 800.0)    # 800 g, 80 g per unit -> 10

        result = shortage_check(self.env, finished.default_code, 4.0)

        b_entry = self._component(result, b.default_code)
        d_entry = self._component(result, d.default_code)
        self.assertEqual(exact(b_entry["required_per_unit"]), exact(1.5))
        self.assertEqual(exact(d_entry["required_per_unit"]), exact(80.0))
        self.assertEqual(result["max_buildable"], 4.0)
        self.assertEqual(result["limiting_components"], [])

        self._assert_not_optimistic(
            result,
            {b: [(1500.0, self.gram)], d: [(40.0, self.gram), (40.0, self.gram)]},
            {b: 6.0, d: 800.0})

    def test_sem2_two_branches_reach_the_same_component_in_two_units(self):
        """A diamond where the two paths are written in different units. If
        conversion happened after aggregation, 0.2 kg + 300 g would be summed
        as 300.2 of something."""
        finished = self._product(self.unit, "FG")
        shared = self._product(self.kg, "SH")
        left = self._product(self.unit, "L")
        right = self._product(self.unit, "R")

        self._bom(left, [(shared, 0.2, self.kg)], bom_type="phantom")
        self._bom(right, [(shared, 300.0, self.gram)], bom_type="phantom")
        self._bom(finished, [(left, 1.0, None), (right, 1.0, None)])
        self._stock(shared, 2.5)     # 0.5 kg per unit -> 5

        result = shortage_check(self.env, finished.default_code, 5.0)
        entry = self._component(result, shared.default_code)

        self.assertEqual(exact(entry["required_per_unit"]), exact(0.5))
        self.assertEqual(result["max_buildable"], 5.0)
        self.assertEqual(result["verdict"], "can_build")
        self._assert_not_optimistic(
            result, {shared: [(0.2, self.kg), (300.0, self.gram)]}, {shared: 2.5})

    def test_sem2_bom_producing_several_units_scales_correctly(self):
        """A bill of materials that yields 4 units, with a line in grams."""
        finished = self._product(self.unit, "FG")
        part = self._product(self.kg, "X")

        self._bom(finished, [(part, 800.0, self.gram)], bom_qty=4.0)
        self._stock(part, 2.0)      # 0.2 kg per finished unit -> 10

        result = shortage_check(self.env, finished.default_code, 10.0)
        entry = self._component(result, part.default_code)

        self.assertEqual(exact(entry["required_per_unit"]), exact(0.2))
        self.assertEqual(result["max_buildable"], 10.0)


# ==========================================================================
# WORKSTREAM C - reservation x UoM (SEM-3)
# ==========================================================================

@tagged("post_install", "-at_install")
class TestReservationUom(SemanticsCase):

    def test_sem3_reservation_is_subtracted_before_conversion_matters(self):
        """The brief's case. 2 kg on hand, 0.8 kg reserved, 1.2 kg free, a
        300 g requirement: four units, not six."""
        finished = self._product(self.unit, "FG")
        part = self._product(self.kg, "X")
        self._bom(finished, [(part, 300.0, self.gram)])
        self._stock(part, 2.0, reserved=0.8)

        result = shortage_check(self.env, finished.default_code, 6.0)
        entry = self._component(result, part.default_code)

        self.assertEqual(exact(entry["on_hand_qty"]), exact(2.0))
        self.assertEqual(exact(entry["reserved_qty"]), exact(0.8))
        self.assertEqual(exact(entry["available_qty"]), exact(1.2))
        self.assertEqual(result["max_buildable"], 4.0)
        self.assertEqual(result["verdict"], "partial")
        self._assert_not_optimistic(
            result, {part: [(300.0, self.gram)]}, {part: 1.2})

    def test_sem3_same_reservation_written_in_grams(self):
        """SEM-1 and SEM-3 together: the same reservation in the other unit."""
        finished = self._product(self.unit, "FG")
        part = self._product(self.gram, "X")
        self._bom(finished, [(part, 0.3, self.kg)])
        self._stock(part, 2000.0, reserved=800.0)

        result = shortage_check(self.env, finished.default_code, 6.0)
        entry = self._component(result, part.default_code)

        self.assertEqual(exact(entry["available_qty"]), exact(1200.0))
        self.assertEqual(result["max_buildable"], 4.0)
        self._assert_not_optimistic(
            result, {part: [(0.3, self.kg)]}, {part: 1200.0})

    def test_sem3_fully_reserved_stock_is_blocked_in_any_unit(self):
        for label, uom, line_uom, qty, on_hand in [
            ("kg", self.kg, self.gram, 300.0, 2.0),
            ("g", self.gram, self.kg, 0.3, 2000.0),
        ]:
            with self.subTest(case=label):
                finished = self._product(self.unit, "FG")
                part = self._product(uom, "X")
                self._bom(finished, [(part, qty, line_uom)])
                self._stock(part, on_hand, reserved=on_hand)

                result = shortage_check(self.env, finished.default_code, 1.0)

                self.assertEqual(result["max_buildable"], 0.0, label)
                self.assertEqual(result["verdict"], "blocked", label)
                self.assertIsNone(result["error"], label)


# ==========================================================================
# WORKSTREAM D - location / warehouse x UoM
# ==========================================================================

@tagged("post_install", "-at_install")
class TestWarehouseUom(SemanticsCase):

    def test_warehouse_scope_survives_conversion(self):
        """Stock at another site must not become available because the two
        quantities happen to normalise into the same unit."""
        finished = self._product(self.unit, "FG")
        part = self._product(self.kg, "X")
        self._bom(finished, [(part, 500.0, self.gram)])

        self._stock(part, 1.0)                                    # here: 1 kg
        self._stock(part, 9.0, location=self.warehouse_2.lot_stock_id)

        scoped = shortage_check(self.env, finished.default_code, 10.0,
                                warehouse_code=self.warehouse.code)
        self.assertEqual(scoped["max_buildable"], 2.0)
        self.assertEqual(scoped["verdict"], "partial")
        self._assert_not_optimistic(
            scoped, {part: [(500.0, self.gram)]}, {part: 1.0}, "scoped")

        company_wide = shortage_check(self.env, finished.default_code, 10.0)
        self.assertEqual(company_wide["max_buildable"], 20.0)

    def test_two_internal_locations_in_one_warehouse_are_summed(self):
        finished = self._product(self.unit, "FG")
        part = self._product(self.gram, "X")
        self._bom(finished, [(part, 0.25, self.kg)])

        shelf = self.env["stock.location"].create({
            "name": "Semantics Shelf", "usage": "internal",
            "location_id": self.stock_location.id})
        self._stock(part, 500.0)
        self._stock(part, 500.0, location=shelf)

        result = shortage_check(self.env, finished.default_code, 4.0,
                                warehouse_code=self.warehouse.code)
        entry = self._component(result, part.default_code)

        self.assertEqual(exact(entry["available_qty"]), exact(1000.0))
        self.assertEqual(result["max_buildable"], 4.0)

    def test_stock_outside_internal_locations_is_not_counted(self):
        """A customer location holds delivered goods. Counting them would
        promise an order out of stock that has already left."""
        finished = self._product(self.unit, "FG")
        part = self._product(self.kg, "X")
        self._bom(finished, [(part, 500.0, self.gram)])

        self._stock(part, 1.0)
        self._stock(part, 50.0, location=self.customer_location)

        result = shortage_check(self.env, finished.default_code, 10.0)
        entry = self._component(result, part.default_code)

        self.assertEqual(exact(entry["available_qty"]), exact(1.0))
        self.assertEqual(result["max_buildable"], 2.0)

    def test_reservation_in_one_location_only_reduces_that_location(self):
        finished = self._product(self.unit, "FG")
        part = self._product(self.kg, "X")
        self._bom(finished, [(part, 250.0, self.gram)])

        shelf = self.env["stock.location"].create({
            "name": "Semantics Shelf B", "usage": "internal",
            "location_id": self.stock_location.id})
        self._stock(part, 2.0, reserved=1.5)
        self._stock(part, 1.0, location=shelf)

        result = shortage_check(self.env, finished.default_code, 10.0,
                                warehouse_code=self.warehouse.code)
        entry = self._component(result, part.default_code)

        self.assertEqual(exact(entry["available_qty"]), exact(1.5))
        self.assertEqual(result["max_buildable"], 6.0)
        self._assert_not_optimistic(
            result, {part: [(250.0, self.gram)]}, {part: 1.5})


# ==========================================================================
# WORKSTREAM E - tracking x UoM
# ==========================================================================

@tagged("post_install", "-at_install")
class TestTrackingUom(SemanticsCase):

    def test_tracking_does_not_change_the_arithmetic(self):
        """Odoo 19 accepts fractional manufacturing orders for serial-tracked
        products, so this tool must not invent a discreteness rule that Odoo
        does not have. Verified against Odoo itself in the test below."""
        answers = set()
        for tracking in ("none", "lot", "serial"):
            with self.subTest(tracking=tracking):
                finished = self._product(self.unit, "FG", tracking=tracking)
                part = self._product(self.kg, "X")
                self._bom(finished, [(part, 500.0, self.gram)])
                self._stock(part, 2.75)

                result = shortage_check(self.env, finished.default_code, 10.0)

                self.assertEqual(result["max_buildable"], 5.5, tracking)
                self.assertEqual(
                    self._component(result, part.default_code)["tracking"],
                    "none")
                answers.add(result["max_buildable"])

        self.assertEqual(len(answers), 1)

    def test_odoo_itself_accepts_a_fractional_order_for_every_tracking(self):
        """The evidence behind the test above, kept as a test so the day Odoo
        changes its mind, this suite says so instead of silently diverging."""
        for tracking in ("none", "lot", "serial"):
            with self.subTest(tracking=tracking):
                finished = self._product(self.unit, "FG", tracking=tracking)
                part = self._product(self.kg, "X")
                bom = self._bom(finished, [(part, 500.0, self.gram)])
                self._stock(part, 10.0)

                order = self.env["mrp.production"].create({
                    "product_id": finished.id, "bom_id": bom.id,
                    "product_qty": 5.5,
                    "product_uom_id": finished.uom_id.id})

                self.assertEqual(order.product_qty, 5.5)

    def test_a_tracked_component_still_reports_its_tracking(self):
        finished = self._product(self.unit, "FG")
        part = self._product(self.kg, "X", tracking="lot")
        self._bom(finished, [(part, 500.0, self.gram)])
        self._stock(part, 2.0)

        result = shortage_check(self.env, finished.default_code, 4.0)
        entry = self._component(result, part.default_code)

        self.assertEqual(entry["tracking"], "lot")
        self.assertEqual(result["max_buildable"], 4.0)


# ==========================================================================
# WORKSTREAM F - fractional finished quantities, precision boundaries (SEM-4)
# ==========================================================================

@tagged("post_install", "-at_install")
class TestFractionalQuantities(SemanticsCase):

    def _ratio_case(self, free_kg, per_unit_g):
        finished = self._product(self.unit, "FG")
        part = self._product(self.kg, "X")
        self._bom(finished, [(part, per_unit_g, self.gram)])
        self._stock(part, free_kg)
        result = shortage_check(self.env, finished.default_code, 1000.0)
        return finished, part, result

    def test_precision_boundaries_around_a_rounding_step(self):
        """One precision step below, exactly on, and one above.

        The steps are taken in the component's own unit, because Odoo rounds a
        quant to the unit's precision when it is written: asking for 2.7495 kg
        of stock stores 2.75, so a fixture built on it would describe stock
        that cannot exist. 0.01 kg is the smallest real step here.
        """
        rounding = self.unit.rounding      # 0.01 in Odoo 19
        self.assertEqual(exact(rounding), exact(0.01))

        for label, free_kg, per_unit_g, expected in [
            ("exact 5.50", 2.75, 500.0, exact("5.5")),
            ("one step below", 2.74, 500.0, exact("5.48")),
            ("one step above", 2.76, 500.0, exact("5.52")),
            ("recurring third", 1.0, 300.0, exact("3.33")),
            ("recurring ninth", 1.0, 900.0, exact("1.11")),
        ]:
            with self.subTest(case=label):
                finished, part, result = self._ratio_case(free_kg, per_unit_g)
                self.assertEqual(
                    exact(result["max_buildable"]), expected, label)
                self._assert_not_optimistic(
                    result, {part: [(per_unit_g, self.gram)]},
                    {part: free_kg}, label)

    def test_small_fractional_results_are_still_answers(self):
        for label, free_kg, per_unit_g in [
            ("just over zero", 0.001, 500.0),
            ("half a unit", 0.25, 500.0),
            ("a tenth", 0.05, 500.0),
        ]:
            with self.subTest(case=label):
                finished, part, result = self._ratio_case(free_kg, per_unit_g)
                self._assert_not_optimistic(
                    result, {part: [(per_unit_g, self.gram)]},
                    {part: free_kg}, label)
                self.assertGreaterEqual(result["max_buildable"], 0.0, label)

    def test_a_result_below_one_precision_step_is_zero_not_rounded_up(self):
        """Rounding DOWN at the bottom of the range: anything under 0.01 of a
        finished unit is not buildable, and must read as blocked rather than
        as a hundredth somebody might act on."""
        finished, part, result = self._ratio_case(0.004, 1000.0)

        self.assertEqual(result["max_buildable"], 0.0)
        self.assertEqual(result["verdict"], "blocked")
        self.assertIsNone(result["error"])


# ==========================================================================
# WORKSTREAM G - numerical safety across magnitudes (SEM-4)
# ==========================================================================

@tagged("post_install", "-at_install")
class TestNumericalSafety(SemanticsCase):

    def test_no_optimistic_result_at_any_magnitude(self):
        """The production answer is compared against an exact rational ratio at
        magnitudes spanning 24 orders. Conservative is allowed - that is what
        rounding DOWN to a precision means. Optimistic is a defect at any
        scale, and the 2**53 case that produced one is in here."""
        magnitudes = [1e-3, 1.0, 1e3, 1e6, 1e9, 1e12, 1e15]
        per_unit_values = [0.01, 0.5, 1.0, 3.0, 1000.0]

        for free_kg, per_unit_kg in itertools.product(magnitudes, per_unit_values):
            with self.subTest(free=free_kg, per_unit=per_unit_kg):
                finished = self._product(self.unit, "FG")
                part = self._product(self.kg, "X")
                self._bom(finished, [(part, per_unit_kg, self.kg)])
                self._stock(part, free_kg)

                result = shortage_check(self.env, finished.default_code, 1.0)
                if not result["components"]:
                    continue        # requirement below the unit's precision

                # Against what Odoo stored, not what was asked for: at these
                # magnitudes the two differ, and the tool is only answerable
                # for the figure it was given.
                _on_hand, _reserved, free = self._stored(part)
                true_ratio = free / exact(per_unit_kg)
                tolerance = Fraction(math.ulp(result["max_buildable"] or 0.0))
                self.assertLessEqual(
                    exact(result["max_buildable"]), true_ratio + tolerance,
                    "optimistic at free=%r per_unit=%r: %r > %s"
                    % (free_kg, per_unit_kg, result["max_buildable"],
                       float(true_ratio)))

    def test_conversion_does_not_introduce_optimism_at_scale(self):
        """The same sweep with the requirement written in the other unit, so
        the conversion multiply happens before the division."""
        for free_kg in [1e-3, 1.0, 1e6, 1e12, 1e15]:
            for per_unit_g in [10.0, 500.0, 1234.5]:
                with self.subTest(free=free_kg, per_unit_g=per_unit_g):
                    finished = self._product(self.unit, "FG")
                    part = self._product(self.kg, "X")
                    self._bom(finished, [(part, per_unit_g, self.gram)])
                    self._stock(part, free_kg)

                    result = shortage_check(self.env, finished.default_code, 1.0)
                    if not result["components"]:
                        continue

                    _on_hand, _reserved, free = self._stored(part)
                    true_ratio = (to_base(float(free), self.kg)
                                  / to_base(per_unit_g, self.gram))
                    tolerance = Fraction(math.ulp(result["max_buildable"] or 0.0))
                    self.assertLessEqual(
                        exact(result["max_buildable"]), true_ratio + tolerance,
                        "optimistic at free=%r kg, per_unit=%r g"
                        % (free_kg, per_unit_g))


# ==========================================================================
# WORKSTREAM H - everything at once
# ==========================================================================

@tagged("post_install", "-at_install")
class TestCombinedAggregation(SemanticsCase):

    def test_repeated_nested_mixed_uom_reserved_multi_location(self):
        """One fixture containing every dimension this audit covers:

          * the same component on two top-level lines (repeated lines)
          * the same component again through a kit (diamond, nested)
          * three different units across those three paths
          * reservations
          * stock split across two internal locations
          * a second component that is the real constraint

        Demand for X per finished unit:
            0.1 kg  +  150 g  +  0.05 kg (via kit)  =  0.3 kg
        Free X: (2 kg - 0.5 kg reserved) + 1 kg on a second shelf = 2.5 kg
            -> 2.5 / 0.3 = 8.333... -> 8.33
        Demand for Y: 2 Units; free Y: 12 -> 6.0    <- the constraint
        """
        finished = self._product(self.unit, "FG")
        x = self._product(self.kg, "X")
        y = self._product(self.unit, "Y")
        kit = self._product(self.unit, "KIT")

        self._bom(kit, [(x, 0.05, self.kg)], bom_type="phantom")
        self._bom(finished, [
            (x, 0.1, self.kg),
            (x, 150.0, self.gram),
            (kit, 1.0, None),
            (y, 2.0, None),
        ])

        shelf = self.env["stock.location"].create({
            "name": "Semantics Shelf C", "usage": "internal",
            "location_id": self.stock_location.id})
        self._stock(x, 2.0, reserved=0.5)
        self._stock(x, 1.0, location=shelf)
        self._stock(y, 12.0)

        result = shortage_check(self.env, finished.default_code, 10.0,
                                warehouse_code=self.warehouse.code)

        x_entry = self._component(result, x.default_code)
        y_entry = self._component(result, y.default_code)

        self.assertEqual(exact(x_entry["required_per_unit"]), exact("0.3"))
        self.assertEqual(exact(x_entry["available_qty"]), exact("2.5"))
        self.assertEqual(exact(y_entry["required_per_unit"]), exact(2.0))
        self.assertEqual(exact(y_entry["available_qty"]), exact(12.0))

        self.assertEqual(result["max_buildable"], 6.0)
        self.assertEqual(result["verdict"], "partial")
        self.assertEqual(result["limiting_components"], [y.default_code])
        self.assertEqual(
            [c["product_code"] for c in result["components"]].count(
                x.default_code), 1)

        self._assert_not_optimistic(
            result,
            {x: [(0.1, self.kg), (150.0, self.gram), (0.05, self.kg)],
             y: [(2.0, None)]},
            {x: 2.5, y: 12.0},
            "combined")


# ==========================================================================
# WORKSTREAM I - cross-tool consistency (SEM-5)
# ==========================================================================

@tagged("post_install", "-at_install")
class TestCrossToolConsistency(SemanticsCase):

    def test_sem5_bom_explode_and_shortage_check_agree_on_requirements(self):
        """Two tools, one physical requirement. An assistant that asks both and
        is told two different numbers has no way to know which to relay."""
        finished = self._product(self.unit, "FG")
        x = self._product(self.kg, "X")
        kit = self._product(self.unit, "KIT")

        self._bom(kit, [(x, 250.0, self.gram)], bom_type="phantom")
        self._bom(finished, [(x, 0.5, self.kg), (kit, 1.0, None)])
        self._stock(x, 10.0)

        quantity = 6.0
        shortage = shortage_check(self.env, finished.default_code, quantity)
        explode = readonly.bom_explode(
            self.env, finished.default_code, quantity=quantity)

        s_entry = self._component(shortage, x.default_code)
        e_entry = next(c for c in explode["components"]
                       if c["product_code"] == x.default_code)

        self.assertEqual(
            exact(s_entry["required_per_unit"]),
            exact(e_entry["required_per_unit"]),
            "SEM-5: the two tools disagree on requirement per unit")
        self.assertEqual(
            exact(s_entry["required_qty"]), exact(e_entry["required_qty"]),
            "SEM-5: the two tools disagree on total requirement")
        self.assertEqual(exact(e_entry["required_per_unit"]), exact("0.75"))

    def test_sem5_stock_status_and_shortage_check_agree_on_free_stock(self):
        finished = self._product(self.unit, "FG")
        x = self._product(self.kg, "X")
        self._bom(finished, [(x, 500.0, self.gram)])

        shelf = self.env["stock.location"].create({
            "name": "Semantics Shelf D", "usage": "internal",
            "location_id": self.stock_location.id})
        self._stock(x, 4.0, reserved=1.0)
        self._stock(x, 2.0, location=shelf)

        shortage = shortage_check(self.env, finished.default_code, 1.0,
                                  warehouse_code=self.warehouse.code)
        status = readonly.stock_status(self.env, x.default_code,
                                       warehouse_code=self.warehouse.code)
        entry = self._component(shortage, x.default_code)

        for field in ("on_hand_qty", "reserved_qty", "available_qty"):
            self.assertEqual(
                exact(entry[field]), exact(status[field]),
                "SEM-5: %s differs between shortage_check and stock_status"
                % field)
        self.assertEqual(exact(status["available_qty"]), exact(5.0))

    def test_sem5_tools_agree_across_a_warehouse_boundary(self):
        finished = self._product(self.unit, "FG")
        x = self._product(self.gram, "X")
        self._bom(finished, [(x, 0.5, self.kg)])

        self._stock(x, 3000.0)
        self._stock(x, 9000.0, location=self.warehouse_2.lot_stock_id)

        for warehouse_code in (self.warehouse.code, self.warehouse_2.code, None):
            with self.subTest(warehouse=warehouse_code):
                shortage = shortage_check(self.env, finished.default_code, 1.0,
                                          warehouse_code=warehouse_code)
                status = readonly.stock_status(self.env, x.default_code,
                                               warehouse_code=warehouse_code)
                entry = self._component(shortage, x.default_code)
                self.assertEqual(
                    exact(entry["available_qty"]), exact(status["available_qty"]),
                    "SEM-5: free stock differs at warehouse %r" % warehouse_code)


# ==========================================================================
# WORKSTREAM J - incompatible units
# ==========================================================================

@tagged("post_install", "-at_install")
class TestIncompatibleUom(SemanticsCase):

    def test_odoo_permits_the_state_and_invents_a_conversion(self):
        """Documents why the refusal below has to exist.

        Odoo 19 lets a bill-of-materials line name a unit from a different
        tree than the component is stocked in, and `_compute_quantity` does not
        refuse: its docstring still promises to raise on an incompatible
        category, but the implementation multiplies factors regardless. One
        kilogramme becomes a thousand Units.
        """
        self.assertNotEqual(uom_root(self.kg), uom_root(self.unit))
        self.assertEqual(self.kg._compute_quantity(1.0, self.unit), 1000.0)

        finished = self._product(self.unit, "FG")
        part = self._product(self.kg, "X")
        bom = self._bom(finished, [(part, 2.0, self.litre)])

        self.assertEqual(bom.bom_line_ids.product_uom_id, self.litre,
                         "Odoo accepted a litre line on a kilogramme component")

    def test_an_incompatible_line_is_refused_not_guessed(self):
        """A litre of a component stocked in kilogrammes is not a quantity this
        tool can reason about. Converting it anyway produces a feasibility
        verdict from invented arithmetic, which is worse than no answer."""
        finished = self._product(self.unit, "FG")
        part = self._product(self.kg, "X")
        self._bom(finished, [(part, 2.0, self.litre)])
        self._stock(part, 10000.0)

        result = shortage_check(self.env, finished.default_code, 1.0)

        self.assertEqual(result["verdict"], "blocked")
        self.assertIsNotNone(result["error"])
        self.assertEqual(result["error"]["code"], "incompatible_uom")
        self.assertTrue(result["limitations"])

    def test_bom_explode_refuses_the_same_state(self):
        """SEM-5: the two tools must not disagree about whether the question
        can be answered at all."""
        finished = self._product(self.unit, "FG")
        part = self._product(self.kg, "X")
        self._bom(finished, [(part, 2.0, self.litre)])

        result = readonly.bom_explode(self.env, finished.default_code, quantity=1.0)

        self.assertIsNotNone(result["error"])
        self.assertEqual(result["error"]["code"], "incompatible_uom")
        self.assertTrue(result["limitations"])

    def test_a_compatible_line_in_another_unit_is_not_refused(self):
        """The refusal must be narrow. Grams against kilogrammes is an ordinary
        conversion and has to keep working."""
        finished = self._product(self.unit, "FG")
        part = self._product(self.kg, "X")
        self._bom(finished, [(part, 500.0, self.gram)])
        self._stock(part, 2.0)

        result = shortage_check(self.env, finished.default_code, 4.0)

        self.assertIsNone(result["error"])
        self.assertEqual(result["max_buildable"], 4.0)


# ==========================================================================
# generated matrix
# ==========================================================================

@tagged("post_install", "-at_install")
class TestGeneratedSemanticMatrix(SemanticsCase):

    def test_deterministic_matrix_holds_every_invariant(self):
        """A deterministic sweep, no randomness and no new dependency.

        Every case is checked against the oracle for SEM-4 (never optimistic)
        and, where the same physical state is expressible in two units, for
        SEM-1 (same conclusion). The point of a matrix rather than more
        examples is the combinations nobody would think to write by hand.
        """
        # (stock unit, line unit, line quantities in the LINE's unit). The
        # quantities are chosen so every derived figure - the halved kit line,
        # the on-hand quantity, the reservation - is representable at both
        # units' precision. Odoo rounds a quant on write, so a fixture that
        # ignores this silently describes stock that does not exist.
        mass_pairs = [
            (self.kg, self.gram, [250.0, 500.0, 1500.0]),
            (self.gram, self.kg, [0.5, 1.5, 2.0]),
        ]
        free_multipliers = [0.0, 0.5, 1.0, 2.5, 10.0]
        reserved_fractions = [0.0, 0.25, 1.0]
        nested = [False, True]

        checked = 0
        for (stock_uom, line_uom, line_quantities) in mass_pairs:
            for line_qty, multiplier, reserved_fraction, use_kit in itertools.product(
                    line_quantities, free_multipliers, reserved_fractions, nested):
                with self.subTest(stock=stock_uom.name, line=line_uom.name,
                                  line_qty=line_qty, free=multiplier,
                                  reserved=reserved_fraction, nested=use_kit):
                    finished = self._product(self.unit, "FG")
                    part = self._product(stock_uom, "X")

                    if use_kit:
                        # Half directly, half through a kit: the same physical
                        # demand, reached by two paths.
                        kit = self._product(self.unit, "KIT")
                        self._bom(kit, [(part, line_qty / 2.0, line_uom)],
                                  bom_type="phantom")
                        self._bom(finished, [(part, line_qty / 2.0, line_uom),
                                             (kit, 1.0, None)])
                        demand_lines = [(line_qty / 2.0, line_uom),
                                        (line_qty / 2.0, line_uom)]
                    else:
                        self._bom(finished, [(part, line_qty, line_uom)])
                        demand_lines = [(line_qty, line_uom)]

                    # Demand per finished unit, in the component's own unit.
                    per_unit = float(from_base(
                        to_base(line_qty, line_uom), stock_uom))
                    requested = per_unit * multiplier * 4.0
                    if requested:
                        self._stock(part, requested,
                                    reserved=requested * reserved_fraction)
                    on_hand, reserved, free = self._stored(part)

                    result = shortage_check(self.env, finished.default_code, 4.0)
                    checked += 1

                    self.assertIsNone(
                        result["error"],
                        "no case in this matrix should be unanswerable")

                    demands = {part: demand_lines}
                    supplies = {part: float(free)}
                    self._assert_not_optimistic(result, demands, supplies)

                    expected = self._expected_max_buildable(
                        finished, demands, supplies)
                    self.assertEqual(
                        exact(result["max_buildable"]), expected,
                        "matrix case diverged from the oracle")

                    # SEM-3: free stock decides, never the on-hand figure.
                    entry = self._component(result, part.default_code)
                    self.assertEqual(exact(entry["available_qty"]), free)
                    self.assertEqual(exact(entry["on_hand_qty"]), on_hand)

                    # SEM-1: the verdict follows the physical ratio, whichever
                    # unit the two sides happen to be written in.
                    if multiplier >= 1.0 and reserved_fraction == 0.0:
                        self.assertEqual(result["verdict"], "can_build")
                    elif free == 0:
                        self.assertEqual(result["verdict"], "blocked")

        self.assertEqual(checked, 180, "the matrix did not run in full")
