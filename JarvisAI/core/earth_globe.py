"""In-window interactive globe, place search, and OSM-based route display."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from urllib.parse import quote

from PyQt6.QtCore import QObject, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

try:
    from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PyQt6.QtWebEngineWidgets import QWebEngineView
except ImportError:  # Keep the rest of the desktop UI usable before setup is run.
    QWebEnginePage = QWebEngineProfile = QWebEngineView = None


_PLACE_CACHE: dict[str, dict] = {}
_PLACE_CACHE_LOCK = threading.Lock()
_NOMINATIM_LOCK = threading.Lock()
_ROUTER_LOCK = threading.Lock()
_LAST_NOMINATIM_REQUEST = 0.0
_LAST_ROUTER_REQUEST = 0.0


class _LookupResult(QObject):
    ready = pyqtSignal(object)


class _JarvisEarthPage(QWebEnginePage if QWebEnginePage else object):
    """Keep map navigation embedded; only explicit external links leave JARVIS."""

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        if url.host() == "jarvis.local" or (is_main_frame and url.scheme() == "data"):
            return True
        if url.scheme() in {"https", "http"}:
            QDesktopServices.openUrl(url)
        return False


class EarthGlobePanel(QWidget):
    """MapLibre 3D globe and detailed maps embedded in the PyQt HUD."""

    close_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:#071116; color:#c2d2ce;")
        self._lookup_signal = _LookupResult(self)
        self._lookup_signal.ready.connect(self._on_lookup_result)
        self._lookup_thread: threading.Thread | None = None
        self._page = None
        self._profile = None
        self._view = None
        self._status = None
        self._origin = None
        self._destination = None
        self._map_ready = False
        self._html_loaded = False
        self._pending_js: list[tuple[str, tuple]] = []
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("◉  EARTH / GLOBAL MAP")
        title.setStyleSheet("color:#71d4c2;font:bold 10pt 'Consolas';")
        header.addWidget(title)
        header.addStretch(1)

        self._origin = self._line_edit("FROM — ROUTE START")
        self._destination = self._line_edit("CITY, LANDMARK, OR COUNTRY")
        header.addWidget(self._origin, stretch=1)
        header.addWidget(self._destination, stretch=1)
        locate = self._button("GO TO")
        locate.clicked.connect(self.locate_query)
        header.addWidget(locate)
        route = self._button("ROUTE")
        route.clicked.connect(self.route_query)
        header.addWidget(route)
        self._rotation_button = self._button("PAUSE ROTATION")
        self._rotation_button.clicked.connect(self._toggle_rotation)
        header.addWidget(self._rotation_button)
        close = self._button("CLOSE")
        close.clicked.connect(self.close_requested.emit)
        header.addWidget(close)
        root.addLayout(header)

        modes = QHBoxLayout()
        modes.addStretch(1)
        street = self._button("STREETS")
        street.clicked.connect(lambda: self._set_map_style("streets"))
        modes.addWidget(street)
        satellite = self._button("SATELLITE")
        satellite.clicked.connect(lambda: self._set_map_style("satellite"))
        modes.addWidget(satellite)
        root.addLayout(modes)

        if QWebEngineView is None:
            self._view = QLabel(
                "Detailed Earth maps need Qt WebEngine.\n"
                "Close JARVIS, then run: python -m pip install PyQt6-WebEngine"
            )
            self._view.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._view.setStyleSheet("color:#c5d8d5;background:#0b171b;font:11pt 'Consolas';")
            root.addWidget(self._view, stretch=1)
        else:
            self._view = QWebEngineView(self)
            self._view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self._profile = QWebEngineProfile("jarvis-earth", self)
            cache_dir = Path(__file__).resolve().parents[1] / "config" / "earth_map_cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            self._profile.setCachePath(str(cache_dir))
            self._profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
            self._profile.setHttpUserAgent(
                "JARVIS-AI-Desktop/1.0 (+https://github.com/Ninja2007S/Jarvis-AI)"
            )
            self._page = _JarvisEarthPage(self._profile, self._view)
            self._view.setPage(self._page)
            self._page.titleChanged.connect(self._on_page_title)
            self._page.loadFinished.connect(self._on_page_loaded)
            root.addWidget(self._view, stretch=1)

        self._status = QLabel("EARTH / satellite globe  ·  Search a destination or drag to explore")
        self._status.setStyleSheet("color:#9aacaa;font:8pt 'Consolas';padding-left:4px;")
        root.addWidget(self._status)

    @staticmethod
    def _line_edit(placeholder: str) -> QLineEdit:
        field = QLineEdit()
        field.setPlaceholderText(placeholder)
        field.setMinimumWidth(130)
        field.setStyleSheet(
            "QLineEdit{background:#101c21;border:1px solid #39545a;padding:6px;"
            "color:#d6e8e3;font:9pt 'Consolas';}"
            "QLineEdit:focus{border-color:#71d4c2;}"
        )
        return field

    @staticmethod
    def _button(label: str) -> QPushButton:
        button = QPushButton(label)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(
            "QPushButton{background:#101c21;border:1px solid #39545a;color:#a8ccc5;"
            "padding:6px 9px;font:bold 8pt 'Consolas';}"
            "QPushButton:hover{border-color:#71d4c2;color:#71d4c2;}"
        )
        return button

    def _on_page_loaded(self, ok: bool):
        if not ok:
            self._status.setText("MAP ENGINE FAILED TO LOAD  ·  Check your internet connection and Qt WebEngine")

    def _on_page_title(self, title: str):
        if title == "JARVIS_EARTH_ERROR":
            self._status.setText("MAP ENGINE ERROR  ·  This computer may not support WebGL")
            return
        if title != "JARVIS_EARTH_READY":
            return
        self._map_ready = True
        pending, self._pending_js = self._pending_js, []
        for function, args in pending:
            self._run_js(function, *args)

    def _js(self, function: str, *args):
        if self._page is None:
            return
        if not self._map_ready:
            self._pending_js.append((function, args))
            return
        self._run_js(function, *args)

    def _run_js(self, function: str, *args):
        payload = ",".join(json.dumps(arg, ensure_ascii=True) for arg in args)
        self._page.runJavaScript(f"window.jarvisEarth && window.jarvisEarth.{function}({payload});")

    def open_view(self, destination: str = "", origin: str = "", mode: str = "show"):
        self.show()
        self._ensure_map_loaded()
        self._origin.setText(origin.strip())
        self._destination.setText(destination.strip())
        if mode == "route":
            if not origin.strip():
                self._status.setText("ROUTE START NEEDED  ·  Enter a start location, then press ROUTE")
                self._origin.setFocus()
            elif destination.strip():
                self.route_query()
        elif destination.strip():
            self.locate_query()
        else:
            self._js("resetGlobe")
            self._rotation_button.setText("PAUSE ROTATION")
            self._status.setText("EARTH / satellite globe  ·  Search a destination or drag to explore")

    def _ensure_map_loaded(self):
        if self._page is None or self._html_loaded:
            return
        self._html_loaded = True
        html_path = Path(__file__).with_name("earth_globe.html")
        self._view.setHtml(html_path.read_text(encoding="utf-8"), QUrl("https://jarvis.local/earth"))

    def locate_query(self):
        if self._page is None:
            self._status.setText("Install the map component with: python -m pip install PyQt6-WebEngine")
            return
        destination = self._destination.text().strip()
        if not destination:
            self._status.setText("Enter a city, landmark, or country.")
            self._destination.setFocus()
            return
        self._start_lookup("locate", "", destination)

    def route_query(self):
        if self._page is None:
            self._status.setText("Install the map component with: python -m pip install PyQt6-WebEngine")
            return
        origin = self._origin.text().strip()
        destination = self._destination.text().strip()
        if not origin:
            self._status.setText("Enter a route start each time, then press ROUTE.")
            self._origin.setFocus()
            return
        if not destination:
            self._status.setText("Enter a route destination.")
            self._destination.setFocus()
            return
        self._start_lookup("route", origin, destination)

    def _start_lookup(self, mode: str, origin: str, destination: str):
        if self._lookup_thread and self._lookup_thread.is_alive():
            self._status.setText("A map lookup is already running.")
            return
        self._status.setText(
            f"LOOKING UP {origin[:45] + ' → ' if origin else ''}{destination[:75]}"
        )
        self._lookup_thread = threading.Thread(
            target=self._lookup_worker, args=(mode, origin, destination),
            daemon=True, name="EarthMapLookup",
        )
        self._lookup_thread.start()

    @staticmethod
    def _geocode(query: str) -> dict:
        key = query.casefold()
        with _PLACE_CACHE_LOCK:
            cached = _PLACE_CACHE.get(key)
        if cached:
            return cached

        global _LAST_NOMINATIM_REQUEST
        with _NOMINATIM_LOCK:
            wait = 1.05 - (time.monotonic() - _LAST_NOMINATIM_REQUEST)
            if wait > 0:
                time.sleep(wait)
            _LAST_NOMINATIM_REQUEST = time.monotonic()

        import requests
        response = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "jsonv2", "limit": 1},
            headers={
                "User-Agent": "JARVIS-AI-Desktop/1.0 (+https://github.com/Ninja2007S/Jarvis-AI)",
                "Accept": "application/json",
            },
            timeout=(5, 15),
        )
        response.raise_for_status()
        results = response.json()
        if not results:
            raise ValueError(f"No match for {query}.")
        row = results[0]
        place = {
            "query": query,
            "name": str(row.get("display_name") or query),
            "lat": float(row["lat"]),
            "lon": float(row["lon"]),
            "kind": str(row.get("addresstype") or row.get("type") or "place"),
            "bbox": [float(value) for value in row.get("boundingbox", [])],
        }
        with _PLACE_CACHE_LOCK:
            _PLACE_CACHE[key] = place
        return place

    @staticmethod
    def _route(start: dict, end: dict) -> dict:
        global _LAST_ROUTER_REQUEST
        with _ROUTER_LOCK:
            wait = 1.05 - (time.monotonic() - _LAST_ROUTER_REQUEST)
            if wait > 0:
                time.sleep(wait)
            _LAST_ROUTER_REQUEST = time.monotonic()
        import requests
        coords = f"{start['lon']},{start['lat']};{end['lon']},{end['lat']}"
        response = requests.get(
            f"https://routing.openstreetmap.de/routed-car/route/v1/driving/{quote(coords, safe=',;')}",
            params={"overview": "full", "geometries": "geojson", "steps": "false"},
            headers={
                "User-Agent": "JARVIS-AI-Desktop/1.0 (+https://github.com/Ninja2007S/Jarvis-AI)",
                "Accept": "application/json",
            },
            timeout=(5, 25),
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != "Ok" or not payload.get("routes"):
            raise ValueError("No driving route was returned for those locations.")
        best = payload["routes"][0]
        return {
            "geometry": best["geometry"],
            "distance_m": float(best["distance"]),
            "duration_s": float(best["duration"]),
            "start": start,
            "end": end,
        }

    def _lookup_worker(self, mode: str, origin: str, destination: str):
        try:
            end = self._geocode(destination)
            if mode == "locate":
                self._lookup_signal.ready.emit({"mode": "locate", "destination": end})
                return
            start = self._geocode(origin)
            route = self._route(start, end)
            self._lookup_signal.ready.emit({"mode": "route", "route": route})
        except Exception as exc:
            self._lookup_signal.ready.emit({"mode": mode, "error": str(exc)[:200]})

    def _on_lookup_result(self, result: dict):
        if result.get("error"):
            self._status.setText(f"MAP REQUEST FAILED  ·  {result['error']}")
            return
        if result["mode"] == "locate":
            place = result["destination"]
            kind = place["kind"].casefold()
            zoom = 5.2 if kind in {"country", "state", "region", "continent"} else 12.4
            if kind in {"city", "municipality", "town"}:
                zoom = 11.2
            elif kind in {"house", "building", "road", "street", "address", "amenity", "shop"}:
                zoom = 16.0
            self._js("focusPlace", place, zoom)
            self._rotation_button.setText("RESUME ROTATION")
            self._status.setText(f"LOCKED ON  ·  {place['name'][:120]}")
            return

        route = result["route"]
        self._js("showRoute", route)
        self._rotation_button.setText("RESUME ROTATION")
        distance_km = route["distance_m"] / 1000.0
        minutes = max(1, round(route["duration_s"] / 60.0))
        self._status.setText(
            f"DRIVING ROUTE  ·  {route['start']['name'][:48]} → "
            f"{route['end']['name'][:48]}  ·  {distance_km:.1f} km  ·  ~{minutes} min"
        )
        self._origin.clear()  # Each new route requires a fresh starting point.

    def _toggle_rotation(self):
        if not self._map_ready:
            return
        self._page.runJavaScript(
            "window.jarvisEarth && window.jarvisEarth.toggleRotation();",
            lambda running: self._rotation_button.setText(
                "PAUSE ROTATION" if running else "RESUME ROTATION"
            ),
        )

    def _set_map_style(self, style: str):
        self._js("setBase", style)
