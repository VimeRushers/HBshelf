"""Background threads: inbox watcher, fetch/convert worker, delivery worker."""
import logging
import re
import shutil
import subprocess
import threading
import time
import traceback
import urllib.parse
from pathlib import Path

import requests

from . import catalog, db
from .config import ARIA2_CONN, FAILED, INBOX, MIN_DOWNLOAD_RATE, STALL_SECONDS, WORK
from .convert import ConvertError, Converter, group_entries, need_space
from .delivery import Cancelled, DeliveryError, Paused, auto_install, ftp_upload, rpi_install
from .downloads import DownloadError, download_session, head_size, is_archive_url, is_aria2, write_cookies

log = logging.getLogger("shelf")

_procs = {}


def work_dir(job_id, created=None) -> Path:
    if created is None:
        rows = db.q("SELECT created FROM jobs WHERE id=?", job_id)
        created = rows[0]["created"] if rows else 0
    return WORK / f"dl-{job_id}-{int(created)}"


def remove_work_dir(job_id, created=None):
    shutil.rmtree(work_dir(job_id, created), ignore_errors=True)

ARIA2_RETRY_CODES = {1, 4, 6, 7, 8}
ARIA2_ERRORS = {
    1: "download failed (aria2 error 1)",
    3: "the download server said the file was not found (HTTP 404)",
    4: "the download server kept dropping the connection; retry to resume",
    5: "the download was too slow",
    6: "network problem while downloading; retry to resume",
    7: "download stopped before finishing; retry to resume",
    8: "the download server does not support resuming",
    9: "not enough free disk space",
    11: "this file is already being downloaded",
    13: "the destination file already exists",
    22: "the download server sent a bad response header",
    23: "too many redirects",
    24: "the download server requires authentication",
}


def reporter(job_id):
    last = [0.0]

    def report(progress, message):
        now = time.monotonic()
        if progress not in (0, 1) and now - last[0] < 1:
            return
        last[0] = now
        kw = {"message": message}
        if progress is not None:
            kw["progress"] = round(progress, 4)
        db.update_job(job_id, **kw)
    return report


def _job_status(job_id) -> str:
    rows = db.q("SELECT status FROM jobs WHERE id=?", job_id)
    return rows[0]["status"] if rows else ""


def request_pause(job_id):
    proc = _procs.get(job_id)
    if proc and proc.poll() is None:
        proc.terminate()


# ---------- inbox watcher ----------

def _fingerprint(p: Path):
    if p.is_file():
        st = p.stat()
        return (st.st_size, st.st_mtime)
    files = [f.stat() for f in p.rglob("*") if f.is_file()]
    return (len(files), sum(s.st_size for s in files), max((s.st_mtime for s in files), default=0))


def watch_inbox(interval=20):
    """A top-level inbox entry (or split set) is picked up once it stops changing for one interval."""
    seen = {}
    while True:
        try:
            entries = [p for p in INBOX.iterdir() if not p.name.startswith(".") and not p.name.endswith((".part", ".crdownload", ".tmp", ".aria2"))]
            groups, singles = group_entries(entries)
            units = [[p] for p in singles] + list(groups.values())
            current = {}
            for unit in units:
                key = unit[0].name
                fp = tuple(_fingerprint(p) for p in unit)
                current[key] = fp
                if seen.get(key) == fp:
                    job = db.add_job("ingest", key, status="hold")
                    dest = WORK / f"ingest-{job}-{int(time.time())}"
                    dest.mkdir()
                    for p in unit:
                        shutil.move(str(p), dest / p.name)
                    db.update_job(job, source=str(dest), status="queued", message=f"picked up {key} from inbox")
                    current.pop(key)
            seen = current
        except Exception:
            log.exception("inbox watcher")
        time.sleep(interval)


# ---------- fetch / convert ----------

def _filename(url, cd=""):
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
    name = urllib.parse.unquote(m[1]) if m else urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)
    return re.sub(r"[/\\\0]", "_", name) or "download.bin"


