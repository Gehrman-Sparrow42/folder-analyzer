"""Behavior tests for scanning and the local HTTP interface."""

from __future__ import annotations

import csv
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scanner import ScanManager
from server import AnalyzerServer


def await_scan(manager: ScanManager) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = manager.snapshot()
        if status["state"] != "scanning":
            return status
        time.sleep(0.01)
    raise AssertionError("Scan timed out")


class ScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "nested").mkdir()
        (self.root / "nested" / "a.txt").write_bytes(b"abc")
        (self.root / "nested" / "b.TXT").write_bytes(b"12345")
        (self.root / "plain").write_bytes(b"x")
        self.manager = ScanManager()

    def tearDown(self) -> None:
        self.manager.close()
        self.temp.cleanup()

    def test_totals_filters_types_export(self) -> None:
        self.manager.start(str(self.root))
        status = await_scan(self.manager)
        self.assertEqual(status["state"], "complete")
        self.assertEqual((status["files"], status["folders"], status["total_bytes"]), (3, 1, 9))
        listing = self.manager.browse(None, "size", "desc", 0, 100)
        self.assertEqual(listing["total"], 2)
        nested = self.manager.browse(str(self.root / "nested"), "name", "asc", 0, 100)
        self.assertEqual(nested["parent"], str(self.root))
        self.assertEqual([item["size"] for item in nested["items"]], [3, 5])
        self.assertEqual(self.manager.files("a.txt", ".txt", 0, 100)["total"], 1)
        self.assertEqual(self.manager.files("", "[none]", 0, 100)["total"], 1)
        self.assertEqual(self.manager.types()["items"][0], {"extension": ".txt", "size": 8, "count": 2})
        data = b"".join(self.manager.export_rows()).decode("utf-8-sig")
        self.assertEqual(len(list(csv.DictReader(io.StringIO(data)))), 3)

    def test_invalid_root_and_symlink_skip(self) -> None:
        with self.assertRaises(ValueError):
            self.manager.start(str(self.root / "missing"))
        link = self.root / "shortcut"
        try:
            link.symlink_to(self.root / "nested", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Directory symlinks are not available")
        self.manager.start(str(self.root))
        status = await_scan(self.manager)
        self.assertEqual(status["skipped"], 1)
        self.assertEqual(self.manager.errors()["total"], 1)

    def test_breakdown_aggregates_all_children(self) -> None:
        for number in range(15):
            (self.root / f"extra-{number}.bin").write_bytes(bytes(number))
        self.manager.start(str(self.root))
        await_scan(self.manager)
        result = self.manager.breakdown(None)
        self.assertEqual(len(result["items"]), 13)
        self.assertEqual(result["items"][-1]["kind"], "other")
        self.assertIsNone(result["items"][-1]["path"])
        self.assertEqual(sum(item["size"] for item in result["items"]), result["total_bytes"])

    def test_cancel_exposes_consistent_partial_index(self) -> None:
        for index in range(2000):
            (self.root / "nested" / f"{index}.dat").write_bytes(b"x")
        self.manager.start(str(self.root))
        self.manager.cancel()
        status = await_scan(self.manager)
        self.assertEqual(status["state"], "cancelled")
        root = self.manager.browse(None, "name", "asc", 0, 100)
        self.assertEqual(root["path"], str(self.root))
        self.assertEqual(sum(item["size"] for item in root["items"]), status["total_bytes"])

    def test_trash_only_indexed_unchanged_child_and_rescan(self) -> None:
        self.manager.start(str(self.root))
        await_scan(self.manager)
        with patch("scanner._recycle_folder") as recycle:
            with self.assertRaises(ValueError):
                self.manager.trash_folder(str(self.root))
            with self.assertRaises(ValueError):
                self.manager.trash_folder(str(self.root.parent))
            fresh = self.root / "fresh"
            fresh.mkdir()
            with self.assertRaises(ValueError):
                self.manager.trash_folder(str(fresh))
            recycle.assert_not_called()
            with tempfile.TemporaryDirectory() as destination:
                moved = Path(destination) / "nested"

                def fake_recycle(path: str) -> None:
                    os.rename(path, moved)

                recycle.side_effect = fake_recycle
                response = self.manager.trash_folder(str(self.root / "nested"))
                self.assertEqual(response["status"], "trashed")
                self.assertEqual(response["path"], str(self.root / "nested"))
                await_scan(self.manager)
                self.assertFalse((self.root / "nested").exists())

    def test_trash_rejects_changed_folder_identity_and_active_scan(self) -> None:
        self.manager.start(str(self.root))
        await_scan(self.manager)
        old = self.root / "nested"
        old.rename(self.root / "renamed")
        old.mkdir()
        with patch("scanner._recycle_folder") as recycle:
            with self.assertRaises(ValueError):
                self.manager.trash_folder(str(old))
            with self.manager.lock:
                self.manager.status["state"] = "scanning"
            with self.assertRaises(RuntimeError):
                self.manager.trash_folder(str(old))
            recycle.assert_not_called()

    def test_trash_rejects_replaced_scan_root(self) -> None:
        scan_root = self.root / "scanned"
        child = scan_root / "child"
        child.mkdir(parents=True)
        self.manager.start(str(scan_root))
        await_scan(self.manager)
        scan_root.rename(self.root / "former")
        (scan_root / "child").mkdir(parents=True)
        with patch("scanner._recycle_folder") as recycle:
            with self.assertRaises(ValueError):
                self.manager.trash_folder(str(child))
            recycle.assert_not_called()


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        (Path(self.temp.name) / "hello.txt").write_text("hello")
        self.server = AnalyzerServer(("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method: str, path: str, body: dict | None = None,
                headers: dict | None = None) -> tuple[int, dict | bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        data = json.dumps(body).encode() if body is not None else None
        req_headers = {"Host": f"127.0.0.1:{self.server.server_port}"}
        if body is not None:
            req_headers["Content-Type"] = "application/json"
        req_headers.update(headers or {})
        conn.request(method, path, data, req_headers)
        response = conn.getresponse()
        payload = response.read()
        code = response.status
        content_type = response.getheader("Content-Type", "")
        conn.close()
        return code, json.loads(payload) if "application/json" in content_type else payload

    def test_api_scan_and_security(self) -> None:
        self.assertEqual(self.request("GET", "/api/health")[0], 200)
        self.assertEqual(self.request("GET", "/api/health", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/scan", {"path": self.temp.name},
                                      {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/scan", {"path": self.temp.name})[0], 202)
        await_scan(self.server.manager)
        status, listing = self.request("GET", "/api/browse")
        self.assertEqual(status, 200)
        self.assertEqual(listing["total"], 1)
        status, export = self.request("GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertIn(b"hello.txt", export)
        self.assertEqual(self.request("POST", "/api/trash", {"path": self.temp.name})[0], 400)

    def test_config_falls_back_to_home_and_serves_web_assets(self) -> None:
        with patch("server.Path.is_dir", return_value=False), patch("server.Path.home", return_value=Path(self.temp.name)):
            status, config = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(config["default_path"], self.temp.name)
        for path, marker in (("/", b"Folder Analyzer"), ("/app.js", b"function"), ("/styles.css", b"body")):
            status, payload = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertIn(marker, payload)


if __name__ == "__main__":
    unittest.main()
