import logging
import os
import re
import shutil
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import catalog, db, worker
from .config import ALLOW_ANY_DOWNLOAD, DATA, FAILED, ICONS, INBOX, LIBRARY, MAX_UPLOAD_BYTES, MAX_UPLOAD_GB, WORK
from .convert import ConvertError, dir_size, need_space
from .delivery import console_delete, console_inventory, pkg_url, probe, probe_http
from .downloads import host_allowed

logging.basicConfig(level=logging.INFO)
STATIC = Path(__file__).parent / "static"
_ps4 = {"ftp": False, "rpi": False, "checked": 0}


def _probe_loop():
    while True:
        s = db.settings()
        _ps4.update(ftp=probe(s["ps4_host"], int(s["ftp_port"] or 0)),
                    rpi=probe_http(s["ps4_host"], int(s["rpi_port"] or 0)), checked=time.time())
        time.sleep(10)


@asynccontextmanager
async def lifespan(_app):
    catalog.load()
    worker.start()
    threading.Thread(target=_probe_loop, daemon=True).start()
    yield


app = FastAPI(title="Homebrew Shelf", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/state")
def state():
    du = shutil.disk_usage(DATA)
    return {
        "items": db.q("SELECT * FROM items ORDER BY title COLLATE NOCASE"),
        "jobs": db.q("SELECT j.*, i.title FROM jobs j LEFT JOIN items i ON i.id=j.item_id ORDER BY j.id DESC LIMIT 60"),
        "settings": {k: v for k, v in db.settings().items() if k != "ftp_pass"},
        "ps4": _ps4,
        "disk": {"free": du.free, "total": du.total},
    }


def _catalog_entry(entry, jobs, items, installed=frozenset()):
    job = jobs.get(entry["url"])
    item = items.get(entry["url"])
    status = ""
    if job and job["status"] in ("queued", "running", "paused"):
        status = job["status"]
    elif item:
        status = "installed"
    elif job:
        status = job["status"]
    item_id = item["id"] if item else (job["item_id"] if job else None)
    return {**entry, "status": status, "job_id": job["id"] if job else None, "item_id": item_id,
            "on_console": entry["title_id"] in installed}


_console = {"at": 0.0, "installed": frozenset()}


def _installed_ids(refresh=False) -> frozenset:
    """Console title ids, cached briefly so a catalog page does not FTP on every request."""
    now = time.monotonic()
    if refresh or now - _console["at"] >= 60:
        ids = console_inventory(db.settings())["installed"]
        _console.update(at=now, installed=frozenset(ids))
    return _console["installed"]


@app.get("/api/catalog")
def api_catalog(q: str = "", region: str = "", limit: int | None = None, offset: int = 0):
    jobs = {j["source"]: j for j in db.q("SELECT id, source, item_id, status FROM jobs WHERE kind='download' ORDER BY id")}
    items = {i["source_url"]: i for i in db.q("SELECT id, source_url FROM items WHERE source_url IS NOT NULL AND source_url != '' ORDER BY id")}
    needle = q.strip().lower()
    reg = region.strip().upper()
    installed = _installed_ids()
    out = []
    for entry in catalog.load():
        if needle and needle not in entry["name"].lower() and needle not in entry["title_id"].lower():
            continue
        if reg and entry["region"].upper() != reg:
            continue
        out.append(_catalog_entry(entry, jobs, items, installed))
    count = len(out)
    if offset:
        out = out[offset:]
    if limit is not None:
        out = out[:limit]
    return {"count": count, "entries": out}


@app.get("/api/console")
def api_console():
    s = db.settings()
    inv = console_inventory(s)
    titles = {r["title_id"]: r["title"] for r in db.q("SELECT title_id, title FROM items WHERE title_id != ''")}
    installed = [{"title_id": t, "name": titles.get(t) or t} for t in inv["installed"]]
    return {"reachable": inv["reachable"], "host": s["ps4_host"], "installed": installed,
            "pkg": inv["pkg"], "pkg_bytes": sum(p["size"] for p in inv["pkg"])}


class ConsoleDelete(BaseModel):
    names: list[str]


def _unsafe_name(name: str) -> bool:
    return not name or "/" in name or "\\" in name or ".." in name


@app.post("/api/console/delete")
def api_console_delete(body: ConsoleDelete):
    for name in body.names:
        if _unsafe_name(name):
            raise HTTPException(400, f"invalid file name: {name!r}")
    return {"removed": console_delete(db.settings(), body.names)}


class Url(BaseModel):
    url: str


def _pending_download_bytes() -> int:
    total = 0
    for row in db.q("SELECT source FROM jobs WHERE kind='download' AND status IN ('queued','running')"):
        entry = catalog.find(row["source"])
        if entry:
            total += int(entry.get("size") or 0)
    return total


@app.post("/api/shop/install")
def shop_install(body: Url):
    url = body.url.strip()
    entry = catalog.find(url)
    if not entry:
        raise HTTPException(400, "not in the game catalog")
    active = db.q("SELECT id FROM jobs WHERE kind='download' AND source=? AND status IN ('queued','running','paused') ORDER BY id DESC LIMIT 1", url)
    if active:
        return {"job": active[0]["id"]}
    items = db.q("SELECT id FROM items WHERE source_url=? ORDER BY id DESC LIMIT 1", url)
    if items:
        return {"job": db.add_job("send_auto", item_id=items[0]["id"])}
    try:
        need_space(WORK, int(entry.get("size") or 0) + _pending_download_bytes())
    except ConvertError as e:
        raise HTTPException(507, str(e))
    return {"job": db.add_job("download", url)}


@app.post("/api/download")
def add_download(body: Url):
    url = body.url.strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "only http(s) URLs")
    if not ALLOW_ANY_DOWNLOAD and not host_allowed(url):
        raise HTTPException(400, "that download host is not allowed")
    entry = catalog.find(url)
    try:
        need_space(WORK, int((entry or {}).get("size") or 0) + _pending_download_bytes())
    except ConvertError as e:
        raise HTTPException(507, str(e))
    return {"job": db.add_job("download", url)}


