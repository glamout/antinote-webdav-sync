#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antinote -> WebDAV Server Ultra-Light macOS Real-Time Mirror Service (v2.3)
Compatible with macOS built-in /usr/bin/python3 (Python 3.8 / 3.9+), zero third-party dependencies.
- Supports reading from the C-mirrored database (--db=...) outside ~/Library/Containers
  so /usr/bin/python3 requires ZERO macOS TCC permissions.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import posixpath
import re
import signal
import shutil
import sqlite3
import ssl
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

APP_SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/AntinoteWebDAVSync")
DEFAULT_CONFIG_PATH = os.path.join(APP_SUPPORT_DIR, "config.json")
DEFAULT_STATE_PATH = os.path.join(APP_SUPPORT_DIR, "state.json")
LOCK_FILE_PATH = os.path.join(APP_SUPPORT_DIR, "daemon.lock")

GREEK_GOD_SLOT_NAMES = {
    "aphrodite",
    "ares",
    "cronus",
    "gaia",
    "hades",
    "hermes",
    "ouranos",
    "poseidon",
    "zeus",
}


# ============================================================================
# 1. Antinote SQLite + WAL Snapshot Collector & Fast Fingerprint
# ============================================================================

def expand_path(p):
    if not p:
        return ""
    return os.path.expanduser(str(p).strip())


def find_antinote_db(custom_path=""):
    if custom_path:
        resolved = expand_path(custom_path)
        if os.path.isfile(resolved):
            return resolved
        if os.path.isdir(resolved):
            found = search_for_db(resolved)
            if found:
                return found

    home = os.path.expanduser("~")
    known_container_db = os.path.join(
        home,
        "Library",
        "Containers",
        "com.chabomakers.Antinote",
        "Data",
        "Library",
        "Application Support",
        "cd-v1-notes.sqlite",
    )
    if os.path.isfile(known_container_db):
        return known_container_db

    candidates = [
        os.path.join(home, "Library", "Containers", "com.chabomakers.Antinote", "Data", "Library", "Application Support"),
        os.path.join(home, "Library", "Application Support", "Antinote"),
        os.path.join(home, "Library", "Application Support", "com.cmdn.antinote"),
        os.path.join(home, "Library", "Group Containers"),
        os.path.join(home, "Library", "Containers"),
    ]
    for d in candidates:
        if not os.path.exists(d):
            continue
        if "antinote" in d.lower():
            found = search_for_db(d)
            if found:
                return found
        else:
            try:
                for entry in os.scandir(d):
                    if entry.is_dir() and "antinote" in entry.name.lower():
                        sub = search_for_db(entry.path)
                        if sub:
                            return sub
            except OSError:
                pass
    return None


def search_for_db(root_dir):
    exts = (".sqlite", ".sqlite3", ".db")
    best_path = None
    best_mtime = -1.0

    def walk(curr, depth):
        nonlocal best_path, best_mtime
        if depth > 5:
            return
        try:
            entries = list(os.scandir(curr))
        except OSError:
            return
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                if re.search(r"backup|void|trash|cache|logs?", e.name, re.I):
                    continue
                walk(e.path, depth + 1)
            elif e.is_file(follow_symlinks=False):
                lower = e.name.lower()
                if not lower.endswith(exts):
                    continue
                if re.search(r"backup|bak|-wal$|-shm$|-journal$", lower, re.I):
                    continue
                try:
                    mtime = e.stat().st_mtime
                    for suf in ("-wal", "-shm"):
                        wpath = e.path + suf
                        if os.path.exists(wpath):
                            try:
                                w_mtime = os.stat(wpath).st_mtime
                                if w_mtime > mtime:
                                    mtime = w_mtime
                            except OSError:
                                pass
                    if mtime > best_mtime:
                        best_mtime = mtime
                        best_path = e.path
                except OSError:
                    pass

    walk(root_dir, 0)
    return best_path


