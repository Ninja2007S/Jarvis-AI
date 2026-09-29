"""Native PyQt Earth globe and one-shot global place lookup for the HUD."""
from __future__ import annotations

import math
import threading
import time

from PyQt6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor, QFont, QPainter, QPainterPath, QPen, QRadialGradient,
)
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QVBoxLayout,
    QWidget,
)


_PLACE_CACHE: dict[str, tuple[str, float, float]] = {}
_PLACE_CACHE_LOCK = threading.Lock()
_NOMINATIM_LOCK = threading.Lock()
_LAST_NOMINATIM_REQUEST = 0.0


class _GeoResult(QObject):
    ready = pyqtSignal(object)


class GlobeCanvas(QWidget):
    """Stylized orthographic globe with a rotatable surface and destination pin."""

    _LAND = (
        # North America
        [(-168, 70), (-150, 72), (-136, 68), (-130, 60), (-123, 55), (-130, 49),
         (-124, 42), (-117, 32), (-108, 31), (-104, 23), (-97, 18), (-90, 15),
         (-84, 10), (-78, 8), (-81, 25), (-76, 35), (-67, 44), (-59, 48),
         (-53, 57), (-62, 64), (-80, 70), (-101, 74), (-125, 73), (-145, 71), (-168, 70)],
        # Greenland
        [(-52, 82), (-34, 78), (-24, 70), (-40, 60), (-54, 59), (-66, 66), (-60, 75), (-52, 82)],
        # Central America
        [(-83, 10), (-78, 8), (-76, 13), (-80, 20), (-86, 22), (-92, 18), (-89, 14), (-83, 10)],
        # South America
        [(-81, 12), (-72, 10), (-64, 6), (-56, 2), (-49, -8), (-45, -19),
         (-52, -31), (-60, -40), (-68, -55), (-75, -48), (-78, -31), (-81, -10), (-80, 2), (-81, 12)],
        # Europe and Asia
        [(-11, 36), (-10, 44), (-7, 50), (-2, 55), (8, 58), (18, 55), (25, 60),
         (33, 68), (50, 72), (75, 72), (95, 68), (118, 61), (140, 57), (156, 62),
         (174, 58), (165, 49), (147, 45), (139, 36), (130, 34), (123, 24), (116, 20),
         (110, 10), (103, 2), (97, 8), (92, 20), (82, 22), (77, 8), (73, 8), (69, 24),
         (60, 25), (53, 16), (45, 12), (42, 23), (35, 30), (28, 36), (24, 41),
         (18, 40), (13, 45), (7, 44), (1, 43), (-5, 36), (-11, 36)],
        # Africa
        [(-17, 36), (-5, 37), (10, 34), (24, 32), (34, 23), (43, 12), (50, 10),
         (44, -2), (40, -13), (33, -22), (28, -34), (18, -35), (12, -28), (5, -20),
         (1, -8), (-5, 5), (-12, 13), (-17, 22), (-17, 36)],
        # Arabian peninsula
        [(35, 31), (45, 30), (56, 24), (53, 16), (43, 12), (39, 20), (35, 31)],
        # India and southeast Asia
        [(68, 24), (78, 29), (89, 27), (93, 20), (87, 8), (80, 7), (75, 12), (72, 19), (68, 24)],
        [(96, 21), (106, 22), (110, 16), (108, 8), (103, 1), (98, 8), (96, 21)],
        # Japan, Indonesia, Philippines and New Guinea
        [(130, 32), (136, 37), (141, 42), (145, 43), (143, 35), (138, 31), (130, 32)],
        [(95, 5), (106, 6), (118, 1), (128, 0), (135, -5), (128, -9), (116, -8), (108, -6), (101, -2), (95, 5)],
        [(120, 19), (124, 18), (126, 12), (123, 7), (119, 11), (120, 19)],
        [(132, -2), (145, -4), (151, -8), (145, -10), (137, -8), (132, -2)],
        # Australia
        [(114, -12), (126, -11), (137, -13), (145, -17), (153, -27), (148, -38),
         (137, -36), (129, -33), (121, -27), (114, -22), (114, -12)],
        # Madagascar
        [(49, -12), (51, -16), (49, -25), (46, -24), (44, -17), (46, -13), (49, -12)],
        # Antarctica
        [(-180, -70), (-140, -73), (-100, -72), (-60, -75), (-20, -71), (20, -74),
         (60, -72), (100, -74), (140, -71), (180, -70), (180, -88), (-180, -88), (-180, -70)],
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(360, 310)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._center_lon = -18.0
        self._center_lat = 18.0
        self._auto_rotate = True
        self._dragging = False
        self._last_mouse = QPointF()
        self._place_name = ""
        self._place_lat: float | None = None
        self._place_lon: float | None = None
        self._pulse = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(45)

    def clear_destination(self):
        self._place_name = ""
        self._place_lat = self._place_lon = None
        self._auto_rotate = True
        self.update()

    def focus_destination(self, name: str, lat: float, lon: float):
        self._place_name = name
        self._place_lat = float(lat)
        self._place_lon = float(lon)
        self._center_lat = max(-82.0, min(82.0, float(lat)))
        self._center_lon = float(lon)
        self._auto_rotate = False
        self.update()

    def set_rotation(self, enabled: bool):
        self._auto_rotate = bool(enabled)
        self.update()

    def _tick(self):
        self._pulse = (self._pulse + 0.055) % (math.tau)
        if self._auto_rotate and not self._dragging:
            self._center_lon = (self._center_lon + 0.18 + 180.0) % 360.0 - 180.0
        self.update()

    def _project(self, lon: float, lat: float, cx: float, cy: float, radius: float):
        lat_r = math.radians(lat)
        lat0 = math.radians(self._center_lat)
        delta = math.radians((lon - self._center_lon + 180.0) % 360.0 - 180.0)
        visibility = math.sin(lat0) * math.sin(lat_r) + math.cos(lat0) * math.cos(lat_r) * math.cos(delta)
        x = math.cos(lat_r) * math.sin(delta)
        y = math.cos(lat0) * math.sin(lat_r) - math.sin(lat0) * math.cos(lat_r) * math.cos(delta)
        return QPointF(cx + radius * x, cy - radius * y), visibility

    def _draw_grid_line(self, painter: QPainter, coords, cx, cy, radius):
        path = QPainterPath()
        drawing = False
        count = 0
        for lon, lat in coords:
            point, visible = self._project(lon, lat, cx, cy, radius)
            if visible >= 0:
                if not drawing:
                    path.moveTo(point)
                    drawing = True
                else:
                    path.lineTo(point)
                count += 1
            else:
                drawing = False
        if count > 1:
            painter.drawPath(path)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#071116"))
        w, h = self.width(), self.height()
        cx, cy = w * 0.50, h * 0.49
        radius = max(100.0, min(w * 0.405, h * 0.405))

        # Sparse fixed stars, sized with the viewport so the globe reads as a
        # navigational instrument rather than a page background.
        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(54):
            x = (i * 97 + 31) % max(w, 1)
            y = (i * 53 + 17) % max(h, 1)
            alpha = 35 + (i * 29) % 75
            painter.setBrush(QColor(130, 203, 210, alpha))
            painter.drawEllipse(QPointF(x, y), 1.0 if i % 5 else 1.6, 1.0 if i % 5 else 1.6)

        # Atmosphere and ocean.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(61, 193, 196, 48), 16))
        painter.drawEllipse(QPointF(cx, cy), radius + 4, radius + 4)
        ocean = QRadialGradient(QPointF(cx - radius * 0.32, cy - radius * 0.38), radius * 1.6)
        ocean.setColorAt(0.0, QColor("#1d6070"))
        ocean.setColorAt(0.55, QColor("#0d3443"))
        ocean.setColorAt(1.0, QColor("#061923"))
        painter.setPen(QPen(QColor("#64c8c7"), 1.2))
        painter.setBrush(ocean)
        sphere = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)
        painter.drawEllipse(sphere)

        clip = QPainterPath()
        clip.addEllipse(sphere)
        painter.save()
        painter.setClipPath(clip)

        # Latitude and longitude telemetry.
        painter.setPen(QPen(QColor(93, 193, 190, 58), 0.8, Qt.PenStyle.DotLine))
        for lat in (-60, -30, 0, 30, 60):
            coords = [(lon, lat) for lon in range(-180, 181, 4)]
            self._draw_grid_line(painter, coords, cx, cy, radius)
        for base_lon in range(-180, 180, 30):
            coords = [(base_lon, lat) for lat in range(-88, 89, 3)]
            self._draw_grid_line(painter, coords, cx, cy, radius)

        # Simplified continent silhouettes, projected onto the visible hemisphere.
        for polygon in self._LAND:
            center_lon = sum(p[0] for p in polygon[:-1]) / max(1, len(polygon) - 1)
            center_lat = sum(p[1] for p in polygon[:-1]) / max(1, len(polygon) - 1)
            _, facing = self._project(center_lon, center_lat, cx, cy, radius)
            if facing < -0.48:
                continue
            path = QPainterPath()
            projected = [self._project(lon, lat, cx, cy, radius)[0] for lon, lat in polygon]
            if projected:
                path.moveTo(projected[0])
                for point in projected[1:]:
                    path.lineTo(point)
                path.closeSubpath()
                painter.setPen(QPen(QColor("#7dbb8e"), 1.0))
                painter.setBrush(QColor(49, 102, 77, 225))
                painter.drawPath(path)

        # Selected location stays centered after the globe pans to it.
        if self._place_lat is not None and self._place_lon is not None:
            point, visible = self._project(self._place_lon, self._place_lat, cx, cy, radius)
            if visible >= 0:
                pulse_r = 8 + (math.sin(self._pulse) + 1) * 6
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(255, 173, 98, 105), 1.4))
                painter.drawEllipse(point, pulse_r, pulse_r)
                painter.setPen(QPen(QColor("#ffd08a"), 1.2))
                painter.setBrush(QColor("#fff0bd"))
                painter.drawEllipse(point, 4.2, 4.2)
                painter.setPen(QPen(QColor("#ffe3b0"), 1))
                painter.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
                painter.drawText(point + QPointF(10, -10), self._place_name[:34])

        painter.restore()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(111, 213, 206, 130), 1.1))
        painter.drawEllipse(sphere)

        # HUD coordinate corners and center reticle.
        painter.setPen(QPen(QColor(113, 212, 194, 130), 1))
        tick = 9
        for x_sign in (-1, 1):
            for y_sign in (-1, 1):
                x = cx + x_sign * (radius + 12)
                y = cy + y_sign * (radius + 12)
                painter.drawLine(QPointF(x, y), QPointF(x - x_sign * tick, y))
                painter.drawLine(QPointF(x, y), QPointF(x, y - y_sign * tick))
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._last_mouse = event.position()
            self._auto_rotate = False
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if not self._dragging:
            return
        delta = event.position() - self._last_mouse
        self._last_mouse = event.position()
        scale = 180.0 / max(180.0, min(self.width(), self.height()))
        self._center_lon = (self._center_lon - delta.x() * scale + 180.0) % 360.0 - 180.0
        self._center_lat = max(-82.0, min(82.0, self._center_lat + delta.y() * scale))
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self.setCursor(Qt.CursorShape.ArrowCursor)


