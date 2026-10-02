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
from .delivery import pkg_url, probe, probe_http
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


def _catalog_entry(entry, jobs, items):
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
    return {**entry, "status": status, "job_id": job["id"] if job else None, "item_id": item_id}


@app.get("/api/catalog")
def api_catalog(q: str = "", region: str = "", limit: int | None = None, offset: int = 0):
    jobs = {j["source"]: j for j in db.q("SELECT id, source, item_id, status FROM jobs WHERE kind='download' ORDER BY id")}
    items = {i["source_url"]: i for i in db.q("SELECT id, source_url FROM items WHERE source_url IS NOT NULL AND source_url != '' ORDER BY id")}
    needle = q.strip().lower()
    reg = region.strip().upper()
    out = []
    for entry in catalog.load():
        if needle and needle not in entry["name"].lower() and needle not in entry["title_id"].lower():
            continue
        if reg and entry["region"].upper() != reg:
            continue
        out.append(_catalog_entry(entry, jobs, items))
    count = len(out)
    if offset:
        out = out[offset:]
    if limit is not None:
        out = out[:limit]
    return {"count": count, "entries": out}


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
    db.update_job(job_id, status="queued", message="retrying", progress=0)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: int):
    rows = db.q("SELECT status FROM jobs WHERE id=?", job_id)
    if not rows:
        raise HTTPException(404)
    status = rows[0]["status"]
    if status in ("queued", "paused"):
        db.update_job(job_id, status="failed", message="Cancelled")
        worker.remove_work_dir(job_id)
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
        db.update_job(job_id, status="queued", message="resuming")
    return {"ok": True}


@app.post("/api/jobs/clear")
def clear_jobs():
    db.x("DELETE FROM jobs WHERE status IN ('done','failed','cancelled')")
    return {"ok": True}


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


@app.post("/api/settings")
def save_settings(values: dict):
    if values.get("auto_send", "none") not in ("none", "ftp", "rpi", "auto"):
        raise HTTPException(400, "auto_send must be none, ftp, rpi or auto")
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
    if values.get("ftp_pass") == "":
        values.pop("ftp_pass")  # blank field = keep the stored password
    db.save_settings(values)
    return {"ok": True}


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
