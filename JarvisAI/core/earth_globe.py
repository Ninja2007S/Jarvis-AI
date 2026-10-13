"""In-window interactive globe, place search, and OSM-based route display."""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path
from urllib.parse import quote

from PyQt6.QtCore import QObject, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

try:
    from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PyQt6.QtWebEngineWidgets import QWebEngineView
except ImportError:  # Keep the rest of the desktop UI usable before setup is run.
    QWebEnginePage = QWebEngineProfile = QWebEngineView = None

try:
    from PyQt6 import sip
except Exception:  # pragma: no cover - depends on the local PyQt build
    sip = None


_PLACE_CACHE: dict[str, dict] = {}
_PLACE_CACHE_LOCK = threading.Lock()
_NOMINATIM_LOCK = threading.Lock()
_ROUTER_LOCK = threading.Lock()
_LAST_NOMINATIM_REQUEST = 0.0
_LAST_ROUTER_REQUEST = 0.0


class _LookupResult(QObject):
    ready = pyqtSignal(object)


class _FlightResult(QObject):
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
    flight_radar_updated = pyqtSignal(object)

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
        self._flight_result = _FlightResult(self)
        self._flight_result.ready.connect(self._on_flight_result)
        self._flight_location: tuple[float, float] | None = None
        self._flight_location_source = ""
        self._flight_radar_active = False
        self._last_flight_result: dict | None = None
        self._flight_fetch_thread: threading.Thread | None = None
        self._flight_last_fetch = 0.0
        self._flight_refresh_seconds = 300
        self._flight_refresh_timer = QTimer(self)
        self._flight_refresh_timer.setInterval(300_000)  # Stay within OpenSky's public daily credit budget.
        self._flight_refresh_timer.timeout.connect(self._refresh_flight_radar)
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
        flights = self._button("NEARBY FLIGHTS")
        flights.setToolTip("Use system location with permission; show nearby aircraft reported by OpenSky")
        flights.clicked.connect(self.show_nearby_flights)
        modes.addWidget(flights)
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
            # A profile must outlive every page that uses it. Parenting the
            # page to the profile gives Qt an explicit safe destruction order.
            self._page = _JarvisEarthPage(self._profile, self._profile)
            self._view.setPage(self._page)
            if hasattr(self._page, "featurePermissionRequested"):
                self._page.featurePermissionRequested.connect(self._handle_page_permission)
            self._page.titleChanged.connect(self._on_page_title)
            self._page.loadFinished.connect(self._on_page_loaded)
            root.addWidget(self._view, stretch=1)

        self._status = QLabel("EARTH / satellite globe  ·  Search a destination or drag to explore")
        self._status.setStyleSheet("color:#9aacaa;font:8pt 'Consolas';padding-left:4px;")
        root.addWidget(self._status)

    def shutdown_web_engine(self) -> None:
        """Release the map page before its dedicated WebEngine profile."""
        if self._page is None or self._profile is None or sip is None:
            return
        page, profile = self._page, self._profile
        try:
            page.triggerAction(QWebEnginePage.WebAction.Stop)
            if self._view is not None:
                self._view.setPage(QWebEnginePage(self._view))
            sip.delete(page)
            sip.delete(profile)
        except RuntimeError:
            # The Qt parent may already have begun destruction.
            pass
        finally:
            self._page = None
            self._profile = None

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
        if title.startswith("JARVIS_LOCATION:"):
            try:
                _, lat, lon = title.split(":", 2)
                latitude, longitude = float(lat), float(lon)
                if not (math.isfinite(latitude) and math.isfinite(longitude)
                        and -90 <= latitude <= 90 and -180 <= longitude <= 180):
                    raise ValueError("coordinates outside valid range")
                self._flight_location = (latitude, longitude)
                self._flight_location_source = "system"
                self._refresh_flight_radar()
            except (TypeError, ValueError):
                message = "LOCATION ERROR  ·  Could not read system coordinates"
                self._status.setText(message)
                self.flight_radar_updated.emit({"error": message})
            return
        if title.startswith("JARVIS_LOCATION_ERROR:"):
            self._flight_radar_active = False
            self._flight_refresh_timer.stop()
            message = "LOCATION UNAVAILABLE  ·  Enter an area in the Flights tab and choose USE AREA"
            self._status.setText(message)
            self.flight_radar_updated.emit({"error": message})
            return
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
        self.stop_flight_radar()
        self._ensure_map_loaded()
        self._origin.setText(origin.strip())
        self._destination.setText(destination.strip())
        if mode == "flights":
            self.show_nearby_flights()
        elif mode in {"route", "distance"}:
            if not origin.strip():
                prompt = ("START LOCATION NEEDED  ·  Enter where to measure from"
                          if mode == "distance" else
                          "ROUTE START NEEDED  ·  Enter a start location, then press ROUTE")
                self._status.setText(prompt)
                self._origin.setFocus()
            elif destination.strip():
                self._start_lookup(mode, origin.strip(), destination.strip())
        elif mode == "flight_route":
            if origin.strip() and destination.strip():
                self._start_lookup("flight_route", origin.strip(), destination.strip())
            else:
                self._status.setText("AIR ROUTE NEEDS AN ORIGIN AND DESTINATION")
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
        self.stop_flight_radar()
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
        self.stop_flight_radar()
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

    def show_nearby_flights(self):
        """Ask Qt for system location, then load an OpenSky aircraft snapshot."""
        if self._page is None:
            message = "Install PyQt6-WebEngine to use the live flight radar."
            self._status.setText(message)
            self.flight_radar_updated.emit({"error": message})
            return
        if self._flight_location is not None and not self._origin.text().strip():
            self._flight_radar_active = True
            if self._last_flight_result:
                self._js("showFlights", self._last_flight_result)
                self.flight_radar_updated.emit(self._last_flight_result)
            self._refresh_flight_radar()
            return
        self._flight_refresh_timer.stop()
        self._flight_radar_active = True
        manual_place = self._origin.text().strip()
        if manual_place:
            self._status.setText(f"LOCATING RADAR CENTER  ·  {manual_place[:75]}")
            self._start_lookup("flight_origin", "", manual_place)
            return
        self._status.setText("REQUESTING SYSTEM LOCATION  ·  JARVIS will ask before using your location")
        self._js("requestLocation")

    def start_flight_radar_in_background(self, manual_place: str = ""):
        """Fetch the nearby feed for the Flights tab without switching away from it."""
        if self._page is None:
            message = "Install PyQt6-WebEngine to use the live flight radar."
            self._status.setText(message)
            self.flight_radar_updated.emit({"error": message})
            return
        self._ensure_map_loaded()
        self._origin.setText(str(manual_place or "").strip())
        if manual_place:
            self.show_nearby_flights()
            return
        if self._flight_location_source == "manual":
            self._flight_location = None
            self._flight_location_source = ""
            self._last_flight_result = None
            self._flight_last_fetch = 0.0
        self.show_nearby_flights()

    def stop_flight_radar(self):
        self._flight_radar_active = False
        self._flight_refresh_timer.stop()

    def _handle_page_permission(self, origin, feature):
        if self._page is None or QWebEnginePage is None:
            return
        try:
            if feature != QWebEnginePage.Feature.Geolocation:
                self._page.setFeaturePermission(
                    origin, feature, QWebEnginePage.PermissionPolicy.PermissionDeniedByUser
                )
                return
            from PyQt6.QtWidgets import QMessageBox
            answer = QMessageBox.question(
                self,
                "JARVIS · Location for Flight Radar",
                "Allow JARVIS to use this device's system location to find aircraft within about 50 km?\n\n"
                "The coordinates are sent to OpenSky Network to request nearby aircraft. "
                "The radar refreshes every five minutes.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            policy = QWebEnginePage.PermissionPolicy
            self._page.setFeaturePermission(
                origin, feature,
                policy.PermissionGrantedByUser if answer == QMessageBox.StandardButton.Yes
                else policy.PermissionDeniedByUser,
            )
        except (AttributeError, RuntimeError, TypeError):
            message = "LOCATION PERMISSION UNAVAILABLE  ·  Enter an area in the Flights tab to continue"
            self._status.setText(message)
            self.flight_radar_updated.emit({"error": message})

    def _refresh_flight_radar(self):
        location = self._flight_location
        if location is None or not self._flight_radar_active:
            return
        if self._flight_fetch_thread and self._flight_fetch_thread.is_alive():
            return
        remaining = self._flight_refresh_seconds - (time.monotonic() - self._flight_last_fetch)
        if self._flight_last_fetch and remaining > 0:
            self._status.setText(f"FLIGHT RADAR REFRESH IN {int(remaining)} SEC  ·  PUBLIC FEED QUOTA")
            return
        self._flight_last_fetch = time.monotonic()
        self._status.setText("SCANNING ADS-B NETWORK  ·  OPEN SKY / APPROX. 50 KM RADIUS")
        self._flight_fetch_thread = threading.Thread(
            target=self._flight_lookup_worker, args=location,
            daemon=True, name="JarvisNearbyFlights",
        )
        self._flight_fetch_thread.start()

    def _flight_lookup_worker(self, latitude: float, longitude: float):
        try:
            radius_km = 50.0
            lat_delta = radius_km / 111.0
            lat_min, lat_max = max(-90.0, latitude - lat_delta), min(90.0, latitude + lat_delta)
            if abs(latitude) >= 85:
                lon_boxes = [(-180.0, 180.0)]
            else:
                lon_delta = radius_km / (111.0 * abs(math.cos(math.radians(latitude))))
                low, high = longitude - lon_delta, longitude + lon_delta
                lon_boxes = []
                if low < -180.0:
                    lon_boxes.extend([(-180.0, high), (low + 360.0, 180.0)])
                elif high > 180.0:
                    lon_boxes.extend([(low, 180.0), (-180.0, high - 360.0)])
                else:
                    lon_boxes.append((low, high))
            box_area = (lat_max - lat_min) * sum(max(0.0, right - left) for left, right in lon_boxes)
            credit_cost = 1 if box_area <= 25 else 2 if box_area <= 100 else 3 if box_area <= 400 else 4
            refresh_seconds = max(300, int(math.ceil(86400 * credit_cost / 300)))
            import requests
            states = []
            for lon_min, lon_max in lon_boxes:
                response = requests.get(
                    "https://opensky-network.org/api/states/all",
                    params={"lamin": lat_min, "lamax": lat_max, "lomin": lon_min, "lomax": lon_max},
                    headers={"User-Agent": "JARVIS-AI-Desktop/1.0 (+https://github.com/Ninja2007S/Jarvis-AI)"},
                    timeout=(5, 15),
                )
                if response.status_code == 429:
                    retry = response.headers.get("X-Rate-Limit-Retry-After-Seconds", "")
                    raise RuntimeError(f"OpenSky rate limit reached; retry after {retry or 'later'} seconds.")
                response.raise_for_status()
                states.extend(response.json().get("states") or [])
            now = time.time()
            aircraft = []
            seen = set()
            for state in states:
                if len(state) < 17 or state[5] is None or state[6] is None or state[8]:
                    continue
                icao24 = str(state[0] or "").upper()
                if not icao24 or icao24 in seen:
                    continue
                seen.add(icao24)
                lat, lon = float(state[6]), float(state[5])
                d_lat = math.radians(lat - latitude)
                d_lon = math.radians(lon - longitude)
                a = (math.sin(d_lat / 2) ** 2 + math.cos(math.radians(latitude)) *
                     math.cos(math.radians(lat)) * math.sin(d_lon / 2) ** 2)
                distance = 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))
                if distance > radius_km:
                    continue
                position_time = int(state[3] or state[4] or now)
                age = max(0, int(now - position_time))
                if age > 180:
                    continue
                aircraft.append({
                    "icao24": icao24,
                    "callsign": str(state[1] or "UNKNOWN").strip() or "UNKNOWN",
                    "country": str(state[2] or "Unknown"),
                    "longitude": lon,
                    "latitude": lat,
                    "altitude_m": float(state[7]) if state[7] is not None else None,
                    "velocity_mps": float(state[9]) if state[9] is not None else None,
                    "heading": float(state[10]) if state[10] is not None else None,
                    "vertical_rate_mps": float(state[11]) if state[11] is not None else None,
                    "squawk": str(state[14] or "—"),
                    "distance_km": distance,
                    "age_s": age,
                })
            aircraft.sort(key=lambda item: item["distance_km"])
            flight_result = {"aircraft": aircraft[:40], "center": {"latitude": latitude, "longitude": longitude},
                             "radius_km": radius_km, "updated": now, "source": "OpenSky Network",
                             "refresh_seconds": refresh_seconds}
            self._flight_result.ready.emit(flight_result)
        except Exception as exc:
            self._flight_result.ready.emit({"error": str(exc)[:240]})

    def _on_flight_result(self, result: dict):
        if not self._flight_radar_active:
            return
        if result.get("error"):
            self._status.setText(f"FLIGHT RADAR UNAVAILABLE  ·  {result['error']}")
            self._flight_refresh_timer.start(self._flight_refresh_seconds * 1000)
            self.flight_radar_updated.emit(result)
            return
        self._last_flight_result = result
        self._flight_refresh_seconds = max(300, int(result.get("refresh_seconds", 300)))
        self._js("showFlights", result)
        self._rotation_button.setText("RESUME ROTATION")
        count = len(result.get("aircraft", []))
        self._status.setText(
            f"OPEN SKY NETWORK  ·  {count} AIRCRAFT WITHIN {result.get('radius_km', 50)} KM  ·  "
            f"UPDATED {time.strftime('%H:%M:%S', time.localtime(result.get('updated', time.time())))}  ·  "
            f"REFRESH {max(1, self._flight_refresh_seconds // 60)} MIN"
        )
        self._flight_refresh_timer.start(self._flight_refresh_seconds * 1000)
        self.flight_radar_updated.emit(result)

    def _start_lookup(self, mode: str, origin: str, destination: str):
        if self._lookup_thread and self._lookup_thread.is_alive():
            self._status.setText("A map lookup is already running.")
            return
        task = "MEASURING ROAD DISTANCE" if mode == "distance" else "LOOKING UP"
        self._status.setText(f"{task}  {origin[:45] + ' → ' if origin else ''}{destination[:75]}")
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

    @staticmethod
    def _flight_route(start: dict, end: dict, steps: int = 80) -> dict:
        """Build a great-circle arc between geocoded cities for globe display.

        This is an illustrative air path, not a live airline airway/track.
        """
        import math
        lat1, lon1 = math.radians(start["lat"]), math.radians(start["lon"])
        lat2, lon2 = math.radians(end["lat"]), math.radians(end["lon"])
        a = (math.cos(lat2) * math.sin(lon2 - lon1)) ** 2
        b = (math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)) ** 2
        c = math.sin(lat1) * math.sin(lat2) + math.cos(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
        angle = math.atan2(math.sqrt(a + b), c)
        coords = []
        for index in range(steps + 1):
            f = index / steps
            if angle < 1e-9:
                lat, lon = lat1, lon1
            else:
                sin_angle = math.sin(angle)
                left, right = math.sin((1 - f) * angle) / sin_angle, math.sin(f * angle) / sin_angle
                x = left * math.cos(lat1) * math.cos(lon1) + right * math.cos(lat2) * math.cos(lon2)
                y = left * math.cos(lat1) * math.sin(lon1) + right * math.cos(lat2) * math.sin(lon2)
                z = left * math.sin(lat1) + right * math.sin(lat2)
                lat, lon = math.atan2(z, math.sqrt(x * x + y * y)), math.atan2(y, x)
            coords.append([math.degrees(lon), math.degrees(lat)])
        return {
            "geometry": {"type": "LineString", "coordinates": coords},
            "distance_m": 6_371_000 * angle,
            "duration_s": 0,
            "start": start,
            "end": end,
            "route_type": "air",
        }

    def _lookup_worker(self, mode: str, origin: str, destination: str):
        try:
            end = self._geocode(destination)
            if mode in {"locate", "flight_origin"}:
                self._lookup_signal.ready.emit({"mode": mode, "destination": end})
                return
            start = self._geocode(origin)
            route = (self._flight_route(start, end) if mode == "flight_route"
                     else self._route(start, end))
            self._lookup_signal.ready.emit({"mode": "route", "route": route})
        except Exception as exc:
            self._lookup_signal.ready.emit({"mode": mode, "error": str(exc)[:200]})

    def _on_lookup_result(self, result: dict):
        if result.get("error"):
            if result.get("mode") == "flight_origin":
                self._flight_radar_active = False
                self._flight_refresh_timer.stop()
                self.flight_radar_updated.emit({"error": f"Could not resolve the radar area: {result['error']}"})
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
        if result["mode"] == "flight_origin":
            place = result["destination"]
            self._flight_location = (place["lat"], place["lon"])
            self._flight_location_source = "manual"
            self._flight_last_fetch = 0.0
            self._refresh_flight_radar()
            return

        route = result["route"]
        self._js("showRoute", route)
        self._rotation_button.setText("RESUME ROTATION")
        distance_km = route["distance_m"] / 1000.0
        duration_s = max(0, int(round(route.get("duration_s", 0))))
        days, remainder = divmod(duration_s, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = int(round(remainder / 60.0))
        if minutes == 60:
            hours += 1
            minutes = 0
        if hours == 24:
            days += 1
            hours = 0
        eta = (f"{days} d {hours} hr {minutes} min" if days else
               f"{hours} hr {minutes} min" if hours else
               f"{max(1, minutes)} min")
        if route.get("route_type") == "air":
            self._status.setText(
                f"GREAT-CIRCLE AIR PATH  ·  {route['start']['name'][:48]} → "
                f"{route['end']['name'][:48]}  ·  {distance_km:.1f} km  ·  illustrative"
            )
        else:
            self._status.setText(
                f"ROAD ROUTE  ·  {route['start']['name'][:38]} → "
                f"{route['end']['name'][:38]}  ·  {distance_km:,.1f} km  ·  est. drive {eta}"
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