@app.put("/api/upload/{filename}")
async def upload(filename: str, request: Request):
    name = os.path.basename(filename)
    if not name or name.startswith("."):
        raise HTTPException(400, "bad filename")
    tmp = INBOX / f".{name}.uploading"   # dot-prefixed: the inbox watcher ignores it until renamed
    total = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"upload is larger than the {MAX_UPLOAD_GB:g} GiB limit")
                f.write(chunk)
        tmp.rename(INBOX / name)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return {"ok": True}


class Send(BaseModel):
    method: str


@app.post("/api/items/{item_id}/send")
def send(item_id: int, body: Send):
    if body.method not in ("ftp", "rpi", "auto"):
        raise HTTPException(400, "method must be ftp, rpi or auto")
    return {"job": db.add_job(f"send_{body.method}", item_id=item_id)}


@app.delete("/api/items/{item_id}")
def delete_item(item_id: int):
    rows = db.q("SELECT * FROM items WHERE id=?", item_id)
    if not rows:
        raise HTTPException(404)
    p = Path(rows[0]["path"])
    p.unlink(missing_ok=True)
    if p.parent.exists() and not any(p.parent.iterdir()):
        p.parent.rmdir()
    (ICONS / f"{rows[0]['content_id']}.png").unlink(missing_ok=True)
    db.x("DELETE FROM items WHERE id=?", item_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
def retry(job_id: int):
    rows = db.q("SELECT id FROM jobs WHERE id=?", job_id)
    if not rows:
        raise HTTPException(404)
    db.update_job(job_id, status="queued", message="retrying", progress=0, attempts=0, retry_at=0)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: int):
    rows = db.q("SELECT status FROM jobs WHERE id=?", job_id)
    if not rows:
        raise HTTPException(404)
    status = rows[0]["status"]
    if status in ("queued", "paused"):
        # Cancel stops the transfer; it never deletes .part/.aria2. Cleanup is Storage-tab only.
        db.update_job(job_id, status="failed", message="Cancelled")
    elif status == "running":
        db.update_job(job_id, status="cancelled", message="Cancelling…")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/delete")
