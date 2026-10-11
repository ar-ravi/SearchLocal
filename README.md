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

## 2. Index folders

Start with a specific folder before indexing your whole home directory:

```bash
cd /Users/ravi/Documents/Codex/2026-10-12/i-w/outputs/SearchLocal
python3 searchlocal.py index ~/Documents ~/Desktop
```

For a broader index:

```bash
python3 searchlocal.py index ~ --max-size-mb 10
```

By default it skips hidden folders, common dependency/cache folders, and large media-heavy folders such as `Library`, `Applications`, `Movies`, `Music`, and `Pictures`.

## 3. Search

```bash
python3 searchlocal.py search "invoice april" --spotlight
```

`--spotlight` also asks macOS Spotlight for filename matches.

## 4. Ask the agent

```bash
python3 searchlocal.py ask "Where did I write about the Q3 launch plan?"
```

Use a different local Ollama model:

```bash
python3 searchlocal.py ask "Summarize my notes on project Atlas" --model mistral
```

This Mac currently has `phi4:latest` installed, so this should work too:

```bash
python3 searchlocal.py ask "Summarize my notes on project Atlas" --model phi4
```

## 5. Check index size

```bash
python3 searchlocal.py stats
```

## Notes

- The default index database is `~/.searchlocal/index.db`.
- The indexer currently reads text-oriented files: Markdown, plain text, source code, JSON, YAML, CSV, logs, and similar formats.
- PDFs, images, Word documents, and emails need extractors before they can be searched by content. macOS Spotlight may still find those by filename.
- For privacy, index only folders you are comfortable searching. The model receives retrieved snippets from your local index.
