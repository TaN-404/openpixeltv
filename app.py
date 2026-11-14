#!/usr/bin/env python3
"""OpenPixel TV - PyQt6 desktop edition.

A TV-style IPTV player built on the iptv-org API.

Keyboard shortcuts
------------------
Alt+M            Open / close the full channel guide
Alt+L            Open / close "My Channels" (channels you gave a number)
0-9              Type a channel number (tunes after 3 s, or press Enter)
Backspace        Delete the last typed digit
Space            Play / pause
F11              Toggle fullscreen (double-click the video works too)
F5               Re-download the channel catalogue
Esc              Close overlay / clear typed number / leave fullscreen

Inside the channel guide
------------------------
Up / Down        Move the selection
PageUp / PageDn  Previous / next page
Enter            Play the selected channel
Shift+Enter      Assign a number to the selected channel

Requirements:  pip install PyQt6 httpx
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlparse
import m3u8

import httpx
from PyQt6.QtCore import (
    QRegularExpression,
    QSettings,
    QSize,
    QSizeF,
    QStandardPaths,
    Qt,
    QThread,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QKeySequence,
    QPainter,
    QPalette,
    QRegularExpressionValidator,
    QShortcut,
)
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer, QPlaybackOptions
from PyQt6.QtMultimediaWidgets import QGraphicsVideoItem
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

STREAMS_API = "https://iptv-org.github.io/api/streams.json"
CHANNELS_API = "https://iptv-org.github.io/api/channels.json"

RESULTS_PER_PAGE = 50
CHANNEL_NUMBER_TIMEOUT_MS = 3000
NUMBER_MESSAGE_TIMEOUT_MS = 2500
CATALOG_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60


# --------------------------------------------------------------------------
# Catalogue building (same logic as the Reflex version)
# --------------------------------------------------------------------------


def quality_score(quality: object, url: str) -> tuple[int, int, int]:
    """Return a sortable score for selecting a channel's best stream.

    Resolution matters most; HTTPS and HLS are only tie-breakers.
    """

    quality_text = str(quality or "").strip().lower()
    height = 0

    aliases = {
        "8k": 4320,
        "uhd": 2160,
        "4k": 2160,
        "fhd": 1080,
        "full hd": 1080,
        "hd": 720,
        "sd": 480,
    }

    for label, alias_height in aliases.items():
        if label in quality_text:
            height = max(height, alias_height)

    resolution_match = re.search(r"(?<!\d)(\d{3,4})(?:p|i)?(?!\d)", quality_text)
    if resolution_match:
        height = max(height, int(resolution_match.group(1)))

    is_https = int(url.lower().startswith("https://"))
    is_hls = int(".m3u8" in url.lower())
    return height, is_https, is_hls


def is_direct_browser_stream(stream: dict) -> bool:
    """Keep streams that can be requested without custom headers.

    QtMultimedia cannot send a custom Referer / User-Agent either, so the
    same filter still makes sense for the desktop player.
    """

    url = stream.get("url")
    return bool(
        isinstance(url, str)
        and url.startswith(("https://", "http://"))
        and not stream.get("referrer")
        and not stream.get("user_agent")
    )


def build_catalog(api_streams: list, api_channels: list) -> list[dict[str, str]]:
    """Merge the two IPTV-org datasets into one best-stream-per-channel list."""

    channels_by_id = {
        channel["id"]: channel for channel in api_channels if channel.get("id")
    }

    best_record_by_id: dict[str, dict[str, str]] = {}
    best_score_by_id: dict[str, tuple[int, int, int]] = {}

    for stream in api_streams:
        channel_id = stream.get("channel")
        if not channel_id or not is_direct_browser_stream(stream):
            continue

        channel_information = channels_by_id.get(channel_id)
        if not channel_information:
            continue

        if channel_information.get("is_nsfw", False):
            continue

        url = stream["url"]
        score = quality_score(stream.get("quality"), url)

        if channel_id in best_score_by_id and score <= best_score_by_id[channel_id]:
            continue

        category_list = channel_information.get("categories") or []
        best_record_by_id[channel_id] = {
            "channel_id": channel_id,
            "title": str(
                channel_information.get("name") or stream.get("title") or channel_id
            ),
            "url": url,
            "quality": str(stream.get("quality") or "Adaptive/unknown"),
            "country": str(channel_information.get("country") or "Unknown"),
            "categories": (
                ", ".join(str(item) for item in category_list)
                if category_list
                else "Uncategorized"
            ),
        }
        best_score_by_id[channel_id] = score

    return sorted(best_record_by_id.values(), key=lambda c: c["title"].casefold())


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def app_data_dir() -> Path:
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation
    )
    path = Path(base) if base else Path.home() / ".openpixel-tv"
    path.mkdir(parents=True, exist_ok=True)
    return path


class ChannelNumbers:
    """Channel-number assignments, saved as JSON on disk.

    Replaces the browser's LocalStorage from the Reflex version.
    """

    FIELDS = ("channel_id", "title", "url")

    def __init__(self, path: Path):
        self.path = path
        self.assignments: dict[str, dict[str, str]] = {}
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}

        self.assignments = {}
        if not isinstance(data, dict):
            return
        for number, channel in data.items():
            if (
                isinstance(channel, dict)
                and str(number).isdigit()
                and int(number) > 0
                and channel.get("url")
            ):
                self.assignments[str(int(number))] = {
                    key: str(channel.get(key, "")) for key in self.FIELDS
                }

    def save(self) -> None:
        tmp_path = self.path.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(self.assignments, indent=2), encoding="utf-8")
            tmp_path.replace(self.path)
        except OSError as error:
            print(f"Could not save channel numbers: {error}", file=sys.stderr)

    def number_by_id(self) -> dict[str, str]:
        return {
            entry["channel_id"]: number
            for number, entry in self.assignments.items()
            if entry.get("channel_id")
        }

    def number_for(self, channel_id: str) -> str:
        return self.number_by_id().get(channel_id, "")

    def assign(self, number: str, channel: dict[str, str]) -> None:
        self.unassign(channel["channel_id"], save=False)
        self.assignments[number] = {key: str(channel[key]) for key in self.FIELDS}
        self.save()

    def unassign(self, channel_id: str, save: bool = True) -> None:
        for number in [
            n for n, e in self.assignments.items() if e.get("channel_id") == channel_id
        ]:
            del self.assignments[number]
        if save:
            self.save()

    def smallest(self) -> dict[str, str] | None:
        if not self.assignments:
            return None
        return self.assignments[min(self.assignments, key=int)]

    def sync_with_catalog(self, catalog: list[dict[str, str]]) -> None:
        """Refresh saved titles/URLs in case the API changed them."""

        by_id = {channel["channel_id"]: channel for channel in catalog}
        changed = False
        for entry in self.assignments.values():
            fresh = by_id.get(entry.get("channel_id", ""))
            if fresh and (entry["url"] != fresh["url"] or entry["title"] != fresh["title"]):
                entry["url"] = fresh["url"]
                entry["title"] = fresh["title"]
                changed = True
        if changed:
            self.save()


class CatalogWorker(QThread):
    """Downloads (or loads from the disk cache) the channel catalogue."""

    loaded = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, cache_file: Path, force: bool = False, parent=None):
        super().__init__(parent)
        self.cache_file = cache_file
        self.force = force

    def _read_cache(self) -> tuple[list, float] | None:
        try:
            age = time.time() - self.cache_file.stat().st_mtime
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                return data, age
        except (OSError, ValueError):
            pass
        return None

    def run(self) -> None:
        cached = self._read_cache()
        if cached and not self.force and cached[1] < CATALOG_CACHE_MAX_AGE_SECONDS:
            self.loaded.emit(cached[0])
            return

        try:
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                streams_response = client.get(STREAMS_API)
                channels_response = client.get(CHANNELS_API)
            streams_response.raise_for_status()
            channels_response.raise_for_status()
            catalog = build_catalog(streams_response.json(), channels_response.json())
        except httpx.HTTPError as error:
            if cached:  # offline? fall back to the stale cache
                self.loaded.emit(cached[0])
            else:
                self.failed.emit(f"Could not download IPTV data: {error}")
            return
        except (TypeError, ValueError, KeyError):
            self.failed.emit("The IPTV API returned data in an unexpected format.")
            return

        if not catalog:
            self.failed.emit("No browser-compatible IPTV channels were found.")
            return

        try:
            self.cache_file.write_text(json.dumps(catalog), encoding="utf-8")
        except OSError:
            pass
        self.loaded.emit(catalog)


# --------------------------------------------------------------------------
# Local HLS proxy
# --------------------------------------------------------------------------
#
# Some channels (e.g. Amagi/Samsung TV Plus) list their video segments as
# URLs like  ".../320x180_288000?bcn=1&...&redirect_url=...ts"  - no ".ts" in
# the path.  Qt's FFmpeg backend refuses those ("is not in
# allowed_segment_extensions"), every playlist parses as empty and the player
# buffers forever.  Qt gives us no way to set FFmpeg's `extension_picky=0`, so
# instead we serve the playlists from a tiny localhost server that rewrites
# every segment URL to  http://127.0.0.1:PORT/seg/stream.ts?u=<original>
# and fetches the real data (following redirects) on demand.

HLS_URI_ATTRIBUTE = re.compile(r'URI="([^"]*)"')
SEGMENT_EXTENSIONS = (
    ".ts", ".aac", ".m4s", ".mp4", ".m4a", ".m4v", ".mp3", ".ac3", ".vtt", ".webvtt",
)
PLAYLIST_URI_TAGS = {
    "#EXT-X-MEDIA",
    "#EXT-X-I-FRAME-STREAM-INF",
    "#EXT-X-RENDITION-REPORT",
}
KEY_URI_TAGS = {"#EXT-X-KEY", "#EXT-X-SESSION-KEY"}


def is_hls_url(url: str) -> bool:
    return ".m3u8" in url.lower()


def proxy_target(proxy_base: str, absolute_url: str, kind: str) -> str:
    """Build the localhost URL for `absolute_url`.

    kind: "pl" (playlist), "seg" (media segment), "key", or "map" (init data).
    """

    if kind == "pl":
        extension = ".m3u8"
    elif kind == "key":
        extension = ".key"
    else:
        path = urlparse(absolute_url).path.lower()
        extension = next((e for e in SEGMENT_EXTENSIONS if path.endswith(e)), ".ts")
        if kind == "map" and extension == ".ts":
            extension = ".mp4"
    # The trailing "&x=<ext>" makes the *whole URL* end in the extension too,
    # so both of FFmpeg's extension checks (path-based and last-dot-based) pass.
    return (
        f"{proxy_base}/{kind}/stream{extension}"
        f"?u={quote(absolute_url, safe='')}&x={extension}"
    )


# Qt's FFmpeg backend has no adaptive-bitrate switching: for a multi-quality
# ("master") playlist it simply plays whichever variant it picks - in practice
# often the tiniest one (e.g. 320x180), which looks extremely pixelated when
# stretched to your window.  So the proxy keeps only ONE variant: the best one
# whose height is <= MAX_STREAM_HEIGHT.  Lower it (e.g. 720) if your machine
# struggles with 1080p, or set the OPENPIXEL_MAX_HEIGHT environment variable.
try:
    MAX_STREAM_HEIGHT = int(os.environ.get("OPENPIXEL_MAX_HEIGHT", "1080"))
except ValueError:
    MAX_STREAM_HEIGHT = 1080

RESOLUTION_ATTRIBUTE = re.compile(r"RESOLUTION=(\d+)x(\d+)", re.IGNORECASE)
BANDWIDTH_ATTRIBUTE = re.compile(r"(?<![A-Za-z-])BANDWIDTH=(\d+)", re.IGNORECASE)


def keep_best_variant(text: str, max_height: int = MAX_STREAM_HEIGHT) -> str:
    """
    Remove all but the best variant from a master playlist.

    `m3u8` is used only to *identify* which variant is best. The actual
    transform is pure text surgery on the original playlist, so every
    other tag (#EXT-X-MEDIA, #EXT-X-KEY, #EXT-X-MAP, CODECS=..., etc.)
    is preserved byte-for-byte.
    """
    # ---- 1. identify the best variant via m3u8 -------------------------
    try:
        parsed = m3u8.loads(text)
        if not parsed.is_variant or len(parsed.playlists) < 2:
            return text

        def score(p):
            res = p.stream_info.resolution
            h = res[1] if res else 0
            bw = p.stream_info.bandwidth or 0
            return (h, bw)

        scored = [(score(p), p.uri) for p in parsed.playlists]
        allowed = [(s, u) for s, u in scored if s[0] == 0 or s[0] <= max_height]
        if not allowed:              # everything above the cap: pick smallest
            allowed = [min(scored, key=lambda x: x[0])]
        best_uri = max(allowed, key=lambda x: x[0])[1]
    except Exception:
        return text                  # anything goes wrong → leave untouched

    # ---- 2. locate every variant span in the ORIGINAL text -------------
    lines = text.splitlines()
    spans = []                       # list of (start_idx, end_exclusive, uri)
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("#EXT-X-STREAM-INF"):
            j = i + 1
            while j < len(lines) and (
                not lines[j].strip() or lines[j].strip().startswith("#")
            ):
                j += 1
            if j < len(lines):
                spans.append((i, j + 1, lines[j].strip()))
                i = j + 1
                continue
        i += 1

    if not spans:
        return text

    best_span = next(((s, e) for s, e, u in spans if u == best_uri), None)
    if best_span is None:            # URI didn't map back → don't touch
        return text

    # ---- 3. drop OTHER variant spans, keep everything else ------------
    drop = set()
    for s, e, _u in spans:
        if (s, e) != best_span:
            drop.update(range(s, e))

    return "\n".join(
        line
        for idx, line in enumerate(lines)
        if idx not in drop
        and not line.strip().startswith("#EXT-X-I-FRAME-STREAM-INF")
    )


def rewrite_playlist(text: str, base_url: str, proxy_base: str) -> str:
    """Point every URI in an m3u8 playlist at the local proxy."""

    text = keep_best_variant(text)
    output: list[str] = []
    next_line_is_playlist = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            output.append(raw_line)
            continue

        if line.startswith("#"):
            tag = line.split(":", 1)[0]
            if tag == "#EXT-X-STREAM-INF":
                next_line_is_playlist = True

            if "URI=" in line:
                if tag in PLAYLIST_URI_TAGS:
                    kind = "pl"
                elif tag in KEY_URI_TAGS:
                    kind = "key"
                elif tag == "#EXT-X-MAP":
                    kind = "map"
                else:
                    kind = "seg"

                def replace(match: re.Match, kind: str = kind) -> str:
                    absolute = urljoin(base_url, match.group(1))
                    return f'URI="{proxy_target(proxy_base, absolute, kind)}"'

                line = HLS_URI_ATTRIBUTE.sub(replace, line)
            output.append(line)
            continue

        kind = "pl" if next_line_is_playlist else "seg"
        next_line_is_playlist = False
        output.append(proxy_target(proxy_base, urljoin(base_url, line), kind))

    return "\n".join(output) + "\n"


class _HlsProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, client: httpx.Client):
        super().__init__(address, handler)
        self.client = client
        self.base_url = f"http://127.0.0.1:{self.server_address[1]}"


class _HlsProxyHandler(BaseHTTPRequestHandler):
    server_version = "OpenPixelProxy/1.0"

    def log_message(self, *args) -> None:  # keep the console quiet
        pass

    def do_GET(self) -> None:
        self._serve(send_body=True)

    def do_HEAD(self) -> None:
        self._serve(send_body=False)

    def _serve(self, send_body: bool) -> None:
        parsed = urlparse(self.path)
        kind = parsed.path.strip("/").split("/", 1)[0]
        target = parse_qs(parsed.query).get("u", [""])[0]
        if kind not in ("pl", "seg", "key", "map") or not target.startswith(
            ("http://", "https://")
        ):
            self.send_error(400)
            return

        request_headers = {}
        if "Range" in self.headers:
            request_headers["Range"] = self.headers["Range"]

        try:
            upstream = self.server.client.get(target, headers=request_headers)
        except httpx.HTTPError:
            self.send_error(502)
            return

        headers: dict[str, str] = {}
        status = upstream.status_code

        if (
            kind == "pl"
            and status == 200
            and upstream.content.lstrip().startswith(b"#EXTM3U")
        ):
            body = rewrite_playlist(
                upstream.text, str(upstream.url), self.server.base_url
            ).encode("utf-8")
            headers["Content-Type"] = "application/vnd.apple.mpegurl"
        else:
            body = upstream.content
            headers["Content-Type"] = upstream.headers.get(
                "content-type", "application/octet-stream"
            )
            if "content-range" in upstream.headers:
                headers["Content-Range"] = upstream.headers["content-range"]

        headers["Content-Length"] = str(len(body))
        headers["Cache-Control"] = "no-store"

        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if send_body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass


class HlsProxy:
    def __init__(self) -> None:
        self._server: _HlsProxyServer | None = None
        self._client: httpx.Client | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> None:
        if self._server:
            return
        self._client = httpx.Client(timeout=15, follow_redirects=True)
        self._server = _HlsProxyServer(("127.0.0.1", 0), _HlsProxyHandler, self._client)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def wrap(self, url: str) -> str:
        """Return the URL the player should open for `url`."""

        if self._server and is_hls_url(url):
            return proxy_target(self._server.base_url, url, "pl")
        return url

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._client:
            self._client.close()
            self._client = None


# --------------------------------------------------------------------------
# Small reusable widgets
# --------------------------------------------------------------------------


def make_button(text: str, object_name: str = "") -> QPushButton:
    """Buttons never take keyboard focus so global key handling keeps working."""

    button = QPushButton(text)
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if object_name:
        button.setObjectName(object_name)
    return button


def make_badge(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("badge")
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


class ElidedLabel(QLabel):
    """A single-line label that shows "..." instead of forcing the layout wider."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setPen(self.palette().color(QPalette.ColorRole.WindowText))
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(
            self.text(), Qt.TextElideMode.ElideRight, rect.width()
        )
        flags = (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter).value
        painter.drawText(rect, flags, text)


class NavLineEdit(QLineEdit):
    """Search box that forwards list-navigation keys instead of swallowing them."""

    move_up = pyqtSignal()
    move_down = pyqtSignal()
    page_up = pyqtSignal()
    page_down = pyqtSignal()
    choose = pyqtSignal(bool)  # True when Shift was held

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Down:
            self.move_down.emit()
        elif key == Qt.Key.Key_Up:
            self.move_up.emit()
        elif key == Qt.Key.Key_PageDown:
            self.page_down.emit()
        elif key == Qt.Key.Key_PageUp:
            self.page_up.emit()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.choose.emit(
                bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            )
        else:
            super().keyPressEvent(event)


def make_scroll_area() -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
    scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
    scroll.viewport().setAutoFillBackground(False)
    return scroll


def set_scroll_content(scroll: QScrollArea, content: QWidget) -> None:
    scroll.setWidget(content)
    # QScrollArea.setWidget() turns this on, which would paint an opaque
    # background over our translucent overlay.
    content.setAutoFillBackground(False)


def repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


# --------------------------------------------------------------------------
# Number-assignment dialog
# --------------------------------------------------------------------------


class NumberDialog(QDialog):
    def __init__(self, store: ChannelNumbers, channel: dict[str, str], parent=None):
        super().__init__(parent)
        self.store = store
        self.channel = channel
        self.number = ""
        self.removed = False

        self.setWindowTitle("Assign channel number")
        self.setModal(True)
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)

        title = QLabel("Assign channel number")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(title)

        description = QLabel(
            f"Assign a number between 1 and 999 to {channel.get('title', '')}."
        )
        description.setTextFormat(Qt.TextFormat.PlainText)
        description.setWordWrap(True)
        layout.addWidget(description)

        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Example: 101")
        self.edit.setMaxLength(3)
        self.edit.setValidator(QRegularExpressionValidator(QRegularExpression(r"\d{0,3}")))
        existing = store.number_for(channel["channel_id"])
        self.edit.setText(existing)
        self.edit.selectAll()
        layout.addWidget(self.edit)

        self.error_label = QLabel()
        self.error_label.setObjectName("error")
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        if existing:
            remove = make_button("Remove number")
            remove.setAutoDefault(False)
            remove.clicked.connect(self._remove)
            buttons.addWidget(remove)
        buttons.addStretch(1)
        cancel = make_button("Cancel")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        save = make_button("Save Number", "primary")
        save.setDefault(True)  # Enter in the text box triggers this
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

        self.edit.textChanged.connect(lambda _: self.error_label.hide())

    def _fail(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def _remove(self) -> None:
        self.removed = True
        self.accept()

    def _save(self) -> None:
        entered = self.edit.text().strip()
        if not entered:
            self._fail("Enter a channel number.")
            return

        number = str(int(entered))
        if number == "0":
            self._fail("Channel numbers must be between 1 and 999.")
            return

        existing = self.store.assignments.get(number)
        if existing and existing.get("channel_id") != self.channel["channel_id"]:
            self._fail(
                f"Channel {number} is already assigned to "
                f"{existing.get('title') or 'another channel'}."
            )
            return

        self.number = number
        self.accept()


# --------------------------------------------------------------------------
# Channel guide (Alt+M)
# --------------------------------------------------------------------------


class PaginationBar(QWidget):
    previous_clicked = pyqtSignal()
    next_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.prev_button = make_button("‹  Previous")
        self.next_button = make_button("Next  ›")
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setStyleSheet("color: #8b94a7;")
        self.prev_button.clicked.connect(self.previous_clicked)
        self.next_button.clicked.connect(self.next_clicked)
        layout.addWidget(self.prev_button)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.next_button)

    def update_state(self, page: int, pages: int, total: int) -> None:
        self.prev_button.setEnabled(page > 0)
        self.next_button.setEnabled(page + 1 < pages)
        self.label.setText(f"Page {page + 1} of {pages}  •  {total} channels")


