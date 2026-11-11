#!/usr/bin/env python3
"""
matching_agent.py - LangGraph agent that ranks resumes against a job description.

The agent contains NO file-system code. Every file operation goes through the
filesystem MCP server, and every database operation through the database MCP
server. This process is only an MCP *client* plus the workflow graph.

Usage:
    python matching_agent.py --job backend_engineer.txt --top 5
    python matching_agent.py --job backend_engineer.txt --watch 5   # re-rank when resumes change
"""
import argparse
import itertools
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, StateGraph

HERE = Path(__file__).resolve().parent
PY = sys.executable
PROTOCOL_VERSION = "2024-11-05"


# ----------------------------------------------------------------------------
# Minimal MCP client: spawns a server process and speaks JSON-RPC 2.0 over stdio
# ----------------------------------------------------------------------------
class MCPClient:
    def __init__(self, name, command):
        self.name = name
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True, bufsize=1, cwd=HERE)
        self._ids = itertools.count(1)
        self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                    "clientInfo": {"name": "matching-agent", "version": "1.0.0"}})
        self.notify("notifications/initialized")

    def _write(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def notify(self, method, params=None):
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method, params=None):
        rid = next(self._ids)
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise ConnectionError(f"{self.name} MCP server exited unexpectedly")
            msg = json.loads(line)
            if msg.get("id") != rid:          # skip notifications / unrelated replies
                continue
            if "error" in msg:
                raise RuntimeError(f"{self.name} JSON-RPC error {msg['error']['code']}: "
                                   f"{msg['error']['message']}")
            return msg["result"]

    def list_resources(self):
        return self.request("resources/list")["resources"]

    def call_tool(self, tool, **arguments):
        result = self.request("tools/call", {"name": tool, "arguments": arguments})
        text = "".join(block.get("text", "") for block in result["content"])
        if result.get("isError"):
            raise RuntimeError(f"{self.name}.{tool} failed: {text}")
        return json.loads(text)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


# ----------------------------------------------------------------------------
# Agent state and helpers
# ----------------------------------------------------------------------------
class AgentState(TypedDict, total=False):
    job_file: str
    top_n: int
    job_title: str
    skills: list[str]
    resume_files: list[str]
    resumes: dict[str, str]
    scores: list[dict]
    history: list[dict]
    errors: list[str]
    report: str


def skill_in_text(skill, text):
    """Whole-token, case-insensitive match so 'java' does not match 'javascript'."""
    pattern = r"(?<![\w+#])" + re.escape(skill) + r"(?![\w+#])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def build_graph(fs: MCPClient, db: MCPClient):
    """Each node is a small step that calls MCP servers, then updates state."""

    def load_job(state):
        text = fs.call_tool("read_file", path=f"jobs/{state['job_file']}")["content"]
        title = text.strip().splitlines()[0].strip()
        m = re.search(r"^Skills:\s*(.+)$", text, flags=re.MULTILINE | re.IGNORECASE)
        skills = [s.strip().lower() for s in m.group(1).split(",") if s.strip()] if m else []
        return {"job_title": title, "skills": skills}

    def discover(state):
        # Resource discovery: resumes are exposed by the server as resume:// resources.
        uris = [r["uri"] for r in fs.list_resources() if r["uri"].startswith("resume://resumes/")]
        return {"resume_files": sorted(u.removeprefix("resume://") for u in uris)}

    def read_resumes(state):
        # Batch tool: one MCP call reads every resume in parallel on the server side.
        batch = fs.call_tool("batch_process", paths=state["resume_files"], operation="read")
        texts = {r["path"]: r["content"] for r in batch["results"] if r["ok"]}
        errors = [f"{r['path']}: {r['error']}" for r in batch["results"] if not r["ok"]]
        return {"resumes": texts, "errors": state.get("errors", []) + errors}

    def score(state):
        skills = state["skills"]
        rows = []
        for path, text in state["resumes"].items():
            matched = [s for s in skills if skill_in_text(s, text)]
            pct = round(100 * len(matched) / len(skills)) if skills else 0
            rows.append({"candidate": Path(path).stem, "file": path, "score": pct,
                         "matched": matched, "missing": [s for s in skills if s not in matched]})
        rows.sort(key=lambda r: (-r["score"], r["candidate"]))
        return {"scores": rows}

    def persist(state):
        # Second MCP server (database): save results, then read history back.
        for row in state["scores"]:
            db.call_tool("save_match", job_title=state["job_title"], candidate=row["candidate"],
                         score=row["score"], matched_skills=row["matched"])
        history = db.call_tool("query_matches", job_title=state["job_title"], limit=5)["matches"]
        return {"history": history}

    def report(state):
        lines = [f"# Ranking for: {state['job_title']}",
                 f"Required skills: {', '.join(state['skills']) or 'none listed'}", ""]
        if not state.get("scores"):
            lines.append("No resumes were scored.")
        for i, row in enumerate(state.get("scores", [])[: state["top_n"]], 1):
            lines.append(f"{i}. {row['candidate']}: {row['score']}%  "
                         f"matched={row['matched']}  missing={row['missing']}")
        if state.get("history"):
            lines += ["", "Stored history for this job (database MCP server):"]
            lines += [f"  - {h['candidate']}: {h['score']}% (updated {h['updated_at']})"
                      for h in state["history"]]
        if state.get("errors"):
            lines += ["", "Errors:"] + [f"  - {e}" for e in state["errors"]]
        return {"report": "\n".join(lines)}

    g = StateGraph(AgentState)
    for name, fn in [("load_job", load_job), ("discover", discover),
                     ("read_resumes", read_resumes), ("score", score),
                     ("persist", persist), ("report", report)]:
        g.add_node(name, fn)
    g.set_entry_point("load_job")
    g.add_edge("load_job", "discover")
    g.add_conditional_edges("discover",
                            lambda s: "read_resumes" if s["resume_files"] else "report",
                            {"read_resumes": "read_resumes", "report": "report"})
    g.add_edge("read_resumes", "score")
    g.add_edge("score", "persist")
    g.add_edge("persist", "report")
    g.add_edge("report", END)
    return g.compile()


def main():
    ap = argparse.ArgumentParser(description="Rank resumes against a job via MCP servers.")
    ap.add_argument("--job", default="backend_engineer.txt", help="file inside workspace/jobs/")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--watch", type=int, default=0, metavar="SECONDS",
                    help="keep running and re-rank whenever resumes are added or changed")
    args = ap.parse_args()

    fs = MCPClient("filesystem", [PY, str(HERE / "filesystem_mcp_server.py")])
    db = MCPClient("database", [PY, str(HERE / "database_mcp_server.py")])
    try:
        app = build_graph(fs, db)

        def run():
            out = app.invoke({"job_file": args.job, "top_n": args.top, "errors": []})
            print(out["report"])

        run()
        if args.watch:
            fs.call_tool("watch_directory", directory="resumes")   # establish baseline
            print(f"\nWatching workspace/resumes every {args.watch}s. Ctrl+C to stop.")
            while True:
                change = fs.call_tool("watch_directory", directory="resumes",
                                      wait_seconds=args.watch)
                if change["new"] or change["modified"]:
                    print(f"\nChange detected: new={change['new']} modified={change['modified']}")
                    run()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        fs.close()
        db.close()


if __name__ == "__main__":
    main()
