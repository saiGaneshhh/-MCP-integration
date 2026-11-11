#!/usr/bin/env python3
"""
database_mcp_server.py - BONUS MCP server: stores match results in SQLite.

Tools: save_match, query_matches, list_jobs
This is the second MCP server the agent connects to (multi-MCP integration).
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from mcp_common import MCPServer, ToolError, schema

DB_PATH = Path(os.environ.get("MCP_DB_PATH", Path(__file__).resolve().with_name("matches.db")))
SCHEMA = """CREATE TABLE IF NOT EXISTS matches (
    job_title TEXT NOT NULL,
    candidate TEXT NOT NULL,
    score INTEGER NOT NULL,
    matched_skills TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (job_title, candidate))"""

server = MCPServer("database-mcp-server", "1.0.0")


def run_sql(sql, params=(), fetch=False):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        con.execute(SCHEMA)
        cur = con.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()] if fetch else []
        con.commit()
        return rows
    finally:
        con.close()


@server.tool("save_match", "Insert or update a candidate's score for a job.",
             schema({"job_title": {"type": "string"},
                     "candidate": {"type": "string"},
                     "score": {"type": "integer", "minimum": 0, "maximum": 100},
                     "matched_skills": {"type": "array", "items": {"type": "string"}}},
                    ["job_title", "candidate", "score"]))
def save_match(job_title, candidate, score, matched_skills=None):
    if not isinstance(score, int) or not 0 <= score <= 100:
        raise ToolError("score must be an integer between 0 and 100")
    run_sql("INSERT OR REPLACE INTO matches VALUES (?,?,?,?,?)",
            (job_title, candidate, score, json.dumps(matched_skills or []),
             datetime.now(timezone.utc).isoformat()))
    return {"saved": True, "job_title": job_title, "candidate": candidate, "score": score}


@server.tool("query_matches", "Query stored matches, highest score first.",
             schema({"job_title": {"type": "string"},
                     "min_score": {"type": "integer", "default": 0},
                     "limit": {"type": "integer", "default": 10}}))
def query_matches(job_title=None, min_score=0, limit=10):
    sql = "SELECT job_title, candidate, score, matched_skills, updated_at FROM matches WHERE score >= ?"
    params = [int(min_score)]
    if job_title:
        sql += " AND job_title = ?"
        params.append(job_title)
    sql += " ORDER BY score DESC, candidate LIMIT ?"
    params.append(int(limit))
    rows = run_sql(sql, params, fetch=True)
    for r in rows:
        r["matched_skills"] = json.loads(r["matched_skills"])
    return {"count": len(rows), "matches": rows}


@server.tool("list_jobs", "Summary of every job that has stored matches.", schema({}))
def list_jobs():
    rows = run_sql("SELECT job_title, COUNT(*) AS candidates, MAX(score) AS best, "
                   "ROUND(AVG(score), 1) AS average FROM matches GROUP BY job_title "
                   "ORDER BY job_title", fetch=True)
    return {"jobs": rows}


if __name__ == "__main__":
    server.run()
