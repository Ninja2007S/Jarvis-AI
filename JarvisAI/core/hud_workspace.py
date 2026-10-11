"""Native, in-HUD media and live-data workspace for JARVIS.

The workspace replaces the avatar canvas only while a user-requested view is
open. It keeps video muted until the user asks for sound and treats web pages
as display-only content; no page is given access to JARVIS tools.
"""
from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import socket
import threading
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QGraphicsOpacityEffect, QScrollArea, QSizePolicy, QStackedLayout, QStackedWidget, QTabWidget,
    QTextBrowser, QTextEdit, QToolButton, QVBoxLayout, QWidget,
)

try:
    from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings, QWebEngineUrlRequestInterceptor
    from PyQt6.QtWebEngineWidgets import QWebEngineView
except Exception:  # WebEngine is an optional feature on minimal installs.
    QWebEnginePage = QWebEngineProfile = QWebEngineSettings = QWebEngineUrlRequestInterceptor = QWebEngineView = None

try:
    from PyQt6.QtPdf import QPdfDocument
    from PyQt6.QtPdfWidgets import QPdfView
except Exception:
    QPdfDocument = QPdfView = None

try:
    from PyQt6 import sip
except Exception:  # pragma: no cover - depends on the local PyQt build
    sip = None


_NEWS_PUBLISHER_HOSTS = {
    "english.newsfirst.lk", "newsfirst.lk", "www.newsfirst.lk",
    "adaderana.lk", "www.adaderana.lk", "sinhala.adaderana.lk",
}


class _OpenGraphImageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.image_url = ""

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta" or self.image_url:
            return
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        field = (values.get("property") or values.get("name") or "").lower()
        if field in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"}:
            self.image_url = values.get("content", "").strip()


def _is_public_https_url(url: str) -> bool:
    """Reject credentials, non-HTTPS URLs, and hosts resolving to private IPs."""
    try:
        parsed = urlparse(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.port not in (None, 443)):
            return False
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(
            ipaddress.ip_address(record[4][0]).is_global for record in addresses
        )
    except (OSError, ValueError):
        return False


class _PublicHttpsRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_public_https_url(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_BG = "#080e12"
_PANEL = "#10191d"
_PANEL2 = "#151f22"
_BORDER = "#34464a"
_TEAL = "#71d4c2"
_AMBER = "#d98a59"
_TEXT = "#c8d9d5"
_DIM = "#78908d"
_WHITE = "#edf3e8"
_VIDEO_SUFFIXES = {".mp4", ".m4v", ".webm", ".mov", ".mkv", ".avi", ".ogv"}

from core.holo_gestures import HoloGestureServer


if QWebEngineUrlRequestInterceptor is not None:
    class _HoloLocalOnlyInterceptor(QWebEngineUrlRequestInterceptor):
        """Keep the Holo panel's requests on the machine; its assets are bundled."""

        def __init__(self, allowed_port: int, parent=None):
            super().__init__(parent)
            self._allowed_port = int(allowed_port)

        def interceptRequest(self, info):
            url = info.requestUrl()
            if url.scheme().lower() in {"http", "https", "ws", "wss"}:
                allowed = url.host().lower() == "127.0.0.1" and url.port() == self._allowed_port
                if not allowed:
                    info.block(True)
else:
    _HoloLocalOnlyInterceptor = None


if QWebEnginePage is not None:
    class _HoloPublisherPage(QWebEnginePage):
        """Display-only publisher view; no JARVIS bridge and no off-site navigation."""

        def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
            if not is_main_frame:
                return True
            try:
                parsed = urlparse(url.toString())
                host = (parsed.hostname or "").lower().rstrip(".")
                return (
                    parsed.scheme == "https"
                    and host in _NEWS_PUBLISHER_HOSTS
                    and parsed.username is None
                    and parsed.password is None
                    and parsed.port in (None, 443)
                )
            except (TypeError, ValueError):
                return False
else:
    _HoloPublisherPage = None


def _valid_http_url(value: str) -> bool:
    try:
        return urlparse(value).scheme.lower() in {"http", "https"}
    except Exception:
        return False


def _youtube_id(value: str) -> str | None:
    try:
        u = urlparse(value)
        if u.hostname and u.hostname.lower().endswith("youtu.be"):
            candidate = u.path.strip("/").split("/")[0]
        elif u.hostname and "youtube.com" in u.hostname.lower():
            candidate = parse_qs(u.query).get("v", [""])[0]
            if not candidate:
                bits = [b for b in u.path.split("/") if b]
                candidate = bits[1] if len(bits) > 1 and bits[0] in {"embed", "shorts", "live"} else ""
        else:
            return None
        return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate or "") else None
    except Exception:
        return None


