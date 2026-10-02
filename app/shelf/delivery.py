"""Getting PKGs onto the console.

ftp -> copies the PKG to the PS4's HDD through the homebrew FTP server (GoldHEN/etaHEN, port 2121).
rpi -> asks Remote Package Installer (port 12800) to download + install it straight from this server.
"""
import ftplib
import json
import logging
import posixpath
import re
import socket
import time
import urllib.parse
from pathlib import Path

import requests

log = logging.getLogger("shelf.delivery")


class DeliveryError(Exception):
    pass


class Cancelled(Exception):
    pass


class Paused(Exception):
    pass


def rpi_json(text: str) -> dict:
    """Remote Package Installer writes numbers as bare hex (`"length": 0x44A0000`), which isn't JSON."""
    return json.loads(re.sub(r"(:\s*)0x([0-9A-Fa-f]+)", lambda m: m[1] + str(int(m[2], 16)), text))


def remote_size(ftp: ftplib.FTP, name: str) -> int:
    """GoldHEN's SIZE is 32-bit and wraps for files >4 GiB; MLST reports the real 64-bit size."""
    try:
        m = re.search(r"\bsize=(\d+)", ftp.sendcmd(f"MLST {name}"))
        if m:
            return int(m.group(1))
    except ftplib.all_errors:
        pass
    try:
        return ftp.size(name) or 0
    except ftplib.all_errors:
        return 0


def probe(host: str, port: int, timeout=1.5) -> bool:
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_http(host: str, port: int, timeout=3.0) -> bool:
    """Unlike probe(), require an actual HTTP reply: a wedged RPI accepts TCP then sends nothing."""
    if not host:
        return False
    try:
        requests.get(f"http://{host}:{port}/", timeout=timeout)
        return True
    except requests.RequestException:
        return False


def ftp_upload(item: dict, s: dict, report, cancelled=lambda: False):
    src = Path(item["path"])
    total = src.stat().st_size
    ftp = ftplib.FTP()
    try:
        ftp.connect(s["ps4_host"], int(s["ftp_port"]), timeout=30)
        ftp.login(s["ftp_user"] or "anonymous", s["ftp_pass"])
        ftp.set_pasv(True)
        path = "/"
        for part in s["ftp_dir"].strip("/").split("/"):
            if not part:
                continue
            path = posixpath.join(path, part)
            try:
                ftp.mkd(path)
            except ftplib.error_perm:
                pass  # already exists
        ftp.cwd(s["ftp_dir"] or "/")
        name = f"{item['content_id']}.pkg"
        ftp.voidcmd("TYPE I")
        offset = remote_size(ftp, name)
        if offset >= total:
            if offset == total:
                log.info("FTP %s already on console (%d bytes)", name, total)
                report(1, f"already on the console at {s['ftp_dir']}/{name}")
                return
            log.warning("remote %s is larger (%d) than local (%d); restarting", name, offset, total)
            offset = 0
        log.info("FTP upload start: %s (%d bytes) -> %s/%s, resuming at %d", name, total, s["ftp_dir"], name, offset)
        sent = offset
        last = 0.0

        def cb(block):
            nonlocal sent, last
            if cancelled():
                raise Cancelled()
            sent += len(block)
            if time.monotonic() - last > 1:
                last = time.monotonic()
                report(sent / total, f"FTP {sent / 2**20:.0f}/{total / 2**20:.0f} MiB")

        with open(src, "rb") as f:
            f.seek(offset)
            try:
                ftp.storbinary(f"STOR {name}", f, 1024 * 1024, cb, rest=offset or None)
            except (ftplib.error_perm, ftplib.error_temp) as e:
                if not offset:
                    raise
                # GoldHEN FTP returns 451 (error_temp) for large REST offsets; also seen for 5xx.
                log.warning("REST resume at %d refused (%s); restarting from 0", offset, e)
                f.seek(0)
                sent = 0
                try:
                    ftp.delete(name)
                except ftplib.all_errors:
                    pass
                ftp.storbinary(f"STOR {name}", f, 1024 * 1024, cb, rest=0)
        remote = remote_size(ftp, name)
        if remote != total:
            raise DeliveryError(f"size mismatch after upload: remote {remote} vs local {total}")
        log.info("FTP upload done: %s (%d bytes) at %s/%s", name, total, s["ftp_dir"], name)
        report(1, f"uploaded to {s['ftp_dir']}/{name}")
    except (ftplib.all_errors) as e:
        raise DeliveryError(f"FTP: {e}") from e
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()


def pkg_url(item: dict, public_url: str) -> str:
    name = urllib.parse.quote(f"{item['content_id']}.pkg")
    return f"{public_url.rstrip('/')}/pkg/{item['id']}/{name}"


