"""Console deletion safety, config import validation, DB backup and on_console merge (no network)."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="shelf-test-"))

from fastapi import HTTPException

from shelf import main, notify


class ConsoleDeleteTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.patch = patch.object(main, "console_delete", side_effect=lambda s, names: self.calls.append(list(names)) or names)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_rejects_unsafe_names(self):
        for name in ("../evil.pkg", "sub/dir.pkg", "back\\slash.pkg", "..", "/abs.pkg", ""):
            with self.subTest(name=name):
                with self.assertRaises(HTTPException) as ctx:
                    main.api_console_delete(main.ConsoleDelete(names=[name]))
                self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self.calls, [])

    def test_deletes_safe_names(self):
        result = main.api_console_delete(main.ConsoleDelete(names=["CUSA00000.pkg", "homebrew.pkg"]))
        self.assertEqual(result, {"removed": ["CUSA00000.pkg", "homebrew.pkg"]})
        self.assertEqual(self.calls, [["CUSA00000.pkg", "homebrew.pkg"]])


class ConfigImportTests(unittest.TestCase):
    def setUp(self):
        self.saved = []
        self.patch = patch.object(main.db, "save_settings", side_effect=lambda values: self.saved.append(dict(values)))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def assertBad(self, values):
        with self.assertRaises(HTTPException) as ctx:
            main.config_import(values)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self.saved, [])

    def test_invalid_values_are_rejected(self):
        for values in (
            {"auto_send": "loud"},
            {"ftp_port": "0"},
            {"ftp_port": "70000"},
            {"rpi_port": "not-a-number"},
            {"ftp_dir": "relative/pkg"},
            {"ftp_dir": "/data/../pkg"},
            {"public_url": "ftp://host"},
            {"notify_webhook": "example.com/hook"},
            {"pause_download_while_sending": "maybe"},
        ):
            with self.subTest(values=values):
                self.assertBad(values)

    def test_valid_import_is_applied(self):
        result = main.config_import({
            "ps4_host": "192.168.1.50",
            "ftp_port": "2121",
            "ftp_dir": "/data/pkg",
            "public_url": "http://192.168.1.10:8420",
            "notify_webhook": "https://hooks.example.com/abc",
            "auto_send": "ftp",
            "pause_download_while_sending": "on",
            "ftp_pass": "",
        })
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(self.saved), 1)
        saved = self.saved[0]
        self.assertNotIn("ftp_pass", saved)
        self.assertEqual(saved["pause_download_while_sending"], "1")
        self.assertEqual(saved["notify_webhook"], "https://hooks.example.com/abc")


class DbBackupTests(unittest.TestCase):
    def test_backup_creates_readable_copy(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch.object(main, "DATA", Path(tmp.name)):
            result = main.db_backup()
        path = Path(result["file"])
        self.assertTrue(path.is_file())
        self.assertTrue(path.name.startswith("shelf-"))
        self.assertEqual(result["size"], path.stat().st_size)
        with sqlite3.connect(path) as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("jobs", names)
        self.assertIn("settings", names)


class CatalogOnConsoleTests(unittest.TestCase):
    def entry(self, title_id="CUSA00000"):
        return {"url": "http://x/a.pkg", "name": "Alpha", "title_id": title_id, "region": "USA",
                "version": "01.00", "size": 42, "duplicate": False}

    def test_merge_sets_on_console_from_stub(self):
        on = main._catalog_entry(self.entry(), {}, {}, frozenset({"CUSA00000"}))
        off = main._catalog_entry(self.entry("CUSA99999"), {}, {}, frozenset({"CUSA00000"}))
        self.assertTrue(on["on_console"])
        self.assertFalse(off["on_console"])
        self.assertEqual(on["name"], "Alpha")

    def test_console_set_is_cached(self):
        patch_cache = patch.object(main, "_console", {"at": 0.0, "installed": frozenset()})
        patch_cache.start()
        self.addCleanup(patch_cache.stop)
        with patch.object(main, "console_inventory", return_value={"installed": ["CUSA00001"]}) as inv:
            first = main._installed_ids()
            second = main._installed_ids()
            refreshed = main._installed_ids(refresh=True)
        self.assertEqual(first, frozenset({"CUSA00001"}))
        self.assertEqual(second, frozenset({"CUSA00001"}))
        self.assertEqual(refreshed, frozenset({"CUSA00001"}))
        self.assertEqual(inv.call_count, 2)


class NotificationTests(unittest.TestCase):
    def test_skips_without_webhook(self):
        with patch.object(notify.db, "settings", return_value={"notify_webhook": ""}), \
                patch.object(notify.requests, "post") as post:
            notify.terminal({"id": 1, "kind": "download", "item_id": None, "message": "x"}, "done")
        post.assert_not_called()

    def test_posts_generic_payload(self):
        with patch.object(notify.db, "settings", return_value={"notify_webhook": "https://hooks.example.com/x"}), \
                patch.object(notify.db, "q", return_value=[{"title": "Game"}]), \
                patch.object(notify.requests, "post") as post:
            notify.terminal({"id": 7, "kind": "send_ftp", "item_id": 3, "message": "uploaded"}, "done")
        self.assertEqual(post.call_args.args[0], "https://hooks.example.com/x")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["text"], body["content"])
        self.assertIn("Game", body["text"])
        self.assertIn("uploaded", body["text"])
        self.assertLessEqual(post.call_args.kwargs["timeout"], 10)

    def test_swallows_webhook_errors(self):
        with patch.object(notify.db, "settings", return_value={"notify_webhook": "https://hooks.example.com/x"}), \
                patch.object(notify.db, "q", return_value=[]), \
                patch.object(notify.requests, "post", side_effect=notify.requests.ConnectionError("down")):
            notify.terminal({"id": 1, "kind": "download", "item_id": None, "message": ""}, "failed")


if __name__ == "__main__":
    unittest.main()
