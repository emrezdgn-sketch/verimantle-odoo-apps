# -*- coding: utf-8 -*-
"""MCP endpoint, served from inside Odoo.

One route, POST /mcp, speaking JSON-RPC 2.0 the way the Model Context Protocol
specifies. Running inside Odoo rather than as a companion process is a
deliberate choice: installing the module is the whole setup, and the tools
execute against the same registry and access rules as the rest of the system.

Authentication is auth='bearer', Odoo's own API-key mechanism. The key belongs
to a user, and this endpoint additionally insists that user is in the MCP Agent
group - a group that holds read rights and nothing else. So a write is refused
by Odoo's own access control, not by this file's good behaviour: the agent
group has no create, write or unlink permission on any business model, and a
write attempted through any protocol against that key is refused.

The honest limit of that sentence: sudo() bypasses access control, and it is
reachable from any Odoo code holding an environment. What keeps it out of this
path is not the platform but a rule - tests/test_readonly_guarantee.py scans
tools/ and controllers/ on every run and fails if any privilege-escalating or
writing call appears. ACL plus an enforced no-sudo rule, not a sandbox.

type='http' rather than type='jsonrpc' on purpose: Odoo's JSON-RPC types wrap
the payload in an envelope of their own, and MCP already is JSON-RPC. Two
envelopes would not nest into anything a client understands.
"""

import json
import logging
import time

from werkzeug.exceptions import Forbidden

from odoo import http
from odoo.http import request

from ..tools import registry

_logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "odoo-mrp-mcp"
SERVER_VERSION = "0.1.0"

AGENT_GROUP = "mrp_mcp_tools.group_mcp_agent"

# JSON-RPC 2.0 error codes, plus the MCP convention that a tool which ran but
# produced a bad answer reports isError in its *result*, not as a protocol
# error. Only the protocol itself errors here.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _summarise(result):
    """One line a person would actually say, put first in the tool result.

    An assistant handed only a JSON blob tends to paraphrase it loosely, and
    the loose paraphrase is where "7 of 10" becomes "yes". Giving it the
    sentence as well makes the common case hard to get wrong.
    """
    if result.get("error"):
        return "Cannot answer: %s" % result["error"]["message"]

    asked = result.get("asked", {})
    code = asked.get("product_code") or asked.get("product_name") or "?"

    # shortage_check: the one answer that is a judgement rather than a listing.
    if "verdict" in result:
        wanted = asked.get("quantity")
        most = result.get("max_buildable")
        if result["verdict"] == "can_build":
            return "Yes - %s x %s can be built from free stock now." % (wanted, code)

        short = ", ".join(
            "%s (short %s)" % (c["product_code"] or c["product_name"], c["shortage_qty"])
            for c in result.get("components", [])
            if c.get("is_limiting")
        ) or "no single component"

        if result["verdict"] == "partial":
            return ("Partly - %s of the %s requested %s can be built. "
                    "Limited by: %s." % (most, wanted, code, short))
        return ("No - none of the %s requested %s can be built. Limited by: %s."
                % (wanted, code, short))

    if "available_qty" in result:
        return ("%s: %s free of %s on hand (%s reserved elsewhere)."
                % (code, result["available_qty"], result["on_hand_qty"],
                   result["reserved_qty"]))

    if "lot" in result:
        lot = result["lot"]
        who = ", ".join(result.get("delivered_to") or []) or "no customers recorded"
        return ("Lot %s of %s: %s on hand, delivered to %s."
                % (lot["name"], lot["product_code"] or lot["product_name"],
                   lot["on_hand_qty"], who))

    if "component_count" in result:
        return ("%s x %s needs %s distinct components."
                % (asked.get("quantity"), code, result["component_count"]))

    if "order" in result:
        order = result["order"]
        return ("%s is %s: %s of %s produced. Components: %s."
                % (order["name"], order["state"], order["produced_qty"],
                   order["quantity"], order["components_availability"] or "unknown"))

    if "order_count" in result:
        return "%s open manufacturing order(s)." % result["order_count"]

    if "movement_count" in result:
        return "%s recent movement(s)." % result["movement_count"]

    if "match_count" in result:
        return "%s product(s) matched %r." % (result["match_count"], asked.get("query"))

    return "Done."