def get_db_signature(db_path):
    h = hashlib.sha256()
    for suffix in ("", "-wal", "-shm"):
        p = db_path + suffix
        try:
            st = os.stat(p)
            h.update("{}:{}:{}|".format(suffix, st.st_mtime_ns, st.st_size).encode("utf-8"))
            with open(p, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    h.update(chunk)
        except OSError as e:
            if e.errno in (1, 13) and suffix == "":
                raise
            h.update("{}:missing|".format(suffix).encode("utf-8"))
    return h.hexdigest()[:24]


def content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


def normalize_nfc(text):
    return unicodedata.normalize("NFC", str(text or ""))


def extract_clean_title(content, max_len=80):
    lines = (content or "").splitlines()
    first_non_empty = ""
    for line in lines:
        s = line.strip()
        if s:
            first_non_empty = s
            break

    name = first_non_empty
    name = re.sub(r"^#+\s*", "", name)
    name = re.sub(r"^[-*+]\s+\[[ xX]\]\s*", "", name)
    name = re.sub(r'[/\\?%*:|"<>#\^\[\]]', " ", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".").strip()
    if not name:
        name = "Untitled"
    if len(name) > max_len:
        name = name[:max_len].rstrip().rstrip(".")
    return normalize_nfc(name or "Untitled")


def is_falsy_db_val(val):
    if val is None:
        return True
    s = str(val).strip().lower()
    return s in ("", "0", "false", "no", "null")


def analyze_columns(cols):
    m = {
        "id": None,
        "content": None,
        "created_at": None,
        "updated_at": None,
        "deleted_cols": [],
        "tutorial_cols": [],
        "archived_cols": [],
        "slot_bool_cols": [],
        "slot_index_cols": [],
        "space_cols": [],
    }
    for c in cols:
        lower = c.lower()
        if not m["id"] and re.match(r"^(id|uuid|note_?id|zid|z_pk)$", lower):
            m["id"] = c
        elif not m["content"] and re.match(r"^(content|body|text|note_?text|raw_?content|zcontent|ztext|zbody)$", lower):
            m["content"] = c
        elif not m["created_at"] and re.match(r"^(created_?at|created|date_?created|creation_?date|zcreated|zcreationdate)$", lower):
            m["created_at"] = c
        elif not m["updated_at"] and re.match(r"^(updated_?at|modified_?at|modified|last_?modified|date_?modified|zlastmodified|zmodificationdate)$", lower):
            m["updated_at"] = c

        if re.search(r"delete|trash|void|expire", lower):
            m["deleted_cols"].append(c)
        if re.search(r"tutorial|onboarding|sample", lower):
            m["tutorial_cols"].append(c)
        if re.search(r"archive", lower):
            m["archived_cols"].append(c)
        if re.match(r"^(is_?slotted|is_?slot|slotted|in_?slot|is_?permanent|permanent|zisslotted)$", lower):
            m["slot_bool_cols"].append(c)
        elif re.search(r"slot|planet", lower):
            m["slot_index_cols"].append(c)
        if re.match(r"^(space|note_?space|type|note_?type|kind|category)$", lower):
            m["space_cols"].append(c)
    return m


def detect_slotted_ids(rows, m):
    slotted_ids = set()
    slot_zero_is_slotted = {}

    for col in m["slot_index_cols"]:
        has_null_or_neg = False
        zero_count = 0
        for r in rows:
            v = r.get(col)
            if v is None or str(v).strip() == "":
                has_null_or_neg = True
            else:
                try:
                    num = float(v)
                    if num < 0:
                        has_null_or_neg = True
                    elif num == 0:
                        zero_count += 1
                except ValueError:
                    pass
        slot_zero_is_slotted[col] = has_null_or_neg and zero_count <= 1

    for r in rows:
        nid = str(r[m["id"]])
        is_slot = False

        for bcol in m["slot_bool_cols"]:
            if not is_falsy_db_val(r.get(bcol)):
                is_slot = True
                break

        if not is_slot:
            for scol in m["slot_index_cols"]:
                val = r.get(scol)
                if val is None or str(val).strip() == "":
                    continue
                try:
                    num = float(val)
                    if num > 0 or (num == 0 and slot_zero_is_slotted.get(scol, False)):
                        is_slot = True
                        break
                except ValueError:
                    if not is_falsy_db_val(val):
                        is_slot = True
                        break

        if not is_slot:
            for spcol in m["space_cols"]:
                spval = r.get(spcol)
                if spval is not None and re.search(r"slot|permanent|planet", str(spval), re.I):
                    is_slot = True
                    break

        if not is_slot:
            raw_text = str(r.get(m["content"]) or "")
            if extract_clean_title(raw_text).lower() in GREEK_GOD_SLOT_NAMES:
                is_slot = True

        if is_slot:
            slotted_ids.add(nid)

    return slotted_ids


def fetch_active_notes(db_path, include_slotted=False, include_archived=False):
    tmp_dir = tempfile.mkdtemp(prefix="antinote-webdav-")
    try:
        tmp_db = os.path.join(tmp_dir, "antinote.sqlite")
        shutil.copy2(db_path, tmp_db)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(db_path + suffix):
                try:
                    shutil.copy2(db_path + suffix, tmp_db + suffix)
                except OSError:
                    pass

        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            table_names = [str(r["name"]) for r in cur.fetchall()]
            candidates = []
            for tname in table_names:
                if re.search(r"(_fts|_config|_data|_idx|_docsize|_content|backup|void_log|schema_migrations|meta)$", tname, re.I):
                    continue
                safe_t = tname.replace('"', '""')
                cols = [str(r["name"]) for r in conn.execute('PRAGMA table_info("{}")'.format(safe_t)).fetchall()]
                cmap = analyze_columns(cols)
                if cmap["id"] and cmap["content"]:
                    candidates.append((tname, cols, cmap))

            if not candidates:
                return []

            chosen = None
            for item in candidates:
                if re.match(r"^(notes|note|znote)$", item[0], re.I):
                    chosen = item
                    break
            if not chosen:
                for item in candidates:
                    if re.search(r"note", item[0], re.I):
                        chosen = item
                        break
            if not chosen:
                chosen = candidates[0]

            tname, _cols, cmap = chosen
            safe_t = tname.replace('"', '""')
            id_col_q = cmap["id"].replace('"', '""')
            if cmap["created_at"]:
                c_col_q = cmap["created_at"].replace('"', '""')
                order_sql = 'ORDER BY "{}" ASC, "{}" ASC'.format(c_col_q, id_col_q)
            else:
                order_sql = 'ORDER BY "{}" ASC'.format(id_col_q)

            rows = [dict(r) for r in conn.execute('SELECT * FROM "{}" {}'.format(safe_t, order_sql)).fetchall()]
        finally:
            conn.close()

        if not rows:
            return []

        slotted_ids = set() if include_slotted else detect_slotted_ids(rows, cmap)
        active_notes = []

        for r in rows:
            nid = str(r[cmap["id"]])
            if any(not is_falsy_db_val(r.get(c)) for c in cmap["deleted_cols"]):
                continue
            if any(not is_falsy_db_val(r.get(c)) for c in cmap["tutorial_cols"]):
                continue
            if not include_archived and any(not is_falsy_db_val(r.get(c)) for c in cmap["archived_cols"]):
                continue
            if not include_slotted and nid in slotted_ids:
                continue

            raw_content = r.get(cmap["content"])
            if raw_content is None:
                continue
            content_str = str(raw_content)
            if not content_str.strip():
                continue

            active_notes.append({
                "id": nid,
                "content": content_str,
                "createdAt": r.get(cmap["created_at"]) if cmap["created_at"] else None,
                "updatedAt": r.get(cmap["updated_at"]) if cmap["updated_at"] else None,
            })

        return active_notes
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def assign_target_filenames(notes, prev_state):
    assigned = {}
    used_lower = set()

    for note in notes:
        nid = note["id"]
        prev = prev_state.get(nid)
        if not prev or not prev.get("filename"):
            continue
        base_title = extract_clean_title(note["content"])
        expected_primary = "{}.md".format(base_title)
        fname = normalize_nfc(prev["filename"])
        if (fname == expected_primary or fname.startswith("{} (".format(base_title))) and fname.lower() not in used_lower:
            assigned[nid] = fname
            used_lower.add(fname.lower())

    for note in notes:
        nid = note["id"]
        if nid in assigned:
            continue
        title = extract_clean_title(note["content"])
        candidate = "{}.md".format(title)
        counter = 2
        while candidate.lower() in used_lower:
            candidate = "{} ({}).md".format(title, counter)
            counter += 1
        assigned[nid] = candidate
        used_lower.add(candidate.lower())

    return assigned


# ============================================================================
# 2. Zero-Dependency WebDAV Protocol Client (with Safe URL & Redirect Handling)
# ============================================================================

def sanitize_and_encode_url(raw_url):
    s = str(raw_url or "").strip()
    parts = urllib.parse.urlsplit(s)
    unquoted_path = urllib.parse.unquote(parts.path or "")
    trailing_slash = unquoted_path.endswith("/") and len(unquoted_path) > 1
    segments = [
        urllib.parse.quote(normalize_nfc(seg), safe="")
        for seg in unquoted_path.split("/")
        if seg
    ]
    encoded_path = ("/" + "/".join(segments)) if segments else ""
    if trailing_slash and encoded_path:
        encoded_path += "/"
    elif not encoded_path and parts.path == "/":
        encoded_path = "/"
    return urllib.parse.urlunsplit((
        parts.scheme,
        parts.netloc,
        encoded_path,
        parts.query,
        parts.fragment,
    ))


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class WebDAVClient(object):
    def __init__(self, base_url, username, password, timeout=15, verify_ssl=True):
        clean_base = sanitize_and_encode_url(base_url).rstrip("/")
        self.base_url = clean_base
        self.username = username or ""
        self.password = password or ""
        self.timeout = timeout
        self.ssl_context = ssl.create_default_context()
        if not verify_ssl:
            self.ssl_context.check_hostname = False
            self.ssl_context.verify_mode = ssl.CERT_NONE

        token = "{}:{}".format(self.username, self.password).encode("utf-8")
        self.auth_header = "Basic " + base64.b64encode(token).decode("ascii")
        self._ensured_dirs = set()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self.ssl_context),
            NoRedirectHandler(),
        )

    def _encode_rel_path(self, rel_path):
        raw = urllib.parse.unquote(str(rel_path or "")).strip("/")
        if not raw:
            return ""
        segments = [urllib.parse.quote(normalize_nfc(seg), safe="") for seg in raw.split("/") if seg]
        return "/" + "/".join(segments)

    def build_url(self, rel_path, is_dir=False):
        encoded = self._encode_rel_path(rel_path)
        url = self.base_url + encoded
        if is_dir and not url.endswith("/"):
            url += "/"
        return url

    def request(self, method, url, data=None, headers=None, expected_codes=(200, 201, 204, 207), max_redirects=5):
        current_url = sanitize_and_encode_url(url)
        req_headers = {
            "Authorization": self.auth_header,
            "User-Agent": "AntinoteWebDAVSync/2.3",
        }
        if headers:
            for k, v in headers.items():
                if k.lower() == "destination":
                    req_headers[k] = sanitize_and_encode_url(v)
                else:
                    req_headers[k] = v

        for _ in range(max_redirects + 1):
            req = urllib.request.Request(current_url, data=data, headers=req_headers, method=method)
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    status = resp.getcode()
                    body = resp.read()
                    return status, body
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308):
                    loc = e.headers.get("Location") or e.headers.get("location")
                    if loc:
                        joined = urllib.parse.urljoin(current_url, loc)
                        current_url = sanitize_and_encode_url(joined)
                        continue
                body = e.read() if hasattr(e, "read") else b""
                if expected_codes and e.code in expected_codes:
                    return e.code, body
                raise RuntimeError("WebDAV {} {} failed with HTTP {}: {}".format(
                    method, current_url, e.code, body.decode("utf-8", errors="ignore")[:200]
                ))
            except urllib.error.URLError as e:
                raise RuntimeError("WebDAV network error on {} {}: {}".format(method, current_url, e.reason))

        raise RuntimeError("WebDAV {} {} exceeded maximum redirects".format(method, url))

    def ensure_dir(self, rel_dir):
        clean = "/" + urllib.parse.unquote(str(rel_dir or "")).strip("/")
        if clean in self._ensured_dirs:
            return
        if clean != "/":
            parts = [p for p in clean.split("/") if p]
            accum = ""
            for p in parts:
                accum += "/" + p
                if accum in self._ensured_dirs:
                    continue
                url = self.build_url(accum, is_dir=True)
                self.request("MKCOL", url, expected_codes=(200, 201, 204, 301, 405))
                self._ensured_dirs.add(accum)
        else:
            self._ensured_dirs.add("/")

    def list_md_files(self, rel_dir):
        self.ensure_dir(rel_dir)
        dir_url = self.build_url(rel_dir, is_dir=True)
        propfind_xml = (
            b'<?xml version="1.0" encoding="utf-8" ?>'
            b'<D:propfind xmlns:D="DAV:"><D:prop><D:resourcetype/></D:prop></D:propfind>'
        )
        _status, body = self.request(
            "PROPFIND",
            dir_url,
            data=propfind_xml,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            expected_codes=(200, 207),
        )
        if not body:
            return {}

        root = ET.fromstring(body)
        req_path = urllib.parse.urlparse(dir_url).path.rstrip("/")
        req_path_unquoted = normalize_nfc(urllib.parse.unquote(req_path)).rstrip("/")

        md_files = {}
        for resp_el in root.iter():
            if not resp_el.tag.endswith("response"):
                continue
            href_text = None
            is_collection = False
            for child in resp_el.iter():
                if child.tag.endswith("href") and href_text is None and child.text:
                    href_text = child.text.strip()
                elif child.tag.endswith("collection"):
                    is_collection = True
            if not href_text or is_collection:
                continue

            parsed_href = urllib.parse.urlparse(href_text).path
            unquoted_href = normalize_nfc(urllib.parse.unquote(parsed_href)).rstrip("/")
            if unquoted_href == req_path_unquoted:
                continue

            base_name = posixpath.basename(unquoted_href)
            if base_name.lower().endswith(".md") and not base_name.startswith(".__antinote_tmp_"):
                md_files[base_name.lower()] = base_name

        return md_files

    def put_markdown(self, rel_dir, filename, content_str):
        self.ensure_dir(rel_dir)
        rel_file = posixpath.join("/" + str(rel_dir or "").strip("/"), filename)
        file_url = self.build_url(rel_file, is_dir=False)
        data = content_str.encode("utf-8")
        self.request(
            "PUT",
            file_url,
            data=data,
            headers={"Content-Type": "text/markdown; charset=utf-8"},
            expected_codes=(200, 201, 204),
        )

    def get_markdown(self, rel_dir, filename):
        rel_file = posixpath.join("/" + str(rel_dir or "").strip("/"), filename)
        file_url = self.build_url(rel_file, is_dir=False)
        status, body = self.request("GET", file_url, expected_codes=(200, 404))
        if status == 404:
            return None
        return body.decode("utf-8", errors="replace")

    def delete_remote_file(self, rel_dir, filename):
        rel_file = posixpath.join("/" + str(rel_dir or "").strip("/"), filename)
        file_url = self.build_url(rel_file, is_dir=False)
        self.request("DELETE", file_url, expected_codes=(200, 202, 204, 404))

    def move_remote_file(self, src_dir, src_filename, dst_dir, dst_filename, fallback_content=None):
        self.ensure_dir(dst_dir)
        src_rel = posixpath.join("/" + str(src_dir or "").strip("/"), src_filename)
        dst_rel = posixpath.join("/" + str(dst_dir or "").strip("/"), dst_filename)
        src_url = self.build_url(src_rel, is_dir=False)
        dst_url = self.build_url(dst_rel, is_dir=False)

        try:
            status, _ = self.request(
                "MOVE",
                src_url,
                headers={"Destination": dst_url, "Overwrite": "T"},
                expected_codes=(200, 201, 204, 404),
            )
            if status != 404:
                return True
            return False
        except Exception:
            text = fallback_content
            if text is None:
                try:
                    text = self.get_markdown(src_dir, src_filename)
                except Exception:
                    text = None
            if text is not None:
                self.put_markdown(dst_dir, dst_filename, text)
            self.delete_remote_file(src_dir, src_filename)
            return True


