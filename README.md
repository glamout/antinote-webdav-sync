# Antinote WebDAV Sync

**English** | [简体中文](./README.zh-CN.md)

[![Platform](https://img.shields.io/badge/platform-macOS%2014%2B-blue)](#)
[![Runtime](https://img.shields.io/badge/dependencies-zero%20(native%20C%20%2B%20Python%20stdlib)-brightgreen)](#)
[![License](https://img.shields.io/badge/license-MIT-S)](LICENSE)

An ultra-lightweight background one-way real-time mirror service for macOS that syncs plain-text notes from the **[Antinote](https://antinote.io/)** menu-bar app (create, edit, rename, delete) into independent `.md` files under a target folder on a **WebDAV server** (`1 note = 1 .md file named after the note's first line`), keeping both sides strictly in sync.

## Core Features

* **Clean `First line of note.md` naming & 100% plain-text preservation**:
  * Automatically uses the first non-empty line of a note as the filename (strips `#` heading prefixes and illegal path characters; disambiguates duplicates as `First line (2).md`).
  * Generated `.md` files preserve **100% of the original text (including the first line)** — no YAML frontmatter is injected.
* **Smart filtering (Antinote 2.0 compatible)**:
  * Reads the live SQLite + `.sqlite-wal` + `.sqlite-shm` state.
  * By default filters out Antinote 2.0's 9 built-in Greek-deity slotted notes ([Slotted Notes: `Aphrodite` ~ `Zeus`](https://antinote.io/user-manual/slotted-notes)), onboarding/tutorial notes, and soft-deleted notes already moved to the recycle bin ([The Void](https://antinote.io/updates/antinote-2-0)).
* **Remote `.trash` recycle-bin & strict mirror alignment**:
  * When a note is deleted in Antinote, or when the periodic audit finds orphan `.md` files on the remote, they are automatically `MOVE`d into the remote `.trash/` subfolder (falls back to `PUT + DELETE` if cross-directory `MOVE` is unsupported).
  * If a note is accidentally deleted on the WebDAV side but still exists in Antinote, the next `PROPFIND` audit re-uploads it.
* **Single-app least-privilege architecture (`AntinoteWebDAVSync.app`) & ultra-low power**:
  * **No need to grant Full Disk Access to the system `/usr/bin/python3`**: the compiled native C daemon `AntinoteWebDAVSync.app` (resident memory **~1.5 MB**, idle CPU **0.0%**) copies the sandboxed container DB into a plain `mirror_db/` directory as its own signed app, then invokes the Python stdlib to perform incremental sync.
  * **Zero third-party dependencies**: only requires the macOS built-in `clang` and system `python3` stdlib — no Homebrew, no `pip install`.

## Project Layout

```text
antinote-webdav-sync/
├── .gitignore                  # Ignores real config.json & runtime state; prevents credential leaks
├── LICENSE                     # MIT License
├── README.md                   # This file (English)
├── README.zh-CN.md             # Chinese documentation
├── config.example.json         # Config template
├── install.sh                  # One-shot compile, install & upgrade script
├── uninstall.sh                # One-shot uninstall script
├── src/
│   ├── daemon.c                # Native C container DB watcher daemon (~1.5 MB RAM)
│   └── antinote_webdav_sync.py # Zero-dependency Python WebDAV one-way incremental mirror engine
└── tests/
    └── test_webdav_sync.py     # Built-in Mock WebDAV Server + SQLite WAL end-to-end test suite
```

## Quick Start

### 1. Clone & configure

```bash
git clone https://github.com/<your-username>/antinote-webdav-sync.git
cd antinote-webdav-sync
cp config.example.json config.json
open -e config.json
```

Fill in your WebDAV server details in `config.json`:

```json
{
  "webdav_url": "https://your-webdav-server.com/dav",
  "username": "your_username",
  "password": "your_password",
  "remote_folder": "/Antinote",
  "trash_folder": ".trash",
  "use_trash_on_delete": true,
  "clean_orphan_files": true,
  "include_slotted": false,
  "include_archived": false,
  "poll_interval_seconds": 2,
  "audit_interval_seconds": 300,
  "timeout_seconds": 15,
  "verify_ssl": true,
  "antinote_db_path": ""
}
```

> **Security note**: `config.json` is gitignored and will never be committed.

### 2. Run the installer

```bash
bash install.sh
```

### 3. Grant `AntinoteWebDAVSync.app` read access (first run only)

Because Antinote's database lives inside the macOS sandbox container `~/Library/Containers/com.chabomakers.Antinote/`:

1. If macOS shows **"AntinoteWebDAVSync would like to access data from other apps"**, click **Allow**.
2. If no popup appears, open **System Settings → Privacy & Security → Full Disk Access**, click `+`, press `⌘ + ⇧ + G`, enter `~/Applications/AntinoteWebDAVSync.app`, and toggle it on. (Only grant this one dedicated app — no need to grant Full Disk Access to `/usr/bin/python3`.)

## Configuration & Live Reload

After installation, the working directory is **`~/Library/Application Support/AntinoteWebDAVSync/`**. Edit the config at any time; changes take effect on the next poll cycle — no restart needed.

```bash
open -e "$HOME/Library/Application Support/AntinoteWebDAVSync/config.json"
```

| Field | Default | Description |
| :--- | :--- | :--- |
| `webdav_url` | required | WebDAV server root URL (spaces and non-ASCII paths are auto-escaped) |
| `username` / `password` | required | WebDAV credentials (or app-specific password) |
| `remote_folder` | `"/Antinote"` | Remote target folder (auto-created via `MKCOL` if missing) |
| `trash_folder` | `".trash"` | Remote recycle bin. Relative `".trash"` means `<remote_folder>/.trash` |
| `use_trash_on_delete` | `true` | `true` moves deleted notes to remote `.trash`; `false` hard-deletes via `DELETE` |
| `clean_orphan_files` | `true` | Auto-purge remote `.md` files that no longer correspond to an Antinote note |
| `include_slotted` | `false` | Sync Antinote 2.0's 9 Greek-deity slotted notes (default: filtered out) |
| `include_archived` | `false` | Sync Antinote's archived (hidden) notes |
| `poll_interval_seconds` | `2` | Local DB change-detection interval (responds within ~2 s) |
| `audit_interval_seconds` | `300` | Full remote `PROPFIND` reconciliation period (default: every 5 min) |
| `timeout_seconds` | `15` | Per-request WebDAV HTTP timeout (seconds) |
| `verify_ssl` | `true` | Verify HTTPS certs (set `false` for self-signed NAS certs) |
| `antinote_db_path` | `""` | Leave empty; the installer auto-detects and fills the Antinote `.sqlite` path |

## Useful Commands

* **Tail logs**:
  ```bash
  tail -n 30 /tmp/antinote-webdav-sync.log /tmp/antinote-webdav-sync.err
  ```
* **Check memory / CPU usage**:
  ```bash
  ps -o pid,%cpu,rss,comm -p $(pgrep -f AntinoteWebDAVSync) | awk 'NR==1{print $0, "MEM_MB"} NR>1{printf "%s %s %s %s (%.2f MB)\n", $1, $2, $3, $4, $3/1024}'
  ```
* **Run the end-to-end test suite**:
  ```bash
  python3 tests/test_webdav_sync.py
  ```
* **Uninstall**:
  ```bash
  bash "$HOME/Library/Application Support/AntinoteWebDAVSync/uninstall.sh"
  ```

## License

[MIT](./LICENSE)
