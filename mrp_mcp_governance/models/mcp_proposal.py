# -*- coding: utf-8 -*-
"""mcp.proposal - what an AI agent is allowed to produce instead of a write.

The contract this model exists to enforce:

  * The MCP layer never writes to a business model. Its most privileged
    action is creating one record here.
  * Turning a proposal into a real document is an ordinary Odoo action,
    performed by a person, under that person's own access rights. There is
    deliberately no sudo() in the apply path - if the reviewer may not create
    a manufacturing order by hand, approving a proposal must not create one
    either.
  * A proposal is validated when it is created, so a reviewer is never shown
    something that would fail on apply.
  * A proposal expires. Stock moves; a two-hour-old "this is buildable" is
    not a fact any more.
"""

import contextlib
import json
import math
import threading
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

# How long a proposal stays actionable unless the type overrides it.
DEFAULT_TTL_HOURS = 4

# Fields that carry the meaning of the proposal: who asked, for what, until
# when. Once created they never change, for anybody. A reviewer needs write
# rights on this model so the transition methods can record their decision,
# and that right is what an attacker uses to rewrite provenance - so the model
# itself has to say no.
FROZEN_FIELDS = frozenset({
    "name", "proposal_type", "summary", "payload", "company_id",
    "agent_session", "agent_label", "requested_uid", "tool_call_id",
    "expiry_date",
})

# Fields the workflow writes. Legal to change, but only from inside one of the
# action methods below - never by a direct write() from a client.
TRANSITION_FIELDS = frozenset({
    "state", "reviewer_uid", "review_date", "rejection_reason",
    "applied_model", "applied_res_id", "applied_ref", "failure_reason",
})

# The gate is a thread-local flag rather than a context key on purpose.
# `self.env.context` is supplied by the caller and travels over RPC, so any
# client could set a bypass flag and walk straight through. A thread-local is
# set only by code running inside this module.
_transition_guard = threading.local()


@contextlib.contextmanager
def _transition():
    """Mark the current thread as executing an authorised state transition."""
    previous = getattr(_transition_guard, "active", False)
    _transition_guard.active = True
    try:
        yield
    finally:
        _transition_guard.active = previous


def _in_transition():
    return getattr(_transition_guard, "active", False)


