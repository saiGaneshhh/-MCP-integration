"""
test_mcp_server.py - Scenario tests for the filesystem MCP server (JSON-RPC 2.0 over stdio).

Run:  python -m unittest -v test_mcp_server.py
No LangGraph needed; these tests only exercise the MCP server.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "filesystem_mcp_server.py"


class FsServer:
    """Runs the server in a temporary sandbox and sends raw JSON-RPC messages."""

    def __init__(self, tmp):
        self.root = Path(tmp) / "root"
        self.root.mkdir()
        (self.root / "resumes").mkdir()
        cfg = Path(tmp) / "config.json"
        cfg.write_text(json.dumps({"root_dir": str(self.root), "max_batch_size": 5}))
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, env=dict(os.environ, MCP_FS_CONFIG=str(cfg)))
        self._id = 0

    def raw(self, line):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def rpc(self, method, params=None):
        self._id += 1
        return self.raw(json.dumps({"jsonrpc": "2.0", "id": self._id,
                                    "method": method, "params": params or {}}))

    def tool(self, name, **args):
        return self.rpc("tools/call", {"name": name, "arguments": args})["result"]

    def close(self):
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if pipe:
                pipe.close()
        self.proc.wait(timeout=5)


def payload(result):
    return json.loads(result["content"][0]["text"])


class FilesystemServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.srv = FsServer(self.tmp.name)

    def tearDown(self):
        self.srv.close()
        self.tmp.cleanup()

    def test_initialize_and_tool_discovery(self):
        init = self.srv.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        self.assertIn("tools", init["result"]["capabilities"])
        names = {t["name"] for t in self.srv.rpc("tools/list")["result"]["tools"]}
        self.assertTrue({"list_files", "read_file", "write_file",
                         "watch_directory", "batch_process"} <= names)

    def test_json_rpc_error_codes(self):
        self.assertEqual(self.srv.rpc("no/such/method")["error"]["code"], -32601)
        self.assertEqual(self.srv.raw("{not json")["error"]["code"], -32700)
        self.assertEqual(self.srv.raw(json.dumps({"id": 1, "method": "ping"}))["error"]["code"], -32600)

    def test_path_traversal_is_blocked(self):
        res = self.srv.tool("read_file", path="../../etc/passwd")
        self.assertTrue(res["isError"])
        self.assertIn("escapes", res["content"][0]["text"])

    def test_resource_discovery_and_read(self):
        self.srv.tool("write_file", path="resumes/alice.txt", content="Python and SQL expert")
        uris = [r["uri"] for r in self.srv.rpc("resources/list")["result"]["resources"]]
        self.assertIn("resume://resumes/alice.txt", uris)
        read = self.srv.rpc("resources/read", {"uri": "resume://resumes/alice.txt"})["result"]
        self.assertEqual(read["contents"][0]["text"], "Python and SQL expert")

    def test_batch_process_reports_partial_failures(self):
        self.srv.tool("write_file", path="resumes/a.txt", content="one two three")
        self.srv.tool("write_file", path="resumes/b.txt", content="four five")
        out = payload(self.srv.tool("batch_process", operation="stats",
                                    paths=["resumes/a.txt", "resumes/b.txt", "resumes/missing.txt"]))
        self.assertEqual((out["total"], out["succeeded"], out["failed"]), (3, 2, 1))

    def test_batch_size_limit(self):
        res = self.srv.tool("batch_process", paths=[f"resumes/f{i}.txt" for i in range(6)])
        self.assertTrue(res["isError"])

    def test_watch_directory_detects_new_resume(self):
        first = payload(self.srv.tool("watch_directory", directory="resumes"))
        self.assertEqual(first["status"], "baseline_established")
        self.srv.tool("write_file", path="resumes/new_candidate.txt", content="Go developer")
        second = payload(self.srv.tool("watch_directory", directory="resumes"))
        self.assertEqual(second["new"], ["resumes/new_candidate.txt"])


if __name__ == "__main__":
    unittest.main()