# ============================================================================
# 3. One-Way Mirror Reconciler
# ============================================================================

def resolve_trash_folder(remote_folder, trash_folder_cfg):
    remote_clean = "/" + str(remote_folder or "").strip("/")
    trash_raw = str(trash_folder_cfg or ".trash").strip()
    if not trash_raw.startswith("/"):
        if remote_clean == "/":
            return "/" + trash_raw.strip("/")
        return remote_clean + "/" + trash_raw.strip("/")
    return "/" + trash_raw.strip("/")


def load_json_file(filepath, default_val):
    if filepath and os.path.isfile(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default_val


def save_json_file(filepath, data):
    parent = os.path.dirname(filepath)
    os.makedirs(parent, exist_ok=True)
    tmp = filepath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, filepath)


def remove_or_trash_remote(client, remote_folder, trash_folder, filename, use_trash, fallback_content=None):
    if use_trash:
        client.move_remote_file(
            remote_folder,
            filename,
            trash_folder,
            filename,
            fallback_content=fallback_content,
        )
    else:
        client.delete_remote_file(remote_folder, filename)


def run_webdav_mirror(config_path=DEFAULT_CONFIG_PATH, state_path=DEFAULT_STATE_PATH, force=False, override_db_path=""):
    cfg = load_json_file(config_path, {})
    webdav_url = str(cfg.get("webdav_url", "")).strip()
    if not webdav_url or "your-webdav-server.com" in webdav_url:
        return {"status": "unconfigured", "message": "请先在 config.json 中填写你的 WebDAV 地址、账号和密码。"}

    username = str(cfg.get("username", ""))
    password = str(cfg.get("password", ""))
    remote_folder = "/" + str(cfg.get("remote_folder", "/Antinote")).strip("/")
    trash_folder = resolve_trash_folder(remote_folder, cfg.get("trash_folder", ".trash"))
    use_trash = bool(cfg.get("use_trash_on_delete", True))
    clean_orphans = bool(cfg.get("clean_orphan_files", True))
    include_slotted = bool(cfg.get("include_slotted", False))
    include_archived = bool(cfg.get("include_archived", False))
    audit_interval = int(cfg.get("audit_interval_seconds", 300))
    timeout_sec = int(cfg.get("timeout_seconds", 15))
    verify_ssl = bool(cfg.get("verify_ssl", True))
    custom_db_path = override_db_path or str(cfg.get("antinote_db_path", "")).strip()

    db_path = find_antinote_db(custom_db_path)
    if not db_path:
        return {"status": "no_db", "message": "未找到 Antinote 数据库文件。"}

    if not override_db_path and not str(cfg.get("antinote_db_path", "")).strip() and os.path.isfile(config_path):
        try:
            cfg["antinote_db_path"] = db_path
            save_json_file(config_path, cfg)
        except Exception:
            pass

    state_doc = load_json_file(state_path, {})
    prev_state = state_doc.get("syncState", {})
    last_db_sig = state_doc.get("lastDbSignature", "")
    last_audit_ts = float(state_doc.get("lastAuditTimestamp", 0.0))
    pending_retry = bool(state_doc.get("pendingRetry", False))
    last_remote_folder = state_doc.get("remoteFolder", "")

    now_ts = time.time()
    current_db_sig = get_db_signature(db_path)
    need_remote_audit = (
        force
        or pending_retry
        or (last_remote_folder != remote_folder)
        or (now_ts - last_audit_ts >= audit_interval)
    )

    if not need_remote_audit and last_db_sig and current_db_sig == last_db_sig:
        return {"status": "skipped_unchanged"}

    notes = fetch_active_notes(db_path, include_slotted=include_slotted, include_archived=include_archived)
    target_filenames = assign_target_filenames(notes, prev_state)
    active_ids = {n["id"] for n in notes}

    has_local_diff = False
    if set(prev_state.keys()) != active_ids:
        has_local_diff = True
    else:
        for n in notes:
            nid = n["id"]
            p_entry = prev_state.get(nid, {})
            if p_entry.get("filename") != target_filenames[nid] or p_entry.get("hash") != content_hash(n["content"]):
                has_local_diff = True
                break

    if not has_local_diff and not need_remote_audit:
        state_doc["lastDbSignature"] = current_db_sig
        save_json_file(state_path, state_doc)
        return {"status": "skipped_no_content_diff"}

    client = WebDAVClient(
        base_url=webdav_url,
        username=username,
        password=password,
        timeout=timeout_sec,
        verify_ssl=verify_ssl,
    )

    added = 0
    updated = 0
    renamed = 0
    deleted = 0
    errors = []
    next_state = {}

    try:
        client.ensure_dir(remote_folder)
        if use_trash:
            client.ensure_dir(trash_folder)

        remote_files_map = None
        if need_remote_audit:
            remote_files_map = client.list_md_files(remote_folder)
    except Exception as e:
        state_doc["pendingRetry"] = True
        save_json_file(state_path, state_doc)
        return {"status": "error", "errors": [str(e)]}

    # Step 1: Delete notes that were removed in Antinote (move to WebDAV .trash)
    for old_id, old_entry in list(prev_state.items()):
        if old_id not in active_ids:
            old_fname = old_entry.get("filename")
            if old_fname:
                try:
                    remove_or_trash_remote(
                        client,
                        remote_folder,
                        trash_folder,
                        old_fname,
                        use_trash,
                    )
                    deleted += 1
                    if remote_files_map is not None:
                        remote_files_map.pop(old_fname.lower(), None)
                except Exception as e:
                    errors.append("Delete failed ({}): {}".format(old_fname, e))
                    next_state[old_id] = old_entry

    # Step 2a: Two-phase staging on WebDAV for notes whose first line changed
    staged_remote_names = {}
    for note in notes:
        nid = note["id"]
        existing = prev_state.get(nid)
        desired_fname = target_filenames[nid]
        if existing and existing.get("filename") and existing["filename"] != desired_fname:
            old_fname = existing["filename"]
            safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", nid)
            tmp_fname = ".__antinote_tmp_{}.md".format(safe_id)
            try:
                moved = client.move_remote_file(
                    remote_folder,
                    old_fname,
                    remote_folder,
                    tmp_fname,
                    fallback_content=note["content"],
                )
                if moved:
                    staged_remote_names[nid] = tmp_fname
                if remote_files_map is not None:
                    remote_files_map.pop(old_fname.lower(), None)
            except Exception as e:
                errors.append("Stage rename failed ({}): {}".format(old_fname, e))

    # Step 2b: Create, finalize renames, and upload modified notes
    for note in notes:
        nid = note["id"]
        raw_body = note["content"]
        h = content_hash(raw_body)
        desired_fname = target_filenames[nid]
        existing = prev_state.get(nid)

        try:
            if not existing:
                client.put_markdown(remote_folder, desired_fname, raw_body)
                added += 1
            elif existing.get("filename") != desired_fname:
                tmp_fname = staged_remote_names.get(nid)
                if tmp_fname:
                    client.move_remote_file(
                        remote_folder,
                        tmp_fname,
                        remote_folder,
                        desired_fname,
                        fallback_content=raw_body,
                    )
                if existing.get("hash") != h or not tmp_fname:
                    client.put_markdown(remote_folder, desired_fname, raw_body)
                renamed += 1
                updated += 1
            else:
                missing_on_remote = (
                    remote_files_map is not None
                    and desired_fname.lower() not in remote_files_map
                )
                if existing.get("hash") != h or missing_on_remote:
                    client.put_markdown(remote_folder, desired_fname, raw_body)
                    if missing_on_remote and existing.get("hash") == h:
                        added += 1
                    else:
                        updated += 1

            next_state[nid] = {
                "filename": desired_fname,
                "hash": h,
                "updatedAt": note.get("updatedAt"),
            }
            if remote_files_map is not None:
                remote_files_map[desired_fname.lower()] = desired_fname
        except Exception as e:
            errors.append("Sync failed ({}): {}".format(desired_fname, e))
            if existing:
                next_state[nid] = existing

    # Step 3: Remote Orphan Cleanup via PROPFIND (moves any non-Antinote .md files to .trash)
    if clean_orphans and remote_files_map is not None and not errors:
        valid_lower = {v["filename"].lower() for v in next_state.values()}
        for rem_lower, rem_actual_name in list(remote_files_map.items()):
            if rem_lower not in valid_lower:
                try:
                    remove_or_trash_remote(
                        client,
                        remote_folder,
                        trash_folder,
                        rem_actual_name,
                        use_trash,
                    )
                    deleted += 1
                except Exception as e:
                    errors.append("Orphan cleanup failed ({}): {}".format(rem_actual_name, e))

    new_state_doc = {
        "syncState": next_state,
        "lastDbSignature": current_db_sig if not errors else "",
        "lastAuditTimestamp": now_ts if (need_remote_audit and not errors) else last_audit_ts,
        "pendingRetry": bool(errors),
        "remoteFolder": remote_folder,
    }
    save_json_file(state_path, new_state_doc)

    return {
        "status": "ok" if not errors else "partial_error",
        "totalActive": len(notes),
        "added": added,
        "updated": updated,
        "renamed": renamed,
        "deleted": deleted,
        "errors": errors,
    }


if __name__ == "__main__":
    if "--detect-db" in sys.argv:
        found = find_antinote_db("")
        if found:
            print(found)
        sys.exit(0)

    cfg_arg = DEFAULT_CONFIG_PATH
    state_arg = DEFAULT_STATE_PATH
    db_override = ""
    force_flag = "--force" in sys.argv
    for arg in sys.argv[1:]:
        if arg.startswith("--config="):
            cfg_arg = expand_path(arg.split("=", 1)[1])
        elif arg.startswith("--state="):
            state_arg = expand_path(arg.split("=", 1)[1])
        elif arg.startswith("--db="):
            db_override = expand_path(arg.split("=", 1)[1])

    res = run_webdav_mirror(cfg_arg, state_arg, force=force_flag, override_db_path=db_override)
    if res.get("status") in ("error", "partial_error"):
        sys.stderr.write("[AntinoteWebDAVSync] Error: {}\n".format(res.get("errors")))
        sys.exit(1)
    elif force_flag or res.get("added") or res.get("updated") or res.get("deleted"):
        print(json.dumps(res, ensure_ascii=False))