class McpProposal(models.Model):
    _name = "mcp.proposal"
    _description = "AI Agent Proposal Awaiting Human Approval"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc, id desc"
    _check_company_auto = True

    name = fields.Char(
        string="Reference", required=True, copy=False, readonly=True, default="New"
    )

    proposal_type = fields.Selection(
        selection=[
            ("mrp_production", "Manufacturing Order"),
            ("sale_order", "Sales Order"),
            ("purchase_order", "Purchase Order"),
            ("inventory_adjustment", "Inventory Adjustment"),
            ("product_revision", "Product Revision"),
        ],
        required=True,
        readonly=True,
        tracking=True,
    )

    state = fields.Selection(
        selection=[
            ("pending", "Awaiting Review"),
            ("approved", "Approved"),
            ("applied", "Applied"),
            ("rejected", "Rejected"),
            ("expired", "Expired"),
            ("failed", "Failed"),
        ],
        default="pending",
        required=True,
        tracking=True,
        index=True,
    )

    summary = fields.Text(
        string="What is being proposed",
        required=True,
        readonly=True,
        help="Written by the tool that created the proposal, from validated "
             "data - never free text from the language model. A reviewer must "
             "be able to decide from this line alone.",
    )
    payload = fields.Text(
        string="Parameters (JSON)",
        required=True,
        readonly=True,
        help="Structured parameters, already validated against the target "
             "model. This is what gets applied, not the summary.",
    )

    # --- provenance -------------------------------------------------------
    agent_session = fields.Char(
        string="Agent Session",
        readonly=True,
        index=True,
        help="Identifier of the MCP client session that produced this. Lets "
             "an auditor pull every action from one conversation.",
    )
    agent_label = fields.Char(
        string="Agent",
        readonly=True,
        help="Which assistant asked, as reported by the MCP client.",
    )
    requested_uid = fields.Many2one(
        "res.users",
        string="Requested On Behalf Of",
        required=True,
        readonly=True,
        help="The person whose session the agent was acting in. An agent is "
             "never an actor in its own right.",
    )
    tool_call_id = fields.Many2one(
        "mcp.tool.call", string="Originating Tool Call", readonly=True, ondelete="set null"
    )

    # --- review -----------------------------------------------------------
    reviewer_uid = fields.Many2one(
        "res.users", string="Reviewed By", readonly=True, tracking=True
    )
    review_date = fields.Datetime(string="Reviewed On", readonly=True)
    rejection_reason = fields.Text(string="Reason For Rejection", readonly=True)

    expiry_date = fields.Datetime(
        string="Valid Until",
        required=True,
        readonly=True,
        index=True,
        help="After this moment the proposal can no longer be applied, "
             "because the stock situation it was based on has aged out.",
    )
    is_expired = fields.Boolean(compute="_compute_is_expired")

    # --- result -----------------------------------------------------------
    applied_model = fields.Char(string="Created Record Model", readonly=True)
    applied_res_id = fields.Integer(string="Created Record ID", readonly=True)
    applied_ref = fields.Char(
        string="Created Document", readonly=True,
        help="Human-readable reference of whatever this became."
    )
    failure_reason = fields.Text(readonly=True)

    company_id = fields.Many2one(
        "res.company", required=True, readonly=True,
        default=lambda self: self.env.company,
    )

    # ------------------------------------------------------------------
    # creation
    # ------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "mcp.proposal"
                ) or "New"
            vals.setdefault("requested_uid", self.env.uid)
            if not vals.get("expiry_date"):
                vals["expiry_date"] = fields.Datetime.now() + timedelta(
                    hours=DEFAULT_TTL_HOURS
                )
        records = super().create(vals_list)
        records._validate_payload()
        return records

    def write(self, vals):
        """A proposal is immutable except through its own transition methods.

        Field-level readonly=True is a web-client attribute; the ORM ignores it,
        and so does XML-RPC. Without this override a reviewer - who must hold
        write rights to record a decision - can rewrite requested_uid and
        approve their own request, or rewrite payload so the approved document
        is not the proposed one.
        """
        if not _in_transition():
            frozen = FROZEN_FIELDS.intersection(vals)
            if frozen:
                raise AccessError(_(
                    "A proposal cannot be changed after it is created. "
                    "Refused fields: %s. Raise a new proposal instead.",
                    ", ".join(sorted(frozen)),
                ))
            workflow = TRANSITION_FIELDS.intersection(vals)
            if workflow:
                raise AccessError(_(
                    "Fields %s change only through Approve, Reject or Apply, "
                    "so that the separation-of-duties and expiry checks always "
                    "run. Direct writes are refused.",
                    ", ".join(sorted(workflow)),
                ))
        return super().write(vals)

    def unlink(self):
        """Proposals are the record of what an agent asked for.

        ir.model.access no longer grants unlink to any group; this override
        keeps that true for code paths that acquire the right some other way,
        and gives a readable reason instead of an access error.
        """
        raise AccessError(_(
            "Proposals are an audit record and cannot be deleted. Reject the "
            "proposal if it should not proceed; it stays visible as rejected."
        ))

    @api.constrains("payload")
    def _check_payload_is_json(self):
        for proposal in self:
            try:
                parsed = json.loads(proposal.payload or "")
            except ValueError as exc:
                raise ValidationError(
                    _("Parameters are not valid JSON: %s", exc)
                ) from exc
            if not isinstance(parsed, dict):
                raise ValidationError(_("Parameters must be a JSON object."))

    def _compute_is_expired(self):
        now = fields.Datetime.now()
        for proposal in self:
            proposal.is_expired = bool(
                proposal.expiry_date and proposal.expiry_date <= now
            )

    def get_payload(self):
        self.ensure_one()
        return json.loads(self.payload or "{}")

    def _lock_for_transition(self):
        """Take an exclusive row lock so a proposal executes at most once.

        Odoo gives every request its own transaction, so two Apply calls
        overlap happily: both read state 'approved', both run the applier, and
        two manufacturing orders appear for one approval. The lock serialises
        them - the second waits, then reads 'applied' and is refused by the
        state check above.

        NOWAIT rather than a plain FOR UPDATE: a caller that cannot have the
        row right now is told immediately instead of holding a worker while
        someone else's apply runs.
        """
        self.ensure_one()
        self.flush_recordset()
        name = self.name
        # The savepoint matters as much as the lock. A failed NOWAIT leaves the
        # transaction in an aborted state, and every statement after it - the
        # translation lookup inside UserError included - fails with
        # InFailedSqlTransaction. Rolling back to the savepoint restores a
        # usable transaction so the caller gets a domain error rather than a
        # database one.
        try:
            with self.env.cr.savepoint(flush=False):
                self.env.cr.execute(
                    "SELECT id FROM mcp_proposal WHERE id = %s FOR UPDATE NOWAIT",
                    (self.id,),
                )
        except Exception as exc:  # psycopg2.errors.LockNotAvailable
            raise UserError(_(
                "%s is being acted on by someone else right now. Nothing was "
                "changed; try again in a moment.", name,
            )) from exc

    # ------------------------------------------------------------------
    # validation and apply dispatch
    # ------------------------------------------------------------------
    #
    # Each proposal type supplies two hooks, resolved by name so that other
    # modules can add types without touching this file:
    #
    #   _validate_<type>(payload)  -> raises ValidationError, or returns None
    #   _apply_<type>(payload)     -> returns the created recordset
    #
    # Adding a type without both hooks is a programming error and is reported
    # as one rather than silently doing nothing.

    def _validate_payload(self):
        for proposal in self:
            handler = getattr(
                proposal, "_validate_%s" % proposal.proposal_type, None
            )
            if handler is None:
                raise ValidationError(
                    _("No validator is registered for proposal type '%s'.",
                      proposal.proposal_type)
                )
            handler(proposal.get_payload())

    def _check_reviewer_is_separate(self):
        """Separation of duties: whoever asked cannot be whoever approves.

        Without this the approval queue is decoration - an agent running in a
        user's session would request and approve in the same breath, and the
        human in "human in the loop" would never appear.
        """
        for proposal in self:
            if proposal.requested_uid.id == self.env.uid:
                raise UserError(
                    _("%s was requested on your behalf, so you cannot also "
                      "approve it. Someone else has to review it.",
                      proposal.name)
                )
            if self.env.user.has_group("mrp_mcp_tools.group_mcp_agent") \
                    and not self.env.user.has_group(
                        "mrp_mcp_governance.group_mcp_reviewer"):
                raise UserError(
                    _("The agent identity may create proposals but never "
                      "review them.")
                )

    def action_approve(self):
        self._check_reviewer_is_separate()
        for proposal in self:
            if proposal.state != "pending":
                raise UserError(
                    _("Only a proposal awaiting review can be approved; %s is %s.",
                      proposal.name, proposal.state)
                )
            # Refuse, but do not try to record the expiry here. Writing a
            # state and then raising in the same transaction loses the write:
            # the exception rolls the transaction back and takes the state
            # with it. Marking expiry is _cron_expire_stale's job, and it runs
            # in a transaction that commits.
            if proposal.is_expired:
                raise UserError(
                    _("%s expired on %s and has to be requested again.",
                      proposal.name, proposal.expiry_date)
                )
            with _transition():
                proposal.write({
                    "state": "approved",
                    "reviewer_uid": self.env.uid,
                    "review_date": fields.Datetime.now(),
                })
        return True

    def action_approve_and_apply(self):
        """One-click path for the common case. Kept separate from
        action_approve so that approving and applying stay two auditable
        events rather than one."""
        self.action_approve()
        return self.action_apply()

    def action_reject(self, reason=None):
        for proposal in self:
            if proposal.state not in ("pending", "approved"):
                raise UserError(
                    _("%s can no longer be rejected.", proposal.name)
                )
            with _transition():
                proposal.write({
                    "state": "rejected",
                    "reviewer_uid": self.env.uid,
                    "review_date": fields.Datetime.now(),
                    "rejection_reason": reason or proposal.rejection_reason,
                })
        return True

    def action_apply(self):
        """Turn an approved proposal into a real document.

        Runs as the current user on purpose. Odoo's own access rules decide
        whether this succeeds, which is what keeps the agent from becoming a
        privilege-escalation path.
        """
        for proposal in self:
            # Take the row before reading its state. Two callers applying the
            # same proposal at once would otherwise both read "approved" and
            # both create a document; the second waits here and then sees
            # "applied". NOWAIT is deliberate: a caller that cannot have the
            # row is told so rather than blocking a worker.
            proposal._lock_for_transition()

            if proposal.state != "approved":
                raise UserError(
                    _("%s must be approved before it can be applied.", proposal.name)
                )
            if proposal.is_expired:
                raise UserError(
                    _("%s expired before it was applied.", proposal.name)
                )

            # Revalidate. Proposal-time validation said the conditions held
            # when it was raised; nothing has said they still hold. A bill of
            # materials archived since approval used to reach the ORM with an
            # empty product and fail as a database constraint violation.
            proposal._validate_payload()

            handler = getattr(proposal, "_apply_%s" % proposal.proposal_type, None)
            if handler is None:
                raise UserError(
                    _("No applier is registered for proposal type '%s'.",
                      proposal.proposal_type)
                )

            # Deliberately no try/except that records the failure here. A
            # write made in a transaction that then raises is rolled back with
            # it, so a "failed" row written this way would vanish exactly when
            # it mattered. The proposal stays "approved" and the failure is
            # what the caller sees; recording it durably needs a separate
            # transaction, which belongs to the tool-call log rather than to
            # the record being applied.
            record = handler(proposal.get_payload())

            with _transition():
                proposal.write({
                    "state": "applied",
                    "applied_model": record._name,
                    "applied_res_id": record.id,
                    "applied_ref": record.display_name,
                })
            proposal.message_post(
                body=_("Applied as %s", record.display_name)
            )
        return True

    def action_open_applied(self):
        self.ensure_one()
        if not self.applied_model or not self.applied_res_id:
            raise UserError(_("%s has not been applied yet.", self.name))
        return {
            "type": "ir.actions.act_window",
            "res_model": self.applied_model,
            "res_id": self.applied_res_id,
            "view_mode": "form",
        }

    # ------------------------------------------------------------------
    # manufacturing order
    # ------------------------------------------------------------------

    def _validate_mrp_production(self, payload):
        """Reject anything that would fail on apply, while a human can still
        be told why."""
        bom_code = payload.get("bom_code")
        if not bom_code:
            raise ValidationError(_("A bill of materials code is required."))

        # active_test=False so an archived bill is found and named as archived
        # rather than reported as missing - and so that a bill archived between
        # approval and apply produces a domain refusal instead of reaching the
        # ORM with an empty recordset and failing as a NOT NULL violation.
        bom = self.env["mrp.bom"].with_context(active_test=False).search(
            [("code", "=", bom_code)], limit=1
        )
        if not bom:
            raise ValidationError(_("No bill of materials '%s'.", bom_code))
        if not bom.active:
            raise ValidationError(
                _("Bill of materials '%s' exists but is archived, so it cannot "
                  "be used for new production.", bom_code)
            )
        if bom.type == "phantom":
            raise ValidationError(
                _("'%s' is a kit, which is expanded into another order rather "
                  "than manufactured on its own.", bom_code)
            )

        qty = payload.get("product_qty")
        if isinstance(qty, bool) or not isinstance(qty, (int, float)):
            raise ValidationError(_("Quantity must be a number greater than zero."))
        if not math.isfinite(qty) or qty <= 0:
            raise ValidationError(_(
                "Quantity must be a finite number greater than zero."))

        warehouse_code = payload.get("warehouse_code")
        if warehouse_code and not self.env["stock.warehouse"].search(
            [("code", "=", warehouse_code)], limit=1
        ):
            raise ValidationError(_("No warehouse with code '%s'.", warehouse_code))

    def _apply_mrp_production(self, payload):
        bom = self.env["mrp.bom"].search(
            [("code", "=", payload["bom_code"])], limit=1
        )
        if not bom:
            raise UserError(_(
                "Bill of materials '%s' is no longer available for production. "
                "The proposal was valid when raised; conditions have changed.",
                payload["bom_code"],
            ))
        product = bom.product_id or bom.product_tmpl_id.product_variant_id

        # product_qty is stated in the product's own unit of measure, which is
        # how a person reads it and how shortage_check reported it. Passing the
        # bill's unit here instead would silently rescale the order whenever the
        # two differ.
        values = {
            "product_id": product.id,
            "bom_id": bom.id,
            "product_qty": payload["product_qty"],
            "product_uom_id": product.uom_id.id,
            "origin": payload.get("origin") or self.name,
        }
        if payload.get("date_start"):
            values["date_start"] = payload["date_start"]
        if payload.get("warehouse_code"):
            warehouse = self.env["stock.warehouse"].search(
                [("code", "=", payload["warehouse_code"])], limit=1
            )
            values["picking_type_id"] = warehouse.manu_type_id.id
            values["location_src_id"] = warehouse.lot_stock_id.id

        # Left in draft on purpose: confirming reserves stock, and that is a
        # second decision the reviewer should make in the manufacturing app
        # with the availability in front of them.
        return self.env["mrp.production"].create(values)

    # ------------------------------------------------------------------
    # housekeeping
    # ------------------------------------------------------------------

    @api.model
    def _cron_expire_stale(self):
        """Mark proposals nobody reviewed in time. Scheduled hourly."""
        stale = self.search([
            ("state", "in", ("pending", "approved")),
            ("expiry_date", "<=", fields.Datetime.now()),
        ])
        with _transition():
            stale.write({"state": "expired"})
        return len(stale)