def rpi_install(item: dict, s: dict, report, cancelled=lambda: False):
    if not s["public_url"]:
        raise DeliveryError("set this server's address (as the console sees it) in Settings first")
    base = f"http://{s['ps4_host']}:{s['rpi_port']}/api"
    url = pkg_url(item, s["public_url"])
    log.info("RPI install start: %s -> %s", item.get("title") or item.get("content_id"), url)
    try:
        r = requests.post(f"{base}/install", data=json.dumps({"type": "direct", "packages": [url]}), timeout=30)
        res = rpi_json(r.text)
    except (requests.RequestException, ValueError) as e:
        log.warning("RPI install POST failed: %r", e)
        raise DeliveryError(f"RPI not reachable: {e}") from e
    if res.get("status") != "success":
        raise DeliveryError(f"RPI refused install: {res}")
    task = res["task_id"]
    log.info("RPI task %s started: %s", task, res.get("title", ""))
    report(0, f"RPI task {task} started: {res.get('title', '')}")
    last_contact = last_change = time.monotonic()
    seen = None
    fails = 0
    while True:
        time.sleep(3)
        if cancelled():
            raise Cancelled()
        try:
            p = rpi_json(requests.post(f"{base}/get_task_progress", data=json.dumps({"task_id": task}), timeout=15).text)
            fails = 0
        except (requests.RequestException, ValueError) as e:
            fails += 1
            silent = time.monotonic() - last_contact
            if fails == 1 or fails % 20 == 0:
                log.warning("RPI progress poll failed (attempt %d, %ds since last contact): %r", fails, int(silent), e)
            if silent > 600:
                raise DeliveryError("lost contact with Remote Package Installer for 10 minutes")
            continue
        last_contact = time.monotonic()
        if p.get("status") != "success":
            raise DeliveryError(f"Remote Package Installer reported: {p}")
        if int(p.get("error", 0)):
            raise DeliveryError(f"console reported install error 0x{int(p['error']):08X}")
        total = int(p.get("length_total") or p.get("length") or 0)
        done = int(p.get("transferred_total") or p.get("transferred") or 0)
        if (total, done) != seen:
            seen, last_change = (total, done), time.monotonic()
        elif time.monotonic() - last_change > 900:
            raise DeliveryError("install made no progress for 15 minutes; check the console")
        if total:
            report(done / total, f"installing on the console {done / 2**20:.0f}/{total / 2**20:.0f} MiB")
            if done >= total:
                report(1, "installed on the console")
                return


def auto_install(item: dict, s: dict, report, cancelled=lambda: False):
    if probe_http(s["ps4_host"], int(s["rpi_port"] or 0)):
        return rpi_install(item, s, report, cancelled)
    return ftp_upload(item, s, report, cancelled)


def _ftp_login(s: dict, timeout=5) -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(s["ps4_host"], int(s["ftp_port"] or 0), timeout=timeout)
    ftp.login(s["ftp_user"] or "anonymous", s["ftp_pass"])
    ftp.set_pasv(True)
    return ftp


def _ftp_close(ftp: ftplib.FTP):
    try:
        ftp.quit()
    except Exception:
        try:
            ftp.close()
        except Exception:
            pass


def console_inventory(s: dict, timeout=5) -> dict:
    """Best-effort console inventory: an unreachable or partial FTP server yields empty lists."""
    empty = {"reachable": False, "installed": [], "pkg": []}
    if not s.get("ps4_host"):
        return empty
    try:
        ftp = _ftp_login(s, timeout)
    except ftplib.all_errors:
        return empty
    try:
        installed = []
        try:
            installed = sorted({posixpath.basename(p) for p in ftp.nlst("/user/appmeta") if posixpath.basename(p)})
        except ftplib.all_errors:
            pass
        pkg = []
        d = (s.get("ftp_dir") or "/").rstrip("/") or "/"
        try:
            for path in ftp.nlst(d):
                name = posixpath.basename(path)
                if not name or name in (".", ".."):
                    continue
                pkg.append({"name": name, "size": remote_size(ftp, posixpath.join(d, name))})
        except ftplib.all_errors:
            pass
        pkg.sort(key=lambda e: e["name"].lower())
        return {"reachable": True, "installed": installed, "pkg": pkg}
    finally:
        _ftp_close(ftp)


def console_delete(s: dict, names: list, timeout=15) -> list:
    if not s.get("ps4_host"):
        return []
    try:
        ftp = _ftp_login(s, timeout)
    except ftplib.all_errors:
        return []
    removed = []
    try:
        d = (s.get("ftp_dir") or "/").rstrip("/") or "/"
        for name in names:
            try:
                ftp.delete(posixpath.join(d, name))
                removed.append(name)
            except ftplib.all_errors:
                continue
    finally:
        _ftp_close(ftp)
    return removed
