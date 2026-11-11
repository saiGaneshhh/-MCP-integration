#!/usr/bin/env python3
"""
filesystem_mcp_server.py - MCP server exposing a sandboxed resume folder.

Tools:     list_files, read_file, write_file, watch_directory, batch_process
Resources: resume://<relative/path>  one per resume file
           config://server           active server configuration (read-only)

Run directly for debugging: python filesystem_mcp_server.py  (JSON-RPC on stdin/stdout)
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mcp_common import MCPServer, RpcError, ToolError, schema, RESOURCE_NOT_FOUND

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("MCP_FS_CONFIG", HERE / "config.json"))


def load_config():
    """Configuration management: defaults <- config.json <- environment override."""
    cfg = {"root_dir": "./workspace", "allowed_extensions": [".txt", ".md", ".json"],
           "max_file_bytes": 1_000_000, "max_workers": 4, "max_batch_size": 200}
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    cfg["root"] = (CONFIG_PATH.parent / cfg["root_dir"]).resolve()
    cfg["root"].mkdir(parents=True, exist_ok=True)
    return cfg


CFG = load_config()
ROOT = CFG["root"]
EXTS = {e.lower() for e in CFG["allowed_extensions"]}
server = MCPServer("filesystem-mcp-server", "1.0.0")
_snapshots = {}   # directory -> {relative_path: mtime_ns}; state for watch_directory


# ---------- helpers ----------
def safe_path(rel):
    """Resolve a client-supplied path and refuse anything outside ROOT (path traversal guard)."""
    if not isinstance(rel, str):
        raise ToolError("path must be a string")
    p = (ROOT / rel).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise ToolError(f"path escapes root directory: {rel}")
    return p


def rel_of(p):
    return p.relative_to(ROOT).as_posix()


def is_resume(p):
    return p.is_file() and p.suffix.lower() in EXTS


def read_text_checked(p):
    if not p.is_file():
        raise ToolError(f"file not found: {rel_of(p)}")
    if p.stat().st_size > CFG["max_file_bytes"]:
        raise ToolError(f"file too large (> {CFG['max_file_bytes']} bytes): {rel_of(p)}")
    return p.read_text(encoding="utf-8")


def scan(directory):
    return {rel_of(p): p.stat().st_mtime_ns for p in directory.rglob("*") if is_resume(p)}


# ---------- tools ----------
@server.tool("list_files", "List resume files under a directory (recursive).",
             schema({"directory": {"type": "string", "default": "."}}))
def list_files(directory="."):
    d = safe_path(directory)
    if not d.is_dir():
        raise ToolError(f"not a directory: {directory}")
    files = sorted(rel_of(p) for p in d.rglob("*") if is_resume(p))
    return {"directory": directory, "count": len(files), "files": files}


@server.tool("read_file", "Read a text resume file.",
             schema({"path": {"type": "string"}}, ["path"]))
def read_file(path):
    return {"path": path, "content": read_text_checked(safe_path(path))}


@server.tool("write_file", "Create or overwrite a resume file.",
             schema({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]))
def write_file(path, content):
    p = safe_path(path)
    if p.suffix.lower() not in EXTS:
        raise ToolError(f"extension not allowed: '{p.suffix}'")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"path": path, "bytes": len(content.encode("utf-8"))}


@server.tool("watch_directory",
             "Report resume files added, modified or removed since the previous call. "
             "The first call sets the baseline. wait_seconds (max 60) blocks until a change or timeout.",
             schema({"directory": {"type": "string", "default": "."},
                     "wait_seconds": {"type": "number", "default": 0},
                     "poll_interval": {"type": "number", "default": 0.5}}))
def watch_directory(directory=".", wait_seconds=0, poll_interval=0.5):
    d = safe_path(directory)
    if not d.is_dir():
        raise ToolError(f"not a directory: {directory}")
    wait = min(max(float(wait_seconds), 0.0), 60.0)
    poll = max(float(poll_interval), 0.1)
    key = rel_of(d)
    deadline = time.monotonic() + wait
    previous = _snapshots.get(key)

    while True:
        current = scan(d)
        if previous is None:
            _snapshots[key] = current
            return {"directory": directory, "status": "baseline_established",
                    "tracked": len(current), "new": [], "modified": [], "removed": []}
        new = sorted(f for f in current if f not in previous)
        modified = sorted(f for f in current if f in previous and current[f] != previous[f])
        removed = sorted(f for f in previous if f not in current)
        if new or modified or removed or time.monotonic() >= deadline:
            _snapshots[key] = current
            changed = bool(new or modified or removed)
            return {"directory": directory,
                    "status": "changes_detected" if changed else "no_changes",
                    "tracked": len(current), "new": new, "modified": modified, "removed": removed}
        time.sleep(poll)


@server.tool("batch_process",
             "Read or compute stats for many resume files in one call. Runs in parallel; "
             "failures are reported per file instead of aborting the batch.",
             schema({"paths": {"type": "array", "items": {"type": "string"}},
                     "operation": {"type": "string", "enum": ["read", "stats"], "default": "read"}},
                    ["paths"]))
def batch_process(paths, operation="read"):
    if not isinstance(paths, list) or not paths:
        raise ToolError("paths must be a non-empty list")
    if len(paths) > CFG["max_batch_size"]:
        raise ToolError(f"batch too large: {len(paths)} > {CFG['max_batch_size']}")
    if operation not in ("read", "stats"):
        raise ToolError("operation must be 'read' or 'stats'")

    def work(rel):
        try:
            text = read_text_checked(safe_path(rel))
        except (ToolError, OSError, UnicodeDecodeError) as e:
            return {"path": rel, "ok": False, "error": str(e)}
        if operation == "read":
            return {"path": rel, "ok": True, "content": text}
        return {"path": rel, "ok": True,
                "stats": {"chars": len(text), "words": len(text.split()),
                          "lines": text.count("\n") + 1}}

    with ThreadPoolExecutor(max_workers=CFG["max_workers"]) as pool:
        results = list(pool.map(work, paths))
    ok = sum(r["ok"] for r in results)
    return {"operation": operation, "total": len(results), "succeeded": ok,
            "failed": len(results) - ok, "results": results}


# ---------- resources ----------
def list_resources():
    items = [{"uri": "config://server", "name": "server-config",
              "mimeType": "application/json", "description": "Active server configuration"}]
    for p in sorted(ROOT.rglob("*")):
        if is_resume(p):
            items.append({"uri": f"resume://{rel_of(p)}", "name": p.name,
                          "mimeType": "text/plain", "description": f"{p.stat().st_size} bytes"})
    return items


def read_resource(uri):
    if uri == "config://server":
        return json.dumps({k: str(v) for k, v in CFG.items()}, indent=2), "application/json"
    if uri.startswith("resume://"):
        try:
            return read_text_checked(safe_path(uri[len("resume://"):])), "text/plain"
        except ToolError as e:
            raise RpcError(RESOURCE_NOT_FOUND, str(e))
    raise RpcError(RESOURCE_NOT_FOUND, f"unknown resource: {uri}")


server.list_resources = list_resources
server.read_resource = read_resource

if __name__ == "__main__":
    server.run()
