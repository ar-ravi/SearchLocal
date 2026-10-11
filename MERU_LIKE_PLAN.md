# SearchLocal MERU-like Plan

SearchLocal can grow toward a MERU-like local assistant, but the best first version should stay smaller and sharp:

- Local-only model runtime through Ollama
- Local file indexing and retrieval
- Answers with file citations
- Explicitly allowed tools only
- Logs for every tool call
- Plain files for config, memory, and skills

## Current Prototype

`searchlocal.py` already provides:

- SQLite FTS5 content index
- File chunking
- Local search over indexed text files
- Ollama answer generation from retrieved snippets
- Optional macOS Spotlight filename search

This is the retrieval core. It is not yet a full background assistant.

## Target Shape

```text
searchlocal          CLI client
searchlocald         background daemon
~/.searchlocal/
  config.toml        allowed folders, model, enabled tools
  secrets.toml       optional local-only tool secrets
  index.db           SQLite search index
  memory/*.md        user-editable memory
  skills/*.md        user-editable behavior packs
  logs/*.jsonl       tool calls, timings, source traces
```

## Milestone 1: Private Local Search

Goal: make the current CLI genuinely useful.

- Add PDF text extraction.
- Add DOCX text extraction.
- Add incremental re-indexing by folder.
- Add `watch` mode using macOS file events or periodic scans.
- Add quoted source citations with path and snippet.
- Add config file for default folders and model.

Recommended commands:

```bash
searchlocal setup
searchlocal index
searchlocal search "query"
searchlocal ask "question"
searchlocal stats
```

## Milestone 2: Agent Loop

Goal: let the model choose from a small allowed tool set.

Tools:

- `search_files`
- `read_file`
- `list_directory`
- `spotlight_search`
- `summarize_sources`

Rules:

- Read-only tools are enabled by default.
- Anything that writes, deletes, opens apps, sends email, or accesses the web requires explicit opt-in.
- Every tool call is logged to JSONL.

## Milestone 3: Daemon

Goal: keep models and indexes warm.

- Run `searchlocald` in the background.
- CLI sends requests over a local socket.
- Daemon owns index updates, model calls, and logs.
- Startup becomes fast because the CLI only connects to the daemon.

## Milestone 4: Hybrid Search

Goal: combine keyword search and semantic search.

- Keep SQLite FTS5 BM25 for exact keywords.
- Add Ollama embeddings, for example `nomic-embed-text`.
- Store vectors locally.
- Merge keyword and vector results with simple rank fusion.

This is the point where search starts feeling more like "find what I meant", not just "find these words".

## Milestone 5: Skills and Memory

Goal: make behavior inspectable and editable.

Memory:

- Save durable facts as Markdown files.
- Let the user edit or delete them directly.
- Never hide important state in opaque model memory.

Skills:

- Store task instructions as Markdown files.
- Load only relevant skills per request.
- Examples: `writing.md`, `code-research.md`, `file-research.md`, `daily-brief.md`.

## Milestone 6: Optional Tools

Goal: expand only when the user allows it.

Possible tools:

- Obsidian vault search
- Gmail and Calendar via MCP
- Local web search through SearXNG in Docker
- Browser page fetch
- Shell commands with confirmation

Default posture: local and read-only.

## Suggested Implementation Choice

For a personal Mac prototype, Python is fastest to iterate.

For a MERU-style polished app, Go or Swift becomes attractive later:

- Go: easy single binaries, good daemon/client story, cross-platform.
- Swift: best native macOS app and Spotlight/FileProvider integration.
- Python: best first prototype and easiest document parsing ecosystem.

The sensible path is to finish the Python version until the product behavior is obvious, then decide whether to keep it or port the stable parts.
