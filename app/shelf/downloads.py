"""Download sessions, including cookie authentication for Internet Archive."""
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .config import ALLOWED_DOWNLOAD_HOSTS


class DownloadError(Exception):
    pass


def host_allowed(url) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if not host:
        return False
    for pattern in ALLOWED_DOWNLOAD_HOSTS:
        base = pattern.lstrip(".")
        if base and (host == base or host.endswith("." + base)):
            return True
    return False


def is_aria2() -> bool:
    return shutil.which("aria2c") is not None


def is_archive_url(url):
    # Match the hostname, not arbitrary text in a query or archive.org.evil.test.
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host == "archive.org" or host.endswith(".archive.org")


def login_archive(session):
    """Authenticate once and scope returned login cookies to Archive HTTPS URLs."""
    user = os.environ.get("ARCHIVE_USER", "").strip()
    password = os.environ.get("ARCHIVE_PASS", "")
    if not user or not password:
        raise DownloadError("Internet Archive login requires ARCHIVE_USER (account email) and ARCHIVE_PASS in .env. Restart Shelf after changing them.")
    try:
        with session.post(
            "https://archive.org/services/xauthn/",
            params={"op": "login"},
            data={"email": user, "password": password},
            timeout=(15, 60),
            allow_redirects=False,  # Never redirect a POST containing credentials.
        ) as response:
            if response.status_code in (401, 403):
                raise DownloadError("Internet Archive login rejected. Check ARCHIVE_USER and ARCHIVE_PASS.")
            response.raise_for_status()
            if response.is_redirect:
                raise DownloadError("Internet Archive login redirected unexpectedly; retry later.")
            payload = response.json()
    except requests.Timeout:
        raise DownloadError("Internet Archive login timed out. Retry later.") from None
    except requests.RequestException:
        raise DownloadError("Could not contact Internet Archive's login service. Retry later.") from None
    except ValueError:
        raise DownloadError("Internet Archive returned an invalid login response. Retry later.") from None

    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise DownloadError("Internet Archive login failed. Check your email/password and whether the account requires additional verification.")
    values = payload.get("values")
    cookies = values.get("cookies") if isinstance(values, dict) else None
    names = ("logged-in-user", "logged-in-sig")
    if not isinstance(cookies, dict) or not all(isinstance(cookies.get(n), str) and cookies[n] for n in names):
        raise DownloadError("Internet Archive login did not return the required session cookies.")
    # Explicitly install cookies from the JSON response: Set-Cookie alone is insufficient.
    session.cookies.clear()
    for name in names:
        session.cookies.set(name, cookies[name], domain=".archive.org", path="/", secure=True)


def write_cookies(session, path) -> bool:
    """Persist archive.org cookies as a Netscape cookie file so they never appear in aria2's argv."""
    lines = ["# Netscape HTTP Cookie File"]
    for c in session.cookies:
        if not (c.domain or "").endswith("archive.org"):
            continue
        lines.append("\t".join([
            c.domain,
            "TRUE" if c.domain.startswith(".") else "FALSE",
            c.path or "/",
            "TRUE" if c.secure else "FALSE",
            str(int(c.expires or 0)),
            c.name,
            c.value,
        ]))
    if len(lines) == 1:
        return False
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def archive_direct_url(url, session) -> str | None:
    """Resolve archive.org/download/<item>/<file> to its real storage server.

    archive.org 302-redirects /download/ to a CDN node (dnXXXX) that is sometimes broken
    and returns HTTP 500; the item's storage server (iaXXXX) keeps working. Using it directly
    avoids those dead redirects.
    """
    parts = [x for x in urlsplit(url).path.split("/") if x]
    if len(parts) < 3 or parts[0] != "download":
        return None
    item, filename = parts[1], "/".join(parts[2:])
    try:
        r = session.get(f"https://archive.org/metadata/{item}", timeout=(10, 30))
        r.raise_for_status()
        meta = r.json()
    except (requests.RequestException, ValueError):
        return None
    server = meta.get("server") or meta.get("d1")
    directory = meta.get("dir")
    if not server or not directory:
        return None
    return f"https://{server}{directory}/{filename}"


def archive_url_ok(url, session) -> bool:
    """Cheap probe: is the /download/ URL (including its redirect target) serving data right now?"""
    try:
        r = session.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=(10, 20))
        return r.status_code < 400
    except requests.RequestException:
        return False


def head_size(url, session, headers) -> int:
    try:
        r = session.head(url, headers=headers, timeout=30, allow_redirects=True)
        r.raise_for_status()
        return int(r.headers.get("content-length", 0))
    except (requests.RequestException, ValueError):
        return 0


@contextmanager
def download_session(url):
    """Keep authentication in memory for this download and close it on every exit."""
    with requests.Session() as session:
        if is_archive_url(url):
            login_archive(session)
        yield session
