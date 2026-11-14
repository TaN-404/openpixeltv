# OpenPixel TV

A TV-style IPTV player for the desktop, built on the [iptv-org](https://github.com/iptv-org/iptv) public API and PyQt6. Browse thousands of free live channels, assign your favourites a remote-control-friendly number, and tune in like a real set-top box.

![Platform](https://img.shields.io/badge/platform-Windows-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## ✨ Features

- **Full IPTV catalogue** — pulls `streams.json` + `channels.json` from iptv-org and automatically picks the best available stream per channel (resolution → HTTPS → HLS).
- **TV-style channel numbers** — assign any channel a number between 1 and 999, then type it on the remote (keyboard) to tune instantly.
- **Searchable channel guide** (`Alt+M`) — filter by name, country, category, quality, channel ID, or assigned number.
- **"My Channels" list** (`Alt+L`) — a compact view of everything you've numbered.
- **Built-in HLS proxy** — transparently rewrites `.m3u8` playlists so Qt's media backend can play streams it would otherwise reject (Samsung TV Plus / Amagi segments with non-`.ts` URLs, etc.). Also collapses multi-variant master playlists to a single best variant so Qt doesn't fall back to the tiny 320×180 rendition.
- **No account, no telemetry, no tracking** — everything stays local.

---

## 📥 Install

1. Go to the **[Releases](../../releases)** tab.
2. Download the latest `OpenPixelTV-Setup-x.y.z.exe`.
3. Run it. Windows SmartScreen may warn about an unsigned installer — click **More info → Run anyway**.
4. Launch **OpenPixel TV** from the Start Menu or the desktop shortcut.

Uninstalling via *Add or Remove Programs* removes the app but leaves your saved channel numbers and catalogue cache in place (see [Where your data lives](#-where-your-data-lives)).

---

## ⌨️ Keyboard shortcuts

### Global

| Key | Action |
|:---|:---|
| `Alt + M` | Open / close the full channel guide |
| `Alt + L` | Open / close **My Channels** |
| `0` – `9` | Type a channel number (auto-tunes after 3 s, or press `Enter`) |
| `Backspace` | Delete the last typed digit |
| `Space` | Play / pause |
| `F11` | Toggle fullscreen (double-click the video also works) |
| `F5` | Force re-download the channel catalogue |
| `Esc` | Close overlay → clear typed number → leave fullscreen |

### Inside the channel guide

| Key | Action |
|:---|:---|
| `↑` / `↓` | Move selection |
| `Page Up` / `Page Down` | Previous / next page |
| `Enter` | Play the selected channel |
| `Shift + Enter` | Assign a number to the selected channel |

### Inside "My Channels"

| Key | Action |
|:---|:---|
| `↑` / `↓` | Move selection |
| `Enter` | Play the selected channel |

---

## 💾 Where your data lives

| File | Purpose |
|:---|:---|
| `numbered_channels.json` | The channel numbers you've assigned |
| `catalog_cache.json` | Cached IPTV catalogue (refreshed every 24 h) |

Location:

- **Windows:** `%APPDATA%\OpenPixel\OpenPixelTV\`

The files are plain JSON — back them up, hand-edit them, sync them between machines. Deleting `catalog_cache.json` and pressing `F5` forces a fresh download.

---

## 🧱 Architecture notes

For developers digging into the source:

- **`build_catalog()`** merges the two iptv-org endpoints and picks the best stream per channel by `(resolution, HTTPS, HLS)`. Filters out streams that need custom Referer / User-Agent headers, since QtMultimedia can't send them.
- **`CatalogWorker`** is a `QThread` that loads the catalogue from the 24-hour disk cache or downloads it. Falls back to the stale cache when the API is unreachable.
- **`HlsProxy`** runs a `ThreadingHTTPServer` on `127.0.0.1:<random>` that:
  1. fetches upstream playlists,
  2. collapses multi-variant masters to a single best variant (`keep_best_variant`, using `m3u8` for parsing then pure text surgery to preserve every other tag byte-for-byte),
  3. rewrites every `URI="..."` and segment line to point back through the proxy with the original URL carried in `?u=...`,
  4. proxies segment data untouched (with `Range` header pass-through for seeking).
  This works around Qt's strict `allowed_segment_extensions` check and gives us variant selection Qt doesn't offer on its own.
- **`ChannelNumbers`** is the local storage layer for assignments. Atomic writes (`<file>.tmp` → rename), tolerant of hand-edited JSON.

---

## 🙏 Credits

- **[iptv-org](https://github.com/iptv-org/iptv)** — the community catalogue that makes this possible. Consider [contributing](https://github.com/iptv-org/iptv#contribute) a stream or fixing a dead link.
- **[Qt / PyQt6](https://www.riverbankcomputing.com/software/pyqt/)** — the media stack and widgets.
- **[Inno Setup](https://jrsoftware.org/isinfo.php)** — the installer.
- Everyone who has ever filed a "channel doesn't play" issue on iptv-org — you're why these catalogues stay honest.