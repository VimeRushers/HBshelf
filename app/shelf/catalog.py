"""Read-only game catalog (GAMES.json), reloaded when the file's mtime changes."""
import json
import logging
import re
from pathlib import Path

from .config import CATALOG_PATH, DATA


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


log = logging.getLogger("shelf.catalog")
_cache = {}


def _path() -> Path:
    p = Path(CATALOG_PATH)
    return p if p.is_file() else DATA / "catalog" / "GAMES.json"


def load() -> list:
    path = _path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        log.warning("catalog: %s not found; the Shop tab will be empty", path)
        _cache.clear()
        return []
    if _cache.get("path") == str(path) and _cache.get("mtime") == mtime:
        return _cache["entries"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        data = raw.get("DATA", raw) if isinstance(raw, dict) else raw
        entries = []
        for url, e in data.items():
            if not isinstance(e, dict):
                continue
            entries.append({
                "url": url,
                "name": e.get("name", ""),
                "title_id": e.get("title_id", ""),
                "region": e.get("region", ""),
                "version": e.get("version", ""),
                "release": e.get("release", ""),
                "size": int(e.get("size") or 0),
                "min_fw": e.get("min_fw", ""),
                "cover_url": e.get("cover_url", ""),
            })
        counts = {}
        for e in entries:
            key = _norm(e["name"])
            if key:
                counts[key] = counts.get(key, 0) + 1
        for e in entries:
            key = _norm(e["name"])
            e["duplicate"] = bool(key) and counts[key] > 1
    except (OSError, ValueError, AttributeError):
        log.exception("catalog: could not parse %s; the Shop tab will be empty", path)
        entries = []
    _cache.update(path=str(path), mtime=mtime, entries=entries, index={e["url"]: e for e in entries})
    return entries


def find(url: str) -> dict | None:
    load()
    return _cache.get("index", {}).get(url)