class ChannelCard(QFrame):
    selected = pyqtSignal(int)
    play_requested = pyqtSignal(dict)
    assign_requested = pyqtSignal(dict)

    def __init__(self, index: int, channel: dict[str, str], number: str):
        super().__init__()
        self.index = index
        self.channel = channel
        self.setObjectName("card")
        self.setProperty("selected", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        icon = QLabel("📺")
        title = ElidedLabel(channel["title"])
        title.setStyleSheet("font-weight: 700; font-size: 15px;")
        title_row.addWidget(icon)
        title_row.addWidget(title, 1)
        layout.addLayout(title_row)

        badge_row = QHBoxLayout()
        badge_row.setSpacing(6)
        badge_row.addWidget(make_badge(channel["country"]))
        badge_row.addWidget(make_badge(channel["quality"]))
        badge_row.addStretch(1)
        layout.addLayout(badge_row)

        categories = ElidedLabel(channel["categories"])
        categories.setStyleSheet("color: #8b94a7; font-size: 12px;")
        layout.addWidget(categories)

        number_row = QHBoxLayout()
        self.number_badge = QLabel()
        self.number_badge.setObjectName("numberBadge")
        number_row.addWidget(self.number_badge)
        number_row.addStretch(1)
        layout.addLayout(number_row)
        self.set_number(number)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        assign = make_button("#  Assign")
        play = make_button("▶  Play", "primary")
        assign.clicked.connect(self._on_assign)
        play.clicked.connect(self._on_play)
        button_row.addWidget(assign, 1)
        button_row.addWidget(play, 1)
        layout.addLayout(button_row)

    def set_number(self, number: str) -> None:
        self.number_badge.setText(f"Channel {number}" if number else "Unassigned")
        self.number_badge.setProperty("assigned", bool(number))
        repolish(self.number_badge)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        repolish(self)

    def mousePressEvent(self, event) -> None:
        self.selected.emit(self.index)
        super().mousePressEvent(event)

    def _on_assign(self) -> None:
        self.selected.emit(self.index)
        self.assign_requested.emit(self.channel)

    def _on_play(self) -> None:
        self.selected.emit(self.index)
        self.play_requested.emit(self.channel)


class ChannelGuide(QWidget):
    """Full-screen translucent overlay listing the whole IPTV catalogue."""

    play_requested = pyqtSignal(str, str)  # url, title
    assign_requested = pyqtSignal(dict)
    close_requested = pyqtSignal()

    def __init__(self, store: ChannelNumbers, parent=None):
        super().__init__(parent)
        self.store = store
        self.setObjectName("overlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._catalog: list[dict[str, str]] = []
        self._blobs: list[str] = []
        self._filtered: list[dict[str, str]] = []
        self._page_items: list[dict[str, str]] = []
        self._cards: list[ChannelCard] = []
        self._page = 0
        self._pages = 1
        self._selected = 0
        self._columns = 3
        self._loading = True
        self._error = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- header (stays put while the grid scrolls) -------------------
        header = QWidget()
        header.setMaximumWidth(1400)
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(32, 28, 32, 12)
        header_layout.setSpacing(14)

        title_row = QHBoxLayout()
        heading = QLabel("📺 Channels")
        heading.setStyleSheet("font-size: 28px; font-weight: 700;")
        back = make_button("←  Back to TV   Esc")
        back.clicked.connect(self.close_requested)
        title_row.addWidget(heading)
        title_row.addStretch(1)
        title_row.addWidget(back)
        header_layout.addLayout(title_row)

        self.search = NavLineEdit()
        self.search.setPlaceholderText(
            "Search name, country, category, quality, ID or number..."
        )
        self.search.setClearButtonEnabled(True)
        header_layout.addWidget(self.search)

        self.error_label = QLabel()
        self.error_label.setObjectName("error")
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        header_layout.addWidget(self.error_label)

        self.top_pagination = PaginationBar()
        header_layout.addWidget(self.top_pagination)
        root.addWidget(header, 0, Qt.AlignmentFlag.AlignHCenter)

        # ---- scrolling grid ----------------------------------------------
        self.scroll = make_scroll_area()
        content = QWidget()
        content.setMaximumWidth(1400)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(32, 4, 32, 40)
        content_layout.setSpacing(16)

        self.state_label = QLabel()
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_label.setStyleSheet("color: #8b94a7; padding: 50px;")
        content_layout.addWidget(self.state_label)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(16)
        content_layout.addWidget(self.grid_host)

        self.bottom_pagination = PaginationBar()
        content_layout.addWidget(self.bottom_pagination)
        content_layout.addStretch(1)

        set_scroll_content(self.scroll, content)
        root.addWidget(self.scroll, 1)

        # ---- wiring ------------------------------------------------------
        self.search.textChanged.connect(lambda _: self._apply_filter(reset_page=True))
        self.search.move_down.connect(lambda: self._move_selection(1))
        self.search.move_up.connect(lambda: self._move_selection(-1))
        self.search.page_down.connect(self.next_page)
        self.search.page_up.connect(self.previous_page)
        self.search.choose.connect(self._choose)
        for bar in (self.top_pagination, self.bottom_pagination):
            bar.previous_clicked.connect(self.previous_page)
            bar.next_clicked.connect(self.next_page)

        self._update_state_label()
        self.top_pagination.update_state(0, 1, 0)
        self.bottom_pagination.update_state(0, 1, 0)

    # ---- public API ------------------------------------------------------

    def on_open(self) -> None:
        self._select(0)
        self.search.setFocus()

    def set_loading(self, loading: bool) -> None:
        self._loading = loading
        self._update_state_label()

    def set_error(self, message: str) -> None:
        self._error = message
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def set_catalog(self, catalog: list[dict[str, str]]) -> None:
        self._catalog = catalog
        self._blobs = [
            " ".join(
                (c["title"], c["channel_id"], c["country"], c["categories"], c["quality"])
            ).casefold()
            for c in catalog
        ]
        self.set_error("")
        self.set_loading(False)
        self._apply_filter(reset_page=True)

    def refresh_numbers(self) -> None:
        """Update the number badges in place (no rebuild, no scroll jump)."""

        numbers = self.store.number_by_id()
        for card in self._cards:
            card.set_number(numbers.get(card.channel["channel_id"], ""))

    # ---- filtering / paging ---------------------------------------------

    def _apply_filter(self, reset_page: bool) -> None:
        terms = self.search.text().casefold().split()
        if terms:
            numbers = self.store.number_by_id()
            matches = []
            for channel, blob in zip(self._catalog, self._blobs):
                text = blob + " " + numbers.get(channel["channel_id"], "")
                if all(term in text for term in terms):
                    matches.append(channel)
        else:
            matches = self._catalog

        self._filtered = matches
        self._pages = max(1, math.ceil(len(matches) / RESULTS_PER_PAGE))
        self._page = 0 if reset_page else min(self._page, self._pages - 1)
        self._render_page()

    def previous_page(self) -> None:
        if self._page > 0:
            self._page -= 1
            self._render_page()

    def next_page(self) -> None:
        if self._page + 1 < self._pages:
            self._page += 1
            self._render_page()

    def _render_page(self) -> None:
        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards = []

        start = self._page * RESULTS_PER_PAGE
        self._page_items = self._filtered[start : start + RESULTS_PER_PAGE]
        numbers = self.store.number_by_id()

        for index, channel in enumerate(self._page_items):
            card = ChannelCard(index, channel, numbers.get(channel["channel_id"], ""))
            card.selected.connect(self._select)
            card.play_requested.connect(
                lambda ch: self.play_requested.emit(ch["url"], ch["title"])
            )
            card.assign_requested.connect(self.assign_requested)
            self._cards.append(card)

        self._columns = self._compute_columns()
        self._relayout()
        self._selected = 0
        if self._cards:
            self._cards[0].set_selected(True)
        self.scroll.verticalScrollBar().setValue(0)

        self.top_pagination.update_state(self._page, self._pages, len(self._filtered))
        self.bottom_pagination.update_state(self._page, self._pages, len(self._filtered))
        self._update_state_label()

    def _update_state_label(self) -> None:
        if self._loading:
            self.state_label.setText("Loading the IPTV catalogue...")
            self.state_label.show()
        elif not self._cards:
            self.state_label.setText("No matching channels found.")
            self.state_label.show()
        else:
            self.state_label.hide()

    # ---- grid layout -----------------------------------------------------

    def _compute_columns(self) -> int:
        width = self.width()
        if width < 760:
            return 1
        if width < 1150:
            return 2
        return 3

    def _relayout(self) -> None:
        for card in self._cards:
            self.grid.removeWidget(card)
        for column in range(3):
            self.grid.setColumnStretch(column, 1 if column < self._columns else 0)
        for position, card in enumerate(self._cards):
            self.grid.addWidget(card, position // self._columns, position % self._columns)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        columns = self._compute_columns()
        if columns != self._columns:
            self._columns = columns
            self._relayout()

    # ---- selection -------------------------------------------------------

    def _select(self, index: int) -> None:
        if not self._cards:
            return
        index = max(0, min(index, len(self._cards) - 1))
        if 0 <= self._selected < len(self._cards):
            self._cards[self._selected].set_selected(False)
        self._selected = index
        self._cards[index].set_selected(True)
        self.scroll.ensureWidgetVisible(self._cards[index], 0, 30)

    def _move_selection(self, step: int) -> None:
        if self._cards:
            self._select((self._selected + step) % len(self._cards))

    def _choose(self, shift_held: bool) -> None:
        if not self._page_items:
            return
        channel = self._page_items[self._selected]
        if shift_held:
            self.assign_requested.emit(channel)
        else:
            self.play_requested.emit(channel["url"], channel["title"])


# --------------------------------------------------------------------------
# "My Channels" list (Alt+L)
# --------------------------------------------------------------------------


class AssignedRow(QFrame):
    selected = pyqtSignal(int)
    play_requested = pyqtSignal(dict)

    def __init__(self, index: int, channel: dict[str, str]):
        super().__init__()
        self.index = index
        self.channel = channel
        self.setObjectName("card")
        self.setProperty("selected", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(16)

        number = QLabel(channel["number"])
        number.setMinimumWidth(56)
        number.setAlignment(Qt.AlignmentFlag.AlignCenter)
        number.setStyleSheet("font-size: 24px; font-weight: 700; color: #93c5fd;")
        layout.addWidget(number)

        divider = QFrame()
        divider.setFixedSize(1, 32)
        divider.setStyleSheet("background: rgba(255,255,255,50);")
        layout.addWidget(divider)

        text_column = QVBoxLayout()
        text_column.setSpacing(0)
        title = ElidedLabel(channel["title"])
        title.setStyleSheet("font-weight: 700; font-size: 15px;")
        identifier = ElidedLabel(channel["channel_id"])
        identifier.setStyleSheet("color: #8b94a7; font-size: 12px;")
        text_column.addWidget(title)
        text_column.addWidget(identifier)
        layout.addLayout(text_column, 1)

        play = make_button("▶  Play", "primary")
        play.clicked.connect(lambda: self.play_requested.emit(self.channel))
        layout.addWidget(play)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        repolish(self)

    def mousePressEvent(self, event) -> None:
        self.selected.emit(self.index)
        super().mousePressEvent(event)


class AssignedGuide(QWidget):
    play_requested = pyqtSignal(str, str)
    close_requested = pyqtSignal()

    def __init__(self, store: ChannelNumbers, parent=None):
        super().__init__(parent)
        self.store = store
        self.setObjectName("overlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._rows: list[AssignedRow] = []
        self._items: list[dict[str, str]] = []
        self._selected = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setMaximumWidth(900)
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(32, 28, 32, 12)
        header_layout.setSpacing(14)

        title_row = QHBoxLayout()
        heading = QLabel("🔢 My Channels")
        heading.setStyleSheet("font-size: 28px; font-weight: 700;")
        back = make_button("←  Back to TV   Esc")
        back.clicked.connect(self.close_requested)
        title_row.addWidget(heading)
        title_row.addStretch(1)
        title_row.addWidget(back)
        header_layout.addLayout(title_row)

        self.search = NavLineEdit()
        self.search.setPlaceholderText("Search by number or name...")
        self.search.setClearButtonEnabled(True)
        header_layout.addWidget(self.search)
        root.addWidget(header, 0, Qt.AlignmentFlag.AlignHCenter)

        self.scroll = make_scroll_area()
        content = QWidget()
        content.setMaximumWidth(900)
        self.rows_layout = QVBoxLayout(content)
        self.rows_layout.setContentsMargins(32, 4, 32, 40)
        self.rows_layout.setSpacing(12)

        self.empty_label = QLabel()
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setStyleSheet("color: #8b94a7; padding: 50px;")
        self.rows_layout.addWidget(self.empty_label)
        self.rows_layout.addStretch(1)

        set_scroll_content(self.scroll, content)
        root.addWidget(self.scroll, 1)

        self.search.textChanged.connect(lambda _: self.reload(reset_selection=True))
        self.search.move_down.connect(lambda: self._move(1))
        self.search.move_up.connect(lambda: self._move(-1))
        self.search.choose.connect(lambda _shift: self._choose())

    def on_open(self) -> None:
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.reload(reset_selection=True)
        self.search.setFocus()

    def reload(self, reset_selection: bool = False) -> None:
        catalog = [
            {
                "number": number,
                "title": entry.get("title", ""),
                "channel_id": entry.get("channel_id", ""),
                "url": entry.get("url", ""),
            }
            for number, entry in self.store.assignments.items()
        ]
        catalog.sort(key=lambda c: int(c["number"]))

        search = self.search.text().strip().casefold()
        if search:
            catalog = [
                c for c in catalog if search in c["number"] or search in c["title"].casefold()
            ]
        self._items = catalog

        for row in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows = []

        for index, channel in enumerate(catalog):
            row = AssignedRow(index, channel)
            row.selected.connect(self._select)
            row.play_requested.connect(
                lambda ch: self.play_requested.emit(ch["url"], ch["title"])
            )
            self.rows_layout.insertWidget(index, row)
            self._rows.append(row)

        if reset_selection or self._selected >= len(self._rows):
            self._selected = 0
        if self._rows:
            self._rows[self._selected].set_selected(True)

        if self._rows:
            self.empty_label.hide()
        else:
            self.empty_label.setText(
                "No assigned channels match your search."
                if search
                else "No channels assigned yet. Open the channel menu (Alt+M) "
                "and use Assign to give a channel a number."
            )
            self.empty_label.show()

    def _select(self, index: int) -> None:
        if not self._rows:
            return
        index = max(0, min(index, len(self._rows) - 1))
        if 0 <= self._selected < len(self._rows):
            self._rows[self._selected].set_selected(False)
        self._selected = index
        self._rows[index].set_selected(True)
        self.scroll.ensureWidgetVisible(self._rows[index], 0, 30)

    def _move(self, step: int) -> None:
        if self._rows:
            self._select((self._selected + step) % len(self._rows))

    def _choose(self) -> None:
        if self._items:
            channel = self._items[self._selected]
            self.play_requested.emit(channel["url"], channel["title"])


# --------------------------------------------------------------------------
# Player stage (video + overlays)
# --------------------------------------------------------------------------


class VideoView(QGraphicsView):
    double_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QColor("black"))
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def mouseDoubleClickEvent(self, event) -> None:
        self.double_clicked.emit()
        event.accept()


class TopBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("topBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(30, 22, 30, 50)
        layout.setSpacing(10)

        title = QLabel("📺 OpenPixel TV")
        title.setStyleSheet("font-size: 24px; font-weight: 700; background: transparent;")
        layout.addWidget(title)
        layout.addStretch(1)

        self.assigned_button = make_button("🔢  My Channels    Alt+L")
        self.menu_button = make_button("☰  Channels    Alt+M")
        self.fullscreen_button = make_button("⛶")
        self.fullscreen_button.setToolTip("Toggle fullscreen (F11)")
        for button in (self.assigned_button, self.menu_button, self.fullscreen_button):
            layout.addWidget(button)


class BottomBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("bottomBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(30, 50, 30, 24)
        layout.setSpacing(12)

        self.title = ElidedLabel()
        self.title.setStyleSheet(
            "font-size: 20px; font-weight: 700; background: transparent;"
        )
        layout.addWidget(self.title, 1)

        self.play_button = make_button("▶")
        self.play_button.setFixedWidth(48)
        self.mute_button = make_button("🔊")
        self.mute_button.setFixedWidth(48)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(130)
        self.volume.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        layout.addWidget(self.play_button)
        layout.addWidget(self.mute_button)
        layout.addWidget(self.volume)

    def set_title(self, title: str) -> None:
        self.title.setText(title)

    def set_playing(self, playing: bool) -> None:
        self.play_button.setText("⏸" if playing else "▶")

    def set_muted(self, muted: bool) -> None:
        self.mute_button.setText("🔇" if muted else "🔊")


class NumberOverlay(QFrame):
    """The big "CHANNEL 12" box (or red "not assigned" message) top-right."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("numberOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setProperty("error", False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 12, 22, 12)
        layout.setSpacing(0)

        self.caption = QLabel("CHANNEL")
        self.caption.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.caption.setStyleSheet("color: rgba(255,255,255,180); font-weight: 700;")
        self.digits = QLabel()
        self.digits.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.caption)
        layout.addWidget(self.digits)
        self.hide()

    @staticmethod
    def _font(pixel_size: int, letter_spacing: float) -> QFont:
        font = QFont()
        font.setPixelSize(pixel_size)
        font.setBold(True)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, letter_spacing)
        return font

    def _set_error_mode(self, error: bool) -> None:
        self.setProperty("error", error)
        repolish(self)

    def show_digits(self, digits: str) -> None:
        self._set_error_mode(False)
        self.caption.show()
        self.digits.setFont(self._font(56, 6))
        self.digits.setText(digits)
        self._show()

    def show_message(self, message: str) -> None:
        self._set_error_mode(True)
        self.caption.hide()
        self.digits.setFont(self._font(18, 0))
        self.digits.setText(message)
        self._show()

    def _show(self) -> None:
        self.adjustSize()
        self.reposition()
        self.show()

    def reposition(self) -> None:
        parent = self.parentWidget()
        if parent:
            self.move(parent.width() - self.width() - 40, 100)


class Stage(QWidget):
    """Holds the video and every overlay; lays them out by hand so the
    overlays can float (translucent) above the video, like the web version."""

    def __init__(self, store: ChannelNumbers, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # Video (QGraphicsVideoItem lets normal widgets draw on top of it
        # reliably on every platform, unlike QVideoWidget).
        self.video_scene = QGraphicsScene(self)
        self.video_item = QGraphicsVideoItem()
        self.video_item.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.video_scene.addItem(self.video_item)
        self.video_view = VideoView(self)
        self.video_view.setScene(self.video_scene)

        # Creation order == stacking order (later = on top).
        self.status_label = QLabel(self)
        self.status_label.setObjectName("status")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.status_label.hide()

        self.number_overlay = NumberOverlay(self)
        self.bottom_bar = BottomBar(self)
        self.top_bar = TopBar(self)
        self.guide = ChannelGuide(store, self)
        self.assigned = AssignedGuide(store, self)
        self.guide.hide()
        self.assigned.hide()

        self.empty_hint = None  # placeholder shown when nothing is playing
        self._build_empty_hint()

    def _build_empty_hint(self) -> None:
        hint = QWidget(self)
        hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QVBoxLayout(hint)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon = QLabel("📺")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("font-size: 80px;")
        line1 = QLabel("Press Alt + M to select a channel")
        line1.setAlignment(Qt.AlignmentFlag.AlignCenter)
        line1.setStyleSheet("font-size: 18px;")
        line2 = QLabel("Press Alt + L to jump to your assigned channels")
        line2.setAlignment(Qt.AlignmentFlag.AlignCenter)
        line2.setStyleSheet("font-size: 14px; color: #8b94a7;")
        for widget in (icon, line1, line2):
            layout.addWidget(widget)
        # Sit just above the video but below the bars.
        hint.stackUnder(self.status_label)
        self.empty_hint = hint

    def set_hint_visible(self, visible: bool) -> None:
        if self.empty_hint:
            self.empty_hint.setVisible(visible)

    def show_status(self, text: str) -> None:
        if not text:
            self.status_label.hide()
            return
        self.status_label.setText(text)
        self.status_label.adjustSize()
        self._place_status()
        self.status_label.show()

    def _place_status(self) -> None:
        label = self.status_label
        label.move((self.width() - label.width()) // 2, int(self.height() * 0.45))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        width, height = self.width(), self.height()

        self.video_view.setGeometry(0, 0, width, height)
        self.video_scene.setSceneRect(0, 0, width, height)
        self.video_item.setSize(QSizeF(width, height))

        if self.empty_hint:
            self.empty_hint.setGeometry(0, 0, width, height)

        top_height = self.top_bar.sizeHint().height()
        self.top_bar.setGeometry(0, 0, width, top_height)
        bottom_height = self.bottom_bar.sizeHint().height()
        self.bottom_bar.setGeometry(0, height - bottom_height, width, bottom_height)

        self.guide.setGeometry(0, 0, width, height)
        self.assigned.setGeometry(0, 0, width, height)

        self.number_overlay.reposition()
        self._place_status()


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OpenPixel TV")
        self.resize(1280, 720)

        data_dir = app_data_dir()
        self.cache_file = data_dir / "catalog_cache.json"
        self.store = ChannelNumbers(data_dir / "numbered_channels.json")
        self.settings = QSettings()

        # Set OPENPIXEL_NO_PROXY=1 to bypass the HLS proxy (for debugging).
        self.hls_proxy = HlsProxy()
        if not os.environ.get("OPENPIXEL_NO_PROXY"):
            self.hls_proxy.start()

        self.view = "player"  # "player" | "menu" | "assigned"
        self.stream_url = ""
        self.stream_title = ""
        self.number_buffer = ""
        self.number_message = ""
        self.worker: CatalogWorker | None = None

        self.stage = Stage(self.store)
        self.setCentralWidget(self.stage)

        # ---- media ---------------------------------------------------------
        self.audio = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        
        try:
            options = QPlaybackOptions()
            options.setPlaybackIntent(QPlaybackOptions.PlaybackIntent.Playback)  # not LowLatency
            options.setNetworkTimeout(30000)      # ms — default is much shorter
            self.player.setPlaybackOptions(options)
        except (AttributeError, ImportError):
            pass  # older Qt — safe to skip

        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.stage.video_item)

        volume = self.settings.value("volume", 80, type=int)
        self.audio.setVolume(volume / 100)
        self.audio.setMuted(self.settings.value("muted", False, type=bool))
        bar = self.stage.bottom_bar
        bar.volume.setValue(volume)
        bar.set_muted(self.audio.isMuted())

        self.player.playbackStateChanged.connect(self._on_playback_state)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.errorOccurred.connect(self._on_player_error)

        bar.play_button.clicked.connect(self.toggle_playback)
        bar.mute_button.clicked.connect(self._toggle_mute)
        bar.volume.valueChanged.connect(self._on_volume_changed)

        # ---- top bar / overlays -------------------------------------------
        top = self.stage.top_bar
        top.menu_button.clicked.connect(self.open_menu)
        top.assigned_button.clicked.connect(self.open_assigned)
        top.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        self.stage.video_view.double_clicked.connect(self.toggle_fullscreen)

        self.stage.guide.play_requested.connect(self.play_stream)
        self.stage.guide.assign_requested.connect(self.assign_channel)
        self.stage.guide.close_requested.connect(self.close_overlays)
        self.stage.assigned.play_requested.connect(self.play_stream)
        self.stage.assigned.close_requested.connect(self.close_overlays)

        # ---- timers --------------------------------------------------------
        self.tune_timer = QTimer(self)
        self.tune_timer.setSingleShot(True)
        self.tune_timer.setInterval(CHANNEL_NUMBER_TIMEOUT_MS)
        self.tune_timer.timeout.connect(self._tune_to_buffer)

        self.message_timer = QTimer(self)
        self.message_timer.setSingleShot(True)
        self.message_timer.setInterval(NUMBER_MESSAGE_TIMEOUT_MS)
        self.message_timer.timeout.connect(self._clear_number_message)

        # ---- shortcuts -----------------------------------------------------
        for sequence, slot in (
            ("Alt+M", self.toggle_menu),
            ("Alt+L", self.toggle_assigned),
            ("Escape", self._on_escape),
            ("F11", self.toggle_fullscreen),
            ("F5", lambda: self.load_catalog(force=True)),
        ):
            QShortcut(QKeySequence(sequence), self).activated.connect(slot)

        self.stage.setFocus()
        QTimer.singleShot(0, self._startup)

    # ---- startup / catalogue ------------------------------------------------

    def _startup(self) -> None:
        # Like the web app: tune to the lowest assigned number on launch.
        channel = self.store.smallest()
        if channel:
            self.play_stream(channel["url"], channel["title"])
        self.load_catalog()

    def load_catalog(self, force: bool = False) -> None:
        if self.worker and self.worker.isRunning():
            return
        self.stage.guide.set_error("")
        self.stage.guide.set_loading(True)
        self.worker = CatalogWorker(self.cache_file, force=force, parent=self)
        self.worker.loaded.connect(self._on_catalog_loaded)
        self.worker.failed.connect(self._on_catalog_failed)
        self.worker.start()

    def _on_catalog_loaded(self, catalog: list) -> None:
        self.store.sync_with_catalog(catalog)
        self.stage.guide.set_catalog(catalog)
        self.stage.assigned.reload()

    def _on_catalog_failed(self, message: str) -> None:
        self.stage.guide.set_loading(False)
        self.stage.guide.set_error(message)

    # ---- views ---------------------------------------------------------------

    def set_view(self, view: str) -> None:
        self.view = view
        self.stage.guide.setVisible(view == "menu")
        self.stage.assigned.setVisible(view == "assigned")
        if view == "menu":
            self.stage.guide.on_open()
        elif view == "assigned":
            self.stage.assigned.on_open()
        else:
            self.stage.setFocus()

    def open_menu(self) -> None:
        self._reset_number_entry()
        self.set_view("menu")

    def open_assigned(self) -> None:
        self._reset_number_entry()
        self.set_view("assigned")

    def close_overlays(self) -> None:
        self.set_view("player")

    def toggle_menu(self) -> None:
        if self.view == "menu":
            self.close_overlays()
        else:
            self.open_menu()

    def toggle_assigned(self) -> None:
        if self.view == "assigned":
            self.close_overlays()
        else:
            self.open_assigned()

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _on_escape(self) -> None:
        if self.view in ("menu", "assigned"):
            self.close_overlays()
        elif self.number_buffer or self.number_message:
            self._reset_number_entry()
        elif self.isFullScreen():
            self.showNormal()

    # ---- playback --------------------------------------------------------------

    def play_stream(self, url: str, title: str) -> None:
        self.stream_url = url
        self.stream_title = title
        self.stage.bottom_bar.set_title(title)
        self.stage.set_hint_visible(False)
        self.stage.show_status("Loading…")
        self.close_overlays()

        self.player.stop()
        self.player.setSource(QUrl(self.hls_proxy.wrap(url)))
        self.player.play()

    def toggle_playback(self) -> None:
        if self.view != "player" or not self.stream_url:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _on_playback_state(self, state) -> None:
        self.stage.bottom_bar.set_playing(
            state == QMediaPlayer.PlaybackState.PlayingState
        )

    def _on_media_status(self, status) -> None:
        S = QMediaPlayer.MediaStatus
        if status in (S.LoadingMedia, S.BufferingMedia, S.StalledMedia):
            self.stage.show_status("Buffering…")
        elif status in (S.LoadedMedia, S.BufferedMedia, S.EndOfMedia):
            self.stage.show_status("")
        elif status == S.InvalidMedia:
            self.stage.show_status("This channel could not be played.")

    def _on_player_error(self, error, message: str) -> None:
        if error != QMediaPlayer.Error.NoError:
            self.stage.show_status(f"Playback error: {message or 'unknown error'}")

    def _toggle_mute(self) -> None:
        self.audio.setMuted(not self.audio.isMuted())
        self.stage.bottom_bar.set_muted(self.audio.isMuted())

    def _on_volume_changed(self, value: int) -> None:
        self.audio.setVolume(value / 100)
        if value > 0 and self.audio.isMuted():
            self.audio.setMuted(False)
            self.stage.bottom_bar.set_muted(False)

    # ---- channel numbers -------------------------------------------------------

    def assign_channel(self, channel: dict) -> None:
        dialog = NumberDialog(self.store, channel, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if dialog.removed:
                self.store.unassign(channel["channel_id"])
            else:
                self.store.assign(dialog.number, channel)
            self.stage.guide.refresh_numbers()
            self.stage.assigned.reload()

    def _reset_number_entry(self) -> None:
        self.tune_timer.stop()
        self.message_timer.stop()
        self.number_buffer = ""
        self.number_message = ""
        self._update_number_overlay()

    def _clear_number_message(self) -> None:
        self.number_message = ""
        self._update_number_overlay()

    def _update_number_overlay(self) -> None:
        overlay = self.stage.number_overlay
        if self.number_buffer:
            overlay.show_digits(self.number_buffer)
        elif self.number_message:
            overlay.show_message(self.number_message)
        else:
            overlay.hide()

    def _tune_to_buffer(self) -> None:
        self.tune_timer.stop()
        if not self.number_buffer:
            return

        number = str(int(self.number_buffer))
        channel = self.store.assignments.get(number)
        self.number_buffer = ""

        if not channel or not channel.get("url"):
            self.number_message = f"Channel {number} is not assigned."
            self.message_timer.start()
            self._update_number_overlay()
            return

        self.number_message = ""
        self._update_number_overlay()
        self.play_stream(channel["url"], channel["title"])

    # ---- keyboard (digits / space / backspace / enter in the player view) ------

    def keyPressEvent(self, event) -> None:
        if self.view != "player":
            super().keyPressEvent(event)
            return

        key = event.key()
        modifiers = event.modifiers()
        has_modifier = bool(
            modifiers
            & (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.AltModifier
                | Qt.KeyboardModifier.MetaModifier
            )
        )
        text = event.text()

        if len(text) == 1 and text in "0123456789" and not has_modifier:
            if len(self.number_buffer) < 3:
                self.number_buffer += text
                self.number_message = ""
                self.message_timer.stop()
                self.tune_timer.start()  # restarts the 3 s countdown
                self._update_number_overlay()
            return

        if key == Qt.Key.Key_Backspace and self.number_buffer:
            self.number_buffer = self.number_buffer[:-1]
            self.number_message = ""
            if self.number_buffer:
                self.tune_timer.start()
            else:
                self.tune_timer.stop()
            self._update_number_overlay()
            return

        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.number_buffer:
            self._tune_to_buffer()
            return

        if key == Qt.Key.Key_Space and not has_modifier:
            if not event.isAutoRepeat():  # a held key toggles only once
                self.toggle_playback()
            return

        super().keyPressEvent(event)

    # ---- shutdown --------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self.settings.setValue("volume", self.stage.bottom_bar.volume.value())
        self.settings.setValue("muted", self.audio.isMuted())
        self.player.stop()
        self.hls_proxy.stop()
        if self.worker and self.worker.isRunning():
            if not self.worker.wait(2000):
                self.worker.terminate()
                self.worker.wait()
        super().closeEvent(event)


# --------------------------------------------------------------------------
# Theme + entry point
# --------------------------------------------------------------------------

STYLESHEET = """
QWidget { color: #e6e9ef; font-size: 14px; }
QMainWindow, QDialog { background: #0f1420; }
QLabel { background: transparent; }

QPushButton {
    background: rgba(255, 255, 255, 26);
    border: none; border-radius: 8px; padding: 8px 14px;
}
QPushButton:hover    { background: rgba(255, 255, 255, 46); }
QPushButton:pressed  { background: rgba(255, 255, 255, 64); }
QPushButton:disabled { color: #5b6475; background: rgba(255, 255, 255, 12); }
QPushButton#primary       { background: #2563eb; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #3b82f6; }

QLineEdit {
    background: rgba(255, 255, 255, 18);
    border: 1px solid rgba(255, 255, 255, 46);
    border-radius: 10px; padding: 10px 14px;
    selection-background-color: #2563eb;
}
QLineEdit:focus { border: 1px solid #3b82f6; }

#overlay   { background: rgba(6, 9, 16, 235); }
#topBar    { background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                 stop:0 rgba(0, 0, 0, 217), stop:1 rgba(0, 0, 0, 0)); }
#bottomBar { background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                 stop:0 rgba(0, 0, 0, 0), stop:1 rgba(0, 0, 0, 204)); }

#numberOverlay {
    background: rgba(0, 0, 0, 199);
    border: 1px solid rgba(255, 255, 255, 38);
    border-radius: 10px;
}
#numberOverlay[error="true"] { background: rgba(180, 20, 20, 230); border: none; }

#status {
    background: rgba(0, 0, 0, 170); border-radius: 10px;
    padding: 10px 20px; font-size: 16px;
}

QFrame#card {
    background: rgba(255, 255, 255, 15);
    border: 2px solid rgba(255, 255, 255, 30);
    border-radius: 16px;
}
QFrame#card:hover { border-color: #60a5fa; }
QFrame#card[selected="true"] {
    background: rgba(59, 130, 246, 56);
    border: 2px solid #3b82f6;
}

QLabel#badge {
    background: rgba(255, 255, 255, 26); border-radius: 6px;
    padding: 2px 8px; font-size: 12px;
}
QLabel#numberBadge {
    background: rgba(255, 255, 255, 20); border-radius: 6px;
    padding: 2px 8px; font-size: 12px;
}
QLabel#numberBadge[assigned="true"] { background: rgba(59, 130, 246, 77); color: #bfdbfe; }

QLabel#error {
    background: rgba(220, 38, 38, 60); color: #fecaca;
    border: 1px solid rgba(220, 38, 38, 140);
    border-radius: 8px; padding: 8px 12px;
}

QScrollBar:vertical { background: transparent; width: 12px; margin: 0; }
QScrollBar::handle:vertical {
    background: rgba(255, 255, 255, 60); border-radius: 5px;
    min-height: 30px; margin: 2px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }

QSlider::groove:horizontal { height: 4px; background: rgba(255, 255, 255, 60); border-radius: 2px; }
QSlider::sub-page:horizontal { background: #3b82f6; border-radius: 2px; }
QSlider::handle:horizontal { background: white; width: 12px; margin: -5px 0; border-radius: 6px; }
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#0f1420"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#e6e9ef"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#151b2b"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#e6e9ef"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#1c2438"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#e6e9ef"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#2563eb"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#6b7488"))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)

def main() -> None:
    os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.symbolsresolver=false")

    if sys.platform == "win32":
        # Force FFmpeg backend — WMF struggles with HLS/AAC on Windows.
        os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")
        # Force *software* decoding ("" means "auto", NOT "off").
        os.environ.setdefault("QT_FFMPEG_DECODING_HW_DEVICE_TYPE", "none")
    else:
        # Linux/macOS: these are the sensible defaults.
        os.environ.setdefault("QT_FFMPEG_DECODING_HW_DEVICE_TYPE", "none")
        # PulseAudio override only matters on Linux.
        os.environ.setdefault("QT_AUDIO_BACKEND", "pulseaudio")

    QApplication.setOrganizationName("OpenPixel")
    QApplication.setApplicationName("OpenPixelTV")
    app = QApplication(sys.argv)
    apply_theme(app)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()