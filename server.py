"""Local-only HTTP interface for the folder analyzer."""

from __future__ import annotations

import argparse
import json
import os
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from scanner import ScanManager

WEB_DIR = Path(__file__).resolve().parent / "web"
STATIC_TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript",
                ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}


class AnalyzerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], manager: ScanManager | None = None):
        self.manager = manager or ScanManager()
        super().__init__(address, AnalyzerHandler)

    def server_close(self) -> None:
        super().server_close()
        self.manager.close()


class AnalyzerHandler(BaseHTTPRequestHandler):
    server: AnalyzerServer

    def _authorized(self) -> bool:
        host = self.headers.get("Host", "")
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if host.lower() not in allowed:
            self._json(403, {"error": "Invalid host."})
            return False
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in {f"http://{value}" for value in allowed}:
            self._json(403, {"error": "Invalid origin."})
            return False
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            self._json(403, {"error": "Cross-site requests are blocked."})
            return False
        return True

    def _json(self, status: int, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _params(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urllib.parse.urlsplit(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

    def do_GET(self) -> None:
        if not self._authorized():
            return
        path, params = self._params()
        manager = self.server.manager
        try:
            if path == "/api/health":
                self._json(200, {"status": "ok", "app": "folder-analyzer"})
            elif path == "/api/status":
                self._json(200, manager.snapshot())
            elif path == "/api/config":
                self._json(200, {"default_path": "C:\\Tools" if os.name == "nt" else str(Path.home())})
            elif path == "/api/browse":
                sort = _single(params, "sort", "size")
                order = _single(params, "order", "desc")
                if sort not in ("size", "name") or order not in ("asc", "desc"):
                    raise ValueError("Invalid sort or order.")
                self._json(200, manager.browse(_single(params, "path", ""), sort, order,
                                                _number(params, "offset", 0), _number(params, "limit", 100)))
            elif path == "/api/breakdown":
                self._json(200, manager.breakdown(_single(params, "path", "")))
            elif path == "/api/files":
                self._json(200, manager.files(_single(params, "query", ""),
                                               _single(params, "extension", ""),
                                               _number(params, "offset", 0), _number(params, "limit", 100)))
            elif path == "/api/types":
                self._json(200, manager.types())
            elif path == "/api/errors":
                self._json(200, manager.errors())
            elif path == "/api/export":
                rows = manager.export_rows()
                first = next(rows)  # Resolve scan/index errors before sending headers.
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="folder-analyzer.csv"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(first)
                    for chunk in rows:
                        self.wfile.write(chunk)
                finally:
                    rows.close()
            elif path.startswith("/api/"):
                self._json(404, {"error": "Unknown endpoint."})
            else:
                self._static(path)
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except RuntimeError as exc:
            self._json(409, {"error": str(exc)})
        except LookupError as exc:
            self._json(404, {"error": str(exc)})
        except OSError as exc:
            self._json(500, {"error": str(exc)})

    def _static(self, path: str) -> None:
        relative = urllib.parse.unquote(path).lstrip("/") or "index.html"
        target = (WEB_DIR / relative).resolve()
        if not target.is_relative_to(WEB_DIR) or not target.is_file() or target.suffix not in STATIC_TYPES:
            self._json(404, {"error": "Not found."})
            return
        payload = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", STATIC_TYPES[target.suffix] + ("; charset=utf-8" if target.suffix in (".html", ".css", ".js") else ""))
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        if not self._authorized():
            return
        path, _ = self._params()
        if path not in ("/api/scan", "/api/cancel", "/api/pick-folder", "/api/trash"):
            self._json(404, {"error": "Unknown endpoint."})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._json(415, {"error": "JSON content type required."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 65536:
                raise ValueError("Invalid request size.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise TypeError("Expected a JSON object.")
            if path == "/api/scan":
                self._json(202, self.server.manager.start(data.get("path")))
            elif path == "/api/cancel":
                self._json(200, self.server.manager.cancel())
            elif path == "/api/trash":
                self._json(200, self.server.manager.trash_folder(data.get("path")))
            else:
                import tkinter as tk
                from tkinter import filedialog

                root = tk.Tk()
                root.withdraw()
                try:
                    selected = filedialog.askdirectory(parent=root, title="Choose folder to analyze")
                finally:
                    root.destroy()
                self._json(200, {"path": selected or ""})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except RuntimeError as exc:
            self._json(409, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - request boundary converts UI failures to JSON
            self._json(500, {"error": str(exc)})


def _single(params: dict[str, list[str]], key: str, default: str) -> str:
    return params.get(key, [default])[0]


def _number(params: dict[str, list[str]], key: str, default: int) -> int:
    try:
        value = int(_single(params, key, str(default)))
    except ValueError as exc:
        raise ValueError(f"Invalid {key}.") from exc
    if value < 0 or (key == "limit" and (value < 1 or value > 1000)):
        raise ValueError(f"Invalid {key}.")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze local folder storage")
    parser.add_argument("--port", type=int, default=8520)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server = AnalyzerServer(("127.0.0.1", args.port))
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Folder Analyzer: {url}")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