class EarthGlobePanel(QWidget):
    """Embed the globe, voice destination lookup, and controls in JARVIS's HUD."""

    close_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:#071116; color:#c2d2ce;")
        self._geo_signal = _GeoResult(self)
        self._geo_signal.ready.connect(self._on_geocode_result)
        self._geo_thread: threading.Thread | None = None
        self._rotation_button: QPushButton | None = None
        self._status: QLabel | None = None
        self._query: QLineEdit | None = None
        self.globe = GlobeCanvas(self)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 9, 12, 8)
        root.setSpacing(7)
        header = QHBoxLayout()
        title = QLabel("◉  EARTH / GLOBAL POSITIONING")
        title.setStyleSheet("color:#71d4c2; font: bold 10pt 'Consolas';")
        header.addWidget(title)
        header.addStretch(1)
        self._query = QLineEdit()
        self._query.setPlaceholderText("CITY, LANDMARK, OR COUNTRY")
        self._query.setFixedWidth(245)
        self._query.setStyleSheet("QLineEdit{background:#101c21;border:1px solid #39545a;padding:6px;color:#d6e8e3;font:9pt 'Consolas';} QLineEdit:focus{border-color:#71d4c2;}")
        self._query.returnPressed.connect(self.locate_query)
        header.addWidget(self._query)
        locate = self._button("LOCATE")
        locate.clicked.connect(self.locate_query)
        header.addWidget(locate)
        self._rotation_button = self._button("PAUSE ROTATION")
        self._rotation_button.clicked.connect(self._toggle_rotation)
        header.addWidget(self._rotation_button)
        close = self._button("CLOSE")
        close.clicked.connect(self.close_requested.emit)
        header.addWidget(close)
        root.addLayout(header)
        root.addWidget(self.globe, stretch=1)
        self._status = QLabel("EARTH / LIVE ROTATION  ·  Drag to pan  ·  Search a city, landmark, or country")
        self._status.setStyleSheet("color:#9aacaa;font:8pt 'Consolas';padding-left:4px;")
        root.addWidget(self._status)
        attribution = QLabel("Place search: © OpenStreetMap contributors  ·  Coastlines are illustrative, not navigation-grade")
        attribution.setStyleSheet("color:#5c7775;font:7pt 'Consolas';padding-left:4px;")
        root.addWidget(attribution)

    @staticmethod
    def _button(label: str) -> QPushButton:
        button = QPushButton(label)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet("QPushButton{background:#101c21;border:1px solid #39545a;color:#a8ccc5;padding:6px 9px;font:bold 8pt 'Consolas';} QPushButton:hover{border-color:#71d4c2;color:#71d4c2;}")
        return button

    def open_view(self, destination: str = ""):
        self.show()
        if destination.strip():
            self._query.setText(destination.strip())
            self.locate_query()
        else:
            self._query.clear()
            self.globe.clear_destination()
            self._rotation_button.setText("PAUSE ROTATION")
            self._status.setText("EARTH / LIVE ROTATION  ·  Drag to pan  ·  Search a city, landmark, or country")

    def _toggle_rotation(self):
        enabled = not self.globe._auto_rotate
        self.globe.set_rotation(enabled)
        self._rotation_button.setText("PAUSE ROTATION" if enabled else "RESUME ROTATION")

    def locate_query(self):
        query = self._query.text().strip()
        if not query:
            self._status.setText("Enter a city, landmark, or country to locate.")
            return
        if self._geo_thread and self._geo_thread.is_alive():
            self._status.setText("A place lookup is already in progress.")
            return
        self._status.setText(f"SEARCHING OPENSTREETMAP  ·  {query[:90]}")
        self._geo_thread = threading.Thread(
            target=self._geocode, args=(query,), daemon=True, name="EarthPlaceLookup"
        )
        self._geo_thread.start()

    def _geocode(self, query: str):
        key = query.casefold()
        with _PLACE_CACHE_LOCK:
            cached = _PLACE_CACHE.get(key)
        if cached:
            self._geo_signal.ready.emit((True, query, *cached))
            return

        global _LAST_NOMINATIM_REQUEST
        with _NOMINATIM_LOCK:
            wait = 1.05 - (time.monotonic() - _LAST_NOMINATIM_REQUEST)
            if wait > 0:
                time.sleep(wait)
            _LAST_NOMINATIM_REQUEST = time.monotonic()
        try:
            import requests
            response = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query, "format": "jsonv2", "limit": 1},
                headers={
                    "User-Agent": "JARVIS-AI-Desktop/1.0 (user-requested globe place lookup)",
                    "Accept": "application/json",
                },
                timeout=(4, 10),
            )
            response.raise_for_status()
            rows = response.json()
            if not rows:
                self._geo_signal.ready.emit((False, query, "No matching place was found."))
                return
            row = rows[0]
            result = (str(row.get("display_name") or query), float(row["lat"]), float(row["lon"]))
            with _PLACE_CACHE_LOCK:
                _PLACE_CACHE[key] = result
            self._geo_signal.ready.emit((True, query, *result))
        except Exception as exc:
            self._geo_signal.ready.emit((False, query, f"Place lookup failed: {exc}"))

    def _on_geocode_result(self, result):
        if not result or len(result) < 3:
            return
        if not result[0]:
            self._status.setText(str(result[2])[:180])
            return
        _ok, _query, name, lat, lon = result
        self.globe.focus_destination(name, lat, lon)
        self._rotation_button.setText("RESUME ROTATION")
        self._status.setText(f"LOCKED ON  ·  {name[:95]}  ·  {lat:.4f}°, {lon:.4f}°")
