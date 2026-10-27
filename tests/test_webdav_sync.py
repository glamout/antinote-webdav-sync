#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
End-to-end integration test suite for antinote_webdav_sync.py against a live in-process WebDAV server.
"""

import ast
import base64
import json
import os
import posixpath
import shutil
import sqlite3
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

SERVICE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SERVICE_DIR)
import antinote_webdav_sync as sync_mod


class MockWebDAVHandler(BaseHTTPRequestHandler):
    root_dir = ""
    expected_auth = ""
    request_count = 0

    def log_message(self, format, *args):
        pass

    def _check_auth(self):
        auth = self.headers.get("Authorization", "")
        if auth != self.expected_auth:
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def _fs_path(self, url_path):
        parsed = urllib.parse.urlparse(url_path).path
        unquoted = urllib.parse.unquote(parsed)
        # Strip /dav prefix
        if unquoted.startswith("/dav"):
            rel = unquoted[4:]
        else:
            rel = unquoted
        rel = rel.lstrip("/")
        return os.path.join(self.root_dir, rel), "/dav/" + rel.rstrip("/")

    def do_MKCOL(self):
        type(self).request_count += 1
        if not self._check_auth():
            return
        fs_p, _ = self._fs_path(self.path)
        if os.path.exists(fs_p):
            self.send_response(405)
            self.end_headers()
            return
        os.makedirs(fs_p, exist_ok=True)
        self.send_response(201)
        self.end_headers()

    def do_PUT(self):
        type(self).request_count += 1
        if not self._check_auth():
            return
        fs_p, _ = self._fs_path(self.path)
        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length) if length > 0 else b""
        os.makedirs(os.path.dirname(fs_p), exist_ok=True)
        existed = os.path.exists(fs_p)
        with open(fs_p, "wb") as f:
            f.write(data)
        self.send_response(204 if existed else 201)
        self.end_headers()

    def do_GET(self):
        type(self).request_count += 1
        if not self._check_auth():
            return
        fs_p, _ = self._fs_path(self.path)
        if not os.path.isfile(fs_p):
            self.send_response(404)
            self.end_headers()
            return
        with open(fs_p, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_DELETE(self):
        type(self).request_count += 1
        if not self._check_auth():
            return
        fs_p, _ = self._fs_path(self.path)
        if not os.path.exists(fs_p):
            self.send_response(404)
            self.end_headers()
            return
        if os.path.isdir(fs_p):
            shutil.rmtree(fs_p)
        else:
            os.remove(fs_p)
        self.send_response(204)
        self.end_headers()

    def do_MOVE(self):
        type(self).request_count += 1
        if not self._check_auth():
            return
        src_p, _ = self._fs_path(self.path)
        dest_hdr = self.headers.get("Destination", "")
        dst_p, _ = self._fs_path(dest_hdr)
        if not os.path.exists(src_p):
            self.send_response(404)
            self.end_headers()
            return
        os.makedirs(os.path.dirname(dst_p), exist_ok=True)
        os.replace(src_p, dst_p)
        self.send_response(201)
        self.end_headers()

    def do_PROPFIND(self):
        type(self).request_count += 1
        if not self._check_auth():
            return
        length = int(self.headers.get("Content-Length", 0))
        if length > 0:
            _ = self.rfile.read(length)
        fs_p, href_base = self._fs_path(self.path)
        if not os.path.exists(fs_p):
            self.send_response(404)
            self.end_headers()
            return

        responses = []
        # Self entry
        responses.append((href_base + "/", True))
        if os.path.isdir(fs_p):
            for entry in os.scandir(fs_p):
                qname = urllib.parse.quote(entry.name, safe="")
                child_href = href_base.rstrip("/") + "/" + qname
                if entry.is_dir():
                    responses.append((child_href + "/", True))
                else:
                    responses.append((child_href, False))

        xml_parts = ['<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">']
        for href, is_dir in responses:
            res_type = "<D:collection/>" if is_dir else ""
            xml_parts.append(
                f"<D:response><D:href>{href}</D:href>"
                f"<D:propstat><D:prop><D:resourcetype>{res_type}</D:resourcetype></D:prop>"
                f"<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
            )
        xml_parts.append("</D:multistatus>")
        payload = "".join(xml_parts).encode("utf-8")
        self.send_response(207)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def test_all():
    # 1. Verify Python 3.8/3.9 syntax (zero type annotations)
    src = open(os.path.join(SERVICE_DIR, "antinote_webdav_sync.py"), "r", encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            assert node.returns is None, f"Function {node.name} has return annotation!"
            for arg in node.args.args:
                assert arg.annotation is None, f"Arg {arg.arg} in {node.name} has annotation!"
    print("1. AST syntax check passed (100% Python 3.8/3.9 compatible).")

    tmp_root = tempfile.mkdtemp(prefix="webdav-test-")
    dav_root = os.path.join(tmp_root, "webdav_storage")
    os.makedirs(dav_root, exist_ok=True)

    user = "test_user"
    pw = "test_password"
    MockWebDAVHandler.root_dir = dav_root
    MockWebDAVHandler.expected_auth = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()

    server = HTTPServer(("127.0.0.1", 0), MockWebDAVHandler)
    port = server.server_port
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        db_path = os.path.join(tmp_root, "antinote.sqlite")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            "CREATE TABLE notes (id TEXT PRIMARY KEY, content TEXT, created_at INT, updated_at INT, "
            "is_deleted INT DEFAULT 0, in_void INT DEFAULT 0, is_tutorial INT DEFAULT 0, is_archived INT DEFAULT 0, slot INT DEFAULT NULL);"
        )
        gods = ["Aphrodite", "Ares", "Cronus", "Gaia", "Hades", "Hermes", "Ouranos", "Poseidon", "Zeus"]
        for idx, g in enumerate(gods):
            conn.execute("INSERT INTO notes VALUES (?, ?, 100, 100, 0, 0, 0, 0, ?);", (f"slot_{idx+1}", f"{g}\nSlot", idx + 1))
        conn.execute("INSERT INTO notes VALUES ('n1', '读书笔记\n第一行是读书笔记\n第二行正文', 200, 200, 0, 0, 0, 0, NULL);")
        conn.execute("INSERT INTO notes VALUES ('n2', '待办清单\n买牛奶\n发邮件', 201, 201, 0, 0, 0, 0, NULL);")
        conn.commit()

        # Pre-seed an orphan file on WebDAV server to verify it gets moved to .trash
        remote_antinote_dir = os.path.join(dav_root, "Antinote")
        os.makedirs(remote_antinote_dir, exist_ok=True)
        with open(os.path.join(remote_antinote_dir, "远端残留孤儿笔记.md"), "w", encoding="utf-8") as f:
            f.write("远端残留孤儿笔记\n旧内容")

        cfg_path = os.path.join(tmp_root, "config.json")
        state_path = os.path.join(tmp_root, "state.json")
        cfg = {
            "webdav_url": f"http://127.0.0.1:{port}/dav",
            "username": user,
            "password": pw,
            "remote_folder": "/Antinote",
            "trash_folder": ".trash",
            "use_trash_on_delete": True,
            "clean_orphan_files": True,
            "include_slotted": False,
            "include_archived": False,
            "audit_interval_seconds": 300,
            "timeout_seconds": 10,
            "verify_ssl": False,
            "antinote_db_path": db_path,
        }
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

        print("2. Testing initial sync (PUT new notes, filter 9 Greek gods, move remote orphan to .trash)...")
        res1 = sync_mod.run_webdav_mirror(cfg_path, state_path, force=True)
        print("   Result 1:", res1)
        assert res1["status"] == "ok" and res1["totalActive"] == 2
        remote_files = sorted(
            f for f in os.listdir(remote_antinote_dir)
            if os.path.isfile(os.path.join(remote_antinote_dir, f))
        )
        assert remote_files == ["待办清单.md", "读书笔记.md"], f"Unexpected remote files: {remote_files}"
        assert open(os.path.join(remote_antinote_dir, "读书笔记.md"), encoding="utf-8").read() == "读书笔记\n第一行是读书笔记\n第二行正文"

        trash_dir = os.path.join(remote_antinote_dir, ".trash")
        assert os.path.isfile(os.path.join(trash_dir, "远端残留孤儿笔记.md")), "Orphan file should be in WebDAV .trash!"

        print("3. Testing fast mtime skip (0 HTTP requests when DB unchanged)...")
        before_reqs = MockWebDAVHandler.request_count
        res2 = sync_mod.run_webdav_mirror(cfg_path, state_path, force=False)
        after_reqs = MockWebDAVHandler.request_count
        assert res2["status"] == "skipped_unchanged"
        assert after_reqs == before_reqs, "Should make 0 HTTP requests when skipped!"

        print("4. Testing first-line rename (MOVE) + content modification (PUT) + deletion to WebDAV .trash...")
        conn.execute("UPDATE notes SET content = '深度学习读书笔记\n第一行改名了\n新增第三行' WHERE id = 'n1';")
        conn.execute("UPDATE notes SET in_void = 1 WHERE id = 'n2';")
        conn.commit()

        res3 = sync_mod.run_webdav_mirror(cfg_path, state_path, force=False)
        print("   Result 3:", res3)
        assert res3["status"] == "ok" and res3["totalActive"] == 1
        remote_files = sorted(
            f for f in os.listdir(remote_antinote_dir)
            if os.path.isfile(os.path.join(remote_antinote_dir, f))
        )
        assert remote_files == ["深度学习读书笔记.md"]
        assert open(os.path.join(remote_antinote_dir, "深度学习读书笔记.md"), encoding="utf-8").read() == "深度学习读书笔记\n第一行改名了\n新增第三行"
        assert os.path.isfile(os.path.join(trash_dir, "待办清单.md")), "Deleted note should be moved to WebDAV .trash!"

        conn.close()
        print("ALL WEBDAV MIRROR TESTS PASSED SUCCESSFULLY!")
    finally:
        server.shutdown()
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    test_all()
