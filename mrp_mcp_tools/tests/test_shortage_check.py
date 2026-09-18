# -*- coding: utf-8 -*-
"""Unit tests for shortage_check.

REPLACES_PROVENANCE_RISK: mrp_mcp_tools/tests/test_shortage_check.py (v0.1.0)

Clean-room specification. Every scenario below is derived from this product's
own published contract and from generic manufacturing arithmetic, not from any
prior suite:

  * the availability rule stated in README.md and RELEASE_NOTES_v0.1.0.md -
    available = max(quantity - reserved_quantity, 0)
  * max_buildable = min over components of floor(available / required_per_unit)
  * the response envelope in schemas/shortage_check.schema.json - verdict,
    components, limiting_components, limitations, schema_version, as_of
  * the error codes raised by tools/shortage.py - product_not_found, no_bom,
    inactive_bom, invalid_quantity, warehouse_not_found
  * Odoo's own documented behaviour: a phantom bill of materials is consumed
    rather than stocked, and a plain search hides archived records

Fixture codes and every quantity in this file were chosen here, for this file,
to exercise the arithmetic at its boundaries.
"""

from odoo.tests import TransactionCase, tagged

from ..tools.shortage import shortage_check


@tagged("post_install", "-at_install")
class TestShortageCheck(TransactionCase):
    """One assembly, two components, deliberately different ratios.

    SC-ASSY = 2 x SC-PART-X + 3 x SC-PART-Y

    Unequal ratios matter: with 1:1 an off-by-one in the division would divide
    correctly by accident, and a test that cannot fail proves nothing.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.uom_unit = cls.env.ref("uom.product_uom_unit")
        cls.warehouse = cls.env["stock.warehouse"].search([], limit=1)
        cls.stock_location = cls.warehouse.lot_stock_id

        # A second site, so that warehouse scoping has something to exclude.
        cls.warehouse_2 = cls.env["stock.warehouse"].create({
            "name": "Shortage Test Site B",
            "code": "SCB",
        })

        def product(code, name):
            return cls.env["product.product"].create({
                "name": name,
                "default_code": code,
                "is_storable": True,
                "uom_id": cls.uom_unit.id,
            })

        cls.assembly = product("SC-ASSY", "Shortage Test Assembly")
        cls.part_x = product("SC-PART-X", "Shortage Test Part X")
        cls.part_y = product("SC-PART-Y", "Shortage Test Part Y")
        cls.subkit = product("SC-SUBKIT", "Shortage Test Sub Kit")

        cls.bom = cls.env["mrp.bom"].create({
            "product_tmpl_id": cls.assembly.product_tmpl_id.id,
            "product_qty": 1.0,
            "type": "normal",
            "code": "SC-BOM-ASSY",
            "bom_line_ids": [
                (0, 0, {"product_id": cls.part_x.id, "product_qty": 2.0}),
                (0, 0, {"product_id": cls.part_y.id, "product_qty": 3.0}),
            ],
        })

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _set_stock(self, product, quantity, reserved=0.0, location=None):
        """Put quantity on hand, of which `reserved` is committed elsewhere.

        reserved_quantity is written directly rather than by running a real
        reservation: the contract under test is how shortage_check *reads* the
        field, not how Odoo comes to fill it.
        """
        location = location or self.stock_location
        self.env["stock.quant"]._update_available_quantity(
            product, location, quantity
        )
        if reserved:
            quant = self.env["stock.quant"].search([
                ("product_id", "=", product.id),
                ("location_id", "=", location.id),
            ], limit=1)
            quant.sudo().write({"reserved_quantity": reserved})

    def _component(self, result, code):
        for comp in result["components"]:
            if comp["product_code"] == code:
                return comp
        self.fail("Component %s missing from result: %s" % (
            code, [c["product_code"] for c in result["components"]]
        ))

    # ------------------------------------------------------------------
    # the arithmetic: max_buildable = min(floor(available / per unit))
    # ------------------------------------------------------------------

    def test_ample_stock_is_buildable(self):
        """Well above requirement on both components: no shortage anywhere."""
        self._set_stock(self.part_x, 400.0)
        self._set_stock(self.part_y, 400.0)

        result = shortage_check(self.env, "SC-ASSY", 12.0)

        self.assertEqual(result["verdict"], "can_build")
        self.assertGreaterEqual(result["max_buildable"], 12.0)
        self.assertEqual(result["limiting_components"], [])
        self.assertEqual(self._component(result, "SC-PART-X")["shortage_qty"], 0.0)
        self.assertEqual(self._component(result, "SC-PART-Y")["shortage_qty"], 0.0)

    def test_exactly_enough_reads_as_buildable(self):
        """Lower boundary of can_build: required == available, on both lines.

        6 units need 12 of X and 18 of Y. Exactly that must read as buildable,
        not as one short - an off-by-one here refuses every perfectly planned
        order, which is the failure a planner would notice last and trust least.
        """
        self._set_stock(self.part_x, 12.0)
        self._set_stock(self.part_y, 18.0)

        result = shortage_check(self.env, "SC-ASSY", 6.0)

        self.assertEqual(result["verdict"], "can_build")
        self.assertEqual(result["max_buildable"], 6.0)
        self.assertEqual(result["limiting_components"], [])

    def test_one_unit_short_is_partial_and_names_the_component(self):
        """Upper boundary of partial: one assembly less than exactly enough.

        10 of X supports 5 units, not the 6 asked for, and X alone is why.
        """
        self._set_stock(self.part_x, 10.0)
        self._set_stock(self.part_y, 18.0)

        result = shortage_check(self.env, "SC-ASSY", 6.0)

        self.assertEqual(result["verdict"], "partial")
        self.assertEqual(result["max_buildable"], 5.0)
        self.assertEqual(result["limiting_components"], ["SC-PART-X"])
        self.assertEqual(self._component(result, "SC-PART-X")["shortage_qty"], 2.0)

    def test_max_buildable_respects_the_products_uom_precision(self):
        """The contract, as stated in schemas/shortage_check.schema.json:
        "Largest quantity of the finished product that current free stock
        supports, rounded DOWN to the product's unit-of-measure precision."

        11 of X at 2 per assembly is 5.5. Odoo 19 gives every unit of measure -
        Units included - a rounding of 0.01, and Odoo itself creates and
        confirms a manufacturing order for 5.5 Units, for untracked, lot- and
        serial-tracked products alike. So 5.5 is a quantity this product may
        legitimately report: it is exactly what 11 components make, and
        flooring it to 5 would understate free stock by half an assembly
        against Odoo's own semantics.

        There is no field in Odoo 19 that marks a product as discrete, so the
        rounding cannot be made conditional on one. A caller that builds whole
        items should floor the number; the tool does not guess that for them.
        """
        self._set_stock(self.part_x, 11.0)
        self._set_stock(self.part_y, 18.0)

        result = shortage_check(self.env, "SC-ASSY", 6.0)

        self.assertEqual(result["max_buildable"], 5.5)
        self.assertEqual(result["verdict"], "partial")
        # And the answer is honest about material: 5.5 assemblies consume
        # exactly the 11 units of X that are free.
        self.assertEqual(
            result["max_buildable"]
            * self._component(result, "SC-PART-X")["required_per_unit"],
            11.0,
        )

    def test_max_buildable_never_exceeds_the_true_ratio(self):
        """The one way rounding DOWN can overpromise: floating point.

        `float_round(value, precision_rounding=0.01, rounding_method="DOWN")`
        divides by the precision before flooring. Past 2**53 that division is
        no longer exact, and the result can come back ABOVE the input - at
        5e14 it returns 500000000000000.5. A max_buildable larger than the
        quantity the stock actually supports is the one error this tool must
        never make, however absurd the magnitude.

        Reached here with a component consumed in thousandths, which is an
        ordinary way to express a bulk material, against a large free stock.
        """
        # 0.001 per assembly, so the ratio is a thousand times the stock.
        tiny = self.env["product.product"].create({
            "name": "Shortage Test Bulk Material",
            "default_code": "SC-BULK",
            "is_storable": True,
            "uom_id": self.uom_unit.id,
        })
        self.bom.write({
            "bom_line_ids": [(0, 0, {"product_id": tiny.id, "product_qty": 0.001})]
        })
        self._set_stock(self.part_x, 1.0e15)
        self._set_stock(self.part_y, 1.0e15)
        self._set_stock(tiny, 1.0e13)

        result = shortage_check(self.env, "SC-ASSY", 1.0)

        for comp in result["components"]:
            true_ratio = comp["available_qty"] / comp["required_per_unit"]
            self.assertLessEqual(
                comp["max_buildable_from_this"], true_ratio,
                "%s: reported %r from %r available at %r each"
                % (comp["product_code"], comp["max_buildable_from_this"],
                   comp["available_qty"], comp["required_per_unit"]),
            )
        self.assertLessEqual(
            result["max_buildable"],
            min(c["available_qty"] / c["required_per_unit"]
                for c in result["components"]),
        )

    def test_a_fractional_component_requirement_divides_correctly(self):
        """Half a unit of X per assembly: 7 free supports 14 assemblies."""
        self.bom.bom_line_ids.filtered(
            lambda line: line.product_id == self.part_x
        ).product_qty = 0.5
        self._set_stock(self.part_x, 7.0)
        self._set_stock(self.part_y, 400.0)

        result = shortage_check(self.env, "SC-ASSY", 20.0)

        self.assertEqual(
            self._component(result, "SC-PART-X")["required_per_unit"], 0.5)
        self.assertEqual(result["max_buildable"], 14.0)

    def test_a_recurring_decimal_is_rounded_down_not_up(self):
        """10 free at 3 each is 3.333... Rounded DOWN to 0.01 that is 3.33,
        which consumes 9.99 - never 3.34, which would need 10.02."""
        self.bom.bom_line_ids.filtered(
            lambda line: line.product_id == self.part_x
        ).product_qty = 3.0
        self._set_stock(self.part_x, 10.0)
        self._set_stock(self.part_y, 400.0)

        result = shortage_check(self.env, "SC-ASSY", 10.0)

        self.assertEqual(result["max_buildable"], 3.33)
        self.assertLessEqual(result["max_buildable"] * 3.0, 10.0)

    def test_two_components_can_limit_together(self):
        """Both run out at the same assembly count, so both are named.

        A report that picks one arbitrarily sends somebody to expedite half a
        shortage and be blocked again on arrival.
        """
        self._set_stock(self.part_x, 8.0)    # 8 / 2 = 4
        self._set_stock(self.part_y, 12.0)   # 12 / 3 = 4
        result = shortage_check(self.env, "SC-ASSY", 6.0)

        self.assertEqual(result["max_buildable"], 4.0)
        self.assertEqual(
            sorted(result["limiting_components"]), ["SC-PART-X", "SC-PART-Y"])
        self.assertTrue(self._component(result, "SC-PART-X")["is_limiting"])
        self.assertTrue(self._component(result, "SC-PART-Y")["is_limiting"])

    def test_a_component_measured_in_a_different_unit_is_converted(self):
        """The bill of materials line is in grams, the component is stocked in
        kilograms. The requirement must be converted before it is compared, or
        a thousandfold error passes as an answer.
        """
        category = self.env.ref("uom.product_uom_kgm").with_context(
            active_test=False)
        gram = self.env.ref("uom.product_uom_gram")

        bulk = self.env["product.product"].create({
            "name": "Shortage Test Adhesive",
            "default_code": "SC-ADHESIVE",
            "is_storable": True,
            "uom_id": category.id,          # stocked in kg
        })
        self.bom.write({
            "bom_line_ids": [(0, 0, {
                "product_id": bulk.id,
                "product_qty": 500.0,       # 500 g per assembly
                "product_uom_id": gram.id,
            })]
        })
        self._set_stock(self.part_x, 400.0)
        self._set_stock(self.part_y, 400.0)
        self._set_stock(bulk, 4.0)          # 4 kg on hand

        result = shortage_check(self.env, "SC-ASSY", 4.0)
        adhesive = self._component(result, "SC-ADHESIVE")

        # 500 g = 0.5 kg per assembly, so 4 kg supports 8 assemblies.
        self.assertEqual(adhesive["uom"], category.name)
        self.assertEqual(adhesive["required_per_unit"], 0.5)
        self.assertEqual(adhesive["required_qty"], 2.0)
        self.assertEqual(adhesive["max_buildable_from_this"], 8.0)

    def test_the_scarcest_component_sets_the_answer(self):
        """min(8 / 2, 15 / 3) = min(4, 5) = 4.

        Both components are short of the 6 asked for, but only the one that
        runs out first limits the build, and the report must say which.
        """
        self._set_stock(self.part_x, 8.0)
        self._set_stock(self.part_y, 15.0)

        result = shortage_check(self.env, "SC-ASSY", 6.0)

        self.assertEqual(result["verdict"], "partial")
        self.assertEqual(result["max_buildable"], 4.0)
        self.assertEqual(result["limiting_components"], ["SC-PART-X"])

        short_x = self._component(result, "SC-PART-X")
        self.assertEqual(short_x["required_qty"], 12.0)
        self.assertEqual(short_x["available_qty"], 8.0)
        self.assertEqual(short_x["shortage_qty"], 4.0)
        self.assertTrue(short_x["is_limiting"])

        # Y is short too - 18 needed, 15 free - but it is not the constraint.
        short_y = self._component(result, "SC-PART-Y")
        self.assertEqual(short_y["shortage_qty"], 3.0)
        self.assertFalse(short_y["is_limiting"])

    def test_no_stock_at_all_is_blocked_without_an_error(self):
        """Nothing on hand is an answer about the factory, not a failure."""
        result = shortage_check(self.env, "SC-ASSY", 6.0)

        self.assertEqual(result["verdict"], "blocked")
        self.assertEqual(result["max_buildable"], 0.0)
        self.assertIsNone(result["error"])

    # ------------------------------------------------------------------
    # the product's distinguishing rule: free stock, not on-hand stock
    # ------------------------------------------------------------------

    def test_reserved_stock_does_not_count_towards_availability(self):
        """available = max(on hand - reserved, 0).

        30 of X on the shelf with 24 committed to other work leaves 6 free,
        which is three assemblies - not the fifteen a connector reporting the
        on-hand figure would promise.
        """
        self._set_stock(self.part_x, 30.0, reserved=24.0)
        self._set_stock(self.part_y, 400.0)

        result = shortage_check(self.env, "SC-ASSY", 10.0)

        comp = self._component(result, "SC-PART-X")
        self.assertEqual(comp["on_hand_qty"], 30.0)
        self.assertEqual(comp["reserved_qty"], 24.0)
        self.assertEqual(comp["available_qty"], 6.0)

        self.assertEqual(result["verdict"], "partial")
        self.assertEqual(result["max_buildable"], 3.0)

    def test_fully_reserved_stock_is_zero_available_not_negative(self):
        """The floor at zero. Over-reservation must not read as negative stock,
        which would make a component look like a credit against another."""
        self._set_stock(self.part_x, 9.0, reserved=9.0)
        self._set_stock(self.part_y, 400.0)

        result = shortage_check(self.env, "SC-ASSY", 1.0)

        self.assertEqual(self._component(result, "SC-PART-X")["available_qty"], 0.0)
        self.assertEqual(result["max_buildable"], 0.0)
        self.assertEqual(result["verdict"], "blocked")

    # ------------------------------------------------------------------
    # explosion: aggregate before deciding
    # ------------------------------------------------------------------

    def test_a_component_reached_by_two_paths_is_summed_once(self):
        """Diamond dependency.

        The assembly needs X directly (2) and again through a kit (4), so six
        per unit. Checking each exploded line against free stock separately
        compares the same 42 units against both requirements and promises them
        twice - 42/2 = 21 and 42/4 = 10 both look fine, while the truth is 7.
        The requirement has to be summed per product before any comparison.
        """
        self.env["mrp.bom"].create({
            "product_tmpl_id": self.subkit.product_tmpl_id.id,
            "product_qty": 1.0,
            "type": "phantom",
            "code": "SC-BOM-SUBKIT",
            "bom_line_ids": [
                (0, 0, {"product_id": self.part_x.id, "product_qty": 4.0}),
            ],
        })
        self.bom.write({
            "bom_line_ids": [(0, 0, {"product_id": self.subkit.id, "product_qty": 1.0})]
        })
        self._set_stock(self.part_x, 42.0)
        self._set_stock(self.part_y, 400.0)

        result = shortage_check(self.env, "SC-ASSY", 20.0)

        comp = self._component(result, "SC-PART-X")
        self.assertEqual(comp["required_per_unit"], 6.0)
        self.assertEqual(comp["required_qty"], 120.0)
        self.assertEqual(result["max_buildable"], 7.0)
        self.assertEqual(result["verdict"], "partial")

        codes = [c["product_code"] for c in result["components"]]
        self.assertEqual(codes.count("SC-PART-X"), 1,
                         "one line per product, or the reader sees two answers")

    def test_a_kit_is_expanded_and_never_listed_as_a_component(self):
        """A phantom bill is consumed, not stocked. Reporting the kit itself
        would send a planner looking for a part that is never held."""
        self.env["mrp.bom"].create({
            "product_tmpl_id": self.subkit.product_tmpl_id.id,
            "product_qty": 1.0,
            "type": "phantom",
            "code": "SC-BOM-SUBKIT",
            "bom_line_ids": [
                (0, 0, {"product_id": self.part_y.id, "product_qty": 5.0}),
            ],
        })
        self.bom.write({
            "bom_line_ids": [(0, 0, {"product_id": self.subkit.id, "product_qty": 1.0})]
        })
        self._set_stock(self.part_x, 400.0)
        self._set_stock(self.part_y, 400.0)

        result = shortage_check(self.env, "SC-ASSY", 1.0)
        codes = [c["product_code"] for c in result["components"]]

        self.assertNotIn("SC-SUBKIT", codes)
        # 3 directly + 5 through the kit
        self.assertEqual(self._component(result, "SC-PART-Y")["required_qty"], 8.0)

    def test_a_purchased_component_is_a_leaf(self):
        """A component with no bill of materials generates no further
        requirement, and says so, so the caller knows not to ask again."""
        self._set_stock(self.part_x, 400.0)
        self._set_stock(self.part_y, 400.0)

        result = shortage_check(self.env, "SC-ASSY", 1.0)

        self.assertEqual(len(result["components"]), 2)
        self.assertFalse(self._component(result, "SC-PART-X")["has_own_bom"])

    # ------------------------------------------------------------------
    # warehouse scoping
    # ------------------------------------------------------------------

    def test_stock_at_another_site_is_excluded_when_a_warehouse_is_named(self):
        """Stock elsewhere is real, but it cannot start work here today.
        Counting it is how an assistant promises an order that never ships."""
        self._set_stock(self.part_x, 400.0, location=self.warehouse_2.lot_stock_id)
        self._set_stock(self.part_y, 400.0, location=self.warehouse_2.lot_stock_id)

        scoped = shortage_check(
            self.env, "SC-ASSY", 6.0, warehouse_code=self.warehouse.code)
        self.assertEqual(scoped["verdict"], "blocked")
        self.assertEqual(scoped["asked"]["warehouse_code"], self.warehouse.code)

        company_wide = shortage_check(self.env, "SC-ASSY", 6.0)
        self.assertEqual(company_wide["verdict"], "can_build")

    def test_an_unknown_warehouse_is_refused_rather_than_ignored(self):
        """Silently widening to every site would answer a question nobody
        asked, and answer it optimistically."""
        result = shortage_check(self.env, "SC-ASSY", 1.0, warehouse_code="NO-SUCH-WH")

        self.assertEqual(result["verdict"], "blocked")
        self.assertEqual(result["error"]["code"], "warehouse_not_found")

    # ------------------------------------------------------------------
    # refusals
    # ------------------------------------------------------------------

    def test_a_product_that_cannot_be_manufactured_is_refused(self):
        result = shortage_check(self.env, "SC-PART-X", 1.0)

        self.assertEqual(result["verdict"], "blocked")
        self.assertEqual(result["error"]["code"], "no_bom")

    def test_an_archived_bom_is_named_as_archived(self):
        """Not "no such code": a plain Odoo search hides archived records, so
        the caller would be sent hunting for a typo that does not exist."""
        self.bom.active = False

        result = shortage_check(self.env, "SC-ASSY", 1.0, bom_code="SC-BOM-ASSY")

        self.assertEqual(result["verdict"], "blocked")
        self.assertEqual(result["error"]["code"], "inactive_bom")

    def test_quantities_that_are_not_quantities_are_refused(self):
        """Zero, negative, unparseable, absent - and the two float values that
        are the real trap. NaN survives float() and compares False against
        every threshold, so an unguarded check would let it through and then
        divide by it."""
        for bad in (0, -5, "abc", None, float("nan"), float("inf")):
            result = shortage_check(self.env, "SC-ASSY", bad)
            self.assertEqual(result["verdict"], "blocked", bad)
            self.assertEqual(result["error"]["code"], "invalid_quantity", bad)

    def test_an_unknown_product_is_refused(self):
        result = shortage_check(self.env, "SC-NO-SUCH-CODE", 1.0)

        self.assertEqual(result["verdict"], "blocked")
        self.assertEqual(result["error"]["code"], "product_not_found")

    # ------------------------------------------------------------------
    # the response contract
    # ------------------------------------------------------------------

    def test_every_answer_carries_its_blind_spots(self):
        """limitations is not decoration. An answer that hides what it did not
        check - incoming procurement, unreserved demand, capacity - reads as a
        promise. Refusals carry them too, because a refusal is still an answer
        somebody may act on."""
        self._set_stock(self.part_x, 400.0)
        self._set_stock(self.part_y, 400.0)

        for result in (
            shortage_check(self.env, "SC-ASSY", 1.0),
            shortage_check(self.env, "SC-NO-SUCH-CODE", 1.0),
            shortage_check(self.env, "SC-ASSY", 0),
        ):
            self.assertTrue(result["limitations"])
            self.assertEqual(result["schema_version"], "1.0")
            self.assertIn(result["verdict"], ("can_build", "partial", "blocked"))
            self.assertTrue(result["as_of"])
