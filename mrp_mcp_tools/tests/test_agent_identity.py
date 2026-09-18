# -*- coding: utf-8 -*-
"""Tools exercised as the MCP agent, and two-company isolation.

Promoted from the independent red-team probes. Two things the previous suite
could not see, because it ran everything as the administrator:

  * whether the agent's ACL actually lets the tools work (it did not, for
    lot_trace, which raised AccessError for every real lot)
  * whether the agent's ACL actually stops writes (it does)

The multi-company scenario passed the independent audit. It is here so it keeps
passing: nothing else in the suite protects it.
"""

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged

from ..tools import readonly
from ..tools.shortage import shortage_check


@tagged("post_install", "-at_install")
class TestAgentIdentity(TransactionCase):
    """Every tool, run as the restricted identity the product ships."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.uom = cls.env.ref("uom.product_uom_unit")
        cls.warehouse = cls.env["stock.warehouse"].search([], limit=1)

        cls.finished = cls.env["product.product"].create({
            "name": "Agent FG", "default_code": "AG-FG",
            "is_storable": True, "uom_id": cls.uom.id,
        })
        cls.component = cls.env["product.product"].create({
            "name": "Agent CM", "default_code": "AG-CM",
            "is_storable": True, "uom_id": cls.uom.id, "tracking": "lot",
        })
        cls.env["mrp.bom"].create({
            "product_tmpl_id": cls.finished.product_tmpl_id.id,
            "product_qty": 1.0, "type": "normal", "code": "AG-BOM",
            "bom_line_ids": [(0, 0, {"product_id": cls.component.id,
                                     "product_qty": 2.0})],
        })
        cls.lot = cls.env["stock.lot"].create({
            "name": "AG-LOT", "product_id": cls.component.id,
        })
        cls.env["stock.quant"]._update_available_quantity(
            cls.component, cls.warehouse.lot_stock_id, 50.0, lot_id=cls.lot
        )
        cls.order = cls.env["mrp.production"].create({
            "product_id": cls.finished.id, "product_qty": 3.0,
            "product_uom_id": cls.finished.uom_id.id,
        })
        cls.order.action_confirm()

        cls.agent = cls.env["res.users"].with_context(
            no_reset_password=True).create({
                "name": "Tool Agent", "login": "tool_agent",
                "group_ids": [(6, 0, [
                    cls.env.ref("mrp_mcp_tools.group_mcp_agent").id,
                    cls.env.ref("base.group_user").id])],
            })
        cls.agent_env = cls.env(user=cls.agent)

    # ------------------------------------------------------------------
    # every tool must answer or refuse - never raise
    # ------------------------------------------------------------------

    def test_lot_trace_works_for_the_agent(self):
        """MCP-SEC-004: this raised AccessError for every existing lot,
        because stock.lot.delivery_ids computes over stock.picking."""
        result = readonly.lot_trace(self.agent_env, "AG-LOT")

        self.assertIsNone(result["error"])
        self.assertEqual(result["lot"]["name"], "AG-LOT")
        self.assertEqual(result["lot"]["on_hand_qty"], 50.0)
        self.assertIn("delivery_count", result)

    def test_every_tool_answers_as_the_agent(self):
        calls = {
            "product_search": lambda: readonly.product_search(self.agent_env, "AG-"),
            "stock_status": lambda: readonly.stock_status(self.agent_env, "AG-CM"),
            "bom_explode": lambda: readonly.bom_explode(self.agent_env, "AG-FG", 5),
            "shortage_check": lambda: shortage_check(self.agent_env, "AG-FG", 5),
            "production_order_status": lambda: readonly.production_order_status(
                self.agent_env, self.order.name),
            "production_backlog": lambda: readonly.production_backlog(self.agent_env),
            "stock_move_history": lambda: readonly.stock_move_history(
                self.agent_env, product_code="AG-CM"),
            "lot_trace": lambda: readonly.lot_trace(self.agent_env, "AG-LOT"),
        }
        for name, call in calls.items():
            try:
                result = call()
            except Exception as exc:  # noqa: BLE001
                self.fail("%s raised %s as the agent: %s"
                          % (name, type(exc).__name__, exc))
            self.assertIn("limitations", result, name)
            self.assertIn("schema_version", result, name)

    # ------------------------------------------------------------------
    # the read-only guarantee, from the identity that ships
    # ------------------------------------------------------------------

    def test_agent_cannot_create_business_records(self):
        attempts = {
            "product.product": {"name": "x", "default_code": "AG-X"},
            "mrp.production": {"product_id": self.finished.id, "product_qty": 1},
            "mrp.bom": {"product_tmpl_id": self.finished.product_tmpl_id.id,
                        "product_qty": 1},
            "stock.lot": {"name": "AG-X", "product_id": self.component.id},
        }
        for model, values in attempts.items():
            with self.assertRaises(AccessError, msg="create on %s permitted" % model):
                self.agent_env[model].create(values)

    def test_agent_cannot_write_or_unlink_business_records(self):
        with self.assertRaises(AccessError):
            self.agent_env["product.product"].browse(
                self.component.id).write({"name": "tampered"})
        with self.assertRaises(AccessError):
            self.agent_env["mrp.bom"].search([], limit=1).write({"code": "TAMPERED"})
        with self.assertRaises(AccessError):
            self.agent_env["product.product"].browse(self.component.id).unlink()

    def test_the_new_picking_grant_is_read_only(self):
        """stock.picking was added so lot_trace can resolve deliveries.
        It must be read, and nothing else."""
        self.assertTrue(self.agent_env["stock.picking"].search_count([]) >= 0)

        with self.assertRaises(AccessError):
            self.agent_env["stock.picking"].create({
                "picking_type_id": self.env.ref("stock.picking_type_out").id})

    def test_agent_holds_no_manufacturing_or_inventory_group(self):
        """The grants are per-model and must never be replaced by a group that
        carries write."""
        for group in ("mrp.group_mrp_user", "mrp.group_mrp_manager",
                      "stock.group_stock_user", "stock.group_stock_manager"):
            self.assertFalse(
                self.agent.has_group(group),
                "the agent must not hold %s - it carries write" % group,
            )


@tagged("post_install", "-at_install")
class TestMultiCompanyIsolation(TransactionCase):
    """Two companies, deliberately confusable data, one agent."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.uom = cls.env.ref("uom.product_uom_unit")
        cls.company_a = cls.env.company
        cls.company_b = cls.env["res.company"].create({"name": "Isolation B"})

        cls.wh_a = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.company_a.id)], limit=1)
        cls.wh_b = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.company_b.id)], limit=1)
        if not cls.wh_b:
            cls.wh_b = cls.env["stock.warehouse"].create({
                "name": "Isolation B WH", "code": "IWB",
                "company_id": cls.company_b.id})

        def product(company, code, name):
            return cls.env["product.product"].create({
                "name": name, "default_code": code, "is_storable": True,
                "uom_id": cls.uom.id, "company_id": company.id})

        # Same code on both sides: the case that would confuse a resolver.
        cls.fg_a = product(cls.company_a, "MC-FG", "MultiCo FG (A)")
        cls.fg_b = product(cls.company_b, "MC-FG", "MultiCo FG (B)")
        cls.cm_a = product(cls.company_a, "MC-CM", "MultiCo CM (A)")
        cls.cm_b = product(cls.company_b, "MC-CM", "MultiCo CM (B)")

        for company, finished, component in ((cls.company_a, cls.fg_a, cls.cm_a),
                                             (cls.company_b, cls.fg_b, cls.cm_b)):
            cls.env["mrp.bom"].create({
                "product_tmpl_id": finished.product_tmpl_id.id,
                "product_qty": 1.0, "type": "normal", "code": "MC-BOM",
                "company_id": company.id,
                "bom_line_ids": [(0, 0, {"product_id": component.id,
                                         "product_qty": 2.0})]})

        cls.env["stock.quant"]._update_available_quantity(
            cls.cm_a, cls.wh_a.lot_stock_id, 10.0)
        cls.env["stock.quant"]._update_available_quantity(
            cls.cm_b, cls.wh_b.lot_stock_id, 9999.0)

        # The same lot label in both companies.
        for company, component in ((cls.company_a, cls.cm_a),
                                   (cls.company_b, cls.cm_b)):
            cls.env["stock.lot"].create({
                "name": "MC-LOT", "product_id": component.id,
                "company_id": company.id})

        cls.agent_a = cls.env["res.users"].with_context(
            no_reset_password=True).create({
                "name": "Company A Agent", "login": "mc_agent_a",
                "company_id": cls.company_a.id,
                "company_ids": [(6, 0, [cls.company_a.id])],
                "group_ids": [(6, 0, [
                    cls.env.ref("mrp_mcp_tools.group_mcp_agent").id,
                    cls.env.ref("base.group_user").id])],
            })
        cls.env_a = cls.env(user=cls.agent_a)

    def _leaks(self, blob):
        import json as _json
        text = _json.dumps(blob, default=str)
        return "(B)" in text or "Isolation B" in text or self.wh_b.code in text

    def test_product_search_stays_in_company_a(self):
        result = readonly.product_search(self.env_a, "MC-")

        self.assertFalse(self._leaks(result), result["matches"])
        for match in result["matches"]:
            self.assertIn("(A)", match["product_name"])

    def test_stock_status_does_not_see_company_b_quantities(self):
        result = readonly.stock_status(self.env_a, "MC-CM")

        self.assertEqual(result["on_hand_qty"], 10.0,
                         "company B holds 9999 and must be invisible")

    def test_an_ambiguity_message_does_not_name_a_company_b_record(self):
        """A refusal can leak too: existence is information."""
        result = readonly.stock_status(self.env_a, "MultiCo")
        message = (result.get("error") or {}).get("message", "")

        self.assertNotIn("(B)", message)

    def test_shortage_check_uses_company_a_bom_and_stock(self):
        result = shortage_check(self.env_a, "MC-FG", 10)

        self.assertFalse(self._leaks(result))
        self.assertEqual(result["max_buildable"], 5.0)

    def test_a_company_b_warehouse_code_is_not_usable(self):
        result = readonly.stock_status(
            self.env_a, "MC-CM", warehouse_code=self.wh_b.code)

        self.assertTrue(
            result.get("error") or result.get("on_hand_qty") == 0.0,
            "a warehouse from another company must not resolve",
        )

    def test_backlog_and_history_stay_in_company_a(self):
        for result in (readonly.production_backlog(self.env_a),
                       readonly.stock_move_history(self.env_a, product_code="MC-CM")):
            self.assertFalse(self._leaks(result))
