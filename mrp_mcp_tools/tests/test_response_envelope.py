# -*- coding: utf-8 -*-
"""INV-5: every tool-level response carries the envelope, refusals included.

The contract, as published in README.md and in each tool's own description: a
client parses one shape. It may read

    response["schema_version"], response["as_of"], response["asked"],
    response["limitations"], response["error"]

on any answer a tool returns, without branching on which tool it called and
without a KeyError.

This file exercises *every* return path of all eight tools, not a sample. The
refusal paths are the point: a refusal is still an answer somebody acts on, and
it is the path where an omission survives longest because nothing else about it
looks wrong.

The boundary this file does NOT cross: a JSON-RPC protocol error - unknown
method, unknown tool, malformed JSON, a missing required argument - is not a
tool response. Those follow the MCP error model and are covered by
devtools/verify_endpoint.py. INV-5 is about what a tool returns once it runs.
"""

from odoo.tests import TransactionCase, tagged

from ..tools import readonly
from ..tools.shortage import shortage_check

ENVELOPE_KEYS = ("schema_version", "as_of", "asked", "limitations", "error")


@tagged("post_install", "-at_install")
class TestResponseEnvelope(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.uom_unit = cls.env.ref("uom.product_uom_unit")
        cls.warehouse = cls.env["stock.warehouse"].search([], limit=1)
        cls.stock_location = cls.warehouse.lot_stock_id

        def product(code, name, tracking="none"):
            return cls.env["product.product"].create({
                "name": name, "default_code": code, "is_storable": True,
                "uom_id": cls.uom_unit.id, "tracking": tracking,
            })

        cls.assembly = product("EN-ASSY", "Envelope Assembly")
        cls.part = product("EN-PART", "Envelope Part", tracking="lot")
        cls.twin_a = product("EN-TWIN-A", "Envelope Twin A", tracking="lot")
        cls.twin_b = product("EN-TWIN-B", "Envelope Twin B", tracking="lot")

        cls.bom = cls.env["mrp.bom"].create({
            "product_tmpl_id": cls.assembly.product_tmpl_id.id,
            "product_qty": 1.0, "type": "normal", "code": "EN-BOM",
            "bom_line_ids": [
                (0, 0, {"product_id": cls.part.id, "product_qty": 2.0})],
        })

        # One lot label against two products, so ambiguous_lot is reachable.
        for twin in (cls.twin_a, cls.twin_b):
            cls.env["stock.lot"].create({
                "name": "EN-SHARED-LOT", "product_id": twin.id})

    # ------------------------------------------------------------------
    # helper
    # ------------------------------------------------------------------

    def _assert_envelope(self, response, label):
        """Every key present, and limitations genuinely populated."""
        for key in ENVELOPE_KEYS:
            self.assertIn(key, response, "%s: missing %r" % (label, key))

        self.assertEqual(response["schema_version"], "1.0", label)
        self.assertTrue(response["as_of"], label)
        self.assertIsInstance(response["asked"], dict, label)

        limitations = response["limitations"]
        self.assertIsInstance(limitations, list, label)
        self.assertTrue(limitations, "%s: limitations is empty" % label)
        for entry in limitations:
            self.assertIsInstance(entry, str, label)
            # Filler would satisfy the schema and tell a reader nothing. A
            # real limitation is a sentence.
            self.assertGreater(len(entry.strip()), 20,
                               "%s: %r reads as filler" % (label, entry))

    def _assert_refusal(self, response, code, label):
        self.assertIsNotNone(response["error"], "%s: expected a refusal" % label)
        self.assertEqual(response["error"]["code"], code, label)
        self.assertTrue(response["error"]["message"], label)
        self._assert_envelope(response, label)

    # ==================================================================
    # shortage_check
    # ==================================================================

    def test_shortage_check_every_path(self):
        self._assert_envelope(
            shortage_check(self.env, "EN-ASSY", 1.0), "shortage ok")
        self._assert_refusal(
            shortage_check(self.env, "EN-NO-SUCH", 1.0),
            "product_not_found", "shortage unknown product")
        self._assert_refusal(
            shortage_check(self.env, "EN-PART", 1.0),
            "no_bom", "shortage no bom")
        self._assert_refusal(
            shortage_check(self.env, "EN-ASSY", 0),
            "invalid_quantity", "shortage zero")
        self._assert_refusal(
            shortage_check(self.env, "EN-ASSY", float("nan")),
            "invalid_quantity", "shortage nan")
        self._assert_refusal(
            shortage_check(self.env, "EN-ASSY", 1.0, warehouse_code="EN-NOPE"),
            "warehouse_not_found", "shortage unknown warehouse")

        self.bom.active = False
        self._assert_refusal(
            shortage_check(self.env, "EN-ASSY", 1.0, bom_code="EN-BOM"),
            "inactive_bom", "shortage archived bom")
        self.bom.active = True

    # ==================================================================
    # product_search
    # ==================================================================

    def test_product_search_every_path(self):
        self._assert_envelope(
            readonly.product_search(self.env, "EN-"), "search hit")
        # No match is an answer, not a refusal - but it is still an answer
        # whose blind spots matter: archived products were not searched.
        self._assert_envelope(
            readonly.product_search(self.env, "EN-NOTHING-LIKE-THIS"),
            "search miss")

    # ==================================================================
    # stock_status
    # ==================================================================

    def test_stock_status_every_path(self):
        self._assert_envelope(
            readonly.stock_status(self.env, "EN-PART"), "stock ok")
        self._assert_refusal(
            readonly.stock_status(self.env, "EN-NO-SUCH"),
            "product_not_found", "stock unknown product")
        self._assert_refusal(
            readonly.stock_status(self.env, "EN-PART", warehouse_code="EN-NOPE"),
            "warehouse_not_found", "stock unknown warehouse")

    # ==================================================================
    # lot_trace
    # ==================================================================

    def test_lot_trace_every_path(self):
        lot = self.env["stock.lot"].create({
            "name": "EN-LOT-OK", "product_id": self.part.id})
        self.env["stock.quant"]._update_available_quantity(
            self.part, self.stock_location, 5.0, lot_id=lot)

        self._assert_envelope(
            readonly.lot_trace(self.env, "EN-LOT-OK"), "lot ok")
        self._assert_refusal(
            readonly.lot_trace(self.env, "EN-LOT-NOPE"),
            "lot_not_found", "lot not found")
        self._assert_refusal(
            readonly.lot_trace(self.env, "EN-SHARED-LOT"),
            "ambiguous_lot", "lot ambiguous")
        self._assert_refusal(
            readonly.lot_trace(self.env, "EN-LOT-OK", product_code="EN-NO-SUCH"),
            "product_not_found", "lot unknown product filter")

    # ==================================================================
    # bom_explode
    # ==================================================================

    def test_bom_explode_every_path(self):
        self._assert_envelope(
            readonly.bom_explode(self.env, "EN-ASSY", quantity=2),
            "explode ok")
        self._assert_refusal(
            readonly.bom_explode(self.env, "EN-ASSY", quantity="lots"),
            "invalid_quantity", "explode non-numeric")
        self._assert_refusal(
            readonly.bom_explode(self.env, "EN-ASSY", quantity=float("inf")),
            "invalid_quantity", "explode infinity")
        self._assert_refusal(
            readonly.bom_explode(self.env, "EN-ASSY", quantity=0),
            "invalid_quantity", "explode zero")
        self._assert_refusal(
            readonly.bom_explode(self.env, "EN-NO-SUCH"),
            "product_not_found", "explode unknown product")
        self._assert_refusal(
            readonly.bom_explode(self.env, "EN-PART"),
            "no_bom", "explode no bom")
        self._assert_refusal(
            readonly.bom_explode(self.env, "EN-ASSY", bom_code="EN-NO-SUCH-BOM"),
            "no_bom", "explode unknown bom code")

        self.bom.active = False
        self._assert_refusal(
            readonly.bom_explode(self.env, "EN-ASSY", bom_code="EN-BOM"),
            "inactive_bom", "explode archived bom")
        self.bom.active = True

    # ==================================================================
    # production_order_status
    # ==================================================================

    def test_production_order_status_every_path(self):
        self.env["stock.quant"]._update_available_quantity(
            self.part, self.stock_location, 50.0)
        order = self.env["mrp.production"].create({
            "product_id": self.assembly.id, "bom_id": self.bom.id,
            "product_qty": 2.0, "product_uom_id": self.assembly.uom_id.id})
        order.action_confirm()

        self._assert_envelope(
            readonly.production_order_status(self.env, order.name), "order ok")
        self._assert_refusal(
            readonly.production_order_status(self.env, "EN/MO/NOPE"),
            "order_not_found", "order not found")

    # ==================================================================
    # production_backlog
    # ==================================================================

    def test_production_backlog_every_path(self):
        self._assert_envelope(
            readonly.production_backlog(self.env), "backlog ok")
        self._assert_refusal(
            readonly.production_backlog(self.env, warehouse_code="EN-NOPE"),
            "warehouse_not_found", "backlog unknown warehouse")

    # ==================================================================
    # stock_move_history
    # ==================================================================

    def test_stock_move_history_every_path(self):
        self._assert_envelope(
            readonly.stock_move_history(self.env, product_code="EN-PART"),
            "history ok")
        self._assert_refusal(
            readonly.stock_move_history(self.env),
            "invalid_params", "history no filter")
        self._assert_refusal(
            readonly.stock_move_history(self.env, product_code="EN-NO-SUCH"),
            "product_not_found", "history unknown product")

    # ==================================================================
    # the invariant itself
    # ==================================================================

    def test_no_tool_response_omits_limitations(self):
        """The whole of INV-5 in one assertion, over every path above.

        Kept deliberately separate from the per-tool tests: those say which
        tool broke, this says the invariant broke. When a ninth tool is added,
        this is the test that will not know about it - which is why the
        registry check below exists too.
        """
        responses = [
            shortage_check(self.env, "EN-ASSY", 1.0),
            shortage_check(self.env, "EN-NO-SUCH", 1.0),
            readonly.product_search(self.env, "EN-"),
            readonly.stock_status(self.env, "EN-PART"),
            readonly.stock_status(self.env, "EN-NO-SUCH"),
            readonly.lot_trace(self.env, "EN-LOT-NOPE"),
            readonly.lot_trace(self.env, "EN-SHARED-LOT"),
            readonly.bom_explode(self.env, "EN-ASSY"),
            readonly.bom_explode(self.env, "EN-PART"),
            readonly.bom_explode(self.env, "EN-ASSY", quantity=-1),
            readonly.production_order_status(self.env, "EN/MO/NOPE"),
            readonly.production_backlog(self.env),
            readonly.production_backlog(self.env, warehouse_code="EN-NOPE"),
            readonly.stock_move_history(self.env),
            readonly.stock_move_history(self.env, product_code="EN-PART"),
        ]
        for index, response in enumerate(responses):
            # The access a client is entitled to make, unguarded, on any of
            # them. This is the literal promise, so this is the literal test.
            self.assertTrue(response["limitations"], "response %d" % index)

    def test_every_registered_tool_is_covered_by_this_file(self):
        """A new tool must not silently escape the envelope check.

        Pins the catalogue: adding a ninth tool fails here until someone adds
        its return paths above, which is the only moment anybody will think
        about its refusal envelope.
        """
        from ..tools import registry

        covered = {
            "shortage_check", "product_search", "stock_status", "lot_trace",
            "bom_explode", "production_order_status", "production_backlog",
            "stock_move_history",
        }
        self.assertEqual(
            set(registry.BY_NAME), covered,
            "The tool catalogue changed. Add the new tool's success and "
            "refusal paths to this file before widening this set."
        )