class McpController(http.Controller):

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    @http.route(
        "/mcp",
        type="http",
        auth="bearer",
        methods=["POST"],
        csrf=False,
    )
    def mcp_endpoint(self, **kwargs):
        # The group check guards the whole endpoint, not just tools/call.
        # It used to sit in _call_tool only, which left initialize, ping and
        # tools/list open to any authenticated Odoo user - handing the entire
        # tool catalogue, descriptions and schemas included, to anybody with a
        # login. Raised here it becomes a 403 before a single byte is parsed.
        self._require_agent()

        try:
            body = request.httprequest.get_data(as_text=True)
            message = json.loads(body) if body else None
        except ValueError:
            return self._error(None, PARSE_ERROR, "Request body is not valid JSON.")

        if isinstance(message, list):
            # A batch. Notifications contribute nothing to the reply, and a
            # batch of only notifications gets an empty 202 like a single one.
            replies = [r for r in (self._handle(m) for m in message) if r is not None]
            if not replies:
                return request.make_response("", status=202)
            return request.make_json_response(replies)

        if not isinstance(message, dict):
            return self._error(None, INVALID_REQUEST, "Expected a JSON-RPC object.")

        reply = self._handle(message)
        if reply is None:
            return request.make_response("", status=202)
        return request.make_json_response(reply)

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------

    def _handle(self, message):
        """Return a JSON-RPC reply, or None for a notification."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self._body(None, error=(INVALID_REQUEST, "Not a JSON-RPC 2.0 message.", None))

        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}
        is_notification = "id" not in message

        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method == "ping":
                result = {}
            elif method and method.startswith("notifications/"):
                return None
            elif method == "tools/list":
                result = {"tools": registry.definitions()}
            elif method == "tools/call":
                result = self._call_tool(params)
            else:
                if is_notification:
                    return None
                return self._body(msg_id, error=(METHOD_NOT_FOUND, "Unknown method %r." % method, None))
        except _McpInvalidParams as exc:
            if is_notification:
                return None
            return self._body(msg_id, error=(INVALID_PARAMS, str(exc), None))
        except Forbidden:
            # Let this reach the HTTP layer as a 403 instead of being buried
            # in a 200 response with a JSON-RPC error inside it.
            raise
        except Exception:  # noqa: BLE001 - never leak a traceback to the client
            _logger.exception("MCP method %r failed", method)
            if is_notification:
                return None
            return self._body(msg_id, error=(INTERNAL_ERROR, "Internal error.", None))

        if is_notification:
            return None
        return self._body(msg_id, result=result)

    def _initialize(self, params):
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
                "title": "Odoo Manufacturing (read-only)",
            },
            "instructions": (
                "Read-only manufacturing tools for this Odoo database. Nothing "
                "here changes a record. Every answer is a snapshot and carries "
                "a 'limitations' list describing what it did not check - relay "
                "those rather than presenting the answer as a guarantee."
            ),
        }

    # ------------------------------------------------------------------
    # tools
    # ------------------------------------------------------------------

    def _call_tool(self, params):
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise _McpInvalidParams("'arguments' must be an object.")

        env = self._agent_env()
        started = time.monotonic()
        error_message = None

        tool = registry.BY_NAME.get(name)
        if tool is None:
            raise _McpInvalidParams("Unknown tool %r." % name)

        missing = [
            field for field in tool["inputSchema"].get("required", [])
            if arguments.get(field) is None
        ]
        if missing:
            raise _McpInvalidParams(
                "%s needs %s." % (name, " and ".join(repr(m) for m in missing))
            )

        try:
            result = tool["handler"](env, arguments)
        except _McpInvalidParams as exc:
            error_message = str(exc)
            raise
        finally:
            self._log_call(
                env, name, arguments,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_message=error_message,
            )

        summary = _summarise(result)
        # isError stays False even for a "blocked" verdict: the tool answered
        # the question correctly. isError is for a tool that could not run.
        return {
            "content": [
                {"type": "text", "text": summary},
                {"type": "text", "text": json.dumps(result, indent=2, default=str)},
            ],
            "structuredContent": result,
            "isError": False,
        }

    # ------------------------------------------------------------------
    # identity and audit
    # ------------------------------------------------------------------

    def _agent_env(self):
        """The environment tools run in."""
        self._require_agent()
        return request.env

    def _require_agent(self):
        """Refuse anyone whose key is not an agent's.

        auth='bearer' has already resolved the API key to a user. Insisting on
        the agent group as well means a stolen key belonging to, say, a
        production manager cannot be pointed at this endpoint to borrow that
        person's write rights.
        """
        if not request.env.user.has_group(AGENT_GROUP):
            # 403, not a JSON-RPC parameter error. The caller authenticated
            # successfully and simply may not use this endpoint; saying
            # "invalid params" would send them looking at their arguments.
            raise Forbidden(
                "This API key belongs to a user who is not in the MCP Agent "
                "group. Create a dedicated agent user rather than reusing a "
                "person's key."
            )
        return request.env

    def _log_call(self, env, tool_name, arguments, duration_ms, error_message):
        """Record the call if the governance module is installed.

        The audit log lives in the paid module, so this is an optional
        dependency rather than a hard one: the free tools work without it and
        gain a flight recorder when it is present.
        """
        if "mcp.tool.call" not in env:
            return
        try:
            # No sudo(): the agent group is granted create on mcp.tool.call and
            # nothing else, so this write is subject to the same access control
            # as every other one. Privileging it would make the audit log the
            # single place the agent identity can act unchecked.
            env["mcp.tool.call"].create({
                "tool_name": tool_name or "?",
                "params": json.dumps(arguments, default=str)[:4000],
                "duration_ms": duration_ms,
                "is_error": bool(error_message),
                "error_message": (error_message or "")[:255] or False,
                "user_id": env.uid,
                "agent_session": request.httprequest.headers.get("Mcp-Session-Id"),
            })
        except Exception:  # noqa: BLE001 - auditing must not break the answer
            _logger.exception("Could not record MCP tool call")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _body(self, msg_id, result=None, error=None):
        payload = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            code, message, data = error
            payload["error"] = {"code": code, "message": message}
            if data is not None:
                payload["error"]["data"] = data
        else:
            payload["result"] = result
        return payload

    def _error(self, msg_id, code, message):
        return request.make_json_response(
            self._body(msg_id, error=(code, message, None))
        )


class _McpInvalidParams(Exception):
    """Raised for anything the caller can fix by sending different arguments."""
