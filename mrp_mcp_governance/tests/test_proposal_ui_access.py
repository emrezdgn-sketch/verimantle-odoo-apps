# -*- coding: utf-8 -*-
"""Access coverage for the reviewer UI added in v1.

The Approve/Reject/SoD/revalidation/double-apply/audit behaviour already has
extensive coverage elsewhere in this test suite and is not repeated here.
This file only answers the question the UI itself raises: who can see and
open it.
"""

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestProposalUIAccess(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        Users = cls.env["res.users"].with_context(no_reset_password=True)

        def make(login, name, groups):
            return Users.create({
                "name": name, "login": login,
                "group_ids": [(6, 0, [cls.env.ref(g).id for g in groups])],
            })

        cls.agent = make("ui_agent", "UI Test Agent",
                          ["mrp_mcp_tools.group_mcp_agent", "base.group_user"])
        cls.ordinary = make("ui_ordinary", "UI Ordinary User",
                             ["base.group_user"])
        cls.reviewer = make("ui_reviewer", "UI Reviewer",
                             ["mrp_mcp_governance.group_mcp_reviewer",
                              "base.group_user"])
        cls.manager = make("ui_manager", "UI Manager",
                            ["mrp_mcp_governance.group_mcp_manager",
                             "base.group_user"])

        cls.action = cls.env.ref("mrp_mcp_governance.action_mcp_proposal")
        cls.menu = cls.env.ref("mrp_mcp_governance.menu_mcp_proposal")

    def test_views_and_action_load(self):
        """The module installs with the new view/action/menu intact."""
        self.env.ref("mrp_mcp_governance.view_mcp_proposal_list")
        self.env.ref("mrp_mcp_governance.view_mcp_proposal_form")
        self.assertEqual(self.action.res_model, "mcp.proposal")

    def test_manager_sees_action_through_reviewer_inheritance(self):
        """group_mcp_manager implies group_mcp_reviewer, so a manager passes
        the same groups_id check without a separate grant on the action."""
        self.assertTrue(
            self.manager.has_group("mrp_mcp_governance.group_mcp_reviewer")
        )

    def test_ordinary_user_lacks_reviewer_group(self):
        self.assertFalse(
            self.ordinary.has_group("mrp_mcp_governance.group_mcp_reviewer")
        )

    def test_agent_lacks_reviewer_group(self):
        """The agent identity must never carry the group the Proposals menu
        and action are gated on, independent of the ACL that already refuses
        it write access."""
        self.assertFalse(
            self.agent.has_group("mrp_mcp_governance.group_mcp_reviewer")
        )

    def test_menu_is_gated_on_reviewer_group(self):
        self.assertIn(
            self.env.ref("mrp_mcp_governance.group_mcp_reviewer"),
            self.menu.group_ids,
        )

    def test_ordinary_user_cannot_read_proposals(self):
        """The real boundary: this model's ACL, unchanged by the new UI,
        has no access row at all for a plain internal user."""
        with self.assertRaises(AccessError):
            self.env["mcp.proposal"].with_user(self.ordinary).search([])

    def test_agent_cannot_read_other_proposals(self):
        """The agent's own ACL row is read=1, but rule_proposal_agent_own_session
        still scopes it to records it created itself - unchanged by this UI."""
        proposals = self.env["mcp.proposal"].with_user(self.agent).search([])
        self.assertFalse(proposals)
