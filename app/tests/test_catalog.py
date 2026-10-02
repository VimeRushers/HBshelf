"""Catalog parsing, duplicate flagging and mtime-cache tests (no network, no DB volume)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="shelf-test-"))

from shelf import catalog


def entry(name="Game", title_id="CUSA00000", size=1024):
    return {"name": name, "title_id": title_id, "region": "USA", "version": "01.00", "size": size}


class CatalogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "GAMES.json"
        for attr, value in (("CATALOG_PATH", str(self.path)), ("DATA", self.dir)):
            p = patch.object(catalog, attr, value)
            p.start()
            self.addCleanup(p.stop)
        catalog._cache.clear()

    def write(self, data, mtime=None):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        if mtime is not None:
            os.utime(self.path, ns=(mtime, mtime))

    def test_parses_data_wrapper_and_fields(self):
        self.write({"DATA": {"http://x/a.pkg": entry(name="Alpha", size=42)}})
        entries = catalog.load()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["url"], "http://x/a.pkg")
        self.assertEqual(entries[0]["name"], "Alpha")
        self.assertEqual(entries[0]["size"], 42)
        self.assertFalse(entries[0]["duplicate"])

    def test_duplicate_flag_matches_normalized_name(self):
        self.write({"DATA": {
            "http://x/a.pkg": entry(name="Hades"),
            "http://x/b.pkg": entry(name="hades!!", title_id="CUSA00001"),
            "http://x/c.pkg": entry(name="Other", title_id="CUSA00002"),
        }})
        flags = {e["url"]: e["duplicate"] for e in catalog.load()}
        self.assertTrue(flags["http://x/a.pkg"])
        self.assertTrue(flags["http://x/b.pkg"])
        self.assertFalse(flags["http://x/c.pkg"])

    def test_mtime_cache_reloads_on_change(self):
        self.write({"DATA": {"http://x/a.pkg": entry(name="First")}}, mtime=1_000_000_000)
        first = catalog.load()
        self.assertIs(catalog.load(), first)
        before = catalog.find("http://x/a.pkg")
        self.write({"DATA": {"http://x/a.pkg": entry(name="Second")}}, mtime=2_000_000_000)
        reloaded = catalog.load()
        self.assertIsNot(reloaded, first)
        self.assertEqual(catalog.find("http://x/a.pkg")["name"], "Second")
        self.assertEqual(before["name"], "First")

    def test_missing_and_malformed_catalog_return_empty(self):
        self.assertIsNone(catalog.find("http://x/a.pkg"))
        self.write("not json")
        self.assertEqual(catalog.load(), [])
        self.assertIsNone(catalog.find("http://x/a.pkg"))


if __name__ == "__main__":
    unittest.main()
