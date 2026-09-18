# -*- coding: utf-8 -*-
"""Unit tests for mcp.proposal.

REPLACES_PROVENANCE_RISK: mrp_mcp_governance/tests/test_mcp_proposal.py (v0.1.0)

Clean-room specification. Every rule exercised here is taken from this
module's own published guarantees and from public Odoo behaviour:

  * the governance guarantees in README.md and RELEASE_NOTES_v0.1.0.md -
    validation on arrival, separation of duties, expiry, apply-time
    revalidation, no sudo in the apply path, an append-only call log
  * the model itself: models/mcp_proposal.py and models/mcp_tool_call.py
  * the groups and rules in security/mcp_security.xml and ir.model.access.csv
  * Odoo's own access control, which is what refuses the writes asserted below

Fixture codes, user logins and payload values were chosen here. Nothing in
this file depends on the scenario catalogue, naming or values of any earlier
suite.
"""

import json
from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMcpProposal(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.warehouse = cls.env["stock.warehouse"].search([], limit=1)
        cls.uom_unit = cls.env.ref("uom.product_uom_unit")

        cls.assembly = cls.env["product.product"].create({
            "name": "Governance Test Assembly",
            "default_code": "GV-ASSY",
            "is_storable": True,
            "uom_id": cls.uom_unit.id,
        })
        cls.part = cls.env["product.product"].create({
            "name": "Governance Test Part",
            "default_code": "GV-PART",
            "is_storable": True,
            "uom_id": cls.uom_unit.id,
        })

        cls.bom = cls.env["mrp.bom"].create({
            "product_tmpl_id": cls.assembly.product_tmpl_id.id,
            "product_qty": 1.0,
            "type": "normal",
            "code": "GV-BOM-ASSY",
            "bom_line_ids": [
                (0, 0, {"product_id": cls.part.id, "product_qty": 4.0}),
            ],
        })

        Users = cls.env["res.users"].with_context(no_reset_password=True)

        # The technical identity the MCP layer connects as. Deliberately in no
        # manufacturing or inventory group: those carry write.
        cls.agent_user = Users.create({
            "name": "Governance Test Agent",
            "login": "gv_agent_primary",
            "group_ids": [(6, 0, [
                cls.env.ref("mrp_mcp_tools.group_mcp_agent").id,
                cls.env.ref("base.group_user").id,
            ])],
        })

        # A second agent identity, so the record rule has something to hide.
        cls.other_agent_user = Users.create({
            "name": "Governance Test Agent (second deployment)",
            "login": "gv_agent_secondary",
            "group_ids": [(6, 0, [
                cls.env.ref("mrp_mcp_tools.group_mcp_agent").id,
                cls.env.ref("base.group_user").id,
            ])],
        })

        # A reviewer who may also create the document being proposed.
        cls.reviewer_user = Users.create({
            "name": "Governance Test Planner",
            "login": "gv_reviewer_full",
            "group_ids": [(6, 0, [
                cls.env.ref("mrp_mcp_governance.group_mcp_reviewer").id,
                cls.env.ref("mrp.group_mrp_user").id,
                cls.env.ref("base.group_user").id,
            ])],
        })

        # A reviewer on paper only, without the right to create the document.
        cls.powerless_reviewer = Users.create({
            "name": "Governance Test Reviewer Without Rights",
            "login": "gv_reviewer_powerless",
            "group_ids": [(6, 0, [
                cls.env.ref("mrp_mcp_governance.group_mcp_reviewer").id,
                cls.env.ref("base.group_user").id,
            ])],
        })

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _propose(self, on_behalf_of=None, created_by=None, **payload_overrides):
        """Create a proposal the way production does.

        The agent identity is always the creator; `on_behalf_of` names the
        person in whose session it acted. Creating the record as a reviewer
        cannot happen for real - the reviewer group holds no create right on
        mcp.proposal, by design - so the helper does not offer it.
        """
        payload = {
            "bom_code": "GV-BOM-ASSY",
            "product_qty": 7.0,
            "warehouse_code": self.warehouse.code,
        }
        payload.update(payload_overrides)
        agent = created_by or self.agent_user
        human = on_behalf_of or agent
        env = self.env(user=agent)
        return env["mcp.proposal"].create({
            "proposal_type": "mrp_production",
            "summary": "Manufacture 7 x GV-ASSY using GV-BOM-ASSY",
            "payload": json.dumps(payload),
            "requested_uid": human.id,
            "agent_session": "gv-session-0001",
            "agent_label": "Test Assistant",
        })

    # ------------------------------------------------------------------
    # creation and validation on arrival
    # ------------------------------------------------------------------

    def test_a_new_proposal_waits_for_a_person(self):
        """Nothing is applied on creation. The queue is the product."""
        proposal = self._propose()

        self.assertEqual(proposal.state, "pending")
        self.assertTrue(proposal.name.startswith("MCP-"))
        self.assertEqual(proposal.requested_uid, self.agent_user)
        self.assertFalse(proposal.is_expired)
        self.assertFalse(proposal.applied_res_id)

    def test_a_quantity_that_is_not_a_quantity_is_refused_on_arrival(self):
        """Refused when proposed, not when applied: a reviewer must never be
        shown something that would fail the moment they approved it."""
        for bad in (0, -1, "many", None):
            with self.assertRaises(ValidationError):
                self._propose(product_qty=bad)

    def test_an_unknown_bom_is_refused_on_arrival(self):
        with self.assertRaises(ValidationError):
            self._propose(bom_code="GV-BOM-NO-SUCH")

    def test_an_archived_bom_is_refused_on_arrival(self):
        """Archived is Odoo's way of saying "not released for use". A proposal
        against one would reach a reviewer looking perfectly ordinary."""
        self.bom.active = False

        with self.assertRaises(ValidationError):
            self._propose()

    def test_a_kit_bom_is_refused(self):
        """A phantom bill is expanded into its parent's order. It is never
        manufactured on its own, so an order against one is meaningless."""
        self.bom.type = "phantom"

        with self.assertRaises(ValidationError):
            self._propose()

    def test_an_unknown_warehouse_is_refused_on_arrival(self):
        with self.assertRaises(ValidationError):
            self._propose(warehouse_code="GV-NO-SUCH-WH")

    def test_the_payload_must_be_a_json_object(self):
        """Not a list, not a bare string, not empty: the apply path indexes it
        by key, and a list would fail there instead of here."""
        for bad in ("not json at all", "[1, 2, 3]", ""):
            with self.assertRaises(ValidationError):
                self.env["mcp.proposal"].create({
                    "proposal_type": "mrp_production",
                    "summary": "malformed payload",
                    "payload": bad,
                })

    # ------------------------------------------------------------------
    # separation of duties
    # ------------------------------------------------------------------

    def test_the_requester_cannot_approve_their_own_proposal(self):
        """Without this the approval queue is decoration: an agent acting in
        someone's session would request and approve in one breath, and no
        second person would ever appear in the loop."""
        proposal = self._propose(on_behalf_of=self.reviewer_user)

        with self.assertRaises(UserError):
            proposal.with_user(self.reviewer_user).action_approve()

        self.assertEqual(proposal.state, "pending")

    def test_the_agent_identity_can_never_approve(self):
        """Even against a proposal requested for somebody else."""
        proposal = self._propose(on_behalf_of=self.reviewer_user)

        with self.assertRaises(UserError):
            proposal.with_user(self.agent_user).action_approve()

    def test_a_second_person_can_approve(self):
        """The rule must refuse the requester without refusing everybody."""
        proposal = self._propose()

        proposal.with_user(self.reviewer_user).action_approve()

        self.assertEqual(proposal.state, "approved")
        self.assertEqual(proposal.reviewer_uid, self.reviewer_user)
        self.assertTrue(proposal.review_date)

    def test_the_agent_cannot_write_to_a_proposal_it_created(self):
        """The agent group holds create and read on mcp.proposal, and nothing
        else. Creating a record does not confer ownership of it."""
        proposal = self._propose()

        with self.assertRaises(AccessError):
            proposal.with_user(self.agent_user).write({"summary": "tampered"})

    def test_an_agent_sees_only_its_own_proposals(self):
        """Record-rule isolation between two deployments sharing a database."""
        mine = self._propose()
        theirs = self._propose(created_by=self.other_agent_user)

        visible = self.env["mcp.proposal"].with_user(self.agent_user).search([])

        self.assertIn(mine, visible)
        self.assertNotIn(theirs, visible)

    # ------------------------------------------------------------------
    # expiry
    # ------------------------------------------------------------------

    def test_an_expired_proposal_cannot_be_approved(self):
        """Stock moves. A buildability answer from hours ago is not a fact any
        more, and approving one silently would be worse than refusing it."""
        proposal = self._propose()
        # expiry_date is frozen by the model, deliberately - a reviewer able to
        # extend it would defeat the staleness control. Ageing a record for a
        # test therefore goes round the ORM rather than through it.
        self.env.cr.execute(
            "UPDATE mcp_proposal SET expiry_date = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(minutes=1), proposal.id))
        proposal.invalidate_recordset()

        with self.assertRaises(UserError):
            proposal.with_user(self.reviewer_user).action_approve()

        # Still pending on purpose: action_approve refuses without writing. A
        # state written just before raising would be rolled back with the
        # transaction, so marking expiry is left to the cron below.
        proposal.invalidate_recordset()
        self.assertEqual(proposal.state, "pending")

    def test_the_cron_marks_stale_proposals_expired(self):
        proposal = self._propose()
        self.env.cr.execute(
            "UPDATE mcp_proposal SET expiry_date = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(hours=1), proposal.id))
        proposal.invalidate_recordset()

        self.env["mcp.proposal"]._cron_expire_stale()

        self.assertEqual(proposal.state, "expired")

    # ------------------------------------------------------------------
    # apply
    # ------------------------------------------------------------------

    def test_applying_creates_the_manufacturing_order_from_the_payload(self):
        proposal = self._propose()
        reviewed = proposal.with_user(self.reviewer_user)
        reviewed.action_approve()

        reviewed.action_apply()

        self.assertEqual(proposal.state, "applied")
        self.assertEqual(proposal.applied_model, "mrp.production")
        self.assertTrue(proposal.applied_res_id)

        order = self.env["mrp.production"].browse(proposal.applied_res_id)
        self.assertEqual(order.product_id, self.assembly)
        self.assertEqual(order.product_qty, 7.0)
        self.assertEqual(order.product_uom_id, self.assembly.uom_id)
        self.assertEqual(order.state, "draft",
                         "confirming reserves stock and is a separate decision")

    def test_a_pending_proposal_cannot_be_applied(self):
        """Approval is the gate. Skipping it would make the state machine
        advisory."""
        proposal = self._propose()

        with self.assertRaises(UserError):
            proposal.with_user(self.reviewer_user).action_apply()

    def test_applying_runs_as_the_reviewer_and_not_as_root(self):
        """The claim "the apply path contains no sudo()" is proved the only way
        that survives a refactor: by having somebody without the right try it
        and watching Odoo refuse. A reviewer who cannot create a manufacturing
        order by hand must not create one by approving.
        """
        proposal = self._propose()
        weak = proposal.with_user(self.powerless_reviewer)
        weak.action_approve()

        with self.assertRaises(AccessError):
            weak.action_apply()

        # The proposal stays approved rather than being marked failed: a write
        # recording the failure would be rolled back with the transaction that
        # raised, so the model does not attempt one.
        proposal.invalidate_recordset()
        self.assertEqual(proposal.state, "approved")
        self.assertFalse(proposal.applied_res_id)

    def test_a_rejected_proposal_stays_rejected(self):
        """Rejection is final and keeps its reason. Re-approving would erase
        the one record of a human decision."""
        proposal = self._propose()
        reviewed = proposal.with_user(self.reviewer_user)

        reviewed.action_reject(reason="Not needed this week")

        self.assertEqual(proposal.state, "rejected")
        self.assertEqual(proposal.rejection_reason, "Not needed this week")
        with self.assertRaises(UserError):
            reviewed.action_approve()

    # ------------------------------------------------------------------
    # the call log
    # ------------------------------------------------------------------

    def test_the_tool_call_log_is_append_only_for_everyone(self):
        """An audit row nobody can edit or delete - not the agent that wrote
        it, not the reviewer who reads it. Append-only is the whole claim."""
        call = self.env["mcp.tool.call"].with_user(self.agent_user).create({
            "tool_name": "shortage_check",
            "params": json.dumps({"product_code": "GV-ASSY", "quantity": 7}),
            "result_summary": "partial",
            "record_count": 2,
            "duration_ms": 41,
            "user_id": self.agent_user.id,
        })

        self.assertTrue(call.id)
        with self.assertRaises(AccessError):
            call.with_user(self.agent_user).write({"result_summary": "can_build"})
        with self.assertRaises(AccessError):
            call.with_user(self.reviewer_user).write({"result_summary": "can_build"})
        with self.assertRaises(AccessError):
            call.with_user(self.agent_user).unlink()
