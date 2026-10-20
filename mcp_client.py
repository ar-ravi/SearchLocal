"""
SearchLocal MCP Client & Agent Framework

Provides an MCP (Model Context Protocol) client runtime that connects to local
MCP servers over stdio, discovers tools, and runs an autonomous tool-calling loop
with local Ollama models (supporting both native function calling and prompt-based ReAct).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


DEFAULT_CONFIG_PATH = Path.home() / ".searchlocal" / "mcp_config.json"


@dataclass
class MCPTool:
    name: str
    description: str
    parameters: dict[str, Any]
    server_name: str = "builtin"
    handler: Callable[[dict[str, Any]], str] | None = None

    def to_ollama_format(self) -> dict[str, Any]:
        """Convert to Ollama / OpenAI function format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class MCPProcessClient:
    """Manages an individual MCP server process communicating via stdio JSON-RPC 2.0."""

    def __init__(self, name: str, command: str, args: list[str]):
        self.name = name
        self.command = command
        self.args = args
        self.proc: subprocess.Popen[str] | None = None
        self._next_id = 1

    def start(self) -> None:
        cmd = [self.command, *self.args]
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise RuntimeError(f"Failed to start MCP server '{self.name}' ({cmd}): {exc}")

        # 1. Initialize
        res = self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "searchlocal-agent", "version": "0.2.0"},
            },
        )
        # 2. Initialized notification
        self._notify("notifications/initialized")

    def _next_request_id(self) -> int:
        req_id = self._next_id
        self._next_id += 1
        return req_id

    def _request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        if not self.proc or not self.proc.stdin or not self.proc.stdout:
            raise RuntimeError(f"MCP server '{self.name}' is not running")

        req_id = self._next_request_id()
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        try:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RuntimeError(f"Error writing to MCP server '{self.name}': {exc}")

        start_time = time.time()
        while time.time() - start_time < timeout:
            line = self.proc.stdout.readline()
            if not line:
                if self.proc.poll() is not None:
                    err = self.proc.stderr.read() if self.proc.stderr else ""
                    raise RuntimeError(f"MCP server '{self.name}' terminated unexpectedly. Stderr: {err}")
                continue
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                if isinstance(msg, dict) and msg.get("id") == req_id:
                    if "error" in msg:
                        raise RuntimeError(f"MCP server '{self.name}' returned error: {msg['error']}")
                    return msg.get("result", {})
            except json.JSONDecodeError:
                # Skip non-JSON log lines output by node/server
                continue

        raise TimeoutError(f"Timed out waiting for response from MCP server '{self.name}'")

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if not self.proc or not self.proc.stdin:
            return
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        try:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
        except OSError:
            pass

    def list_tools(self) -> list[MCPTool]:
        result = self._request("tools/list")
        raw_tools = result.get("tools", [])
        tools: list[MCPTool] = []
        for t in raw_tools:
            tool_name = f"{self.name}__{t['name']}"
            tools.append(
                MCPTool(
                    name=tool_name,
                    description=t.get("description", f"Tool {t['name']} from {self.name}"),
                    parameters=t.get("inputSchema", {"type": "object", "properties": {}}),
                    server_name=self.name,
                )
            )
        return tools

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        # Strip server namespace prefix if present
        actual_name = tool_name
        if tool_name.startswith(f"{self.name}__"):
            actual_name = tool_name[len(self.name) + 2 :]

        result = self._request("tools/call", {"name": actual_name, "arguments": arguments})
        content_items = result.get("content", [])
        texts = []
        for item in content_items:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(item.get("text", ""))
            else:
                texts.append(str(item))
        return "\n".join(texts).strip() or "Tool executed successfully (empty response)."

    def close(self) -> None:
        if self.proc:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.terminate()
                self.proc.wait(timeout=2.0)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            finally:
                self.proc = None