def download(job, report) -> Path:
    url = job["source"]
    archive = is_archive_url(url)
    if archive:
        # Keep authenticated transfers on HTTPS, even for pasted http:// links.
        url = urllib.parse.urlsplit(url)._replace(scheme="https").geturl()
    dest_dir = work_dir(job["id"], job["created"])
    dest_dir.mkdir(exist_ok=True)
    partial = next(dest_dir.glob("*.part"), None)
    headers = {}
    if partial:
        headers["Range"] = f"bytes={partial.stat().st_size}-"
    with download_session(url) as session, session.get(url, stream=True, headers=headers, timeout=60, allow_redirects=True) as r:
        if archive and (r.status_code in (401, 403) or (
            is_archive_url(r.url) and urllib.parse.urlsplit(r.url).path.rstrip("/") in ("/account/login", "/account/login.php")
        )):
            raise DownloadError("Internet Archive denied this download. Your account must have access to the item; login does not grant access to private or restricted files.")
        if r.status_code == 416 and partial:
            final = partial.with_suffix("")
            partial.rename(final)
            return dest_dir
        r.raise_for_status()
        resumed = partial is not None and r.status_code == 206
        name = partial.name[:-5] if resumed else _filename(r.url, r.headers.get("content-disposition", ""))
        if partial and not resumed:
            partial.unlink()
        target = dest_dir / (name + ".part")
        have = target.stat().st_size if resumed else 0
        total = int(r.headers.get("content-length", 0)) + have
        if total:
            need_space(WORK, total - have)
        with open(target, "ab" if resumed else "wb") as f:
            for chunk in r.iter_content(4 * 2**20):
                status = _job_status(job["id"])
                if status == "paused":
                    raise Paused()
                if status == "cancelled":
                    raise Cancelled()
                f.write(chunk)
                have += len(chunk)
                report(have / total if total else None, f"downloading {name}: {have / 2**20:.0f}/{total / 2**20:.0f} MiB")
    if total and have < total:
        raise ConvertError("download ended early; retry to resume")
    target.rename(dest_dir / name)
    return dest_dir


def _stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _poll_aria2(cmd, job, part, total, name, report) -> int:
    with open(part.parent / "aria2.log", "wb") as logf:
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)
        _procs[job["id"]] = proc
        last_size = 0
        last_grow = time.monotonic()
        try:
            while True:
                try:
                    return proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                status = _job_status(job["id"])
                if status == "paused":
                    _stop(proc)
                    raise Paused()
                if status == "cancelled":
                    _stop(proc)
                    raise Cancelled()
                # aria2 writes sparse files (--file-allocation=none): st_size jumps to ~full
                # immediately, so use allocated blocks for real progress.
                st = part.stat() if part.exists() else None
                size = st.st_blocks * 512 if st else 0
                now = time.monotonic()
                if size > last_size:
                    last_size, last_grow = size, now
                elif now - last_grow > STALL_SECONDS:
                    _stop(proc)
                    raise DownloadError(
                        f"download stalled at {size / 2**20:.0f} MiB (no progress for {STALL_SECONDS}s); "
                        "the source node is not serving data — retry later"
                    )
                report(size / total if total else None,
                       f"downloading {name}: {size / 2**20:.0f}/{total / 2**20:.0f} MiB")
        finally:
            _procs.pop(job["id"], None)


def download_aria2(job, report) -> Path:
    url = job["source"]
    archive = is_archive_url(url)
    if archive:
        url = urllib.parse.urlsplit(url)._replace(scheme="https").geturl()
    dest_dir = work_dir(job["id"], job["created"])
    dest_dir.mkdir(exist_ok=True)
    entry = catalog.find(job["source"]) or catalog.find(url)
    total = int(entry.get("size") or 0) if entry else 0
    cookie_file = dest_dir / "cookies.txt"
    have_cookie = False
    if archive:
        with download_session(url) as session:
            have_cookie = write_cookies(session, cookie_file)
            if not total:
                total = head_size(url, session, {})
    elif not total:
        with requests.Session() as session:
            total = head_size(url, session, {})
    if total:
        need_space(WORK, total)
    name = _filename(url)
    part = dest_dir / f"{name}.part"
    common = ["aria2c", "--continue=true", "--auto-file-renaming=false", "--allow-overwrite=true",
              "--file-allocation=none", f"--max-connection-per-server={ARIA2_CONN}",
              "--min-split-size=8M", "--disable-ipv6=true", "--max-tries=10", "--retry-wait=5",
              "--timeout=60", "--connect-timeout=20", "--disk-cache=32M", "--auto-save-interval=10",
              f"--lowest-speed-limit={MIN_DOWNLOAD_RATE}",
              f"--out={name}.part", f"--dir={dest_dir}", "--user-agent=Shelf/1.0"]
    if have_cookie:
        common.append(f"--load-cookies={cookie_file}")
    rc = -1
    try:
        for split in dict.fromkeys((ARIA2_CONN, 1)):
            rc = _poll_aria2(common + [f"--split={split}", url], job, part, total, name, report)
            status = _job_status(job["id"])
            if status == "paused":
                raise Paused()
            if status == "cancelled":
                raise Cancelled()
            if rc == 0 or split == 1 or rc not in ARIA2_RETRY_CODES:
                break
    finally:
        cookie_file.unlink(missing_ok=True)
    if rc != 0:
        try:
            args = " ".join(a for a in common[1:] if not a.startswith("--header="))
            log.warning("aria2 exit %s, args %s, log tail:\n%s", rc, args, (dest_dir / "aria2.log").read_text(errors="replace")[-1200:])
        except OSError:
            pass
        raise DownloadError(ARIA2_ERRORS.get(rc, f"download failed (aria2 error {rc})"))
    (dest_dir / f"{name}.part.aria2").unlink(missing_ok=True)
    if not part.exists():
        raise DownloadError("aria2 reported success but produced no file")
    final = dest_dir / name
    part.rename(final)
    if total and final.stat().st_size < total:
        raise ConvertError("download ended early; retry to resume")
    return dest_dir


