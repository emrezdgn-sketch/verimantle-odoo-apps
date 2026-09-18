# -*- coding: utf-8 -*-
"""Unit tests for the seven tools around shortage_check.

REPLACES_PROVENANCE_RISK: mrp_mcp_tools/tests/test_readonly_tools.py (v0.1.0)

Clean-room specification. Each test below is derived from the tool's own
declared contract and from public Odoo behaviour:

  * the input schemas and handlers in tools/registry.py and tools/readonly.py
  * the shared envelope in tools/common.py - error, schema_version, as_of,
    limitations
  * the availability rule published in README.md: free stock, never on-hand
  * Odoo's documented semantics for stock.quant, stock.move, stock.lot and
    mrp.production, which are what the tools read

Fixture codes and quantities were chosen here. Nothing in this file depends on
the scenario catalogue, naming or values of any earlier suite.
"""

from odoo.tests import TransactionCase, tagged

from ..tools import readonly


@tagged("post_install", "-at_install")
class TestReadOnlyTools(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.uom_unit = cls.env.ref("uom.product_uom_unit")
        cls.warehouse = cls.env["stock.warehouse"].search([], limit=1)
        cls.stock_location = cls.warehouse.lot_stock_id
        cls.supplier_location = cls.env.ref("stock.stock_location_suppliers")

        cls.warehouse_2 = cls.env["stock.warehouse"].create({
            "name": "Read Tools Site B", "code": "RTB",
        })

        def product(code, name, tracking="none"):
            return cls.env["product.product"].create({
                "name": name,
                "default_code": code,
                "is_storable": True,
                "uom_id": cls.uom_unit.id,
                "tracking": tracking,
            })

        # RT- prefix so the search tests have a stable, self-contained
        # namespace that cannot collide with Odoo demo data.
        cls.assembly = product("RT-ASSY", "Read Tools Assembly")
        cls.part_x = product("RT-PART-X", "Read Tools Part X", tracking="lot")
        cls.part_y = product("RT-PART-Y", "Read Tools Part Y")
        cls.subkit = product("RT-SUBKIT", "Read Tools Sub Kit")
        cls.bystander = product("RT-BYSTANDER", "Read Tools Bystander")

        # RT-ASSY = 2 x RT-PART-X + 3 x RT-PART-Y
        cls.bom = cls.env["mrp.bom"].create({
            "product_tmpl_id": cls.assembly.product_tmpl_id.id,
            "product_qty": 1.0,
            "type": "normal",
            "code": "RT-BOM-ASSY",
            "bom_line_ids": [
                (0, 0, {"product_id": cls.part_x.id, "product_qty": 2.0}),
                (0, 0, {"product_id": cls.part_y.id, "product_qty": 3.0}),
            ],
        })

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _stock(self, product, quantity, location=None, lot=None):
        self.env["stock.quant"]._update_available_quantity(
            product, location or self.stock_location, quantity, lot_id=lot
        )

    def _lot(self, product, name):
        return self.env["stock.lot"].create({
            "name": name, "product_id": product.id,
        })

    def _receive(self, product, quantity, lot=None):
        """A completed incoming movement, so there is real history to read."""
        move = self.env["stock.move"].create({
            "product_id": product.id,
            "product_uom_qty": quantity,
            "product_uom": product.uom_id.id,
            "location_id": self.supplier_location.id,
            "location_dest_id": self.stock_location.id,
        })
        move._action_confirm()
        move._action_assign()
        line = move.move_line_ids
        if not line:
            line = self.env["stock.move.line"].create({
                "move_id": move.id,
                "product_id": product.id,
                "product_uom_id": product.uom_id.id,
                "location_id": self.supplier_location.id,
                "location_dest_id": self.stock_location.id,
            })
        line.quantity = quantity
        if lot:
            line.lot_id = lot.id
        move.picked = True
        move._action_done()
        return move

    def _confirmed_order(self, quantity=3.0):
        order = self.env["mrp.production"].create({
            "product_id": self.assembly.id,
            "bom_id": self.bom.id,
            "product_qty": quantity,
            "product_uom_id": self.assembly.uom_id.id,
        })
        order.action_confirm()
        order.action_assign()
        return order

    # ==================================================================
    # product_search
    # ==================================================================

    def test_product_search_finds_an_exact_code(self):
        result = readonly.product_search(self.env, "RT-ASSY")

        self.assertIsNone(result["error"])
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["matches"][0]["product_code"], "RT-ASSY")

    def test_product_search_says_whether_a_product_can_be_made(self):
        """has_bom is what tells the assistant which tool to reach for next:
        shortage_check for something manufactured, stock_status for a part."""
        made = readonly.product_search(self.env, "RT-ASSY")["matches"][0]
        bought = readonly.product_search(self.env, "RT-PART-Y")["matches"][0]

        self.assertTrue(made["has_bom"])
        self.assertFalse(bought["has_bom"])

    def test_product_search_truncates_and_admits_it(self):
        """A silently cut list reads as the whole catalogue."""
        result = readonly.product_search(self.env, "RT-", limit=3)

        self.assertEqual(len(result["matches"]), 3)
        self.assertTrue(result["truncated"])

    def test_product_search_finding_nothing_is_not_an_error(self):
        result = readonly.product_search(self.env, "RT-NOTHING-LIKE-THIS")

        self.assertIsNone(result["error"])
        self.assertEqual(result["match_count"], 0)

    def test_product_search_states_its_limitations(self):
        """Every tool, including the cheapest one. A catalogue lookup that
        omits them breaks the one rule the whole envelope exists to keep."""
        result = readonly.product_search(self.env, "RT-")

        self.assertTrue(result["limitations"])

    # ==================================================================
    # stock_status
    # ==================================================================

    def test_stock_status_reports_free_stock_not_on_hand(self):
        """A confirmed order for 4 assemblies reserves 8 of X. 24 on the shelf
        is then 16 free - and 16 is the number a planner can act on."""
        self._stock(self.part_x, 24.0)
        self._stock(self.part_y, 90.0)
        self._confirmed_order(4.0)

        result = readonly.stock_status(self.env, "RT-PART-X")

        self.assertEqual(result["on_hand_qty"], 24.0)
        self.assertEqual(result["reserved_qty"], 8.0)
        self.assertEqual(result["available_qty"], 16.0)

    def test_stock_status_refuses_an_unknown_product(self):
        result = readonly.stock_status(self.env, "RT-NO-SUCH-PRODUCT")

        self.assertEqual(result["error"]["code"], "product_not_found")

    def test_stock_status_does_not_mix_two_products(self):
        """A neighbour holding 900 must not leak into this answer."""
        self._stock(self.part_x, 7.0)
        self._stock(self.bystander, 900.0)

        result = readonly.stock_status(self.env, "RT-PART-X")

        self.assertEqual(result["on_hand_qty"], 7.0)

    def test_stock_status_scopes_to_the_named_warehouse(self):
        self._stock(self.part_x, 5.0)
        self._stock(self.part_x, 100.0, location=self.warehouse_2.lot_stock_id)

        here = readonly.stock_status(
            self.env, "RT-PART-X", warehouse_code=self.warehouse.code)
        everywhere = readonly.stock_status(self.env, "RT-PART-X")

        self.assertEqual(here["available_qty"], 5.0)
        self.assertEqual(everywhere["available_qty"], 105.0)

    def test_stock_status_with_nothing_on_hand_is_zero_not_an_error(self):
        result = readonly.stock_status(self.env, "RT-PART-Y")

        self.assertIsNone(result["error"])
        self.assertEqual(result["on_hand_qty"], 0.0)
        self.assertEqual(result["locations"], [])

    # ==================================================================
    # lot_trace
    # ==================================================================

    def test_lot_trace_reports_the_lot_and_where_it_sits(self):
        lot = self._lot(self.part_x, "RT-LOT-0001")
        self._stock(self.part_x, 12.0, lot=lot)

        result = readonly.lot_trace(self.env, "RT-LOT-0001")

        self.assertIsNone(result["error"])
        self.assertEqual(result["lot"]["name"], "RT-LOT-0001")
        self.assertEqual(result["lot"]["product_code"], "RT-PART-X")
        self.assertEqual(result["lot"]["on_hand_qty"], 12.0)

    def test_lot_trace_refuses_an_unknown_lot(self):
        result = readonly.lot_trace(self.env, "RT-LOT-NOPE")

        self.assertEqual(result["error"]["code"], "lot_not_found")

    def test_lot_trace_does_not_borrow_a_sibling_lots_quantity(self):
        """Two lots of the same product. Tracing one and reporting the other's
        stock is the failure that matters here: in a recall, the quantity is
        the number somebody acts on."""
        traced = self._lot(self.part_x, "RT-LOT-AAA")
        sibling = self._lot(self.part_x, "RT-LOT-BBB")
        self._stock(self.part_x, 4.0, lot=traced)
        self._stock(self.part_x, 900.0, lot=sibling)

        result = readonly.lot_trace(self.env, "RT-LOT-AAA")

        self.assertEqual(result["lot"]["on_hand_qty"], 4.0)

    def test_lot_trace_refuses_an_ambiguous_label_then_resolves_it(self):
        """The same lot label can exist against two products. Guessing which
        one the caller meant is how an assistant traces the wrong part."""
        second = self.env["product.product"].create({
            "name": "Read Tools Part Z", "default_code": "RT-PART-Z",
            "is_storable": True, "uom_id": self.uom_unit.id, "tracking": "lot",
        })
        self._lot(self.part_x, "RT-SHARED-LABEL")
        self._lot(second, "RT-SHARED-LABEL")

        ambiguous = readonly.lot_trace(self.env, "RT-SHARED-LABEL")
        self.assertEqual(ambiguous["error"]["code"], "ambiguous_lot")

        resolved = readonly.lot_trace(
            self.env, "RT-SHARED-LABEL", product_code="RT-PART-Z")
        self.assertIsNone(resolved["error"])
        self.assertEqual(resolved["lot"]["product_code"], "RT-PART-Z")

    # ==================================================================
    # bom_explode
    # ==================================================================

    def test_bom_explode_scales_the_components_to_the_quantity(self):
        result = readonly.bom_explode(self.env, "RT-ASSY", quantity=10)

        self.assertIsNone(result["error"])
        self.assertEqual(result["component_count"], 2)
        by_code = {c["product_code"]: c for c in result["components"]}
        self.assertEqual(by_code["RT-PART-X"]["required_qty"], 20.0)
        self.assertEqual(by_code["RT-PART-Y"]["required_qty"], 30.0)

    def test_bom_explode_sums_a_component_reached_by_two_paths(self):
        """The same aggregation rule shortage_check applies. The two tools must
        agree: an assistant that asks both and is told two different numbers
        has no way to tell which one to relay.
        """
        self.env["mrp.bom"].create({
            "product_tmpl_id": self.subkit.product_tmpl_id.id,
            "product_qty": 1.0, "type": "phantom", "code": "RT-BOM-SUBKIT",
            "bom_line_ids": [
                (0, 0, {"product_id": self.part_x.id, "product_qty": 4.0})],
        })
        self.bom.write({
            "bom_line_ids": [(0, 0, {"product_id": self.subkit.id, "product_qty": 1.0})]
        })

        result = readonly.bom_explode(self.env, "RT-ASSY", quantity=10)
        by_code = {c["product_code"]: c for c in result["components"]}

        self.assertEqual(by_code["RT-PART-X"]["required_per_unit"], 6.0)
        self.assertEqual(by_code["RT-PART-X"]["required_qty"], 60.0)
        self.assertNotIn("RT-SUBKIT", by_code, "a kit is consumed, not stocked")

    def test_bom_explode_refuses_a_product_with_no_bom(self):
        result = readonly.bom_explode(self.env, "RT-PART-Y")

        self.assertEqual(result["error"]["code"], "no_bom")

    def test_bom_explode_names_an_archived_bom_as_archived(self):
        self.bom.active = False

        result = readonly.bom_explode(self.env, "RT-ASSY", bom_code="RT-BOM-ASSY")

        self.assertEqual(result["error"]["code"], "inactive_bom")

    def test_bom_explode_refuses_a_non_positive_quantity(self):
        for bad in (0, -3, "lots"):
            result = readonly.bom_explode(self.env, "RT-ASSY", quantity=bad)
            self.assertEqual(result["error"]["code"], "invalid_quantity", bad)

    # ==================================================================
    # production_order_status
    # ==================================================================

    def test_production_order_status_reports_state_progress_and_components(self):
        self._stock(self.part_x, 200.0)
        self._stock(self.part_y, 200.0)
        order = self._confirmed_order(4.0)

        result = readonly.production_order_status(self.env, order.name)

        self.assertIsNone(result["error"])
        self.assertEqual(result["order"]["state"], "confirmed")
        self.assertEqual(result["order"]["quantity"], 4.0)
        self.assertEqual(result["order"]["produced_qty"], 0.0)
        codes = {c["product_code"] for c in result["order"]["components"]}
        self.assertEqual(codes, {"RT-PART-X", "RT-PART-Y"})

    def test_production_order_status_refuses_an_unknown_order(self):
        result = readonly.production_order_status(self.env, "RT/MO/NO-SUCH")

        self.assertEqual(result["error"]["code"], "order_not_found")

    # ==================================================================
    # production_backlog
    # ==================================================================

    def test_production_backlog_lists_work_in_hand_not_drafts(self):
        """A draft order holds no stock and commits nobody. Listing it as
        backlog inflates the queue with intentions."""
        self._stock(self.part_x, 200.0)
        self._stock(self.part_y, 200.0)
        confirmed = self._confirmed_order(2.0)
        draft = self.env["mrp.production"].create({
            "product_id": self.assembly.id,
            "bom_id": self.bom.id,
            "product_qty": 5.0,
            "product_uom_id": self.assembly.uom_id.id,
        })

        result = readonly.production_backlog(self.env)
        names = {o["name"] for o in result["orders"]}

        self.assertIn(confirmed.name, names)
        self.assertNotIn(draft.name, names)

    def test_production_backlog_truncates_and_admits_it(self):
        self._stock(self.part_x, 200.0)
        self._stock(self.part_y, 200.0)
        self._confirmed_order(1.0)
        self._confirmed_order(1.0)

        result = readonly.production_backlog(self.env, limit=1)

        self.assertEqual(len(result["orders"]), 1)
        self.assertTrue(result["truncated"])

    def test_production_backlog_refuses_an_unknown_warehouse(self):
        result = readonly.production_backlog(self.env, warehouse_code="NO-SUCH-WH")

        self.assertEqual(result["error"]["code"], "warehouse_not_found")

    # ==================================================================
    # stock_move_history
    # ==================================================================

    def test_stock_move_history_reports_direction_not_just_quantity(self):
        """A quantity with no source and destination explains nothing: it
        cannot distinguish a receipt from a shipment."""
        self._receive(self.part_y, 25.0)

        result = readonly.stock_move_history(self.env, product_code="RT-PART-Y")

        self.assertIsNone(result["error"])
        self.assertGreaterEqual(result["movement_count"], 1)
        movement = result["movements"][0]
        self.assertEqual(movement["quantity"], 25.0)
        # Compared against the records themselves rather than literal names:
        # Odoo's location labels differ by version and locale.
        self.assertEqual(movement["from_location"], self.supplier_location.complete_name)
        self.assertEqual(movement["to_location"], self.stock_location.complete_name)

    def test_stock_move_history_requires_a_filter(self):
        """Returning the whole movement table is a denial of service on the
        assistant's context window, not a helpful default."""
        result = readonly.stock_move_history(self.env)

        self.assertEqual(result["error"]["code"], "invalid_params")

    def test_stock_move_history_does_not_mix_two_products(self):
        self._receive(self.part_y, 5.0)
        self._receive(self.bystander, 900.0)

        result = readonly.stock_move_history(self.env, product_code="RT-PART-Y")

        codes = {m["product_code"] for m in result["movements"]}
        self.assertEqual(codes, {"RT-PART-Y"})

    def test_stock_move_history_filters_by_lot(self):
        lot = self._lot(self.part_x, "RT-LOT-HIST")
        self._receive(self.part_x, 8.0, lot=lot)

        result = readonly.stock_move_history(self.env, lot_name="RT-LOT-HIST")

        self.assertGreaterEqual(result["movement_count"], 1)
        self.assertTrue(all(m["lot"] == "RT-LOT-HIST" for m in result["movements"]))

    # ==================================================================
    # the shared envelope
    # ==================================================================

    def test_every_tool_answers_in_the_same_envelope(self):
        """One shape to parse, successes and refusals alike. A client that has
        to branch on which tool it called will eventually miss a refusal."""
        answers = [
            readonly.product_search(self.env, "RT-"),
            readonly.stock_status(self.env, "RT-PART-X"),
            readonly.stock_status(self.env, "RT-NO-SUCH"),
            readonly.lot_trace(self.env, "RT-LOT-NOPE"),
            readonly.bom_explode(self.env, "RT-ASSY"),
            readonly.production_backlog(self.env),
            readonly.stock_move_history(self.env, product_code="RT-PART-X"),
        ]
        for answer in answers:
            self.assertEqual(answer["schema_version"], "1.0")
            self.assertTrue(answer["as_of"])
            self.assertIn("error", answer)

    def test_every_successful_answer_carries_limitations(self):
        """The blind-spot list is the contract every successful answer keeps.
        An answer that hides what it did not check reads as a promise."""
        self._stock(self.part_x, 10.0)
        answers = [
            readonly.product_search(self.env, "RT-"),
            readonly.stock_status(self.env, "RT-PART-X"),
            readonly.bom_explode(self.env, "RT-ASSY"),
            readonly.production_backlog(self.env),
            readonly.stock_move_history(self.env, product_code="RT-PART-X"),
        ]
        for answer in answers:
            self.assertIsNone(answer["error"], answer)
            self.assertTrue(answer["limitations"], answer)

    def test_a_stock_refusal_still_carries_limitations(self):
        """A refusal is still an answer somebody may act on."""
        refused = readonly.stock_status(self.env, "RT-NO-SUCH")

        self.assertEqual(refused["error"]["code"], "product_not_found")
        self.assertTrue(refused["limitations"])

    def test_every_refusal_carries_limitations_too(self):
        """The gap this used to pin is closed.

        Until the pre-publication correctness pass, these three refusal paths
        returned no `limitations` key at all, and the test here asserted that
        omission so it could not be believed fixed. It is fixed now:
        `envelope()` requires limitations rather than accepting them, so the
        omission cannot recur silently. Full per-path coverage lives in
        tests/test_response_envelope.py; this keeps the three paths that were
        actually broken under the eye of the tool suite that found them.
        """
        for refusal in (
            readonly.lot_trace(self.env, "RT-LOT-NOPE"),
            readonly.bom_explode(self.env, "RT-PART-Y"),
            readonly.stock_move_history(self.env),
        ):
            self.assertIsNotNone(refusal["error"])
            self.assertTrue(refusal["limitations"], refusal)
