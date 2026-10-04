"""Loopback-only asset host for the Holo gesture deck embedded in JARVIS."""
from __future__ import annotations

import json
import mimetypes
import ipaddress
import re
import socket
import threading
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


_ASSET_ROOT = Path(__file__).resolve().parent.parent / "assets" / "holo"
_SAMPLE_ROOT = _ASSET_ROOT / "sample-notes"
_MAX_BODY = 16 * 1024
_MAX_ARTICLE_BYTES = 1_500_000
_NEWS_HOSTS = {
    "newsfirst.lk", "www.newsfirst.lk", "english.newsfirst.lk",
    "adaderana.lk", "www.adaderana.lk", "sinhala.adaderana.lk",
}

_FEATURES = (
    ("video", "MEDIA PLAYER", "Open videos and control playback."),
    ("web", "WEB / ARTICLE", "Read a web page or saved article in the HUD."),
    ("images", "IMAGE GALLERY", "Browse image search and screenshot results."),
    ("document", "DOCUMENTS", "Read PDF and document previews with summaries."),
    ("news", "LIVE HEADLINES", "See current Newsfirst and Ada Derana headlines."),
    ("newsfirst", "NEWSFIRST WEBSITE", "Open Newsfirst inside a floating Holo window."),
    ("adaderana", "ADA DERANA WEBSITE", "Open Ada Derana inside a floating Holo window."),
    ("email", "EMAIL", "Open unread mail and email cards."),
    ("calendar", "CALENDAR", "Open today's events and agenda timeline."),
    ("system", "SYSTEM STATUS", "View CPU, GPU, memory, battery and charts."),
    ("weather", "WEATHER", "Open current conditions and forecast cards."),
    ("flights", "FLIGHT FINDER", "Review flight results and route details."),
    ("timers", "TIMERS / REMINDERS", "Open active countdowns and reminders."),
    ("market", "MARKET WATCH", "Open the stock and crypto chart panel."),
    ("earth", "EARTH / MAP", "Explore the globe and locate destinations."),
)
_MEDIA_FEATURES = {"video", "web", "images", "document", "news", "newsfirst", "adaderana"}


def _is_publisher_url(value: str) -> bool:
    """Allow article fetches only from the two configured Sri Lankan publishers."""
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme != "https" or host not in _NEWS_HOSTS
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            return False
        records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return bool(records) and all(ipaddress.ip_address(row[4][0]).is_global for row in records)
    except (OSError, ValueError):
        return False


