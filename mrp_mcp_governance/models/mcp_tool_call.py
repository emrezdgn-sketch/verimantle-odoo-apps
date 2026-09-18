# -*- coding: utf-8 -*-
"""mcp.tool.call - the flight recorder.

Every tool invocation is logged, read-only ones included. Competing products
only record writes; the interesting audit question is usually "what did the
agent look at before it suggested that", and that is unanswerable if reads
go unrecorded.

Append-only by design: ir.model.access.csv grants no group write or unlink
rights on this model. Note the honest limit of that claim - a database
superuser bypasses access control entirely, so this is tamper-evident for
application users, not tamper-proof against someone with server access.
"""

from odoo import fields, models


class McpToolCall(models.Model):
    _name = "mcp.tool.call"
    _description = "MCP Tool Invocation Log"
    _order = "create_date desc, id desc"

    tool_name = fields.Char(required=True, readonly=True, index=True)
    params = fields.Text(readonly=True, help="Arguments as received, JSON encoded.")
    result_summary = fields.Char(
        readonly=True,
        help="Short outcome, e.g. the verdict of a shortage check. Never the "
             "full payload - this table is read often and must stay small.",
    )
    record_count = fields.Integer(
        readonly=True, help="How many records the call touched, for spotting bulk reads."
    )
    duration_ms = fields.Integer(readonly=True)
    is_error = fields.Boolean(readonly=True, index=True)
    error_message = fields.Char(readonly=True)

    agent_session = fields.Char(readonly=True, index=True)
    agent_label = fields.Char(readonly=True)
    user_id = fields.Many2one(
        "res.users", string="On Behalf Of", required=True, readonly=True, index=True
    )
    company_id = fields.Many2one(
        "res.company", required=True, readonly=True,
        default=lambda self: self.env.company,
    )

    proposal_ids = fields.One2many(
        "mcp.proposal", "tool_call_id", string="Proposals Produced", readonly=True
    )
