"""Loopback-only asset host for the Holo gesture deck embedded in JARVIS."""
from __future__ import annotations

import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


_ASSET_ROOT = Path(__file__).resolve().parent.parent / "assets" / "holo"
_SAMPLE_ROOT = _ASSET_ROOT / "sample-notes"
_MAX_BODY = 16 * 1024


def _sample_tree(root: Path) -> list[dict]:
    """Read a chosen local notes folder, limited to Markdown and text files."""
    out: list[dict] = []
    try:
        folders = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
        loose = sorted(p for p in root.iterdir()
                       if p.is_file() and p.suffix.lower() in {".md", ".txt"})
        if loose:
            folders.append(None)
        for folder in folders:
            path = folder if folder is not None else root
            notes = []
            files = sorted(path.glob("*.md")) + sorted(path.glob("*.txt"))
            for file in files[:14]:
                try:
                    lines = [line for line in file.read_text(encoding="utf-8", errors="replace").splitlines()
                             if line.strip()]
                except OSError:
                    continue
                title = (lines[0].lstrip("# ").strip() if lines else file.stem)[:48] or file.stem
                body = "\n".join(line for line in lines[1:] if not line.startswith("#"))
                notes.append({"name": file.name, "title": title, "body": body[:420],
                              "full": "\n".join(lines[1:])[:4000]})
            if notes:
                out.append({"kind": "folder", "name": (folder.name if folder else "NOTES").upper()[:22],
                            "files": notes})
    except OSError:
        pass
    return out


class HoloGestureServer:
    """Serve bundled assets and the selected notes folder only on loopback."""

    def __init__(self, event_callback=None):
        owner = self
        self._notes_root = _SAMPLE_ROOT

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _send(self, status: int, body: bytes, content_type: str):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store" if content_type.startswith("text/html") else "public, max-age=3600")
                if content_type.startswith("text/html"):
                    self.send_header(
                        "Content-Security-Policy",
                        "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' blob:; "
                        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                        "media-src 'self' blob:; worker-src 'self' blob:; connect-src 'self'; frame-src 'self' blob:",
                    )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = unquote(urlsplit(self.path).path)
                if path in {"/", "/holo.html"}:
                    target = _ASSET_ROOT / "holo.html"
                elif path == "/api/tree":
                    self._send(200, json.dumps(_sample_tree(owner._notes_root)).encode("utf-8"), "application/json; charset=utf-8")
                    return
                elif path == "/api/props":
                    props = sorted(p.name for p in (_ASSET_ROOT / "props").glob("*.glb"))[:6]
                    self._send(200, json.dumps(props).encode("utf-8"), "application/json; charset=utf-8")
                    return
                elif path.startswith(("/vendor/", "/props/")):
                    target = (_ASSET_ROOT / path.lstrip("/")).resolve()
                    if not target.is_relative_to(_ASSET_ROOT.resolve()):
                        self._send(404, b"not found", "text/plain; charset=utf-8")
                        return
                else:
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                try:
                    body = target.read_bytes()
                except OSError:
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                if target.suffix == ".mjs":
                    content_type = "text/javascript"
                elif target.suffix == ".wasm":
                    content_type = "application/wasm"
                elif target.suffix == ".task":
                    content_type = "application/octet-stream"
                elif target.suffix == ".glb":
                    content_type = "model/gltf-binary"
                if target.name == "holo.html":
                    content_type = "text/html; charset=utf-8"
                self._send(200, body, content_type)

            def do_POST(self):
                path = urlsplit(self.path).path
                if path not in {"/api/state", "/api/diag"}:
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                try:
                    length = max(0, int(self.headers.get("Content-Length", "0")))
                    if length > _MAX_BODY:
                        self._send(413, b"request too large", "text/plain; charset=utf-8")
                        return
                    data = json.loads(self.rfile.read(length) or b"{}")
                    event = str(data.get("event", ""))[:32]
                    card = str(data.get("card", ""))[:96]
                    if path == "/api/state" and event and owner._event_callback:
                        owner._event_callback(event, card)
                except Exception:
                    pass
                self._send(200, b'{"ok":true}', "application/json; charset=utf-8")

        self._event_callback = event_callback
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="jarvis-holo-assets", daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_port}/"

    def set_notes_directory(self, path: str | Path) -> bool:
        """Switch the Holo deck to a user-chosen local folder for this session."""
        try:
            selected = Path(path).expanduser().resolve(strict=True)
            if not selected.is_dir():
                return False
        except (OSError, RuntimeError, TypeError):
            return False
        self._notes_root = selected
        return True

    def close(self):
        server = getattr(self, "_server", None)
        if server is not None:
            self._server = None
            server.shutdown()
            server.server_close()
