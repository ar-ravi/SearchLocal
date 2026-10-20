"""Loopback-only local Mac assistant dashboard."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HOST = "127.0.0.1"
PORT = 18765

PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SearchLocal</title><style>
:root{color-scheme:light dark;font:15px system-ui,-apple-system,sans-serif}body{max-width:960px;margin:32px auto;padding:0 18px;background:#10131a;color:#e7eaf0}h1{margin-bottom:4px}p{color:#aeb7c6}.card{background:#191e28;border:1px solid #2d3544;border-radius:12px;padding:18px;margin:14px 0}label{display:block;margin:10px 0 5px;color:#c5ccda}input,textarea{box-sizing:border-box;width:100%;background:#0e1218;color:inherit;border:1px solid #3a4558;border-radius:7px;padding:10px;font:inherit}textarea{min-height:86px;resize:vertical}.row{display:flex;gap:12px;align-items:end}.row>*{flex:1}button{background:#6ea8fe;color:#10131a;border:0;border-radius:7px;padding:10px 15px;font-weight:650;cursor:pointer;margin-top:12px}button:hover{filter:brightness(1.08)}.check{display:flex;align-items:center;gap:8px}.check input{width:auto}.out{white-space:pre-wrap;overflow-wrap:anywhere;background:#0b0e13;border-radius:8px;padding:14px;min-height:100px;max-height:360px;overflow:auto}.muted{font-size:13px;color:#91a0b5}#status{color:#9bbcff}
</style></head><body><h1>SearchLocal</h1><p>Local file indexing, full-text search, and grounded questions with Ollama.</p>
<section class="card"><label for="script">Project script path</label><input id="script" placeholder="/path/to/SearchLocal/searchlocal.py"><div class="muted">Select the existing searchlocal.py file by entering its full path.</div>
<label for="limit">Result limit</label><input id="limit" type="number" min="1" max="50" value="8"></section>
<section class="card"><h2>Index</h2><label for="roots">Files or folders (one full path per line)</label><textarea id="roots" placeholder="/Users/you/Documents"></textarea><label class="check"><input id="force" type="checkbox">Reindex unchanged files</label><button onclick="run('index')">Index roots</button><button onclick="run('stats')">Show index stats</button></section>
<section class="card"><h2>Search</h2><label for="query">Search query</label><input id="query" placeholder="e.g. project architecture"><label class="check"><input id="spotlight" type="checkbox">Also search filenames with macOS Spotlight</label><button onclick="run('search')">Search content</button></section>
<section class="card"><h2>Ask Ollama</h2><label for="question">Question about indexed files</label><textarea id="question" placeholder="What does my project do?"></textarea><div class="row"><div><label for="model">Model</label><input id="model" value="qwen2.5:7b"></div><div><label for="url">Ollama URL</label><input id="url" value="http://localhost:11434"></div></div><button onclick="run('ask')">Ask with local context</button></section>
<section class="card"><h2>Output <span id="status"></span></h2><pre class="out" id="out">Choose the project script path to get started.</pre></section>
<script>
async function run(action){const payload={action,script:document.querySelector('#script').value.trim(),limit:Number(document.querySelector('#limit').value)||8,spotlight:document.querySelector('#spotlight').checked};if(action==='index'){payload.roots=document.querySelector('#roots').value.split('\n').map(s=>s.trim()).filter(Boolean);payload.force=document.querySelector('#force').checked}else if(action==='search'){payload.query=document.querySelector('#query').value.trim()}else if(action==='ask'){payload.question=document.querySelector('#question').value.trim();payload.model=document.querySelector('#model').value.trim();payload.ollama_url=document.querySelector('#url').value.trim()}document.querySelector('#status').textContent=' · Working…';document.querySelector('#out').textContent='Running locally…';try{const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await r.json();document.querySelector('#out').textContent=data.output||data.error||'(no output)';document.querySelector('#status').textContent=' · '+(data.ok?'Done':'Error')}catch(e){document.querySelector('#out').textContent=String(e);document.querySelector('#status').textContent=' · Error'}}
</script></body></html>'''

PAGE = Path(__file__).with_name("searchlocal_dashboard.html").read_text(encoding="utf-8")
HEALTH_PAGE = Path(__file__).with_name("searchlocal_health.html").read_text(encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route == "/api/health":
            try:
                self._json(200, self.health_snapshot())
            except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as exc:
                self._json(500, {"error": str(exc)})
            return
        if route == "/api/model":
            self._json(200, self.model_snapshot())
            return
        if route not in ("/", "/health"):
            self.send_error(404)
            return
        body = (HEALTH_PAGE if route == "/health" else PAGE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def health_snapshot(self) -> dict:
        if platform.system() != "Darwin":
            raise RuntimeError("Mac Health is available when SearchLocal runs on macOS.")

        total_memory = int(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
        vm_text = subprocess.check_output(["vm_stat"], text=True)
        page_match = re.search(r"page size of (\d+) bytes", vm_text)
        if not page_match:
            raise RuntimeError("Could not read macOS virtual memory statistics.")
        page_size = int(page_match.group(1))
        pages: dict[str, int] = {}
        for line in vm_text.splitlines():
            match = re.match(r"Pages ([\w ]+):\s*([\d.]+)", line)
            if match:
                pages[match.group(1).strip().lower()] = int(match.group(2).replace(".", ""))
        available_pages = sum(pages.get(k, 0) for k in ("free", "inactive", "speculative", "purgeable"))
        available_memory = min(total_memory, available_pages * page_size)
        used_memory = max(0, total_memory - available_memory)

        data_volume = Path("/System/Volumes/Data")
        disk_path = data_volume if data_volume.exists() else Path.home()
        disk = shutil.disk_usage(disk_path)

        processes = []
        process_message = ""
        try:
            ps_output = subprocess.check_output(["ps", "-axo", "pid=,%cpu=,rss=,comm="], text=True)
            for line in ps_output.splitlines():
                parts = line.strip().split(None, 3)
                if len(parts) < 4:
                    continue
                try:
                    processes.append({"pid": int(parts[0]), "cpu": max(0, float(parts[1])), "memory": int(parts[2]) * 1024,
                                      "name": Path(parts[3]).name or parts[3]})
                except ValueError:
                    continue
        except (OSError, subprocess.SubprocessError):
            process_message = "This runtime cannot read the macOS process table. Open Activity Monitor (Applications → Utilities) to inspect per-app CPU and RAM."
        by_memory = sorted(processes, key=lambda p: p["memory"], reverse=True)[:8]
        by_cpu = sorted(processes, key=lambda p: p["cpu"], reverse=True)[:8]
        return {
            "ram": {"total": total_memory, "used": used_memory, "available": available_memory,
                    "used_percent": round(used_memory * 100 / total_memory) if total_memory else 0},
            "disk": {"total": disk.total, "used": disk.used, "free": disk.free,
                     "used_percent": round(disk.used * 100 / disk.total) if disk.total else 0,
                     "location": str(disk_path)},
            "processes_by_memory": by_memory,
            "processes_by_cpu": by_cpu,
            "processes_message": process_message,
            "updated_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if route not in ("/api/run", "/api/chat"):
            self._json(404, {"ok": False, "error": "Not found"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(size))
            result = self.local_chat(data) if route == "/api/chat" else self.run_command(data)
            self._json(200, result)
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            self._json(400, {"ok": False, "error": str(exc)})

    @staticmethod
    def _ollama_request(path: str, payload: dict | None = None) -> dict:
        # Deliberately fixed to this Mac. Never accept a remote model host from the UI.
        request = urllib.request.Request(
            "http://127.0.0.1:11434" + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json"},
            method="POST" if payload is not None else "GET",
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read())

    def model_snapshot(self) -> dict:
        try:
            data = self._ollama_request("/api/tags")
            models = [m.get("name", "") for m in data.get("models", [])
                      if m.get("name") and not re.search(r"embed|rerank", m.get("name", ""), re.I)]
            return {"available": True, "models": models}
        except (OSError, urllib.error.URLError, ValueError) as exc:
            return {"available": False, "models": [], "message": "Ollama is not responding on this Mac yet."}

    def local_chat(self, data: dict) -> dict:
        message = str(data.get("message", "")).strip()
        model = str(data.get("model", "")).strip()
        if not message:
            raise ValueError("Write a message first.")
        if not model or len(model) > 100:
            raise ValueError("Choose an installed local model.")
        history = data.get("history", [])
        if not isinstance(history, list):
            history = []
        messages = [{"role": "system", "content": "You are a helpful Mac assistant. Be clear and concise. You run locally and cannot claim to perform actions unless an available tool actually did them."}]
        for item in history[-12:]:
            if isinstance(item, dict) and item.get("role") in ("user", "assistant"):
                messages.append({"role": item["role"], "content": str(item.get("content", ""))[:8000]})
        messages.append({"role": "user", "content": message[:8000]})
        try:
            result = self._ollama_request("/api/chat", {"model": model, "messages": messages, "stream": False})
        except (OSError, urllib.error.URLError) as exc:
            raise ValueError("Could not reach Ollama at 127.0.0.1:11434. Start Ollama and choose an installed model.") from exc
        return {"ok": True, "reply": result.get("message", {}).get("content", "")}

    def run_command(self, data: dict) -> dict:
        script = Path(str(data.get("script", ""))).expanduser().resolve()
        if not script.is_file() or script.name != "searchlocal.py":
            raise ValueError("Choose the full path to the project's searchlocal.py file.")
        action = data.get("action")
        command = [sys.executable, str(script)]
        if action == "stats":
            command.append("stats")
        elif action == "index":
            roots = [str(Path(p).expanduser()) for p in data.get("roots", []) if str(p).strip()]
            if not roots:
                raise ValueError("Enter at least one file or folder path to index.")
            command += ["index", *roots]
            if data.get("force"):
                command.append("--force")
        elif action in ("search", "ask", "agent", "tools"):
            value = str(data.get("query" if action == "search" else "question", "")).strip()
            if action == "tools":
                command.append("tools")
            elif not value:
                raise ValueError("Enter a query first.")
            elif action == "agent":
                command += ["agent", value]
            else:
                command += [action, value, "--limit", str(max(1, min(50, int(data.get("limit", 8)))))]
            if action == "search":
                command += ["--mode", str(data.get("mode", "hybrid"))]
            if action == "ask":
                command += ["--model", str(data.get("model", "phi4")), "--ollama-url", "http://127.0.0.1:11434"]
            if action == "ask" and data.get("agent"):
                command.append("--agent")
            if data.get("spotlight"):
                command.append("--spotlight")
        else:
            raise ValueError("Unknown action.")
        result = subprocess.run(command, cwd=script.parent, capture_output=True, text=True, timeout=3600)
        output = result.stdout + (("\n" if result.stdout else "") + result.stderr if result.stderr else "")
        if result.returncode:
            output += f"\nCommand exited with status {result.returncode}."
        return {"ok": result.returncode == 0, "output": output}

    def _json(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Macmate local assistant running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