def delete_job(job_id: int):
    rows = db.q("SELECT status FROM jobs WHERE id=?", job_id)
    if not rows:
        raise HTTPException(404)
    if rows[0]["status"] in ("queued", "running", "paused"):
        raise HTTPException(409, "cancel the job before deleting it")
    worker.remove_work_dir(job_id)
    db.x("DELETE FROM jobs WHERE id=?", job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/pause")
def pause(job_id: int):
    rows = db.q("SELECT status FROM jobs WHERE id=?", job_id)
    if not rows:
        raise HTTPException(404)
    if rows[0]["status"] in ("queued", "running"):
        db.update_job(job_id, status="paused")
        worker.request_pause(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/resume")
def resume(job_id: int):
    rows = db.q("SELECT status FROM jobs WHERE id=?", job_id)
    if not rows:
        raise HTTPException(404)
    if rows[0]["status"] == "paused":
        db.update_job(job_id, status="queued", message="resuming", retry_at=0)
    return {"ok": True}


@app.post("/api/jobs/clear")
def clear_jobs():
    db.x("DELETE FROM jobs WHERE status IN ('done','failed','cancelled')")
    return {"ok": True}


@app.get("/api/jobs")
def api_jobs(limit: int = 50):
    limit = min(max(limit, 1), 500)
    return {"jobs": db.q(
        "SELECT id, kind, item_id, status, progress, message, source, attempts, retry_at, created, updated "
        "FROM jobs ORDER BY id DESC LIMIT ?", limit)}


def _storage_dirs():
    if WORK.is_dir():
        for pattern, category in (("dl-*", "download"), ("ingest-*", "ingest")):
            for d in sorted(WORK.glob(pattern)):
                if d.is_dir():
                    yield d, category, d.name
    if FAILED.is_dir():
        for d in sorted(FAILED.glob("*")):
            if d.is_dir():
                yield d, "failed", f"failed/{d.name}"


def _dir_job_id(d: Path) -> int | None:
    m = re.match(r"(?:dl|ingest)-(\d+)-", d.name)
    return int(m.group(1)) if m else None


def _clean_target(sid: str):
    base, rel = (FAILED, sid[len("failed/"):]) if sid.startswith("failed/") else (WORK, sid)
    target = (base / rel).resolve()
    root = base.resolve()
    if root not in target.parents or not target.is_dir():
        raise HTTPException(400, f"invalid storage id: {sid}")
    return target


@app.get("/api/storage")
def storage():
    du = shutil.disk_usage(DATA)
    jobs = {j["id"]: j for j in db.q(
        "SELECT j.id, j.kind, j.status, j.source, i.title FROM jobs j LEFT JOIN items i ON i.id=j.item_id")}
    out = []
    failed_bytes = 0
    for d, category, sid in _storage_dirs():
        job_id = _dir_job_id(d)
        job = jobs.get(job_id) if job_id is not None else None
        alloc = apparent = 0
        files = []
        for f in d.rglob("*"):
            if not f.is_file():
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            alloc += st.st_blocks * 512
            apparent += st.st_size
            files.append(str(f.relative_to(d)))
        name = (job or {}).get("title") or Path((job or {}).get("source") or "").name or d.name
        if category == "failed":
            failed_bytes += alloc
        out.append({
            "id": sid,
            "category": category,
            "job_id": job_id,
            "dir": str(d),
            "kind": job["kind"] if job else "",
            "status": job["status"] if job else "",
            "name": name,
            "alloc_bytes": alloc,
            "apparent_bytes": apparent,
            "files": files,
            "modified": d.stat().st_mtime,
        })
    return {"free": du.free, "total": du.total, "failed_bytes": failed_bytes,
            "work": out, "library_bytes": dir_size(LIBRARY)}


class Clean(BaseModel):
    job_ids: list[int] = []
    ids: list[str] = []
    all_orphans: bool = False


@app.post("/api/storage/clean")
def storage_clean(body: Clean):
    active = {r["id"] for r in db.q("SELECT id FROM jobs WHERE status IN ('queued','running','paused')")}
    wanted = set(body.job_ids)
    removed, freed = [], 0

    def drop(d: Path):
        nonlocal freed
        alloc = sum(f.stat().st_blocks * 512 for f in d.rglob("*") if f.is_file())
        shutil.rmtree(d, ignore_errors=True)
        removed.append(str(d))
        freed += alloc

    for sid in dict.fromkeys(body.ids):
        d = _clean_target(sid)
        job_id = _dir_job_id(d)
        if job_id is not None and job_id in active:
            continue
        drop(d)

    for d, category, sid in _storage_dirs():
        job_id = _dir_job_id(d)
        if category == "failed" or (job_id is not None and job_id in active):
            continue
        if body.all_orphans or (category == "download" and job_id is not None and job_id in wanted):
            drop(d)

    return {"removed": removed, "freed": freed}


def _bool_setting(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return "1"
    if text in ("0", "false", "no", "off"):
        return "0"
    raise HTTPException(400, "pause_download_while_sending must be a boolean")


@app.post("/api/settings")
def save_settings(values: dict):
    _validate_settings(values)
    db.save_settings(values)
    return {"ok": True}


def _validate_settings(values: dict):
    if values.get("auto_send", "none") not in ("none", "ftp", "rpi", "auto"):
        raise HTTPException(400, "auto_send must be none, ftp, rpi or auto")
    if "pause_download_while_sending" in values:
        values["pause_download_while_sending"] = _bool_setting(values["pause_download_while_sending"])
    for key in ("ftp_port", "rpi_port"):
        if str(values.get(key, "")).strip():
            try:
                port = int(values[key])
            except (TypeError, ValueError):
                raise HTTPException(400, f"{key} must be a port number")
            if not 1 <= port <= 65535:
                raise HTTPException(400, f"{key} must be between 1 and 65535")
    if "ftp_dir" in values:
        d = str(values["ftp_dir"]).strip()
        if d and (not d.startswith("/") or ".." in d):
            raise HTTPException(400, "ftp_dir must be an absolute path without '..'")
    if "public_url" in values:
        u = str(values["public_url"]).strip()
        if u and not re.match(r"^https?://", u):
            raise HTTPException(400, "public_url must start with http:// or https://")
    if "notify_webhook" in values:
        u = str(values["notify_webhook"]).strip()
        if u and not re.match(r"^https?://", u):
            raise HTTPException(400, "notify_webhook must start with http:// or https://")
    if values.get("ftp_pass") == "":
        values.pop("ftp_pass")  # blank field = keep the stored password


@app.get("/api/config/export")
def config_export():
    return {"settings": {k: v for k, v in db.settings().items() if k != "ftp_pass"}, "exported_at": time.time()}


@app.post("/api/config/import")
def config_import(values: dict):
    _validate_settings(values)
    db.save_settings(values)
    return {"ok": True}


@app.post("/api/db/backup")
def db_backup():
    dest = DATA / "backups" / f"shelf-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.db"
    db.backup(dest)
    return {"file": str(dest), "size": dest.stat().st_size}


@app.api_route("/pkg/{item_id}/{name}", methods=["GET", "HEAD"])
def serve_pkg(item_id: int, name: str):
    rows = db.q("SELECT path FROM items WHERE id=?", item_id)
    if not rows or not Path(rows[0]["path"]).is_file():
        raise HTTPException(404)
    return FileResponse(rows[0]["path"], media_type="application/octet-stream", filename=name)


@app.get("/icons/{content_id}.png")
def icon(content_id: str):
    p = ICONS / f"{os.path.basename(content_id)}.png"
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


# FPKGi (the PS4 homebrew store app) reads one JSON per section in this exact shape,
# so pointing FPKGi at http://<server>/fpkgi/GAMES.json browses this server's own library.
FPKGI_SECTIONS = {"GAMES": "game", "UPDATES": "update", "DLC": "dlc", "HOMEBREW": "homebrew"}


@app.get("/fpkgi/{section}.json")
def fpkgi(section: str):
    cat = FPKGI_SECTIONS.get(section.upper())
    if not cat:
        raise HTTPException(404)
    base = db.settings()["public_url"].rstrip("/")
    data = {}
    for it in db.q("SELECT * FROM items WHERE category=? ORDER BY title COLLATE NOCASE", cat):
        data[pkg_url(it, base)] = {
            "title_id": it["title_id"], "region": it["region"], "name": it["title"],
            "version": it["version"], "release": time.strftime("%m-%d-%Y", time.gmtime(it["added"] or 0)),
            "size": it["size"], "min_fw": it["min_fw"],
            "cover_url": f"{base}/icons/{it['content_id']}.png" if it["has_icon"] else "",
        }
    return JSONResponse({"DATA": data})