class MCPManager:
    """Orchestrates local MCP servers and built-in SearchLocal tools."""

    def __init__(self, config_path: Path = DEFAULT_CONFIG_PATH):
        self.config_path = config_path
        self.servers: dict[str, MCPProcessClient] = {}
        self.tools: dict[str, MCPTool] = {}
        self._register_builtins()

    def _register_builtins(self) -> None:
        """Register built-in local tools."""

        def _tool_search_index(args: dict[str, Any]) -> str:
            from searchlocal import connect, search_index, DEFAULT_DB
            query = args.get("query", "")
            limit = int(args.get("limit", 6))
            db_path = Path(args.get("db_path", DEFAULT_DB))
            if not db_path.exists():
                return f"Index database does not exist at {db_path}. Please run `searchlocal index` first."
            hits = search_index(db_path, query, limit)
            if not hits:
                return f"No indexed content matches found for query: '{query}'."
            parts = []
            conn = connect(db_path)
            for i, hit in enumerate(hits, 1):
                row = conn.execute(
                    "SELECT content FROM chunks WHERE path = ? AND chunk_index = ?",
                    (hit.path, hit.chunk_index),
                ).fetchone()
                content = row[0] if row else hit.snippet
                parts.append(f"[{i}] {hit.path}#chunk-{hit.chunk_index}\n{content}")
            return "\n\n---\n\n".join(parts)

        def _tool_read_file(args: dict[str, Any]) -> str:
            from searchlocal import read_file_content
            file_path = Path(args.get("path", "")).expanduser().resolve()
            if not file_path.exists():
                return f"Error: File does not exist at {file_path}"
            max_chars = int(args.get("max_chars", 8000))
            content = read_file_content(file_path)
            if content is None:
                return f"Could not read or parse text/document content from {file_path}."
            if len(content) > max_chars:
                return content[:max_chars] + f"\n... [Truncated, showing first {max_chars} characters]"
            return content

        def _tool_list_dir(args: dict[str, Any]) -> str:
            dir_path = Path(args.get("path", "")).expanduser().resolve()
            if not dir_path.is_dir():
                return f"Error: Not a directory: {dir_path}"
            try:
                entries = sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
                lines = []
                for entry in entries[:60]:
                    marker = "/" if entry.is_dir() else ""
                    lines.append(f"{entry.name}{marker}")
                if len(entries) > 60:
                    lines.append(f"... ({len(entries) - 60} more items)")
                return "\n".join(lines)
            except OSError as exc:
                return f"Error listing directory: {exc}"

        def _tool_spotlight(args: dict[str, Any]) -> str:
            from searchlocal import search_filenames_with_spotlight
            query = args.get("query", "")
            limit = int(args.get("limit", 10))
            results = search_filenames_with_spotlight(query, limit)
            if not results:
                return f"No Spotlight matches found for filename query: '{query}'."
            return "\n".join(f"- {p}" for p in results)

        builtins = [
            MCPTool(
                name="search_index",
                description="Search the local SQLite FTS index of indexed documents, code, and notes using BM25 keyword matching.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search terms or keywords"},
                        "limit": {"type": "integer", "description": "Max chunks to retrieve (default: 6)"},
                    },
                    "required": ["query"],
                },
                server_name="builtin",
                handler=_tool_search_index,
            ),
            MCPTool(
                name="read_local_file",
                description="Read full text or document content (supports .txt, .py, .pdf, .docx, .md, etc.) from an absolute path.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute file path to read"},
                        "max_chars": {"type": "integer", "description": "Maximum characters to return (default: 8000)"},
                    },
                    "required": ["path"],
                },
                server_name="builtin",
                handler=_tool_read_file,
            ),
            MCPTool(
                name="list_directory",
                description="List files and directories in a given local directory path.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Directory path to list"},
                    },
                    "required": ["path"],
                },
                server_name="builtin",
                handler=_tool_list_dir,
            ),
            MCPTool(
                name="spotlight_search",
                description="Search macOS Spotlight by filename across the entire system.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Filename keyword or pattern"},
                        "limit": {"type": "integer", "description": "Max results to return (default: 10)"},
                    },
                    "required": ["query"],
                },
                server_name="builtin",
                handler=_tool_spotlight,
            ),
        ]

        for b in builtins:
            self.tools[b.name] = b

    def load_config(self) -> None:
        """Load external MCP servers from JSON configuration file."""
        if not self.config_path.exists():
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as exc:
            print(f"[warning] Could not load MCP config {self.config_path}: {exc}", file=sys.stderr)
            return

        servers_cfg = config.get("mcp_servers", {})
        for name, s_cfg in servers_cfg.items():
            command = s_cfg.get("command")
            args = s_cfg.get("args", [])
            if not command:
                continue
            try:
                client = MCPProcessClient(name, command, args)
                client.start()
                self.servers[name] = client
                for tool in client.list_tools():
                    self.tools[tool.name] = tool
                print(f"[mcp] Connected to MCP server '{name}' with {len(client.list_tools())} tools.")
            except Exception as exc:
                print(f"[warning] Could not connect to MCP server '{name}': {exc}", file=sys.stderr)

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Dispatch tool call to either a built-in handler or an external MCP server."""
        if tool_name not in self.tools:
            # Fallback check if user or model called without namespace prefix
            for full_name, t in self.tools.items():
                if full_name.endswith(f"__{tool_name}") or tool_name == t.name:
                    tool_name = full_name
                    break

        tool = self.tools.get(tool_name)
        if not tool:
            return f"Error: Tool '{tool_name}' not found. Available tools: {list(self.tools.keys())}"

        if tool.server_name == "builtin" and tool.handler:
            try:
                return tool.handler(arguments)
            except Exception as exc:
                return f"Error executing built-in tool '{tool_name}': {exc}"

        client = self.servers.get(tool.server_name)
        if not client:
            return f"Error: MCP server '{tool.server_name}' not found or not active."

        try:
            return client.call_tool(tool.name, arguments)
        except Exception as exc:
            return f"Error executing MCP tool '{tool.name}': {exc}"

    def close(self) -> None:
        """Shut down all active MCP servers."""
        for client in self.servers.values():
            client.close()
        self.servers.clear()


class MCPAgent:
    """Autonomous agent loop using Ollama and local MCP tools."""

    def __init__(
        self,
        mcp_manager: MCPManager,
        model: str = "phi4",
        ollama_url: str = "http://127.0.0.1:11434",
        verbose: bool = True,
    ):
        self.mcp = mcp_manager
        self.model = model
        self.ollama_url = ollama_url.rstrip("/")
        self.verbose = verbose

    def run(self, user_question: str, max_turns: int = 6) -> str:
        """Run the agent loop until the question is answered or turn limit reached."""
        # Try native function calling first; fall back to ReAct prompt if model doesn't support tools
        try:
            return self._run_native_tools(user_question, max_turns)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="ignore")
            if "does not support tools" in err_body or exc.code == 400:
                if self.verbose:
                    print(f"[*] Note: Model '{self.model}' does not support native tool-calling. Using ReAct prompt loop.\n")
                return self._run_prompt_tools(user_question, max_turns)
            raise

    def _run_native_tools(self, user_question: str, max_turns: int) -> str:
        tools_payload = [tool.to_ollama_format() for tool in self.mcp.tools.values()]
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are a local search agent with access to local tools. "
                    "Use tools to discover, search, and read files before answering. "
                    "Always cite exact file paths in your final response."
                ),
            },
            {"role": "user", "content": user_question},
        ]

        for turn in range(max_turns):
            payload = {
                "model": self.model,
                "messages": messages,
                "tools": tools_payload,
                "stream": False,
                "options": {"temperature": 0.1},
            }
            req = urllib.request.Request(
                f"{self.ollama_url}/api/chat",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            msg = data.get("message", {})
            messages.append(msg)

            tool_calls = msg.get("tool_calls", [])
            if not tool_calls:
                return msg.get("content", "").strip()

            for call in tool_calls:
                func = call.get("function", {})
                func_name = func.get("name", "")
                func_args = func.get("arguments", {})
                if self.verbose:
                    print(f"-> [MCP Tool Call] {func_name}({json.dumps(func_args)})")

                result_text = self.mcp.call_tool(func_name, func_args)
                if self.verbose:
                    snippet = result_text.replace("\n", " ")[:120]
                    print(f"<- [Result] {snippet}...")

                messages.append(
                    {
                        "role": "tool",
                        "content": result_text,
                    }
                )

        return "Reached maximum turn limit without a final answer. Please narrow your query."

    def _run_prompt_tools(self, user_question: str, max_turns: int) -> str:
        tool_descriptions = []
        for t in self.mcp.tools.values():
            params_str = ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in t.parameters.get("properties", {}).items())
            tool_descriptions.append(f"- {t.name}({params_str}): {t.description}")

        system_instruction = (
            "You are a local Mac search agent. You have access to local tools to answer questions.\n\n"
            "Available tools:\n"
            + "\n".join(tool_descriptions)
            + "\n\n"
            "Rules:\n"
            "1. When you need to gather information or read a file, respond ONLY with a tool call formatted like:\n"
            "```tool_call\n"
            '{"name": "tool_name", "arguments": {"param1": "val1"}}\n'
            "```\n"
            "2. When you have sufficient information to answer the user's question, provide your final answer directly "
            "with exact file citations, without any ```tool_call block.\n"
        )

        history = f"{system_instruction}\n\nUser Question:\n{user_question}\n"

        for turn in range(max_turns):
            payload = {
                "model": self.model,
                "prompt": history,
                "stream": False,
                "options": {"temperature": 0.1},
            }
            req = urllib.request.Request(
                f"{self.ollama_url}/api/generate",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            response_text = data.get("response", "").strip()

            match = re.search(r"```(?:tool_call|json)?\s*({.+?})\s*```", response_text, re.DOTALL)
            if not match and response_text.startswith("{") and response_text.endswith("}"):
                match = re.search(r"({.+?})", response_text, re.DOTALL)

            if not match:
                # Model provided final answer
                return response_text

            call_json_str = match.group(1)
            try:
                call_data = json.loads(call_json_str)
                func_name = call_data.get("name", "")
                func_args = call_data.get("arguments", {})
            except json.JSONDecodeError:
                return response_text

            if not func_name:
                return response_text

            if self.verbose:
                print(f"-> [MCP Tool Call] {func_name}({json.dumps(func_args)})")

            result_text = self.mcp.call_tool(func_name, func_args)
            if self.verbose:
                snippet = result_text.replace("\n", " ")[:120]
                print(f"<- [Result] {snippet}...")

            history += f"\nAssistant:\n```tool_call\n{call_json_str}\n```\n\nTool Output for {func_name}:\n{result_text}\n"

        return "Reached maximum turn limit. Please refine your query."
