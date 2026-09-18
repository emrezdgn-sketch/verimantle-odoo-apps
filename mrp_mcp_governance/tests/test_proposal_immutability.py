# -*- coding: utf-8 -*-
"""Security regressions for the red-team P0 findings.

Every reproduction from MCP_INDEPENDENT_RED_TEAM_REPORT.md that demonstrated a
bypass is here as a permanent test. They are written from the attacker's side:
each one performs the exact sequence that worked, and requires it to fail.

Nothing here runs as the administrator. The whole point of the findings was
that administrator-shaped tests cannot see these problems.
"""

import json
from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestProposalImmutability(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.uom = cls.env.ref("uom.product_uom_unit")
        cls.warehouse = cls.env["stock.warehouse"].search([], limit=1)

        cls.finished = cls.env["product.product"].create({
            "name": "Immutability FG", "default_code": "IM-FG",
            "is_storable": True, "uom_id": cls.uom.id,
        })
        cls.component = cls.env["product.product"].create({
            "name": "Immutability CM", "default_code": "IM-CM",
            "is_storable": True, "uom_id": cls.uom.id,
        })
        cls.bom = cls.env["mrp.bom"].create({
            "product_tmpl_id": cls.finished.product_tmpl_id.id,
            "product_qty": 1.0, "type": "normal", "code": "IM-BOM",
            "bom_line_ids": [(0, 0, {"product_id": cls.component.id,
                                     "product_qty": 2.0})],
        })
        cls.env["stock.quant"]._update_available_quantity(
            cls.component, cls.warehouse.lot_stock_id, 500.0
        )

        Users = cls.env["res.users"].with_context(no_reset_password=True)

        def make(login, name, groups):
            return Users.create({
                "name": name, "login": login,
                "group_ids": [(6, 0, [cls.env.ref(g).id for g in groups])],
            })

        cls.agent_a = make("im_agent_a", "MCP Agent A",
                           ["mrp_mcp_tools.group_mcp_agent", "base.group_user"])
        cls.agent_b = make("im_agent_b", "MCP Agent B",
                           ["mrp_mcp_tools.group_mcp_agent", "base.group_user"])
        cls.reviewer = make("im_reviewer", "Human Reviewer",
                            ["mrp_mcp_governance.group_mcp_reviewer",
                             "mrp.group_mrp_user", "base.group_user"])
        cls.reviewer_2 = make("im_reviewer2", "Second Reviewer",
                              ["mrp_mcp_governance.group_mcp_reviewer",
                               "mrp.group_mrp_user", "base.group_user"])
        cls.manager = make("im_manager", "MCP Manager",
                           ["mrp_mcp_governance.group_mcp_manager",
                            "mrp.group_mrp_user", "base.group_user"])
        cls.outsider = make("im_outsider", "Unauthorised Human",
                            ["base.group_user"])

    # ------------------------------------------------------------------

    def _propose(self, on_behalf_of=None, created_by=None, **overrides):
        payload = {"bom_code": "IM-BOM", "product_qty": 5.0,
                   "warehouse_code": self.warehouse.code}
        payload.update(overrides)
        agent = created_by or self.agent_a
        human = on_behalf_of or agent
        return self.env(user=agent)["mcp.proposal"].create({
            "proposal_type": "mrp_production",
            "summary": "Manufacture 5 x IM-FG",
            "payload": json.dumps(payload),
            "requested_uid": human.id,
        })

    # ==================================================================
    # MCP-SEC-001 — provenance rewrite
    # ==================================================================

    def test_reviewer_cannot_rewrite_the_requester(self):
        """Red team step 3: write({'requested_uid': someone_else})."""
        proposal = self._propose(on_behalf_of=self.reviewer)

        with self.assertRaises(AccessError):
            proposal.with_user(self.reviewer).write(
                {"requested_uid": self.manager.id}
            )

        proposal.invalidate_recordset()
        self.assertEqual(proposal.requested_uid, self.reviewer)

    def test_the_full_sod_bypass_sequence_fails(self):
        """The exact four steps that worked before, end to end."""
        proposal = self._propose(on_behalf_of=self.reviewer)

        with self.assertRaises(UserError):
            proposal.with_user(self.reviewer).action_approve()

        with self.assertRaises(AccessError):
            proposal.with_user(self.reviewer).write(
                {"requested_uid": self.manager.id})

        with self.assertRaises(UserError):
            proposal.with_user(self.reviewer).action_approve()

        proposal.invalidate_recordset()
        self.assertEqual(proposal.state, "pending")
        self.assertFalse(proposal.reviewer_uid)

    # ==================================================================
    # MCP-SEC-002 — direct state write
    # ==================================================================

    def test_reviewer_cannot_write_state_directly(self):
        proposal = self._propose()

        for value in ("approved", "applied", "rejected", "expired"):
            with self.assertRaises(AccessError, msg="state=%s was permitted" % value):
                proposal.with_user(self.reviewer).write({"state": value})

        proposal.invalidate_recordset()
        self.assertEqual(proposal.state, "pending")

    def test_manager_cannot_write_state_directly_either(self):
        """The manager group holds create and write; it is still not a way in."""
        proposal = self._propose()

        with self.assertRaises(AccessError):
            proposal.with_user(self.manager).write({"state": "approved"})

    def test_a_context_flag_does_not_open_the_gate(self):
        """The guard is thread-local precisely so a client cannot set it.

        Context travels over RPC. If the bypass were a context key, this would
        pass and the whole control would be decorative.
        """
        proposal = self._propose()

        for key in ("mcp_transition", "transition", "bypass", "install_mode"):
            with self.assertRaises(AccessError):
                proposal.with_user(self.reviewer).with_context(
                    **{key: True}
                ).write({"state": "approved"})

    # ==================================================================
    # MCP-SEC-003 — payload rewrite
    # ==================================================================

    def test_reviewer_cannot_rewrite_the_payload(self):
        proposal = self._propose()
        original = proposal.payload

        with self.assertRaises(AccessError):
            proposal.with_user(self.reviewer).write({
                "payload": json.dumps({"bom_code": "IM-BOM",
                                       "product_qty": 99999.0,
                                       "warehouse_code": self.warehouse.code})
            })

        proposal.invalidate_recordset()
        self.assertEqual(proposal.payload, original)

    def test_what_is_applied_is_what_was_proposed(self):
        """The quantity in the created order comes from the frozen payload."""
        proposal = self._propose(product_qty=5.0)
        reviewed = proposal.with_user(self.reviewer)
        reviewed.action_approve()
        reviewed.action_apply()

        order = self.env["mrp.production"].browse(proposal.applied_res_id)
        self.assertEqual(order.product_qty, 5.0)

    def test_reviewer_cannot_rewrite_the_summary(self):
        """summary is what a human reads; payload is what runs. They must not
        be allowed to drift apart."""
        proposal = self._propose()

        with self.assertRaises(AccessError):
            proposal.with_user(self.reviewer).write({"summary": "something else"})

    # ==================================================================
    # MCP-SEC-009 — expiry extension
    # ==================================================================

    def test_reviewer_cannot_extend_expiry(self):
        proposal = self._propose()
        original = proposal.expiry_date

        with self.assertRaises(AccessError):
            proposal.with_user(self.reviewer).write({
                "expiry_date": fields.Datetime.now() + timedelta(days=365)})

        proposal.invalidate_recordset()
        self.assertEqual(proposal.expiry_date, original)

    # ==================================================================
    # MCP-SEC-006 — audit erasure
    # ==================================================================

    def test_nobody_can_unlink_a_proposal(self):
        proposal = self._propose()

        for who in (self.agent_a, self.reviewer, self.manager):
            with self.assertRaises(AccessError,
                                   msg="%s could unlink" % who.login):
                proposal.with_user(who).unlink()

        self.assertTrue(proposal.exists())

    def test_nobody_can_unlink_a_tool_call(self):
        call = self.env(user=self.agent_a)["mcp.tool.call"].create({
            "tool_name": "probe", "user_id": self.agent_a.id,
        })

        for who in (self.agent_a, self.reviewer, self.manager):
            with self.assertRaises(AccessError):
                call.with_user(who).unlink()
            with self.assertRaises(AccessError):
                call.with_user(who).write({"tool_name": "tampered"})

    # ==================================================================
    # separation of duties, the paths that must keep working
    # ==================================================================

    def test_agent_cannot_approve(self):
        proposal = self._propose(on_behalf_of=self.reviewer)

        with self.assertRaises(UserError):
            proposal.with_user(self.agent_a).action_approve()

    def test_unauthorised_human_cannot_approve(self):
        proposal = self._propose()

        with self.assertRaises(AccessError):
            proposal.with_user(self.outsider).action_approve()

    def test_a_second_reviewer_can_approve_and_apply(self):
        proposal = self._propose(on_behalf_of=self.reviewer)
        second = proposal.with_user(self.reviewer_2)

        second.action_approve()
        second.action_apply()

        self.assertEqual(proposal.state, "applied")
        self.assertEqual(proposal.reviewer_uid, self.reviewer_2)
        self.assertTrue(proposal.applied_res_id)

    # ==================================================================
    # MCP-SEC-005 — apply-time revalidation
    # ==================================================================

    def test_a_bom_archived_after_approval_is_refused_cleanly(self):
        """Before: NotNullViolation, which aborted the transaction."""
        proposal = self._propose()
        reviewed = proposal.with_user(self.reviewer)
        reviewed.action_approve()

        self.bom.active = False

        with self.assertRaises(ValidationError):
            reviewed.action_apply()

        proposal.invalidate_recordset()
        self.assertFalse(proposal.applied_res_id,
                         "nothing may be created when apply is refused")

    def test_apply_revalidates_the_payload(self):
        """Approval is not a licence; conditions are checked again at apply."""
        proposal = self._propose()
        reviewed = proposal.with_user(self.reviewer)
        reviewed.action_approve()

        self.bom.type = "phantom"   # a kit cannot be manufactured on its own

        with self.assertRaises(ValidationError):
            reviewed.action_apply()

    def test_expired_proposal_cannot_be_applied(self):
        proposal = self._propose()
        reviewed = proposal.with_user(self.reviewer)
        reviewed.action_approve()

        self.env.cr.execute(
            "UPDATE mcp_proposal SET expiry_date = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(hours=1), proposal.id),
        )
        proposal.invalidate_recordset()

        with self.assertRaises(UserError):
            reviewed.action_apply()

    def test_double_apply_is_refused(self):
        proposal = self._propose()
        reviewed = proposal.with_user(self.reviewer)
        reviewed.action_approve()
        reviewed.action_apply()
        first = proposal.applied_res_id

        with self.assertRaises(UserError):
            reviewed.action_apply()

        self.assertEqual(proposal.applied_res_id, first)
        self.assertEqual(
            self.env["mrp.production"].search_count(
                [("origin", "=", proposal.name)]), 1,
            "exactly one document per approval",
        )

    # ==================================================================
    # non-finite quantities (MCP-SEC-010) at the proposal boundary
    # ==================================================================

    def test_non_finite_quantities_are_refused_at_creation(self):
        for bad in (float("nan"), float("inf"), float("-inf"), 0, -1, True):
            with self.assertRaises(ValidationError,
                                   msg="quantity %r was accepted" % bad):
                self._propose(product_qty=bad)

    # ==================================================================
    # INV-10 — visibility
    # ==================================================================

    def test_an_agent_sees_only_what_it_created(self):
        mine = self._propose(created_by=self.agent_a)
        theirs = self._propose(created_by=self.agent_b)

        visible = self.env(user=self.agent_a)["mcp.proposal"].search([])

        self.assertIn(mine, visible)
        self.assertNotIn(theirs, visible)
