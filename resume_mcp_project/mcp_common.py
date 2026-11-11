"""
mcp_common.py - Tiny, dependency-free MCP server core (JSON-RPC 2.0 over stdio).

Handles: initialize, ping, tools/list, tools/call, resources/list, resources/read,
JSON-RPC envelopes, batch requests, notifications and standard error codes.
"""
import inspect
import json
import logging
import sys

PROTOCOL_VERSION = "2024-11-05"

# JSON-RPC 2.0 standard error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# Application-defined range (-32000 .. -32099)
RESOURCE_NOT_FOUND = -32002

log = logging.getLogger("mcp")


class RpcError(Exception):
    """Protocol-level error -> JSON-RPC "error" object."""

    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class ToolError(Exception):
    """Tool ran but failed -> MCP result with isError=true (not a protocol error)."""


def schema(properties, required=()):
    """Build a JSON Schema object for a tool's inputSchema."""
    return {"type": "object", "properties": properties, "required": list(required)}


class MCPServer:
    def __init__(self, name, version):
        self.info = {"name": name, "version": version}
        self.tools = {}
        self.list_resources = lambda: []      # () -> list[dict]
        self.read_resource = None             # (uri) -> (text, mimeType)

    def tool(self, name, description, input_schema):
        """Decorator that registers a Python function as an MCP tool."""
        def register(fn):
            self.tools[name] = {"name": name, "description": description,
                                "inputSchema": input_schema, "fn": fn}
            return fn
        return register

    # ---------- JSON-RPC plumbing ----------
    @staticmethod
    def _error(req_id, code, message, data=None):
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": req_id, "error": err}

    def dispatch(self, msg):
        """Handle one JSON-RPC message. Returns the reply dict, or None for notifications."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            req_id = msg.get("id") if isinstance(msg, dict) else None
            return self._error(req_id, INVALID_REQUEST, "Invalid JSON-RPC 2.0 request")

        is_notification = "id" not in msg          # notifications have no id -> no reply
        req_id = msg.get("id")
        params = msg.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return None if is_notification else self._error(req_id, INVALID_PARAMS, "params must be an object")

        try:
            result = self._handle(msg["method"], params)
        except RpcError as e:
            return None if is_notification else self._error(req_id, e.code, e.message, e.data)
        except Exception as e:                      # never crash the server on one bad request
            log.exception("internal error handling %s", msg["method"])
            return None if is_notification else self._error(req_id, INTERNAL_ERROR, str(e))

        return None if is_notification else {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _handle(self, method, params):
        if method == "initialize":
            return {"protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}, "resources": {}},
                    "serverInfo": self.info}
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [{k: t[k] for k in ("name", "description", "inputSchema")}
                              for t in self.tools.values()]}
        if method == "tools/call":
            return self._call_tool(params)
        if method == "resources/list":
            return {"resources": self.list_resources()}
        if method == "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str):
                raise RpcError(INVALID_PARAMS, "uri is required")
            text, mime = self.read_resource(uri)
            return {"contents": [{"uri": uri, "mimeType": mime, "text": text}]}
        raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")

    def _call_tool(self, params):
        name = params.get("name")
        if name not in self.tools:
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise RpcError(INVALID_PARAMS, "arguments must be an object")

        fn = self.tools[name]["fn"]
        try:
            inspect.signature(fn).bind(**args)     # validate argument names/count
        except TypeError as e:
            raise RpcError(INVALID_PARAMS, f"Bad arguments for {name}: {e}")

        try:
            out = fn(**args)
        except ToolError as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(out, indent=2, default=str)}],
                "isError": False}

    # ---------- stdio transport ----------
    @staticmethod
    def _send(obj):
        sys.stdout.write(json.dumps(obj, default=str) + "\n")   # stdout = protocol ONLY
        sys.stdout.flush()

    def run(self):
        logging.basicConfig(level=logging.INFO, stream=sys.stderr,   # logs go to stderr
                            format="%(levelname)s %(name)s: %(message)s")
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as e:
                self._send(self._error(None, PARSE_ERROR, f"Parse error: {e.msg}"))
                continue
            if isinstance(msg, list):                       # JSON-RPC batch
                if not msg:
                    self._send(self._error(None, INVALID_REQUEST, "Empty batch"))
                    continue
                replies = [r for r in (self.dispatch(m) for m in msg) if r is not None]
                if replies:
                    self._send(replies)
            else:
                reply = self.dispatch(msg)
                if reply is not None:
                    self._send(reply)