def run_fetch(job):
    report = reporter(job["id"])
    if job["kind"] == "download":
        try:
            folder = (download_aria2 if is_aria2() else download)(job, report)
        except Paused:
            raise
        except Cancelled:
            remove_work_dir(job["id"], job["created"])
            raise
        except Exception:
            d = work_dir(job["id"], job["created"])
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
            raise
    else:
        folder = Path(job["source"])
    conv = Converter(report)
    try:
        conv.process(folder)
        if not conv.added:
            raise ConvertError("no installable PKG found" + (f" (skipped: {', '.join(conv.skipped[:5])})" if conv.skipped else ""))
    except Exception:
        if folder.exists():
            shutil.move(str(folder), FAILED / folder.name)
        raise
    shutil.rmtree(folder, ignore_errors=True)
    titles = [db.q("SELECT title FROM items WHERE id=?", i)[0]["title"] for i in conv.added]
    msg = "added: " + ", ".join(titles)
    if conv.skipped:
        msg += f" | ignored {len(conv.skipped)} other file(s)"
    db.finish_job(job["id"], "done", progress=1, message=msg)
    src = job.get("source", "")
    if src.startswith(("http://", "https://")):
        for item_id in conv.added:
            db.x("UPDATE items SET source_url=? WHERE id=? AND (source_url IS NULL OR source_url='')", src, item_id)
    method = (job.get("autosend") or "") or db.settings()["auto_send"]
    if method in ("ftp", "rpi"):
        for item_id in conv.added:
            db.add_job(f"send_{method}", item_id=item_id, parent_id=job["id"])
    elif method == "auto":
        for item_id in conv.added:
            db.add_job("send_auto", item_id=item_id, parent_id=job["id"])


def run_send(job):
    s = db.settings()
    if not s["ps4_host"]:
        raise DeliveryError("console IP not set: open Settings and enter it")
    items = db.q("SELECT * FROM items WHERE id=?", job["item_id"])
    if not items:
        raise DeliveryError("library item no longer exists")
    report = reporter(job["id"])

    def cancelled():
        return _job_status(job["id"]) == "cancelled"
    fn = {"send_ftp": ftp_upload, "send_rpi": rpi_install, "send_auto": auto_install}[job["kind"]]
    log.info("job %s %s item %s (%s)", job["id"], job["kind"], items[0]["id"], items[0]["title"])
    try:
        fn(items[0], s, report, cancelled)
    except Cancelled:
        db.finish_job(job["id"], "failed", message="Cancelled")
        return
    log.info("job %s %s done", job["id"], job["kind"])
    db.finish_job(job["id"], "done", progress=1)


def loop(kinds, fn):
    while True:
        job = db.claim_job(kinds)
        if not job:
            time.sleep(2)
            continue
        try:
            fn(job)
        except Paused:
            continue
        except Cancelled:
            db.finish_job(job["id"], "failed", message="Cancelled")
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            db.finish_job(job["id"], "failed", message=f"The link answered HTTP {code} ({e.response.reason if e.response is not None else 'error'}). Check the address.")
        except requests.ConnectionError:
            db.finish_job(job["id"], "failed", message="Couldn't connect to the download server. Check the link and the server's internet access.")
        except requests.Timeout:
            db.finish_job(job["id"], "failed", message="The download server stopped responding. Retry to resume.")
        except (ConvertError, DeliveryError, DownloadError, requests.RequestException, OSError) as e:
            log.warning("job %s %s failed: %s", job["id"], job["kind"], e)
            db.finish_job(job["id"], "failed", message=str(e)[:500])
        except Exception as e:
            log.error(traceback.format_exc())
            db.finish_job(job["id"], "failed", message=f"{type(e).__name__}: {e}"[:500])


def start():
    for target, args in ((watch_inbox, ()), (loop, (["download", "ingest"], run_fetch)), (loop, (["send_ftp", "send_rpi", "send_auto"], run_send))):
        threading.Thread(target=target, args=args, daemon=True).start()
