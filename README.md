# SearchLocal

SearchLocal is a local-first search agent for your Mac. It indexes local text-like files into SQLite full-text search, retrieves relevant snippets, then asks a local Ollama model to answer from those snippets.

It does not send your files to a cloud API.

If you want something MERU-like, read `MERU_LIKE_PLAN.md`. This prototype is the retrieval core: local index, local search, local Ollama answers, and citations.

## 1. Install Ollama

Install Ollama from <https://ollama.com>, then pull a model:

```bash
ollama pull llama3.2
```

If Ollama is not already running:

```bash
ollama serve
```

## 2. Setup & Requirements

Install dependencies (for PDF parsing and DOCX fallbacks):

```bash
cd /Users/ravi/Desktop/Workspace/SearchLocal
pip install -r requirements.txt
```

*(Note: On macOS, Word `.docx`, `.doc`, `.rtf`, and `.odt` files are converted natively using macOS's built-in `textutil` utility with zero external configuration).*

## 3. Index folders

Start with specific folders (e.g. Documents, Desktop, or Downloads):

```bash
python3 searchlocal.py index ~/Documents ~/Desktop ~/Downloads
```

For a broader index:

```bash
python3 searchlocal.py index ~ --max-size-mb 10
```

By default it skips hidden folders, common dependency/cache folders, and large media-heavy folders such as `Library`, `Applications`, `Movies`, `Music`, and `Pictures`.

## 4. Search

```bash
python3 searchlocal.py search "invoice april" --spotlight
```

`--spotlight` also asks macOS Spotlight for filename matches.

## 5. Ask with Autonomous MCP Agent (Multi-Tool Calling)

SearchLocal can run as an **autonomous agent** that connects to local MCP servers (such as `@modelcontextprotocol/server-filesystem`) and built-in search tools:

```bash
python3 searchlocal.py agent "What projects are inside /Users/ravi/Desktop/Workspace?"
```

Inspect all active tools from connected MCP servers and built-in tools:

```bash
python3 searchlocal.py tools
```

Or run `ask` with the `--agent` flag:

```bash
python3 searchlocal.py ask "Find my Amazon resume and summarize my experience" --agent
```

### Configuring Local MCP Servers

SearchLocal reads MCP server configurations from `~/.searchlocal/mcp_config.json` (see [`mcp_config.example.json`](mcp_config.example.json)):

```json
{
  "mcp_servers": {
    "filesystem": {
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/server-filesystem",
        "/Users/ravi/Desktop",
        "/Users/ravi/Documents",
        "/Users/ravi/Downloads"
      ]
    }
  }
}
```

## 6. Check index size

```bash
python3 searchlocal.py stats
```

## Supported Formats & Notes

- The default index database is `~/.searchlocal/index.db`.
- **Text & Code**: Markdown, plain text, source code (`.py`, `.ts`, `.cpp`, `.go`, `.java`, `.sh`, etc.), JSON, YAML, CSV, SQL, logs.
- **Documents**: PDF (`.pdf`), Microsoft Word (`.docx`, `.doc`), Rich Text (`.rtf`), and OpenDocument (`.odt`).
- For privacy, all indexing and Ollama inference run strictly on your local machine. No data leaves your computer.
