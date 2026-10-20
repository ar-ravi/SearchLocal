#!/usr/bin/env python3
"""
SearchLocal

Indexes local text-like files into SQLite FTS and answers questions using a
local Ollama model with retrieved file snippets as context.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_DB = Path.home() / ".searchlocal" / "index.db"
DEFAULT_MODEL = "phi4"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".rst",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".log",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".html",
    ".css",
    ".scss",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".swift",
    ".sh",
    ".zsh",
    ".fish",
    ".sql",
    ".xml",
}

DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".doc",
    ".rtf",
    ".odt",
}

ALL_SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | DOCUMENT_EXTENSIONS

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".cache",
    "Library",
    "Applications",
    "Movies",
    "Music",
    "Pictures",
}


@dataclass(frozen=True)
class SearchHit:
    path: str
    chunk_index: int
    snippet: str
    rank: float


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL,
            indexed_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
            path UNINDEXED,
            chunk_index UNINDEXED,
            content,
            tokenize='porter unicode61'
        )
        """
    )
    return conn


def is_supported_file(path: Path, max_size_mb: int) -> bool:
    if path.suffix.lower() not in ALL_SUPPORTED_EXTENSIONS:
        return False
    try:
        return path.stat().st_size <= max_size_mb * 1024 * 1024
    except OSError:
        return False


# Backward compatibility alias
is_probably_text = is_supported_file


def iter_files(roots: Iterable[Path], max_size_mb: int, include_hidden: bool) -> Iterable[Path]:
    for root in roots:
        root = root.expanduser().resolve()
        if root.is_file() and is_supported_file(root, max_size_mb):
            yield root
            continue
        if not root.exists():
            print(f"warning: root does not exist: {root}", file=sys.stderr)
            continue
        for current, dirnames, filenames in os.walk(root):
            current_path = Path(current)
            dirnames[:] = [
                name
                for name in dirnames
                if name not in SKIP_DIRS and (include_hidden or not name.startswith("."))
            ]
            for filename in filenames:
                if not include_hidden and filename.startswith("."):
                    continue
                path = current_path / filename
                if is_supported_file(path, max_size_mb):
                    yield path