class _MetricPlot(QWidget):
    """Small dependency-free scrolling plot for resource/market history."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._series: dict[str, list[float | None]] = {}
        self._colors = {
            "CPU": QColor("#17baff"), "RAM": QColor("#00e5ad"),
            "GPU": QColor("#d52dff"), "DISK": QColor("#ffbd38"),
            "RX": QColor("#00e5ad"), "TX": QColor("#168fff"),
            "PRICE": QColor(_TEAL),
        }

    def add_sample(self, values: dict[str, float | None], limit: int = 100):
        keys = set(self._series) | set(values)
        for key in keys:
            row = self._series.setdefault(key, [])
            row.append(values.get(key))
            del row[:-limit]
        self.update()

    def set_values(self, key: str, values):
        row = [float(v) for v in values if v is not None]
        self._series[key] = row[-100:]
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(_BG))
        area = self.rect().adjusted(34, 14, -12, -22)
        p.setPen(QPen(QColor(_BORDER), 1))
        for i in range(5):
            y = area.top() + i * area.height() / 4
            p.drawLine(area.left(), int(y), area.right(), int(y))
            p.setPen(QColor(_DIM))
            p.setFont(QFont("Courier New", 7))
            p.drawText(1, int(y) + 3, f"{100 - i * 25:>3}")
            p.setPen(QPen(QColor(_BORDER), 1))
        for key, row in self._series.items():
            valid = [(i, v) for i, v in enumerate(row) if v is not None]
            if len(valid) < 2:
                continue
            max_value = max(max(v for _, v in valid), 1.0)
            n = max(len(row) - 1, 1)
            points = []
            for i, value in valid:
                # Percentage series uses the 0–100 axis. PRICE rescales to its
                # own range so small market changes remain visible.
                if key == "PRICE":
                    vals = [v for _, v in valid]
                    low, high = min(vals), max(vals)
                    norm = 0.5 if high == low else (value - low) / (high - low)
                else:
                    norm = max(0.0, min(1.0, value / max_value if max_value > 100 else value / 100.0))
                x = area.left() + i * area.width() / n
                y = area.bottom() - norm * area.height()
                points.append((x, y))
            color = self._colors.get(key, QColor(_TEAL))
            p.setPen(QPen(color, 2))
            for a, b in zip(points, points[1:]):
                p.drawLine(int(a[0]), int(a[1]), int(b[0]), int(b[1]))
        p.setPen(QColor(_DIM))
        p.setFont(QFont("Courier New", 7))
        legend = "   ".join(self._series.keys())
        p.drawText(area.left(), self.height() - 4, legend)


class _TelemetryGauge(QWidget):
    """Low-cost animated neon ring, drawn only by Qt when its value changes."""

    def __init__(self, accent: str, label: str = "USAGE", parent=None):
        super().__init__(parent)
        self._accent = QColor(accent)
        self._label = label.upper()
        self._value = 0.0
        self._start_value = 0.0
        self._target_value = 0.0
        self._available = False
        self._started = 0.0
        self._focused = False
        self._timer = QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._advance)
        self.setMinimumSize(104, 104)
        self.setMaximumSize(148, 148)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_value(self, value):
        if value is None:
            self._available = False
            self._timer.stop()
            self.update()
            return
        try:
            target = max(0.0, min(100.0, float(value)))
        except (TypeError, ValueError):
            return
        self._available = True
        self._start_value = self._value
        self._target_value = target
        self._started = time.monotonic()
        self._timer.start()

    def set_focused(self, focused: bool):
        self._focused = bool(focused)
        self.update()

    def _advance(self):
        progress = min(1.0, (time.monotonic() - self._started) / 0.55)
        eased = 1.0 - (1.0 - progress) ** 3
        self._value = self._start_value + (self._target_value - self._start_value) * eased
        self.update()
        if progress >= 1.0:
            self._timer.stop()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(4, 11, 24, 210))
        p.drawEllipse(self.rect().adjusted(2, 2, -2, -2))
        side = max(12, min(self.width(), self.height()) - 18)
        ring = self.rect().adjusted((self.width() - side) // 2,
                                    (self.height() - side) // 2,
                                    -(self.width() - side) // 2,
                                    -(self.height() - side) // 2)
        p.setPen(QPen(QColor(self._accent.red(), self._accent.green(), self._accent.blue(), 54), 7))
        p.drawArc(ring, 225 * 16, -270 * 16)
        if self._available:
            p.setPen(QPen(QColor(self._accent.red(), self._accent.green(), self._accent.blue(), 45), 11))
            p.drawArc(ring, 225 * 16, -int(270 * 16 * self._value / 100.0))
            p.setPen(QPen(self._accent, 5 if self._focused else 4))
            p.drawArc(ring, 225 * 16, -int(270 * 16 * self._value / 100.0))
        if self._focused:
            p.setPen(QPen(QColor(self._accent.red(), self._accent.green(), self._accent.blue(), 110), 1))
            p.drawEllipse(ring.adjusted(-6, -6, 6, 6))
        p.setPen(QColor("#e7f5ff" if self._available else "#7890a8"))
        p.setFont(QFont("Segoe UI", max(13, side // 6), QFont.Weight.DemiBold))
        value_text = f"{self._value:.0f}%" if self._available else "N/A"
        p.drawText(self.rect().adjusted(0, -4, 0, -5), Qt.AlignmentFlag.AlignCenter, value_text)
        p.setPen(self._accent)
        p.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        p.drawText(self.rect().adjusted(0, side // 3, 0, -7), Qt.AlignmentFlag.AlignCenter, self._label)


class HudWorkspace(QWidget):
    """One native display surface for media, documents and live information."""

    close_requested = pyqtSignal()
    interrupt_requested = pyqtSignal()
    route_requested = pyqtSignal(str, str)
    market_loaded = pyqtSignal(object)
    system_metrics_loaded = pyqtSignal(object)
    thumbnail_loaded = pyqtSignal(int, bytes)
    holo_event = pyqtSignal(str, str)
    news_image_loaded = pyqtSignal(int, str, bytes, str)
    feature_requested = pyqtSignal(str)
    neural_hand_update = pyqtSignal(object)

    _TABS = (
        ("gestures", "HOLO HANDS"), ("video", "MEDIA"), ("web", "WEB"), ("images", "IMAGES"),
        ("document", "DOCUMENT"), ("news", "HEADLINES"),
        ("email", "INBOX"), ("calendar", "AGENDA"),
        ("system", "SYSTEM"), ("weather", "WEATHER"),
        ("flights", "FLIGHTS"), ("timers", "TIMERS"),
        ("market", "MARKET"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("HudWorkspace")
        self.setStyleSheet(f"background:{_BG}; color:{_TEXT};")
        self._reminders: list[dict] = []
        self._video_muted = True
        self._video_active = False
        self._timer_total = 0
        self._timer_deadline = 0.0
        self._timer_running = False
        self._pomodoro_work = False
        self._system_sample_active = False
        self._system_focus = "overview"
        self._system_gauges = {}
        self._system_details = {}
        self._system_plots = {}
        self._system_cards = {}
        self._sample_timer = QTimer(self)
        self._sample_timer.timeout.connect(self._sample_system)
        self._sample_timer.start(1500)
        self.market_loaded.connect(self._on_market_loaded)
        self.system_metrics_loaded.connect(self._on_system_metrics_loaded)
        self.thumbnail_loaded.connect(self._on_thumbnail_loaded)
        self.news_image_loaded.connect(self._on_news_image_loaded)
        self._gallery_buttons = []
        self._holo_server = None
        self._holo_interceptor = None
        self._holo_profile = None
        self._holo_page = None
        self._holo_container = None
        self._holo_view = None
        self._holo_stage = None
        self._holo_stage_stack = None
        self._holo_article_overlay = None
        self._holo_article_view = None
        self._holo_article_profile = None
        self._holo_article_page = None
        self._holo_article_title = None
        self._holo_article_index: int | None = None
        self._holo_requested = False
        self._is_fullscreen = False
        self._embedded_parent = None
        self._interrupt_shortcut = QShortcut(QKeySequence("Escape"), self)
        self._interrupt_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._interrupt_shortcut.setEnabled(False)
        self._interrupt_shortcut.activated.connect(self.interrupt_requested.emit)
        self.holo_event.connect(self._on_holo_event)
        self._last_news_items: list[dict] = []
        self._news_image_slots = threading.BoundedSemaphore(4)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 7, 8, 8)
        root.setSpacing(6)
        top = QHBoxLayout()
        brand = QLabel("◈  JARVIS / VISUAL FIELD")
        brand.setStyleSheet(f"color:{_TEAL}; font: bold 9pt 'Courier New';")
        top.addWidget(brand)
        self._view_title = QLabel("LIVE VIEW")
        self._view_title.setStyleSheet(f"color:{_DIM}; font: 8pt 'Courier New';")
        top.addWidget(self._view_title)
        top.addStretch(1)
        close = QPushButton("RETURN TO AVATAR  ×")
        close.setStyleSheet(self._button_style())
        close.clicked.connect(self.close_requested.emit)
        top.addWidget(close)
        root.addLayout(top)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setStyleSheet(f"""
            QTabWidget::pane {{ border:1px solid {_BORDER}; background:{_BG}; }}
            QTabBar::tab {{ background:{_PANEL}; color:{_DIM}; padding:6px 9px;
                border:1px solid {_BORDER}; font: bold 7pt 'Courier New'; }}
            QTabBar::tab:selected {{ color:{_TEAL}; background:{_PANEL2};
                border-bottom:2px solid {_TEAL}; }}
            QTabBar::tab:hover {{ color:{_WHITE}; }}
        """)
        self._page_index = {}
        for key, title in self._TABS:
            page = self._make_page(key)
            self._page_index[key] = self.tabs.addTab(page, title)
        self.tabs.currentChanged.connect(self._on_holo_tab_changed)
        root.addWidget(self.tabs, 1)

    @staticmethod
    def _button_style():
        return f"QPushButton{{background:{_PANEL2};color:{_TEXT};border:1px solid {_BORDER};padding:5px 9px;font:8pt 'Courier New';}} QPushButton:hover{{color:{_TEAL};border-color:{_TEAL};}}"

    def _make_page(self, key: str) -> QWidget:
        if key == "gestures":
            return self._make_gesture_page()
        if key == "video":
            return self._make_video_page()
        if key == "web":
            return self._make_web_page()
        if key == "images":
            return self._make_image_page()
        if key == "document":
            return self._make_document_page()
        if key == "system":
            return self._make_system_page()
        if key == "timers":
            return self._make_timer_page()
        if key == "market":
            return self._make_market_page()
        return self._make_cards_page(key)

    def _make_gesture_page(self) -> QWidget:
        page = QWidget()
        self._holo_container = page
        lay = QVBoxLayout(page)
        bar = QHBoxLayout()
        self._holo_status = QLabel("CAMERA IS LOCAL · HAND TRACKING STAYS ON THIS PC")
        self._holo_status.setTextFormat(Qt.TextFormat.PlainText)
        self._holo_status.setStyleSheet(f"color:{_TEAL};font:bold 8pt 'Courier New';")
        bar.addWidget(self._holo_status, 1)
        choose_notes = QPushButton("CHOOSE NOTES FOLDER…")
        choose_notes.setStyleSheet(self._button_style())
        choose_notes.clicked.connect(self._select_holo_notes)
        bar.addWidget(choose_notes)
        stop = QPushButton("STOP CAMERA")
        stop.setStyleSheet(self._button_style())
        stop.clicked.connect(self.stop_holo_camera)
        bar.addWidget(stop)
        lay.addLayout(bar)

        self._holo_view = QWebEngineView() if QWebEngineView else None
        if self._holo_view:
            self._configure_webview(self._holo_view)
            # Holo's starfield, interaction layer, and face mesh use Canvas 2D;
            # 3D props use WebGL. Explicitly enable their accelerated paths.
            holo_settings = self._holo_view.settings()
            holo_settings.setAttribute(
                QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, True
            )
            holo_settings.setAttribute(
                QWebEngineSettings.WebAttribute.WebGLEnabled, True
            )
            self._holo_server = HoloGestureServer(
                lambda event, card: self.holo_event.emit(event, card),
                lambda pose: self.neural_hand_update.emit(pose),
            )
            if QWebEnginePage is not None and QWebEngineProfile is not None:
                self._holo_profile = QWebEngineProfile(self._holo_view)
                # Keep the page under its profile so Qt destroys the page before
                # releasing the profile at application shutdown.
                self._holo_page = QWebEnginePage(self._holo_profile, self._holo_profile)
                self._holo_page.featurePermissionRequested.connect(self._grant_holo_camera)
                if _HoloLocalOnlyInterceptor is not None:
                    local_port = QUrl(self._holo_server.url).port()
                    self._holo_interceptor = _HoloLocalOnlyInterceptor(local_port, self._holo_profile)
                    self._holo_profile.setUrlRequestInterceptor(self._holo_interceptor)
                self._holo_view.setPage(self._holo_page)
            self._holo_stage = QWidget(page)
            self._holo_stage_stack = QStackedLayout(self._holo_stage)
            self._holo_stage_stack.setContentsMargins(0, 0, 0, 0)
            self._holo_stage_stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
            self._holo_stage_stack.addWidget(self._holo_view)
            self._holo_article_overlay = QWidget(self._holo_stage)
            self._holo_article_overlay.setStyleSheet("background: rgba(0, 4, 8, 145);")
            overlay_layout = QVBoxLayout(self._holo_article_overlay)
            overlay_layout.setContentsMargins(38, 30, 38, 24)
            article_panel = QFrame()
            article_panel.setMinimumSize(440, 320)
            article_panel.setMaximumSize(1540, 1050)
            article_panel.setStyleSheet(
                f"QFrame{{background:{_BG};border:1px solid {_TEAL};border-radius:10px;}}"
            )
            article_layout = QVBoxLayout(article_panel)
            article_layout.setContentsMargins(10, 8, 10, 10)
            article_bar = QHBoxLayout()
            self._holo_article_title = QLabel("NEWS ARTICLE")
            self._holo_article_title.setStyleSheet(
                f"color:{_TEAL};font:bold 9pt 'Courier New';border:0;"
            )
            article_bar.addWidget(self._holo_article_title, 1)
            for label, callback in (
                ("BACK", lambda: self._holo_article_view.back() if self._holo_article_view else None),
                ("RELOAD", lambda: self._holo_article_view.reload() if self._holo_article_view else None),
                ("CLOSE  ×", self.close_holo_article),
            ):
                button = QPushButton(label)
                button.setStyleSheet(self._button_style())
                button.clicked.connect(callback)
                article_bar.addWidget(button)
            article_layout.addLayout(article_bar)
            article_hint = QLabel("OPEN PALM + MOVE TO SCROLL  ·  PINCH TO CLOSE  ·  PUBLISHER VIEW IS DISPLAY-ONLY")
            article_hint.setStyleSheet(f"color:{_DIM};font:7pt 'Courier New';border:0;padding:2px 4px;")
            article_layout.addWidget(article_hint)
            self._holo_article_view = QWebEngineView() if QWebEngineView else None
            if self._holo_article_view:
                self._configure_webview(self._holo_article_view)
                self._holo_article_view.loadFinished.connect(self._on_holo_article_loaded)
                if _HoloPublisherPage is not None:
                    # Keep publisher cookies/cache separate from other JARVIS
                    # WebEngine panels; this profile is off the record.
                    self._holo_article_profile = QWebEngineProfile(self._holo_article_view)
                    self._holo_article_page = _HoloPublisherPage(
                        self._holo_article_profile, self._holo_article_view
                    )
                    self._holo_article_view.setPage(self._holo_article_page)
                    self._holo_article_page.setAudioMuted(True)
                article_layout.addWidget(self._holo_article_view, 1)
            else:
                article_layout.addWidget(self._placeholder("Publisher article view unavailable."), 1)
            overlay_layout.addWidget(article_panel, 1, Qt.AlignmentFlag.AlignCenter)
            self._holo_stage_stack.addWidget(self._holo_article_overlay)
            self._holo_article_overlay.hide()
            lay.addWidget(self._holo_stage, 1)
            self._holo_view.loadFinished.connect(self._on_holo_loaded)
            self._holo_view.load(QUrl(self._holo_server.url))
        else:
            lay.addWidget(self._placeholder(
                "The Holo controls run inside this JARVIS HUD and need PyQt6-WebEngine. "
                "Install the project's requirements to enable the gesture view."
            ), 1)
        return page

    def _grant_holo_camera(self, origin, feature) -> None:
        if self._holo_page is None:
            return
        try:
            host = QUrl(origin).host().lower()
            port = QUrl(origin).port()
            video = QWebEnginePage.Feature.MediaVideoCapture
            policy = QWebEnginePage.PermissionPolicy
            expected_port = QUrl(self._holo_server.url).port() if self._holo_server else -1
            allowed = host == "127.0.0.1" and port == expected_port and feature == video
            self._holo_page.setFeaturePermission(
                origin, feature,
                policy.PermissionGrantedByUser if allowed else policy.PermissionDeniedByUser,
            )
            if allowed:
                self._holo_status.setText("CAMERA ACTIVE · VIDEO PROCESSES LOCALLY")
        except Exception:
            self._holo_status.setText("CAMERA PERMISSION UNAVAILABLE · CHECK DESKTOP CAMERA ACCESS")

    def _on_holo_event(self, event: str, card: str) -> None:
        if event == "feature":
            valid = {"video", "web", "images", "document", "news", "newsfirst", "adaderana", "email", "calendar",
                     "system", "weather", "flights", "timers", "market", "earth"}
            if card in valid:
                self.feature_requested.emit(card)
            return
        if event == "open_article":
            try:
                item = self._last_news_items[int(card)]
            except (ValueError, IndexError, TypeError):
                return
            title = str(item.get("title") or "NEWS ARTICLE")
            if self.open_holo_article(str(item.get("url") or ""), title, int(card)):
                source = str(item.get("source") or "NEWS")
                self._holo_status.setText(f"HOLO ARTICLE · {source[:42]} · OPEN PALM TO SCROLL")
            return
        if event == "close_article":
            self.close_holo_article()
            return
        if event == "web_scroll" and self._holo_article_view is not None:
            if self._holo_article_overlay is not None and self._holo_article_overlay.isVisible():
                try:
                    delta = max(-180.0, min(180.0, float(card)))
                except (TypeError, ValueError):
                    return
                if delta:
                    self._holo_article_view.page().runJavaScript(
                        f"window.scrollBy(0, {-delta:.1f})"
                    )
            return
        labels = {"open": "OPENED", "close": "CLOSED", "dock": "FILED", "dismiss": "DISMISSED", "sum": "SUMMARIZED",
                  "tidy": "ARRANGED", "jarvis": "JARVIS MODE", "exit": "RETURNING TO AVATAR"}
        if event == "exit":
            self.close_requested.emit()
            return
        if event not in labels:
            return
        self._holo_status.setText(f"GESTURE {labels[event]} · {card[:48]}".strip(" ·"))

    def _on_holo_tab_changed(self, _index: int) -> None:
        if self._holo_view is None:
            return
        if self.tabs.currentWidget() is self._holo_container:
            if not self._is_fullscreen:
                QTimer.singleShot(0, self.show_holo_gestures)
            else:
                self.start_holo_camera()
        else:
            self.stop_holo_camera()

    def _on_holo_loaded(self, ok: bool) -> None:
        if ok and self._holo_requested:
            self.start_holo_camera()

    def _select_holo_notes(self) -> None:
        if self._holo_server is None:
            return
        folder = QFileDialog.getExistingDirectory(self, "Choose Local Holo Notes", str(Path.home()))
        if not folder:
            return
        if not self._holo_server.set_notes_directory(folder):
            self._holo_status.setText("COULD NOT OPEN THAT NOTES FOLDER")
            return
        resume_camera = self._holo_requested
        self.stop_holo_camera()
        self._holo_requested = resume_camera
        self._holo_status.setText(f"LOCAL NOTES · {Path(folder).name[:40]} · SESSION ONLY")
        self._holo_view.load(QUrl(self._holo_server.url))

    def start_holo_camera(self) -> None:
        self._holo_requested = True
        self._holo_status.setText("REQUESTING LOCAL CAMERA ACCESS…")
        if self._holo_view is not None:
            self._holo_view.page().runJavaScript(
                "if (window.jarvisStartCamera) window.jarvisStartCamera();"
            )

    def stop_holo_camera(self) -> None:
        self._holo_requested = False
        self._holo_status.setText("CAMERA STOPPED · VIDEO TRACK RELEASED")
        if self._holo_view is not None:
            try:
                self._holo_view.page().runJavaScript(
                    "if (window.jarvisStopCamera) window.jarvisStopCamera();"
                )
            except RuntimeError:
                pass

    def show_holo_gestures(self) -> None:
        if "gestures" in self._page_index:
            self._view_title.setText("HOLO HAND GESTURES")
            self.tabs.setCurrentIndex(self._page_index["gestures"])
            if not self._is_fullscreen:
                self._embedded_parent = self.parentWidget()
                self.tabs.tabBar().hide()
                self.setParent(None)
                self.setWindowFlags(Qt.WindowType.Window)
                self.setWindowTitle("JARVIS — HOLO HAND GESTURES")
                self._is_fullscreen = True
                self._interrupt_shortcut.setEnabled(True)
                self.showFullScreen()
            if self._holo_page is not None and self._holo_server is not None:
                try:
                    self._holo_page.setFeaturePermission(
                        QUrl(self._holo_server.url),
                        QWebEnginePage.Feature.MediaVideoCapture,
                        QWebEnginePage.PermissionPolicy.PermissionGrantedByUser,
                    )
                except Exception:
                    pass
            self.start_holo_camera()

    @staticmethod
    def _is_supported_holo_article(url: str) -> bool:
        try:
            parsed = urlparse(str(url or ""))
            host = (parsed.hostname or "").lower().rstrip(".")
            return (
                parsed.scheme == "https"
                and host in _NEWS_PUBLISHER_HOSTS
                and parsed.username is None
                and parsed.password is None
                and parsed.port in (None, 443)
            )
        except (TypeError, ValueError):
            return False

    def open_holo_article(self, url: str, title: str = "NEWS ARTICLE", news_index: int | None = None) -> bool:
        """Open a publisher story in a floating, gesture-scrollable Holo panel."""
        if not self._holo_article_view or not self._is_supported_holo_article(url):
            return False
        if self.tabs.currentWidget() is not self._holo_container:
            self.show_holo_gestures()
        if self._holo_article_title is not None:
            self._holo_article_title.setText((title or "NEWS ARTICLE").upper()[:120])
        self._holo_article_index = news_index
        if self._holo_article_overlay is not None:
            self._holo_article_overlay.show()
            self._holo_stage_stack.setCurrentWidget(self._holo_article_overlay)
        self._holo_article_view.setUrl(QUrl(url))
        return True

    def _on_holo_article_loaded(self, ok: bool) -> None:
        if ok or self._holo_article_index is None or self._holo_view is None:
            if not ok and self._holo_article_title is not None and self._holo_article_index is None:
                self._holo_article_title.setText("PAGE DID NOT LOAD · TRY RELOAD")
            return
        index = self._holo_article_index
        self._holo_article_title.setText("PUBLISHER VIEW UNAVAILABLE · SHOWING LOCAL ARTICLE TEXT")
        self.close_holo_article()
        self._holo_view.page().runJavaScript(
            f"window.__holo && window.__holo.showNewsText && window.__holo.showNewsText({index})"
        )

    def close_holo_article(self) -> None:
        if self._holo_article_view is not None:
            self._holo_article_view.stop()
        self._holo_article_index = None
        if self._holo_article_overlay is not None:
            self._holo_article_overlay.hide()
        if self._holo_stage_stack is not None and self._holo_view is not None:
            self._holo_stage_stack.setCurrentWidget(self._holo_view)
        if self._holo_view is not None:
            self._holo_view.page().runJavaScript(
                "window.__holo && window.__holo.closeExternalArticle && window.__holo.closeExternalArticle()"
            )

    def open_latest_holo_article(self, source: str = "") -> bool:
        """Open the hovered or newest available publisher headline in Holo."""
        self.show_holo_gestures()
        source = str(source or "").strip().casefold()
        if source:
            for index, item in enumerate(self._last_news_items):
                host = (urlparse(str(item.get("url") or "")).hostname or "").casefold()
                matches_source = (
                    ("newsfirst" in source and "newsfirst" in host)
                    or ("derana" in source and "adaderana" in host)
                )
                if matches_source:
                    if self.open_holo_article(item.get("url", ""), item.get("title", ""), index):
                        return True
            homepage = (
                "https://www.newsfirst.lk/" if "newsfirst" in source
                else "https://www.adaderana.lk/" if "derana" in source
                else ""
            )
            if homepage:
                return self.open_holo_article(homepage, source.upper() + " LIVE NEWS")
            return False
        if self._holo_view is not None:
            def open_from_deck():
                if self._holo_view is None:
                    return
                self._holo_view.page().runJavaScript(
                    "if(window.__holo){window.__holo.refreshNews(); "
                    "setTimeout(()=>window.__holo.openLatestNews(),450)}"
                )
            QTimer.singleShot(450, open_from_deck)
            return True
        return False

    def restore_to_embedded(self, parent=None) -> None:
        if not self._is_fullscreen:
            return
        self.close_holo_article()
        self.stop_holo_camera()
        self.hide()
        target = parent or self._embedded_parent
        self.setParent(target)
        self.setWindowFlags(Qt.WindowType.Widget)
        self.tabs.tabBar().show()
        self._is_fullscreen = False
        self._interrupt_shortcut.setEnabled(False)
        self._embedded_parent = None

    def keyPressEvent(self, event):
        if self._is_fullscreen and event.key() == Qt.Key.Key_Escape:
            # Holo is a separate top-level window and consumes its own key
            # events. Route Escape to the desktop interrupt action so a
            # briefing can be stopped without closing the workspace.
            self.interrupt_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        if self._is_fullscreen:
            event.ignore()
            self.close_requested.emit()
            return
        super().closeEvent(event)

    def shutdown_holo(self) -> None:
        self.stop_holo_camera()
        self.restore_to_embedded()
        if self._holo_article_page is not None and self._holo_article_profile is not None:
            try:
                page = self._holo_article_page
                profile = self._holo_article_profile
                if self._holo_article_view is not None:
                    page.triggerAction(QWebEnginePage.WebAction.Stop)
                    self._holo_article_view.setPage(QWebEnginePage(self._holo_article_view))
                if sip is not None:
                    sip.delete(page)
                    sip.delete(profile)
                self._holo_article_page = None
                self._holo_article_profile = None
            except RuntimeError:
                pass
        if self._holo_page is not None and self._holo_profile is not None and sip is not None:
            try:
                page = self._holo_page
                profile = self._holo_profile
                if self._holo_view is not None:
                    page.triggerAction(QWebEnginePage.WebAction.Stop)
                    # Detach the custom-profile page from the live view first,
                    # then synchronously destroy it before its profile.
                    self._holo_view.setPage(QWebEnginePage(self._holo_view))
                sip.delete(page)
                sip.delete(profile)
                self._holo_page = None
                self._holo_profile = None
                self._holo_interceptor = None
            except RuntimeError:
                # Qt may already be partway through application teardown.
                pass
        if self._holo_server is not None:
            self._holo_server.close()
            self._holo_server = None

    def _placeholder(self, message: str) -> QLabel:
        label = QLabel(message)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        label.setStyleSheet(f"color:{_DIM}; padding:18px; background:{_BG};")
        return label

    def _make_video_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        bar = QHBoxLayout()
        self._video_label = QLabel("VIDEO / MUTED BY DEFAULT")
        self._video_label.setStyleSheet(f"color:{_TEAL}; font:bold 8pt 'Courier New';")
        bar.addWidget(self._video_label)
        bar.addStretch(1)
        self._video_sound = QPushButton("ENABLE SOUND  ◌")
        self._video_sound.setStyleSheet(self._button_style())
        self._video_sound.clicked.connect(self._toggle_video_sound)
        bar.addWidget(self._video_sound)
        self._video_pause = QPushButton("PAUSE")
        self._video_pause.setStyleSheet(self._button_style())
        self._video_pause.clicked.connect(self._pause_video)
        bar.addWidget(self._video_pause)
        pick = QPushButton("LOAD LOCAL VIDEO…")
        pick.setStyleSheet(self._button_style())
        pick.clicked.connect(self._pick_video)
        bar.addWidget(pick)
        lay.addLayout(bar)
        self._video_view = QWebEngineView() if QWebEngineView else None
        if self._video_view:
            self._configure_webview(self._video_view)
            lay.addWidget(self._video_view, 1)
        else:
            lay.addWidget(self._placeholder("Embedded playback requires PyQt6-WebEngine. Install the project requirements to enable the HUD video player."), 1)
        return page

    def _make_web_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        bar = QHBoxLayout()
        self._url_entry = QLineEdit()
        self._url_entry.setPlaceholderText("https://…  Internal HUD page")
        self._url_entry.setStyleSheet(f"background:{_PANEL};color:{_TEXT};border:1px solid {_BORDER};padding:6px;font:9pt 'Courier New';")
        self._url_entry.returnPressed.connect(self._navigate_web)
        bar.addWidget(self._url_entry, 1)
        for label, callback in (("GO", self._navigate_web), ("BACK", self._web_back), ("RELOAD", self._web_reload)):
            button = QPushButton(label)
            button.setStyleSheet(self._button_style())
            button.clicked.connect(callback)
            bar.addWidget(button)
        lay.addLayout(bar)
        self._web_view = QWebEngineView() if QWebEngineView else None
        if self._web_view:
            self._configure_webview(self._web_view)
            self._web_view.urlChanged.connect(lambda u: self._url_entry.setText(u.toString()))
            lay.addWidget(self._web_view, 1)
        else:
            lay.addWidget(self._placeholder("Internal web pages require PyQt6-WebEngine."), 1)
        return page

    @staticmethod
    def _configure_webview(view):
        # QWebEngineView does not expose setBackgroundColor() in all PyQt6
        # releases. Keep the widget's empty/loading area styled using the
        # QWidget API available across supported versions.
        view.setStyleSheet(f"background-color: {_BG};")
        settings = view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)

    def _make_image_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        self._image_view = QWebEngineView() if QWebEngineView else None
        if self._image_view:
            self._configure_webview(self._image_view)
            lay.addWidget(self._image_view, 1)
        else:
            self._image_view = QLabel("Install PyQt6-WebEngine for URL image previews.")
            self._image_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(self._image_view, 1)
        self._gallery = QWidget()
        self._gallery_row = QHBoxLayout(self._gallery)
        self._gallery_row.setContentsMargins(0, 3, 0, 0)
        self._gallery_row.setSpacing(6)
        self._gallery_scroll = QScrollArea()
        self._gallery_scroll.setWidgetResizable(True)
        self._gallery_scroll.setFixedHeight(92)
        self._gallery_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._gallery_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._gallery_scroll.setWidget(self._gallery)
        lay.addWidget(self._gallery_scroll)
        return page

    def _make_document_page(self) -> QWidget:
        page = QWidget()
        lay = QHBoxLayout(page)
        self._doc_view_stack = QStackedWidget()
        self._pdf_document = QPdfDocument(self) if QPdfDocument else None
        if QPdfView and self._pdf_document:
            # Some PyQt6 builds require the QWidget parent argument explicitly.
            self._pdf_view = QPdfView(self._doc_view_stack)
            self._pdf_view.setDocument(self._pdf_document)
            self._doc_view_stack.addWidget(self._pdf_view)
        else:
            self._pdf_view = None
        self._doc_text = QTextBrowser()
        self._doc_text.setOpenExternalLinks(False)
        self._doc_text.setStyleSheet(f"background:{_BG};color:{_TEXT};border:1px solid {_BORDER};padding:12px;")
        self._doc_view_stack.addWidget(self._doc_text)
        lay.addWidget(self._doc_view_stack, 3)
        summary_frame = QFrame()
        summary_frame.setStyleSheet(f"background:{_PANEL};border-left:1px solid {_BORDER};")
        side = QVBoxLayout(summary_frame)
        self._doc_title = QLabel("DOCUMENT NOTES")
        self._doc_title.setStyleSheet(f"color:{_TEAL};font:bold 8pt 'Courier New';")
        side.addWidget(self._doc_title)
        self._doc_summary = QTextEdit()
        self._doc_summary.setReadOnly(True)
        self._doc_summary.setStyleSheet(f"background:{_BG};color:{_TEXT};border:1px solid {_BORDER};padding:7px;")
        side.addWidget(self._doc_summary, 1)
        lay.addWidget(summary_frame, 2)
        return page

    def _make_cards_page(self, key: str) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        self._cards_status = getattr(self, "_cards_status", {})
        status = QLabel("Awaiting data")
        status.setStyleSheet(f"color:{_DIM};font:8pt 'Courier New';padding:4px;")
        self._cards_status[key] = status
        status_row = QHBoxLayout()
        status_row.addWidget(status, 1)
        if key == "flights":
            self._flight_route = ("", "")
            route = QPushButton("PLOT AIR PATH ON GLOBE")
            route.setStyleSheet(self._button_style())
            route.clicked.connect(lambda: self.route_requested.emit(*self._flight_route))
            status_row.addWidget(route)
        lay.addLayout(status_row)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea{{border:0;background:{_BG};}}")
        host = QWidget()
        cards = QVBoxLayout(host)
        cards.setContentsMargins(2, 2, 6, 2)
        cards.setSpacing(7)
        cards.addStretch(1)
        scroll.setWidget(host)
        lay.addWidget(scroll, 1)
        self._card_hosts = getattr(self, "_card_hosts", {})
        self._card_hosts[key] = (host, cards)
        return page

    def _make_system_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background:#020917;color:#d9efff;")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(8, 6, 8, 8)
        lay.setSpacing(7)

        title_row = QHBoxLayout()
        heading = QLabel("SYSTEM TELEMETRY")
        heading.setStyleSheet("color:#57caff;font: bold 15pt 'Segoe UI';letter-spacing:2px;")
        title_row.addWidget(heading)
        subtitle = QLabel("REAL-TIME PERFORMANCE  /  LOCAL MACHINE")
        subtitle.setStyleSheet("color:#69bce8;font: bold 8pt 'Segoe UI';letter-spacing:1px;")
        title_row.addWidget(subtitle)
        title_row.addStretch(1)
        self._system_datetime = QLabel(datetime.now().strftime("%H:%M  ·  %a, %d %b %Y"))
        self._system_datetime.setStyleSheet("color:#9bc9e7;font:bold 9pt 'Segoe UI';padding-right:10px;")
        title_row.addWidget(self._system_datetime)
        self._system_status = QLabel("●  SAMPLING SYSTEM SENSORS")
        self._system_status.setStyleSheet("color:#00e5ad;font:bold 8pt 'Consolas';")
        title_row.addWidget(self._system_status)
        lay.addLayout(title_row)

        self._system_hardware = QLabel("DETECTING PROCESSOR · GRAPHICS · MEMORY · OPERATING SYSTEM")
        self._system_hardware.setStyleSheet("color:#82a9c4;font:8pt 'Consolas';padding:2px 4px;")
        lay.addWidget(self._system_hardware)

        host = QWidget()
        host.setStyleSheet("background:transparent;")
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)

        def metric_panel(key: str, title: str, accent: str, detail: str):
            frame = QFrame()
            frame.setObjectName(f"telemetry_{key}")
            frame.setMinimumHeight(205)
            frame.setStyleSheet(
                f"QFrame#telemetry_{key}{{background:#050f20;border:1px solid #15517c;}}"
                "QLabel{border:0;background:transparent;}"
            )
            box = QVBoxLayout(frame)
            box.setContentsMargins(9, 7, 9, 7)
            box.setSpacing(4)
            top = QHBoxLayout()
            label = QLabel(title.upper())
            label.setStyleSheet(f"color:{accent};font:bold 10pt 'Segoe UI';letter-spacing:1px;")
            top.addWidget(label)
            top.addStretch(1)
            live = QLabel("LIVE")
            live.setStyleSheet(f"color:{accent};font:bold 7pt 'Consolas';border:1px solid {accent};padding:2px 5px;")
            top.addWidget(live)
            box.addLayout(top)
            body = QHBoxLayout()
            gauge = _TelemetryGauge(accent)
            body.addWidget(gauge, 0, Qt.AlignmentFlag.AlignVCenter)
            details = QLabel(detail)
            details.setWordWrap(True)
            details.setMinimumWidth(90)
            details.setStyleSheet("color:#d8efff;font:9pt 'Segoe UI';line-height:1.35;")
            body.addWidget(details, 1, Qt.AlignmentFlag.AlignVCenter)
            box.addLayout(body, 1)
            plot = _MetricPlot()
            plot.setMinimumHeight(66)
            plot.setMaximumHeight(83)
            plot._colors[key.upper()] = QColor(accent)
            box.addWidget(plot)
            self._system_gauges[key] = gauge
            self._system_details[key] = details
            self._system_plots[key] = plot
            self._system_cards[key] = frame
            return frame

        grid.addWidget(metric_panel("cpu", "CPU", "#16baff", "Loading processor details…"), 0, 0)
        grid.addWidget(metric_panel("gpu", "GPU", "#d52dff", "Reading graphics adapter…"), 0, 1)
        grid.addWidget(metric_panel("ram", "MEMORY", "#00e5ad", "Reading physical memory…"), 0, 2)
        grid.addWidget(metric_panel("disk", "STORAGE", "#ffbd38", "Reading system drive…"), 1, 0)

        network = QFrame()
        network.setObjectName("telemetry_network")
        network.setMinimumHeight(205)
        network.setStyleSheet("QFrame#telemetry_network{background:#050f20;border:1px solid #15517c;} QLabel{border:0;background:transparent;}")
        net_box = QVBoxLayout(network)
        net_box.setContentsMargins(9, 7, 9, 7)
        net_top = QHBoxLayout()
        net_title = QLabel("NETWORK")
        net_title.setStyleSheet("color:#27aaff;font:bold 10pt 'Segoe UI';letter-spacing:1px;")
        net_top.addWidget(net_title)
        net_top.addStretch(1)
        net_live = QLabel("LIVE")
        net_live.setStyleSheet("color:#27aaff;font:bold 7pt 'Consolas';border:1px solid #27aaff;padding:2px 5px;")
        net_top.addWidget(net_live)
        net_box.addLayout(net_top)
        net_values = QHBoxLayout()
        self._system_rx_label = QLabel("↑  — Mbps  DOWNLOAD")
        self._system_rx_label.setStyleSheet("color:#00e5ad;font:bold 11pt 'Segoe UI';")
        self._system_tx_label = QLabel("↓  — Mbps  UPLOAD")
        self._system_tx_label.setStyleSheet("color:#27aaff;font:bold 11pt 'Segoe UI';")
        net_values.addWidget(self._system_rx_label)
        net_values.addWidget(self._system_tx_label)
        net_box.addLayout(net_values)
        self._system_net_plot = _MetricPlot()
        self._system_net_plot._colors.update({"RX": QColor("#00e5ad"), "TX": QColor("#168fff")})
        self._system_net_plot.setMinimumHeight(100)
        net_box.addWidget(self._system_net_plot, 1)
        self._system_cards["network"] = network
        grid.addWidget(network, 1, 1)

        quick = QFrame()
        quick.setObjectName("telemetry_quick")
        quick.setMinimumHeight(205)
        quick.setStyleSheet("QFrame#telemetry_quick{background:#050f20;border:1px solid #15517c;} QLabel{border:0;background:transparent;}")
        quick_box = QVBoxLayout(quick)
        quick_box.setContentsMargins(11, 9, 11, 9)
        quick_title = QLabel("QUICK INFO  /  DEVICE STATUS")
        quick_title.setStyleSheet("color:#68caff;font:bold 10pt 'Segoe UI';letter-spacing:1px;")
        quick_box.addWidget(quick_title)
        self._system_quick = QLabel("Operating system: —\nBattery: —\nUptime: —\nProcesses: —")
        self._system_quick.setWordWrap(True)
        self._system_quick.setStyleSheet("color:#d9efff;font:10pt 'Segoe UI';line-height:1.6;padding:4px;")
        quick_box.addWidget(self._system_quick, 1)
        self._system_temp = QLabel("TEMPERATURES   CPU  —     GPU  —")
        self._system_temp.setStyleSheet("color:#8cb9d5;font:bold 8pt 'Consolas';border-top:1px solid #163750;padding-top:8px;")
        quick_box.addWidget(self._system_temp)
        self._system_cards["overview"] = quick
        grid.addWidget(quick, 1, 2)
        for col in range(3):
            grid.setColumnStretch(col, 1)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:#020917;border:0;} QScrollBar:vertical{background:#06111f;width:9px;} QScrollBar::handle:vertical{background:#15517c;min-height:24px;}")
        scroll.setWidget(host)
        lay.addWidget(scroll, 1)
        return page

    def _make_timer_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        title = QLabel("LOCAL COUNTDOWN / POMODORO")
        title.setStyleSheet(f"color:{_TEAL};font:bold 9pt 'Courier New';")
        lay.addWidget(title)
        self._timer_face = QLabel("25:00")
        self._timer_face.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._timer_face.setStyleSheet(f"color:{_WHITE};font:bold 42pt 'Courier New';background:{_PANEL};border:1px solid {_BORDER};padding:20px;")
        lay.addWidget(self._timer_face, 1)
        controls = QHBoxLayout()
        self._timer_minutes = QLineEdit("25")
        self._timer_minutes.setPlaceholderText("minutes")
        self._timer_minutes.setMaximumWidth(90)
        self._timer_minutes.setStyleSheet(f"background:{_PANEL};color:{_TEXT};border:1px solid {_BORDER};padding:5px;")
        controls.addWidget(self._timer_minutes)
        for label, callback in (("START / PAUSE", self._toggle_timer), ("RESET", self._reset_timer), ("POMODORO 25/5", self._start_pomodoro)):
            b = QPushButton(label)
            b.setStyleSheet(self._button_style())
            b.clicked.connect(callback)
            controls.addWidget(b)
        controls.addStretch(1)
        lay.addLayout(controls)
        self._reminder_list = QLabel("No JARVIS reminders in this session.")
        self._reminder_list.setWordWrap(True)
        self._reminder_list.setStyleSheet(f"color:{_TEXT};background:{_PANEL};border:1px solid {_BORDER};padding:9px;")
        lay.addWidget(self._reminder_list)
        self._timer_tick = QTimer(self)
        self._timer_tick.timeout.connect(self._tick_timer)
        self._timer_tick.start(250)
        return page

    def _make_market_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        row = QHBoxLayout()
        self._market_symbol = QLineEdit()
        self._market_symbol.setPlaceholderText("Ticker / crypto pair (e.g. AAPL or BTC-USD)")
        self._market_symbol.setStyleSheet(f"background:{_PANEL};color:{_TEXT};border:1px solid {_BORDER};padding:6px;")
        self._market_symbol.returnPressed.connect(self._load_market)
        row.addWidget(self._market_symbol, 1)
        load = QPushButton("LOAD 1-DAY CHART")
        load.setStyleSheet(self._button_style())
        load.clicked.connect(self._load_market)
        row.addWidget(load)
        lay.addLayout(row)
        self._market_status = QLabel("Public quote history; no API key is stored by JARVIS.")
        self._market_status.setStyleSheet(f"color:{_DIM};font:8pt 'Courier New';padding:4px;")
        lay.addWidget(self._market_status)
        self._market_plot = _MetricPlot()
        lay.addWidget(self._market_plot, 1)
        return page

    def _select(self, key: str, title: str | None = None):
        index = self._page_index.get(key, self._page_index["system"])
        self.tabs.setCurrentIndex(index)
        self._view_title.setText((title or key.replace("_", " ")).upper()[:45])

    def show_view(self, key: str, title: str | None = None, focus: str | None = None):
        if key == "system":
            self.show_system_panel(focus)
            return
        self._select(key, title)

    def show_system_panel(self, focus: str | None = None):
        aliases = {
            "memory": "ram", "mem": "ram", "disk": "disk", "storage": "disk",
            "processor": "cpu", "graphics": "gpu", "temperature": "overview",
            "temps": "overview", "all": "overview", "system": "overview",
        }
        normalized = str(focus or "overview").strip().casefold()
        normalized = aliases.get(normalized, normalized)
        if normalized not in {"overview", "cpu", "gpu", "ram", "disk", "network"}:
            normalized = "overview"
        self._system_focus = normalized
        title = "SYSTEM TELEMETRY" if normalized == "overview" else f"SYSTEM TELEMETRY · {normalized.upper()}"
        self._select("system", title)
        for key, frame in self._system_cards.items():
            if key == "overview":
                focused = normalized == "overview"
            else:
                focused = key == normalized
            color = {
                "cpu": "#16baff", "gpu": "#d52dff", "ram": "#00e5ad",
                "disk": "#ffbd38", "network": "#27aaff", "overview": "#68caff",
            }.get(key, "#15517c")
            border = color if focused else "#15517c"
            frame.setStyleSheet(
                f"QFrame#{frame.objectName()}{{background:#050f20;border:1px solid {border};}}"
                "QLabel{border:0;background:transparent;}"
            )
        for key, gauge in self._system_gauges.items():
            gauge.set_focused(normalized == key)
        self._sample_system()

    def show_video(self, source: str, title: str = "JARVIS VIDEO"):
        source = str(source or "").strip()
        if not self._video_view or not source:
            return
        path = Path(source).expanduser()
        local = path.is_file()
        if local:
            source_url = QUrl.fromLocalFile(str(path.resolve())).toString()
            base_url = QUrl.fromLocalFile(str(path.resolve().parent) + os.sep)
            suffix = path.suffix.lower()
        elif _valid_http_url(source):
            source_url = source
            base_url = QUrl(source)
            suffix = Path(urlparse(source).path).suffix.lower()
        else:
            return

        yt = _youtube_id(source_url)
        if yt:
            # Give YouTube a stable HTTPS app origin. Its current embedded
            # player rejects clients that provide neither a Referer nor an
            # equivalent client identity (player error 153).
            app_origin = "https://jarvis.local"
            base_url = QUrl(app_origin + "/")
            watch_url = f"https://www.youtube.com/watch?v={yt}&autoplay=1&mute=1"
            media_html = """<!doctype html><html><head><meta charset='utf-8'>
            <style>html,body{margin:0;width:100%;height:100%;background:#05090b;overflow:hidden}
            #yt{width:100%;height:100%;border:0} #ytError{position:fixed;left:12px;right:12px;bottom:10px;
            color:#f0c58b;background:#111b20;padding:8px;font:13px 'Courier New';z-index:5}</style></head>
            <body><iframe id='yt' src='https://www.youtube-nocookie.com/embed/__VIDEO_ID__?autoplay=1&mute=1&controls=1&playsinline=1&enablejsapi=1&origin=https%3A%2F%2Fjarvis.local'
            allow='autoplay; encrypted-media; picture-in-picture' allowfullscreen></iframe>
            <div id='ytError' hidden></div><script>
            const fallbackUrl=__WATCH_URL__;
            function fallbackToWatch(reason){const box=document.getElementById('ytError');box.hidden=false;box.textContent=reason+' — opening the YouTube watch page inside the HUD…';setTimeout(()=>location.replace(fallbackUrl),900);}
            let apiReady=false;
            window.jarvisYtPlayer=null;
            function onYouTubeIframeAPIReady(){apiReady=true;window.jarvisYtPlayer=new YT.Player('yt',{events:{onError:(event)=>{
              const messages={2:'Invalid video request',5:'HTML5 playback error',100:'Video unavailable or private',101:'This video blocks embedded playback',150:'This video blocks embedded playback',153:'YouTube did not receive an HTTP Referer/client identity'};
              const message='YouTube player error '+event.data+': '+(messages[event.data]||'Playback failed');
              if([101,150,153].includes(event.data))fallbackToWatch(message);else{const box=document.getElementById('ytError');box.hidden=false;box.textContent=message;}
            }}});}
            setTimeout(()=>{if(!apiReady)fallbackToWatch('YouTube player did not initialize');},12000);
            </script><script async src='https://www.youtube.com/iframe_api'></script></body></html>"""
            media_html = media_html.replace("__VIDEO_ID__", yt).replace(
                "__WATCH_URL__", json.dumps(watch_url)
            )
            self._video_view.page().setAudioMuted(True)
        else:
            if not local and suffix not in _VIDEO_SUFFIXES:
                self._open_web_url(source)
                return
            media_html = f"""<!doctype html><html><body style='margin:0;background:#05090b;display:grid;place-items:center;height:100vh'>
            <video id='jarvisVideo' src='{html.escape(source_url, quote=True)}' controls autoplay muted playsinline style='width:100%;height:100%;object-fit:contain'></video>
            </body></html>"""
            self._video_view.page().setAudioMuted(True)
        self._video_label.setText((title or path.name if local else title or "VIDEO")[:70].upper())
        self._video_muted = True
        self._video_active = True
        self._video_sound.setText("ENABLE SOUND  ◌")
        self._video_view.setHtml(media_html, base_url)
        self._select("video", title or "VIDEO / MUTED")

    def _toggle_video_sound(self):
        if not self._video_view:
            return
        self.set_video_muted(not self._video_muted)

    def set_video_muted(self, muted: bool):
        if not self._video_view or not self._video_active:
            return
        self._video_muted = bool(muted)
        self._video_view.page().setAudioMuted(self._video_muted)
        if not self._video_muted:
            self._video_view.page().runJavaScript("""(()=>{const v=document.querySelector('video'); if(v){v.muted=false;v.volume=1;} const f=document.querySelector('iframe'); if(f){f.contentWindow.postMessage(JSON.stringify({event:'command',func:'unMute',args:[]}), '*');}})()""")
            self._video_sound.setText("MUTE SOUND  ◖))")
        else:
            self._video_view.page().runJavaScript("""(()=>{const v=document.querySelector('video'); if(v)v.muted=true; const f=document.querySelector('iframe'); if(f)f.contentWindow.postMessage(JSON.stringify({event:'command',func:'mute',args:[]}), '*');})()""")
            self._video_sound.setText("ENABLE SOUND  ◌")

    def _pause_video(self):
        if self._video_view and self._video_active:
            self._video_view.page().runJavaScript("""(()=>{const v=document.querySelector('video'); if(v)v.pause(); const f=document.querySelector('iframe'); if(f)f.contentWindow.postMessage(JSON.stringify({event:'command',func:'pauseVideo',args:[]}), '*');})()""")

    def seek_video(self, seconds: int = 10):
        if not self._video_view or not self._video_active:
            return
        seconds = max(5, min(120, int(seconds)))
        self._video_view.page().runJavaScript(f"""(()=>{{
          const v=document.querySelector('video');
          if(v){{v.currentTime=Math.max(0,v.currentTime+{seconds});return;}}
          const p=window.jarvisYtPlayer;
          if(p&&typeof p.getCurrentTime==='function'){{p.seekTo(p.getCurrentTime()+{seconds},true);return;}}
        }})()""")

    def stop_video(self):
        if not self._video_view or not self._video_active:
            return
        self._video_view.page().runJavaScript("""(()=>{
          const v=document.querySelector('video');if(v){v.pause();v.currentTime=0;}
          const p=window.jarvisYtPlayer;
          if(p&&typeof p.stopVideo==='function'){p.stopVideo();return;}
          const f=document.querySelector('iframe');
          if(f)f.contentWindow.postMessage(JSON.stringify({event:'command',func:'stopVideo',args:[]}), '*');
        })()""")
        self._video_view.page().setAudioMuted(True)
        self._video_active = False
        self._video_muted = True
        self._video_view.setHtml(
            "<!doctype html><html><body style='margin:0;background:#05090b'></body></html>"
        )
        self._video_label.setText("VIDEO STOPPED")
        self._video_sound.setText("ENABLE SOUND  ◌")

    def _pick_video(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a video", "", "Video (*.mp4 *.m4v *.webm *.mov *.mkv *.avi *.ogv)")
        if path:
            self.show_video(path, Path(path).name)

    def show_webpage(self, url: str, title: str = "ARTICLE"):
        self._open_web_url(url)
        self._view_title.setText((title or "ARTICLE").upper()[:45])

    def _open_web_url(self, url: str):
        if not self._web_view:
            return
        url = str(url or "").strip()
        if not _valid_http_url(url):
            return
        self._url_entry.setText(url)
        self._web_view.setUrl(QUrl(url))
        self._select("web", "ARTICLE / WEB PAGE")

    def _navigate_web(self):
        self._open_web_url(self._url_entry.text())

    def _web_back(self):
        if self._web_view:
            self._web_view.back()

    def _web_reload(self):
        if self._web_view:
            self._web_view.reload()

    def show_images(self, images, query: str = "IMAGE RESULTS"):
        items = []
        for item in images or []:
            if isinstance(item, dict):
                source = str(item.get("url") or item.get("path") or item.get("image") or "").strip()
                title = str(item.get("title") or item.get("name") or Path(source).name or "Image")
            else:
                source, title = str(item).strip(), "Image"
            if source and (Path(source).expanduser().is_file() or _valid_http_url(source)):
                items.append({"source": source, "title": title[:90]})
        while self._gallery_row.count():
            child = self._gallery_row.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self._gallery_buttons = []
        if not items:
            if self._image_view:
                self._image_view.setHtml(f"<body style='background:{_BG};color:{_DIM};font-family:monospace'>No image previews were returned.</body>")
        else:
            for item in items[:20]:
                button = QToolButton()
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
                button.setIconSize(QSize(64, 48))
                button.setText(item["title"][:22])
                button.setToolTip(item["source"])
                button.setMinimumWidth(120)
                button.setMinimumHeight(76)
                button.setMaximumWidth(180)
                button.setStyleSheet(self._button_style().replace("QPushButton", "QToolButton"))
                button.clicked.connect(lambda _checked=False, it=item: self._display_image(it))
                self._gallery_row.addWidget(button)
                index = len(self._gallery_buttons)
                self._gallery_buttons.append(button)
                local_path = Path(item["source"]).expanduser()
                if local_path.is_file():
                    px = QPixmap(str(local_path))
                    if not px.isNull():
                        button.setIcon(QIcon(px.scaled(64, 48, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)))
                else:
                    import threading
                    threading.Thread(target=self._fetch_thumbnail, args=(index, item["source"]), daemon=True, name="JarvisImageThumb").start()
            self._gallery_row.addStretch(1)
            self._display_image(items[0])
        self._select("images", query)

    def _fetch_thumbnail(self, index: int, url: str):
        try:
            req = Request(url, headers={"User-Agent": "JARVIS Desktop/1.0"})
            with urlopen(req, timeout=6) as response:
                mime = response.headers.get("Content-Type", "")
                data = response.read(2_000_001)
            if mime.startswith("image/") and 0 < len(data) <= 2_000_000:
                self.thumbnail_loaded.emit(index, data)
        except Exception:
            pass

    def _on_thumbnail_loaded(self, index: int, data: bytes):
        if index < 0 or index >= len(self._gallery_buttons):
            return
        px = QPixmap()
        if px.loadFromData(data):
            self._gallery_buttons[index].setIcon(QIcon(px.scaled(64, 48, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)))

    def _display_image(self, item: dict):
        source = item["source"]
        if self._image_view and isinstance(self._image_view, QWebEngineView):
            if Path(source).expanduser().is_file():
                src = QUrl.fromLocalFile(str(Path(source).expanduser().resolve())).toString()
            else:
                src = source
            page = f"<html><body style='margin:0;background:{_BG};display:grid;place-items:center;height:100vh'><img src='{html.escape(src, quote=True)}' style='max-width:100%;max-height:100%;object-fit:contain'><div style='position:fixed;bottom:8px;color:{_TEXT};font:12px monospace'>{html.escape(item['title'])}</div></body></html>"
            self._image_view.setHtml(page, QUrl("https://jarvis-view.local/"))
        elif Path(source).expanduser().is_file():
            px = QPixmap(source)
            if not px.isNull():
                self._image_view.setPixmap(px.scaled(self._image_view.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def show_document(self, path: str, summary: str = ""):
        file = Path(path).expanduser()
        if not file.is_file():
            return
        self._doc_title.setText(file.name[:70])
        self._doc_summary.setPlainText(str(summary or "Summary not available."))
        if file.suffix.lower() == ".pdf" and self._pdf_view and self._pdf_document:
            self._pdf_document.load(str(file.resolve()))
            self._doc_view_stack.setCurrentWidget(self._pdf_view)
        else:
            text = ""
            try:
                if file.suffix.lower() == ".docx":
                    from docx import Document
                    text = "\n\n".join(p.text for p in Document(file).paragraphs)
                elif file.suffix.lower() == ".pdf":
                    import pdfplumber
                    with pdfplumber.open(file) as pdf:
                        text = "\n\n".join(p.extract_text() or "" for p in pdf.pages)
                else:
                    text = file.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                text = f"In-HUD preview unavailable: {exc}\n\n{file}"
            self._doc_text.setPlainText(text[:250_000])
            self._doc_view_stack.setCurrentWidget(self._doc_text)
        self._select("document", "DOCUMENT / SUMMARY")

    def show_news(self, data, title: str = "NEWSFIRST / ADA DERANA", keep_holo: bool = False):
        items = []
        if isinstance(data, list):
            for value in data[:12]:
                if isinstance(value, dict):
                    items.append({
                        "title": str(value.get("title") or "Headline")[:180],
                        "detail": str(value.get("detail") or value.get("snippet") or value.get("description") or "")[:600],
                        "url": str(value.get("url") or value.get("link") or "")[:1000],
                        "source": str(value.get("source") or "")[:60],
                    })
                else:
                    items.append({"title": str(value)[:180], "detail": "", "url": "", "source": ""})
        else:
            text = str(data or "")
            url_re = re.compile(r"https?://[^\s<>]+", re.I)
            current_source = ""
            current = None
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                if re.match(r"^(?:Newsfirst|Ada Derana)\s*[—:-]", line, re.I):
                    current_source = line[:60]
                    continue
                if line.casefold().startswith("sri lankan local news"):
                    continue
                if line.startswith(("•", "- ", "* ")):
                    if current:
                        items.append(current)
                    current = {"title": line.lstrip("•-* ").strip()[:180], "detail": "", "url": "", "source": current_source}
                    continue
                if line.lower().startswith("source:"):
                    urls = url_re.findall(line)
                    if current and urls:
                        current["url"] = urls[0].rstrip(".,)")
                    continue
                if line.startswith(("http://", "https://")) and current:
                    current["url"] = line.rstrip(".,)")
                    continue
                if current:
                    current["detail"] = (current["detail"] + " " + line).strip()[:600]
            if current:
                items.append(current)
            if not items:
                for block in re.split(r"\n\s*\n", text):
                    block = block.strip()
                    if block:
                        urls = url_re.findall(block)
                        items.append({"title": block.splitlines()[0][:180],
                                      "detail": " ".join(block.splitlines()[1:])[:600],
                                      "url": urls[0].rstrip(".,)") if urls else "", "source": ""})
        self._last_news_items = items[:12]
        self._set_cards("news", self._last_news_items, title, field_map=("title", "detail", "source"), link_label="OPEN STORY")
        if keep_holo and "gestures" in self._page_index:
            self._select("gestures", "HOLO / LIVE NEWS")
        if self._holo_server is not None:
            self._holo_server.set_news_items(self._last_news_items)
        for index, item in enumerate(self._last_news_items):
            article_url = item.get("url", "")
            host = urlparse(article_url).hostname
            if (urlparse(article_url).scheme == "https" and host in _NEWS_PUBLISHER_HOSTS
                    and self._news_image_slots.acquire(blocking=False)):
                try:
                    threading.Thread(
                        target=self._fetch_news_image_guarded,
                        args=(index, article_url),
                        daemon=True,
                        name="JarvisHeadlineImage",
                    ).start()
                except Exception:
                    self._news_image_slots.release()

    def _fetch_news_image_guarded(self, index: int, article_url: str) -> None:
        try:
            self._fetch_news_image(index, article_url)
        finally:
            self._news_image_slots.release()

    def _fetch_news_image(self, index: int, article_url: str) -> None:
        """Fetch the publisher's OpenGraph image, capped and restricted to public HTTPS."""
        try:
            if not _is_public_https_url(article_url):
                return
            opener = build_opener(_PublicHttpsRedirectHandler())
            request = Request(article_url, headers={"User-Agent": "JARVIS Desktop/1.0", "Accept": "text/html"})
            with opener.open(request, timeout=7) as response:
                if not response.headers.get_content_type().startswith("text/html"):
                    return
                page = response.read(1_000_001)
                if len(page) > 1_000_000:
                    return
                charset = response.headers.get_content_charset() or "utf-8"
                page_url = response.geturl()
            parser = _OpenGraphImageParser()
            parser.feed(page.decode(charset, errors="replace"))
            image_url = urljoin(page_url, parser.image_url)
            if not parser.image_url or not _is_public_https_url(image_url):
                return
            request = Request(image_url, headers={"User-Agent": "JARVIS Desktop/1.0", "Accept": "image/*"})
            with opener.open(request, timeout=7) as response:
                content_type = response.headers.get_content_type().lower()
                data = response.read(2_000_001)
            if content_type in {"image/jpeg", "image/png", "image/webp", "image/gif"} and len(data) <= 2_000_000:
                self.news_image_loaded.emit(index, article_url, data, content_type)
        except Exception:
            return

    def _on_news_image_loaded(self, index: int, article_url: str, data: bytes, content_type: str) -> None:
        if (self._holo_server is not None and 0 <= index < len(self._last_news_items)
                and self._last_news_items[index].get("url") == article_url):
            self._holo_server.set_news_image(index, data, content_type)

    def show_email(self, items, title: str = "UNREAD / RECENT EMAIL"):
        self._set_cards("email", items, title, field_map=("subject", "from", "snippet", "date"))

    def show_calendar(self, events, title: str = "TODAY / UPCOMING AGENDA"):
        normalized = []
        for ev in events or []:
            if isinstance(ev, dict):
                start = ev.get("start", {})
                when = start.get("dateTime") or start.get("date") if isinstance(start, dict) else start
                normalized.append({"title": ev.get("summary", "(untitled)"), "time": when or "Time not set", "detail": ev.get("description", "")})
            else:
                normalized.append({"title": str(ev)})
        self._set_cards("calendar", normalized, title, field_map=("title", "time", "detail"))

    def show_weather(self, report: dict):
        self._set_cards("weather", report.get("days", []), f"WEATHER / {report.get('location','FORECAST')}", field_map=("day", "summary", "detail"))

    def show_flights(self, flights, origin: str, destination: str, date: str):
        self._flight_route = (destination, origin)
        items = []
        for flight in flights or []:
            if isinstance(flight, dict):
                items.append({"title": flight.get("airline", "Flight option"), "time": f"{flight.get('departure','--:--')} → {flight.get('arrival','--:--')}", "detail": f"{flight.get('duration','')} · {flight.get('stops',0)} stop(s) · {flight.get('price','')} {flight.get('currency','')}"})
        self._set_cards("flights", items, f"FLIGHTS / {origin} → {destination} / {date}", field_map=("title", "time", "detail"))

    def _set_cards(self, key: str, items, title: str, field_map=None, link_label: str = "OPEN"):
        self._select(key, title)
        host, layout = self._card_hosts[key]
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self._cards_status[key].setText(f"{len(items or [])} ITEMS  /  UPDATED {datetime.now().strftime('%H:%M:%S')}")
        for item_index, item in enumerate(list(items or [])[:40]):
            if not isinstance(item, dict):
                item = {"title": str(item)}
            card = QFrame()
            card.setStyleSheet(f"QFrame{{background:{_PANEL};border:1px solid {_BORDER};}}")
            row = QHBoxLayout(card)
            row.setContentsMargins(10, 8, 10, 8)
            detail_col = QVBoxLayout()
            title_key = (field_map or ("title",))[0]
            heading = QLabel(str(item.get(title_key) or item.get("title") or item.get("subject") or "Untitled")[:220])
            heading.setWordWrap(True)
            heading.setStyleSheet(f"color:{_WHITE};font:bold 9pt 'Courier New';border:0;")
            detail_col.addWidget(heading)
            for field in (field_map or ("title",))[1:]:
                value = item.get(field)
                if value:
                    label = QLabel(str(value)[:900])
                    label.setWordWrap(True)
                    label.setStyleSheet(f"color:{_TEXT if field not in ('date','time') else _TEAL};font:8pt 'Courier New';border:0;")
                    detail_col.addWidget(label)
            row.addLayout(detail_col, 1)
            url = str(item.get("url") or item.get("link") or "")
            if url and _valid_http_url(url):
                btn = QPushButton(link_label)
                btn.setStyleSheet(self._button_style())
                btn.clicked.connect(lambda _checked=False, target=url: self.show_webpage(target, "STORY"))
                row.addWidget(btn)
            layout.addWidget(card)
            # Brief staggered arrival: reads as the instrument board populating
            # its results, not a generic full-screen modal animation.
            effect = QGraphicsOpacityEffect(card)
            card.setGraphicsEffect(effect)
            animation = QPropertyAnimation(effect, b"opacity", card)
            animation.setDuration(260)
            animation.setStartValue(0.0)
            animation.setEndValue(1.0)
            animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            card._jarvis_card_animation = animation
            QTimer.singleShot(min(item_index * 38, 500), animation.start)
        layout.addStretch(1)

    def _sample_system(self):
        """Collect hardware metrics off the UI thread to keep the HUD responsive."""
        if not hasattr(self, "tabs") or self.tabs.currentIndex() != self._page_index.get("system", -1):
            return
        if self._system_sample_active:
            return
        self._system_sample_active = True
        threading.Thread(
            target=self._collect_system_sample,
            daemon=True,
            name="JarvisSystemTelemetry",
        ).start()

    def _collect_system_sample(self):
        try:
            from actions.system_monitor import get_system_status

            snapshot = get_system_status()
            self.system_metrics_loaded.emit(snapshot)
        except Exception as exc:
            self.system_metrics_loaded.emit({"error": str(exc)[:160]})

    def _on_system_metrics_loaded(self, snapshot: dict):
        self._system_sample_active = False
        if not isinstance(snapshot, dict) or snapshot.get("error"):
            self._system_status.setText("● SENSOR READ FAILED · RETRYING")
            return

        values = {
            "cpu": snapshot.get("cpu_percent"),
            "gpu": snapshot.get("gpu_percent"),
            "ram": snapshot.get("ram_percent"),
            "disk": snapshot.get("disk_percent"),
        }
        for key, value in values.items():
            self._system_gauges[key].set_value(value)
            self._system_gauges[key].set_focused(self._system_focus == key)
            self._system_plots[key].add_sample({key.upper(): value})

        cpu_name = str(snapshot.get("cpu_name") or "CPU model unavailable")
        cores = snapshot.get("cpu_cores") or "—"
        threads = snapshot.get("cpu_threads") or "—"
        freq = snapshot.get("cpu_freq_mhz")
        cpu_detail = f"{cpu_name}\n{cores} cores / {threads} threads"
        if freq:
            cpu_detail += f"\n{float(freq) / 1000:.2f} GHz current"
        self._system_details["cpu"].setText(cpu_detail)

        ram_used = snapshot.get("ram_used_gb")
        ram_total = snapshot.get("ram_total_gb")
        ram_available = snapshot.get("ram_available_gb")
        self._system_details["ram"].setText(
            f"{ram_used if ram_used is not None else '—'} / {ram_total if ram_total is not None else '—'} GB used\n"
            f"{ram_available if ram_available is not None else '—'} GB available"
        )
        self._system_details["gpu"].setText(str(snapshot.get("gpu_name") or "GPU model unavailable")[:100])
        self._system_details["disk"].setText(
            f"{snapshot.get('disk_used_gb') if snapshot.get('disk_used_gb') is not None else '—'} / "
            f"{snapshot.get('disk_total_gb') if snapshot.get('disk_total_gb') is not None else '—'} GB used\n"
            f"{snapshot.get('disk_free_gb') if snapshot.get('disk_free_gb') is not None else '—'} GB free"
        )

        rx = float(snapshot.get("network_rx_mbps") or 0.0)
        tx = float(snapshot.get("network_tx_mbps") or 0.0)
        self._system_rx_label.setText(f"↑  {rx:.1f} Mbps  DOWNLOAD")
        self._system_tx_label.setText(f"↓  {tx:.1f} Mbps  UPLOAD")
        self._system_net_plot.add_sample({"RX": rx, "TX": tx})

        battery = snapshot.get("battery_percent")
        if battery is None:
            battery_text = "Not reported by this device"
        else:
            battery_text = f"{battery:.0f}%  ·  {'AC power' if snapshot.get('battery_plugged') else 'on battery'}"
        self._system_quick.setText(
            f"Operating system: {snapshot.get('os_name', '—')}\n"
            f"Architecture: {snapshot.get('architecture', '—')}\n"
            f"Battery: {battery_text}\n"
            f"Uptime: {snapshot.get('uptime', '—')}   ·   Processes: {snapshot.get('process_count', '—')}"
        )
        self._system_temp.setText(
            f"TEMPERATURES   CPU  {snapshot.get('cpu_temp_c') if snapshot.get('cpu_temp_c') is not None else 'N/A'}°C"
            "     GPU  driver sensor unavailable"
        )
        self._system_hardware.setText(
            f"{cpu_name[:66]}    ·    {str(snapshot.get('gpu_name') or 'Graphics adapter unavailable')[:62]}    ·    "
            f"{snapshot.get('ram_total_gb', '—')} GB RAM    ·    {snapshot.get('os_name', 'Unknown OS')}"
        )
        self._system_status.setText(f"●  LIVE  /  UPDATED {datetime.now().strftime('%H:%M:%S')}")
        self._system_datetime.setText(datetime.now().strftime("%H:%M  ·  %a, %d %b %Y"))

    def _load_market(self):
        symbol = self._market_symbol.text().strip().upper()
        if not re.fullmatch(r"[A-Z0-9.^=_-]{1,24}", symbol):
            self._market_status.setText("Enter a ticker such as AAPL or BTC-USD.")
            return
        self._market_status.setText(f"Fetching {symbol}…")
        import threading
        threading.Thread(target=self._market_fetch, args=(symbol,), daemon=True, name="JarvisMarketFetch").start()

    def show_market_query(self, query: str):
        raw = str(query or "").strip()
        lowered = raw.lower()
        aliases = {"bitcoin": "BTC-USD", "btc": "BTC-USD", "ethereum": "ETH-USD", "eth": "ETH-USD", "solana": "SOL-USD", "sol": "SOL-USD"}
        symbol = next((value for name, value in aliases.items() if re.search(rf"\b{re.escape(name)}\b", lowered)), "")
        if not symbol:
            candidates = re.findall(r"\b[A-Z][A-Z0-9.^=_-]{0,12}\b", raw.upper())
            ignored = {"PRICE", "STOCK", "CHART", "SHOW", "QUOTE", "TODAY", "MARKET", "COST", "VALUE"}
            symbol = next((item for item in candidates if item not in ignored), "")
        self._market_symbol.setText(symbol)
        self._select("market", "MARKET HISTORY")
        if symbol:
            self._load_market()
        else:
            self._market_status.setText("Enter a ticker (for example AAPL) or crypto pair (BTC-USD) to load its chart.")

    def _market_fetch(self, symbol: str):
        try:
            from urllib.request import Request, urlopen
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=5m"
            req = Request(url, headers={"User-Agent": "JARVIS Desktop/1.0"})
            with urlopen(req, timeout=12) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            result = body["chart"]["result"][0]
            closes = [v for v in result.get("indicators", {}).get("quote", [{}])[0].get("close", []) if v is not None]
            meta = result.get("meta", {})
            self.market_loaded.emit({"symbol": symbol, "values": closes, "price": meta.get("regularMarketPrice"), "currency": meta.get("currency", ""), "error": "" if closes else "No intraday history was returned."})
        except Exception as exc:
            self.market_loaded.emit({"symbol": symbol, "values": [], "error": str(exc)[:180]})

    def _on_market_loaded(self, result: dict):
        if result.get("values"):
            self._market_plot.set_values("PRICE", result["values"])
            price = result.get("price")
            price_text = f"{price:,.4f} {result.get('currency','')}" if isinstance(price, (int, float)) else ""
            self._market_status.setText(f"{result['symbol']}  {price_text}  ·  1-DAY / 5-MINUTE PUBLIC HISTORY")
        else:
            self._market_status.setText(f"{result['symbol']}: {result.get('error') or 'No chart data available.'}")

    def add_reminder(self, message: str, target: datetime):
        self._reminders.append({"message": str(message), "target": target})
        self._reminder_list.setText("\n".join(f"{item['target']:%b %d, %I:%M %p}  ·  {item['message']}" for item in self._reminders[-10:]))

    def start_countdown(self, minutes: int, label: str = "COUNTDOWN"):
        minutes = max(1, min(24 * 60, int(minutes)))
        self._timer_minutes.setText(str(minutes))
        self._timer_total = minutes * 60
        self._timer_deadline = time.monotonic() + self._timer_total
        self._timer_running = True
        self._timer_face.setStyleSheet(f"color:{_WHITE};font:bold 42pt 'Courier New';background:{_PANEL};border:1px solid {_BORDER};padding:20px;")
        self._select("timers", label)
        self._tick_timer()

    def _toggle_timer(self):
        if self._timer_running:
            self._timer_running = False
            self._timer_total = max(0, int(self._timer_deadline - time.monotonic()))
        else:
            if self._timer_total <= 0:
                try:
                    self._timer_total = max(1, min(24 * 3600, int(self._timer_minutes.text()) * 60))
                except Exception:
                    self._timer_total = 25 * 60
            self._timer_deadline = time.monotonic() + self._timer_total
            self._timer_running = True
        self._tick_timer()

    def _reset_timer(self):
        self._timer_running = False
        try:
            self._timer_total = max(1, min(24 * 3600, int(self._timer_minutes.text()) * 60))
        except Exception:
            self._timer_total = 25 * 60
        self._timer_face.setText(f"{self._timer_total // 60:02d}:{self._timer_total % 60:02d}")

    def _start_pomodoro(self):
        self._pomodoro_work = not getattr(self, "_pomodoro_work", True)
        minutes = 25 if self._pomodoro_work else 5
        self._timer_minutes.setText(str(minutes))
        self._timer_total = minutes * 60
        self._timer_running = True
        self._timer_deadline = time.monotonic() + self._timer_total
        self._tick_timer()

    def _tick_timer(self):
        remaining = max(0, int(self._timer_deadline - time.monotonic())) if self._timer_running else self._timer_total
        self._timer_face.setText(f"{remaining // 60:02d}:{remaining % 60:02d}")
        if self._timer_running and remaining <= 0:
            self._timer_running = False
            self._timer_face.setStyleSheet(f"color:{_AMBER};font:bold 42pt 'Courier New';background:{_PANEL};border:1px solid {_BORDER};padding:20px;")
            self._timer_face.setText("TIME")