class _PublisherRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_publisher_url(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _ArticleText(HTMLParser):
    """Extract plain article text; remote markup never executes in the Holo page."""
    _SKIP = {"script", "style", "svg", "nav", "header", "footer", "aside", "form", "noscript"}
    _BLOCKS = {"h1", "h2", "h3", "p", "li", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.lines: list[str] = []
        self._skip_depth = 0
        self._block_tag = ""
        self._buffer: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if tag == "meta":
            field = (values.get("name") or values.get("property") or "").lower()
            if field in {"description", "og:description", "twitter:description"} and not self.description:
                self.description = values.get("content", "").strip()
            if field in {"og:title", "twitter:title"} and not self.title:
                self.title = values.get("content", "").strip()
        elif tag == "title":
            self._in_title = True
        elif tag in self._BLOCKS and not self._block_tag:
            self._block_tag = tag
            self._buffer = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        if tag == self._block_tag:
            line = " ".join("".join(self._buffer).split())
            if len(line) > 20:
                self.lines.append(line)
            self._block_tag = ""
            self._buffer = []

    def handle_data(self, data):
        if self._skip_depth:
            return
        value = " ".join(data.split())
        if not value:
            return
        if self._in_title and not self.title:
            self.title = value[:180]
        if self._block_tag:
            self._buffer.append(value)


def _fetch_publisher_article(url: str) -> dict:
    if not _is_publisher_url(url):
        raise ValueError("Only Newsfirst and Ada Derana article links are supported.")
    request = Request(url, headers={
        "User-Agent": "JARVIS Desktop/1.0 (local Holo article reader)",
        "Accept": "text/html,application/xhtml+xml",
    })
    opener = build_opener(_PublisherRedirects())
    with opener.open(request, timeout=9) as response:
        final_url = response.geturl()
        if not _is_publisher_url(final_url):
            raise ValueError("The publisher redirected to an unsupported site.")
        if not response.headers.get_content_type().startswith("text/html"):
            raise ValueError("The publisher returned a non-article page.")
        raw = response.read(_MAX_ARTICLE_BYTES + 1)
        if len(raw) > _MAX_ARTICLE_BYTES:
            raise ValueError("The article page is too large to display.")
        charset = response.headers.get_content_charset() or "utf-8"
    parser = _ArticleText()
    parser.feed(raw.decode(charset, errors="replace"))
    lines = list(dict.fromkeys(parser.lines))
    text = "\n\n".join(lines)
    if len(text) < 80:
        text = parser.description or text
    if not text:
        raise ValueError("Could not extract readable article text from this page.")
    host = (urlsplit(final_url).hostname or "Publisher").replace("www.", "")
    return {"title": parser.title[:180], "source": host, "url": final_url, "text": text[:24000]}


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
        self._news_items: list[dict] = []
        self._news_images: dict[int, tuple[bytes, str]] = {}
        self._news_lock = threading.RLock()

        def _tree_payload():
            tree = _sample_tree(owner._notes_root)
            for name, keys in (("MEDIA & READING", _MEDIA_FEATURES), ("LIVE & SYSTEM", None)):
                tree.append({
                    "kind": "folder",
                    "name": name,
                    "files": [
                        {"title": title, "body": detail, "full": detail, "feature": key}
                        for key, title, detail in _FEATURES
                        if (key in keys if keys is not None else key not in _MEDIA_FEATURES)
                    ],
                })
            return tree

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _send(self, status: int, body: bytes, content_type: str):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("X-Content-Type-Options", "nosniff")
                dynamic = content_type.startswith(("text/html", "application/json", "image/"))
                self.send_header("Cache-Control", "no-store" if dynamic else "public, max-age=3600")
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
                request_url = urlsplit(self.path)
                path = unquote(request_url.path)
                if path in {"/", "/holo.html"}:
                    target = _ASSET_ROOT / "holo.html"
                elif path == "/api/tree":
                    self._send(200, json.dumps(_tree_payload()).encode("utf-8"), "application/json; charset=utf-8")
                    return
                elif path == "/api/news":
                    with owner._news_lock:
                        payload = [
                            {key: value for key, value in item.items() if key != "image_bytes"}
                            | ({"image": f"/api/news-image/{index}"} if index in owner._news_images else {})
                            for index, item in enumerate(owner._news_items)
                        ]
                    self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                    return
                elif path == "/api/article":
                    article_url = parse_qs(request_url.query).get("url", [""])[0][:1500]
                    try:
                        article = _fetch_publisher_article(article_url)
                        body = json.dumps(article, ensure_ascii=False).encode("utf-8")
                        self._send(200, body, "application/json; charset=utf-8")
                    except (ValueError, HTTPError, URLError, OSError, TimeoutError) as exc:
                        body = json.dumps({"error": str(exc)[:240]}, ensure_ascii=False).encode("utf-8")
                        self._send(422, body, "application/json; charset=utf-8")
                    except Exception:
                        body = b'{"error":"Could not load the publisher article."}'
                        self._send(502, body, "application/json; charset=utf-8")
                    return
                elif path.startswith("/api/news-image/"):
                    try:
                        index = int(path.rsplit("/", 1)[-1])
                    except ValueError:
                        self._send(404, b"not found", "text/plain; charset=utf-8")
                        return
                    with owner._news_lock:
                        image = owner._news_images.get(index)
                    if image is None:
                        self._send(404, b"not found", "text/plain; charset=utf-8")
                        return
                    self._send(200, image[0], image[1])
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

    def set_news_items(self, items) -> None:
        """Publish display-safe headline metadata and clear stale local images."""
        clean = []
        for item in list(items or [])[:12]:
            if not isinstance(item, dict):
                continue
            title = " ".join(str(item.get("title") or "").split())[:180]
            if not title:
                continue
            clean.append({
                "title": title,
                "detail": " ".join(str(item.get("detail") or item.get("snippet") or "").split())[:500],
                "url": str(item.get("url") or "")[:1000],
                "source": str(item.get("source") or "")[:60],
            })
        with self._news_lock:
            self._news_items = clean
            self._news_images.clear()

    def set_news_image(self, index: int, data: bytes, content_type: str) -> None:
        """Cache a small, validated publisher image for the loopback viewer."""
        if content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            return
        if not data or len(data) > 2_000_000:
            return
        with self._news_lock:
            if 0 <= index < len(self._news_items):
                self._news_images[index] = (bytes(data), content_type)

    def close(self):
        server = getattr(self, "_server", None)
        if server is not None:
            self._server = None
            server.shutdown()
            server.server_close()