def extract_pdf_text(path: Path) -> str | None:
    # 1. Try pypdf library
    try:
        import pypdf

        reader = pypdf.PdfReader(str(path))
        pages_text: list[str] = []
        for idx, page in enumerate(reader.pages):
            page_content = page.extract_text() or ""
            page_content = page_content.strip()
            if page_content:
                pages_text.append(f"--- Page {idx + 1} ---\n{page_content}")
        if pages_text:
            return "\n\n".join(pages_text)
    except Exception:
        pass

    # 2. Try CLI pdftotext if installed
    try:
        res = subprocess.run(
            ["pdftotext", str(path), "-"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass

    return None


def extract_docx_text(path: Path) -> str | None:
    # 1. macOS native textutil (built into macOS, supports docx, doc, rtf, odt)
    if sys.platform == "darwin":
        try:
            res = subprocess.run(
                ["textutil", "-convert", "txt", "-stdout", str(path)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass

    # 2. Try docx2txt library
    try:
        import docx2txt

        text = docx2txt.process(str(path))
        if text and text.strip():
            return text.strip()
    except Exception:
        pass

    return None


def read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return None


def read_file_content(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf_text(path)
    if suffix in {".docx", ".doc", ".rtf", ".odt"}:
        return extract_docx_text(path)
    return read_text(path)


def chunk_text(text: str, chunk_chars: int = 2400, overlap: int = 250) -> list[str]:
    clean = "\n".join(line.rstrip() for line in text.splitlines())
    chunks: list[str] = []
    start = 0
    while start < len(clean):
        end = min(start + chunk_chars, len(clean))
        chunk = clean[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end == len(clean):
            break
        start = max(0, end - overlap)
    return chunks


def index_roots(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    roots = [Path(root) for root in args.roots]
    indexed = 0
    skipped = 0
    start = time.time()

    for path in iter_files(roots, args.max_size_mb, args.include_hidden):
        try:
            stat = path.stat()
        except OSError:
            skipped += 1
            continue

        row = conn.execute("SELECT mtime, size FROM files WHERE path = ?", (str(path),)).fetchone()
        if row and row[0] == stat.st_mtime and row[1] == stat.st_size and not args.force:
            skipped += 1
            continue

        text = read_file_content(path)
        if not text:
            skipped += 1
            continue

        conn.execute("DELETE FROM chunks WHERE path = ?", (str(path),))
        for chunk_index, chunk in enumerate(chunk_text(text)):
            conn.execute(
                "INSERT INTO chunks(path, chunk_index, content) VALUES (?, ?, ?)",
                (str(path), chunk_index, chunk),
            )
        conn.execute(
            """
            INSERT INTO files(path, mtime, size, indexed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                mtime = excluded.mtime,
                size = excluded.size,
                indexed_at = excluded.indexed_at
            """,
            (str(path), stat.st_mtime, stat.st_size, time.time()),
        )
        indexed += 1
        if indexed % 100 == 0:
            conn.commit()
            print(f"indexed {indexed} files...")

    conn.commit()
    elapsed = time.time() - start
    print(f"done: indexed {indexed} files, skipped {skipped}, took {elapsed:.1f}s")
    print(f"database: {args.db}")


def fts_query(query: str) -> str:
    terms = [term.strip('"') for term in query.split() if term.strip()]
    if not terms:
        return query
    return " OR ".join(f'"{term}"' for term in terms)


def search_index(db_path: Path, query: str, limit: int) -> list[SearchHit]:
    conn = connect(db_path)
    rows = conn.execute(
        """
        SELECT
            path,
            chunk_index,
            snippet(chunks, 2, '[', ']', ' ... ', 18) AS snippet,
            bm25(chunks) AS rank
        FROM chunks
        WHERE chunks MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (fts_query(query), limit),
    ).fetchall()
    return [SearchHit(path=row[0], chunk_index=row[1], snippet=row[2], rank=row[3]) for row in rows]


def search_filenames_with_spotlight(query: str, limit: int) -> list[str]:
    try:
        result = subprocess.run(
            ["mdfind", f"kMDItemFSName == '*{query}*'cd"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line for line in result.stdout.splitlines() if line][:limit]


def print_search(args: argparse.Namespace) -> None:
    hits = search_index(args.db, args.query, args.limit)
    if hits:
        print("Content matches:")
        for i, hit in enumerate(hits, 1):
            print(f"\n{i}. {hit.path}#chunk-{hit.chunk_index}")
            print(f"   {hit.snippet}")
    else:
        print("No content matches found.")

    if args.spotlight:
        names = search_filenames_with_spotlight(args.query, args.limit)
        if names:
            print("\nFilename matches from Spotlight:")
            for i, path in enumerate(names, 1):
                print(f"{i}. {path}")


def build_context(db_path: Path, query: str, limit: int) -> tuple[str, list[SearchHit]]:
    hits = search_index(db_path, query, limit)
    parts = []
    conn = connect(db_path)
    for i, hit in enumerate(hits, 1):
        row = conn.execute(
            "SELECT content FROM chunks WHERE path = ? AND chunk_index = ?",
            (hit.path, hit.chunk_index),
        ).fetchone()
        if row:
            parts.append(f"[{i}] {hit.path}#chunk-{hit.chunk_index}\n{row[0]}")
    return "\n\n---\n\n".join(parts), hits


def ollama_generate(base_url: str, model: str, prompt: str) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2},
    }
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data.get("response", "").strip()
    except urllib.error.URLError as exc:
        raise SystemExit(
            "Could not reach Ollama. Start it with `ollama serve` and make sure "
            f"the model is pulled with `ollama pull {model}`.\nDetails: {exc}"
        )


def agent_command(args: argparse.Namespace) -> None:
    from mcp_client import MCPAgent, MCPManager, DEFAULT_CONFIG_PATH

    config_path = args.config if args.config else DEFAULT_CONFIG_PATH
    mcp = MCPManager(config_path=config_path)
    if not args.no_mcp:
        mcp.load_config()

    try:
        agent = MCPAgent(mcp, model=args.model, ollama_url=args.ollama_url)
        print(f"[*] Starting SearchLocal MCP Agent ({args.model}) with {len(mcp.tools)} active tools...\n")
        answer = agent.run(args.question, max_turns=args.max_turns)
        print("\n" + "=" * 48)
        print("Final Answer:")
        print("=" * 48)
        print(answer)
    finally:
        mcp.close()


def tools_command(args: argparse.Namespace) -> None:
    from mcp_client import MCPManager, DEFAULT_CONFIG_PATH

    config_path = args.config if args.config else DEFAULT_CONFIG_PATH
    mcp = MCPManager(config_path=config_path)
    if not args.no_mcp:
        mcp.load_config()

    try:
        print(f"\nActive SearchLocal Tools ({len(mcp.tools)} total):\n")
        for name, t in sorted(mcp.tools.items()):
            source = f"[{t.server_name}]"
            print(f"  • {name:<35} {source:<12} {t.description}")
        print()
    finally:
        mcp.close()


def ask(args: argparse.Namespace) -> None:
    if getattr(args, "agent", False):
        return agent_command(args)

    context, hits = build_context(args.db, args.question, args.limit)
    if not context:
        print("I could not find indexed content for that question.")
        if args.spotlight:
            names = search_filenames_with_spotlight(args.question, args.limit)
            if names:
                print("\nFilename matches from Spotlight:")
                for i, path in enumerate(names, 1):
                    print(f"{i}. {path}")
        return

    prompt = f"""You are a local Mac search agent. Answer using only the provided local file context.
If the context is insufficient, say what is missing. Cite file paths in your answer.

Question:
{args.question}

Local file context:
{context}
"""
    answer = ollama_generate(args.ollama_url, args.model, prompt)
    print(answer)
    print("\nSources:")
    for i, hit in enumerate(hits, 1):
        print(f"{i}. {hit.path}#chunk-{hit.chunk_index}")


def stats(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    print(f"files: {file_count}")
    print(f"chunks: {chunk_count}")
    print(f"database: {args.db}")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SearchLocal: local Ollama-powered Mac search & MCP agent")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"SQLite index path (default: {DEFAULT_DB})")

    sub = parser.add_subparsers(required=True)

    p_index = sub.add_parser("index", help="Index local files")
    p_index.add_argument("roots", nargs="+", help="Files or folders to index")
    p_index.add_argument("--max-size-mb", type=int, default=10, help="Skip files larger than this")
    p_index.add_argument("--include-hidden", action="store_true", help="Include hidden files and folders")
    p_index.add_argument("--force", action="store_true", help="Reindex unchanged files")
    p_index.set_defaults(func=index_roots)

    p_search = sub.add_parser("search", help="Search indexed file contents")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=8)
    p_search.add_argument("--spotlight", action="store_true", help="Also search filenames with macOS Spotlight")
    p_search.set_defaults(func=print_search)

    p_ask = sub.add_parser("ask", help="Ask Ollama a question using retrieved local context or MCP agent loop")
    p_ask.add_argument("question")
    p_ask.add_argument("--limit", type=int, default=6)
    p_ask.add_argument("--model", default=DEFAULT_MODEL)
    p_ask.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    p_ask.add_argument("--spotlight", action="store_true", help="Show Spotlight filename matches if no content matches")
    p_ask.add_argument("--agent", action="store_true", help="Run as autonomous MCP agent using local tools")
    p_ask.add_argument("--config", type=Path, default=None, help="Custom path to mcp_config.json")
    p_ask.add_argument("--no-mcp", action="store_true", help="Disable external MCP servers and use only built-in tools")
    p_ask.add_argument("--max-turns", type=int, default=6, help="Maximum agent turns")
    p_ask.set_defaults(func=ask)

    p_agent = sub.add_parser("agent", help="Run autonomous agent using local MCP tools (filesystem, search, spotlight)")
    p_agent.add_argument("question", help="Question or task for the local agent")
    p_agent.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama model (default: {DEFAULT_MODEL})")
    p_agent.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL, help=f"Ollama endpoint (default: {DEFAULT_OLLAMA_URL})")
    p_agent.add_argument("--config", type=Path, default=None, help="Custom path to mcp_config.json")
    p_agent.add_argument("--no-mcp", action="store_true", help="Disable external MCP servers and use only built-in tools")
    p_agent.add_argument("--max-turns", type=int, default=6, help="Maximum agent turns (default: 6)")
    p_agent.set_defaults(func=agent_command)

    p_tools = sub.add_parser("tools", help="List all active tools from built-in engine and connected MCP servers")
    p_tools.add_argument("--config", type=Path, default=None, help="Custom path to mcp_config.json")
    p_tools.add_argument("--no-mcp", action="store_true", help="Show only built-in tools")
    p_tools.set_defaults(func=tools_command)

    p_stats = sub.add_parser("stats", help="Show index statistics")
    p_stats.set_defaults(func=stats)

    return parser


def main() -> None:
    parser = make_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
