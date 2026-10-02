# Homebrew Shelf

> **⚠️ Fully vibecoded.** This project was written end-to-end by AI coding agents
> (opencode) under human direction. It is a hobby project: expect rough edges, review the
> code before trusting it, and use it only with content you are legally allowed to handle.

Self-hosted remote-install shelf for a homebrew-enabled PS4. Point it at a download link (or drop a
file into its inbox), and it downloads, validates, converts and delivers content to the console over
FTP or through Remote Package Installer. There is a small web UI, a background job queue, and an
optional Shop fed by a catalog file you supply.

**Scope: legitimate content only** — homebrew apps and backups dumped from games you own.
No catalog is bundled with the image. The optional Shop tab reads a `GAMES.json` that you supply and
bind-mount read-only at runtime; you are responsible for making sure every URL in it points at content
you are allowed to download.

---

## Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Console setup](#console-setup)
- [Features](#features)
- [API reference](#api-reference)
- [Job model](#job-model)
- [Configuration](#configuration)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Testing](#testing)
- [Limitations / not implemented](#limitations--not-implemented)
- [Project layout](#project-layout)

---

## Overview

Homebrew Shelf is a single-container hobby service. It watches a data directory, turns whatever you
give it into a validated, installable PS4 `.pkg`, keeps a library of metadata + icons in SQLite, and
can push each item to a console either by copying it over the homebrew FTP server or by asking
Remote Package Installer (RPI) to pull it straight from the shelf over HTTP.

What it is good at:

- One place to queue downloads (including `aria2c` multi-connection transfers from Internet Archive).
- Converting a raw game dump folder into a fake PKG with LibOrbisPkg's `PkgTool`.
- Resumable, size-verified FTP delivery, or direct install via RPI.
- A browsable library with covers, plus an optional Shop that shows a catalog you provide.
- Publishing the library as FPKGi JSON so the FPKGi homebrew app can browse it.

What it deliberately is not: a store, a CDN, a multi-user service, or a DRM bypass. It ships no
content, has no authentication, and should only ever be exposed on your LAN or Tailscale network.

---

## Architecture

FastAPI app (`app/shelf/main.py`) + SQLite (`db.py`) + background daemon threads (`worker.py`).
One Uvicorn process, no broker, no external cache. Everything lives in the data directory.

### Data flow

```
                 POST /api/download         drop into inbox/          PUT /api/upload/{name}
           download jobs ─────────────┐                                  │
    POST /api/shop/install ───────────┤                                  │
                                      ▼                                  ▼
                             work/dl-<id>  ◄────────────  inbox watcher  ──┐
                                      │   (fetch worker thread)           │
                                      ▼                                   │
                        Converter.process()  (convert.py)                 │
        .pkg validate · split-part merge · unar extract · dump→GP4→PkgTool │
                                      │                                   │
                     ┌────────────────┴───────────────┐                   │
                     ▼                                ▼                   │
            library/<TITLE_ID>/<CONTENT_ID>.pkg   failed/<name>            │
            icons/<CONTENT_ID>.png                                        │
            items row in shelf.db  ◄──────────────────────────────────────┘
                     │
          auto_send / send jobs (send worker thread)
                     ▼
        delivery.py:  ftp_upload  |  rpi_install  |  auto_install
                     │                │
                     ▼                ▼
        GoldHEN FTP :2121     Remote Package Installer :12800
        copies to PS4         PS4 pulls GET /pkg/<id>/<name> and installs
```

### Background threads

`worker.start()` launches three daemon threads and `main.lifespan` adds a fourth:

| Thread | Code | Role |
|---|---|---|
| Inbox watcher | `worker.watch_inbox` | Every 20 s, finds top-level inbox entries that stopped changing, moves them to work and queues an `ingest` job. Ignores dotfiles and `.part/.crdownload/.tmp/.aria2`. |
| Fetch worker | `worker.loop(["download","ingest"], run_fetch)` | Downloads then converts, one job at a time. |
| Send worker | `worker.loop(["send_ftp","send_rpi","send_auto"], run_send)` | Delivers library items, one job at a time. |
| PS4 probe | `main._probe_loop` | Every 10 s, TCP-probes the FTP port and HTTP-probes the RPI port, exposed as `ps4` in `/api/state`. |

Jobs are persisted in SQLite, so they survive a restart. On startup `db.py` requeues any job left
`running` and finalises any left `cancelled`.

### Storage layout

Everything under the data dir (`DATA_DIR`, `/data` in the container; bind-mounted from `SHELF_DATA`):

```
/data/
├── inbox/                          drop PKGs, split parts, archives, or dump folders here
├── library/<TITLE_ID>/<CONTENT_ID>.pkg   validated, installable PKGs
├── work/dl-<job>-<created>/        active downloads + conversion scratch (+ aria2.log, cookies.txt)
│   └── ingest-<job>-<ts>/          inbox handoff; removed after processing
├── failed/<name>/                  inputs that could not be converted (kept for inspection)
├── icons/<CONTENT_ID>.png           extracted title icons
├── backups/shelf-<timestamp>.db     snapshots from POST /api/db/backup
└── shelf.db                        SQLite (WAL): items, jobs, settings
```

The Storage tab enumerates three kinds of directories: `work/dl-*` (downloads), `work/ingest-*`
(inbox staging) and `failed/*` (kept for inspection). Each entry is tagged with a **Download**,
**Prepare** or **Failed** badge.

### Container constraints (`docker-compose.yml`)

- `mem_limit: 2g`, `cpus: 1.5` — the `.NET` `PkgTool` conversion is the heavy part; give it room if you
  convert large dumps (see [Troubleshooting](#troubleshooting)).
- Runs as `user: "1000:1000"`, so the bind-mounted `SHELF_DATA` must be writable by your UID.
- Optionally joins an external reverse-proxy network (name via `PROXY_NETWORK`, default `proxy`) with alias `shelf`, so a reverse proxy can expose it.
- Publishes `${SHELF_PORT:-8420}:8420` on `0.0.0.0` on purpose: the console downloads PKGs from it.
- `CATALOG_PATH=/app/catalog/GAMES.json` and `ARIA2_CONN=8` are set in the service environment.

---

## Quick start

```bash
git clone <your-repo> shelf
cd shelf

cp .env.example .env
$EDITOR .env                 # optional credentials/limits; defaults are fine to start

# optional: only if you front Shelf with a reverse proxy; name must match PROXY_NETWORK
docker network create "${PROXY_NETWORK:-proxy}" 2>/dev/null || true

docker compose up -d --build
```

Open the UI at `http://<server-LAN-IP>:8420` (or your reverse-proxy hostname).

In the UI, open **Settings** and set:

- **Console IP address** — the PS4's LAN IP (`PS4_HOST`).
- **This server's address, as the console sees it** — the server's LAN address (`PUBLIC_URL`), e.g.
  `http://192.168.1.10:8420`. RPI and FPKGi need this; use the LAN IP, not the Tailscale name.

The status light in the header shows whether RPI, FTP, or neither is answering.

---

## Console setup

1. **Install a homebrew enabler** (GoldHEN or etaHEN) on the PS4. This provides the FTP server and the
   Remote Package Installer payload.

2. **FTP delivery** — start the FTP server in GoldHEN/etaHEN. Default port is `2121`; the default
   destination folder is `/data/pkg`. Shelf creates the folder if missing, resumes partial uploads,
   and verifies the size after upload. Configure in Settings: FTP port, FTP folder, FTP user/password.

3. **Direct install (RPI)** — start **Remote Package Installer** on the console (default port `12800`).
   Shelf calls `POST /api/install` with a `direct` package URL and polls `POST /api/get_task_progress`
   until the install finishes. The console downloads the PKG from `PUBLIC_URL/pkg/...`, so that URL
   must be reachable from the console.

4. **Install from `/data/pkg` manually** — if you copied a PKG over FTP (or Shelf did), on the console
   go to **Debug Settings → Package Installer → Install Package**, choose source
   `hdd:/data/pkg/`, select the PKG and install it. This path needs no RPI and no `PUBLIC_URL`.

5. **FPKGi app** — point the FPKGi homebrew app at this shelf to browse its library:
   `http://<server>:8420/fpkgi/GAMES.json` (also `UPDATES.json`, `DLC.json`, `HOMEBREW.json`).

---

## Features

### Library and conversion pipeline

`convert.py:Converter.process` handles inputs recursively, so an archive of split parts works too:

| Input | What happens |
|---|---|
| `.pkg` | Header magic (`\x7fCNT`) and `param.sfo` validated by `pkg.py:read_pkg`; title, title ID, version, min firmware, category, region and icon are extracted. |
| Split parts `x.pkg.part0..`, `x.pkg.000..`, `x_0.pkg..` | `group_entries` detects the first part as a PKG header and the rest as non-PKG, then merges them in order. |
| `.zip .7z .rar .tar .tar.gz .tgz .tar.xz .tar.bz2` | Extracted with `unar`; contents processed recursively. Extraction size capped by `EXTRACT_MAX_GB` (decompression-bomb guard). |
| Dump folder (`sce_sys/param.sfo` at its root) | `make_gp4` writes a `.shelf.gp4` project, `PkgTool pkg_build` creates the fake PKG, then `PkgTool pkg_validate` verifies it. Only base-game/app dumps (`CATEGORY gd`) are accepted. |

Validated PKGs land in `library/<TITLE_ID>/<CONTENT_ID>.pkg`, icons in `icons/<CONTENT_ID>.png`, and a
row is upserted into `items` (keyed on `content_id`). Anything unusable is moved to `failed/` so you
can inspect it. Disk space is checked before downloading, merging, extracting and building via
`convert.need_space` (needs `size * 1.05 + 512 MiB` free). A failure surfaces the reason in the job's
`message`.

### Delivery

Three kinds, selected by the job kind in `worker.run_send`:

- **FTP** (`delivery.py:ftp_upload`) — connects to the homebrew FTP server, creates the destination
  folder, and uploads with `STOR`. Partial transfers resume using the remote size as the `REST`
  offset. Remote size is read with `MLST` because GoldHEN's `SIZE` is 32-bit and wraps above 4 GiB;
  `SIZE` is the fallback (`delivery.py:remote_size`). If the server refuses a large `REST` with `451`
  or another permanent/temporary error, the upload restarts from 0 and deletes the partial. After
  upload, the remote and local sizes are compared and a mismatch is an error.
- **RPI** (`delivery.py:rpi_install`) — requires `PUBLIC_URL`. POSTs a `direct` install request to
  `http://<ps4>:<rpi_port>/api/install`, then polls `get_task_progress` every 3 s, reporting progress.
  RPI writes hex numbers that are not valid JSON (`"length": 0x44A0000`); `rpi_json` converts them.
  Gives up after 10 minutes without contact or 15 minutes without progress.
- **Auto** (`delivery.py:auto_install`) — HTTP-probes RPI (`probe_http`, requires a real HTTP reply,
  since a wedged RPI accepts TCP then sends nothing). If RPI answers, it installs directly; otherwise
  it falls back to FTP.

`AUTO_SEND` (Settings → "When something new is ready") can be `none`, `ftp`, `rpi` or `auto`; after a
successful conversion `run_fetch` queues the matching send job for each new item. The values `ftp`
and `rpi` are exposed in the UI; `auto` is accepted by the API and config. Each library item also has
an **Auto install/copy** button that queues `send_auto`, so you can pick RPI-with-FTP-fallback per
item even when `AUTO_SEND` is `none`.

### Shop

The Shop tab is driven entirely by a catalog file. No catalog ships with the image.

- **Format** (`catalog.py`) — FPKGi's shape: `{"DATA": {url: {title_id, region, name, version,
  release, size, min_fw, cover_url}}}`. A bare mapping without the `DATA` wrapper is also accepted.
- **Mounting** — put your catalog at `catalog/GAMES.json`; `docker-compose.yml` bind-mounts `./catalog`
  read-only to `/app/catalog`, and `CATALOG_PATH` points at `/app/catalog/GAMES.json`. `catalog.py:_path`
  falls back to `<DATA_DIR>/catalog/GAMES.json` if the configured path does not exist. The catalog is
  **not** committed (it is gitignored) — supply your own.
- **Reload** — the file is re-read automatically when its mtime changes; no restart needed (covered by
  `tests/test_catalog.py`).
- **One-click install** (`main.shop_install`) — dedupes an in-flight download for the same URL
  (returns its job id), sends directly via `send_auto` when the item is already in the library, and
  otherwise queues a `download` job after a disk-space check. A full disk returns HTTP 507.
- **Duplicate badges** — `catalog.load` normalises names and flags every entry whose normalised name
  appears more than once with `duplicate: true`; the UI shows "another version in library".
- **Search/filter** — `GET /api/catalog` supports `q` (matches name and title ID), `region`, `limit`
  and `offset`. The UI also filters the already-loaded list client-side and populates a region dropdown.
- **Cover art** — `cover_url` is loaded directly by the browser; the shelf does not proxy it.

### aria2c parallel downloader

Internet Archive throttles per connection, so a single stream is slow and a slow node can stall
forever. When `aria2c` is present (`downloads.is_aria2()`), downloads use `worker.download_aria2`:

- Multi-connection with `--split`/`--max-connection-per-server` (`ARIA2_CONN`, default 8) and
  `--min-split-size=8M`.
- Sparse output: `--file-allocation=none`, so the file's apparent size jumps to full almost
  immediately while only written blocks are allocated. Progress is therefore measured from
  `st_blocks * 512`, not `st_size`.
- Resume across restarts: `--continue=true` keeps `<name>.part` and the `<name>.part.aria2` control
  file. Pausing terminates the process but leaves both files, so Resume picks up where it left off.
- Archive jobs log in first and pass cookies to aria2 through a temporary Netscape cookie file in the
  job's work dir (`downloads.write_cookies`), removed in a `finally`. Cookies never touch the command
  line or logs.
- If a multi-connection attempt fails with a retryable aria2 exit code (`ARIA2_RETRY_CODES`), it is
  retried once with `--split=1`. Fatal codes map to human-readable messages in `ARIA2_ERRORS`.
- Archive.org server rotation: `downloads.archive_candidates` reads the item metadata and returns the
  `/download/` URL first, then each storage server (`server`, `d1`, `d2`, `workable_servers`). A node
  that fails its health probe or exits aria2 with a retryable code is skipped for the next candidate.
- Checksum verification: when `VERIFY_CHECKSUM` is on (default), the archive item metadata's `md5`,
  `sha1` and `crc32` are compared against the finished file (`downloads.verify_checksum`). A mismatch
  keeps the file for inspection and raises a transient error so the job retries.
- Without aria2, `worker.download` (requests) does a streamed download with `Range` resume of an
  existing `*.part` and retries.

### Stall guard

Dead or throttled archive.org nodes can sit at 0 bytes/s indefinitely. Two guards:

- `MIN_DOWNLOAD_RATE` (default `100K`) is passed to aria2 as `--lowest-speed-limit`; aria2 drops a
  connection slower than this. Suffixes `K`/`M` are allowed.
- `STALL_SECONDS` (default 180) is a watchdog in `worker._poll_aria2`: if allocated bytes do not grow
  for that long, aria2 is terminated and the job fails with a "download stalled" message so you can
  retry later.

A transfer that is already `FINISH_NEAR_COMPLETE_PCT` (default 95) complete is exempt: aria2 is not
given `--lowest-speed-limit` for it, and the stall deadline is tripled, so a slow tail is allowed to
finish instead of throwing away a near-complete multi-GB file.

### Storage / cleanup tab

`GET /api/storage` returns free/total disk, `failed_bytes`, `library_bytes`, and every work/failed
directory with:

- `id` — deletion token: the directory name for `work/` entries (`dl-…`, `ingest-…`) or
  `failed/<name>` for `failed/` entries
- `category` — `download`, `ingest` or `failed`
- `job_id` (or `null` for an orphan or a `failed/` entry), `kind`, `status`, `name`, `modified`
- sparse-aware `alloc_bytes` (`st_blocks * 512`) and `apparent_bytes` (`st_size`)
- the list of files in the directory

The UI shows summary cards, a per-category badge, per-row delete, checkbox bulk delete, and **Clean
all orphans**. Per-row and bulk deletes post `{"ids": [...]}`; **Clean all orphans** posts
`{"all_orphans": true}`. `POST /api/storage/clean` returns `{"removed": [...], "freed": n}`:
`ids` are resolved and must stay inside `work/` or `failed/` (escapes are rejected with 400),
`all_orphans` deletes only `work/dl-*` and `work/ingest-*` directories, and `failed/` entries are
deleted only via an explicit `ids` request. Directories belonging to queued, running or paused jobs
are never deleted. **Library items** can be deleted from the same tab (`DELETE /api/items/{id}`
removes the PKG, the icon and the DB row).

### Console tab

`GET /api/console` connects to the homebrew FTP server (`ps4_host:ftp_port`, same credentials as FTP
delivery) just long enough to inventory the console: `reachable`, the installed title ids from
`/user/appmeta`, and the files in the destination `ftp_dir` with their sizes. The connect/login is
best-effort like `probe_http` — any failure returns HTTP 200 with `reachable: false` and empty lists
rather than an error, so the UI can show the console as offline without the request failing.
`POST /api/console/delete` removes named files from `ftp_dir`; names containing `/`, `\` or `..` are
rejected with 400 and missing files are ignored. The Shop also marks entries already installed on the
console (`on_console`).

### Notifications

Set `notify_webhook` (Settings, or `NOTIFY_WEBHOOK`) to an http(s) URL to be POSTed a generic
`{"text": "...", "content": "..."}` payload whenever a job reaches `done` or `failed` — suitable for
Slack/Discord-style incoming webhooks. Delivery is best-effort with a 5 s timeout and never fails the
worker; a failed POST is logged at warning. Cancelled jobs do not notify unless their final status is
`failed`.

### Config export / import and DB backup

`GET /api/config/export` returns every setting (never the `ftp_pass` value) plus `exported_at`, for
copying a configuration between installs. `POST /api/config/import` accepts that dict and validates each
field with exactly the same rules as `/api/settings` (ports, `ftp_dir`, `public_url`, `notify_webhook`,
`auto_send`, booleans), applying valid values or returning 400. `POST /api/db/backup` writes a consistent
snapshot with SQLite's online backup API to `<DATA_DIR>/backups/shelf-<UTC timestamp>.db` and returns
the path and size.



`POST /api/jobs/{id}/pause` pauses a queued or running job and calls `worker.request_pause`, which
terminates the aria2 process but keeps `.part` + `.aria2`. `resume` requeues a paused job so the
download continues. `cancel` is **non-destructive**: it stops the transfer but never deletes the work
dir, `.part` or `.aria2` (a mis-click must not destroy multi-GB progress). A queued/paused job becomes
`failed` / "Cancelled"; a running job is marked `cancelled` and the worker finalises it. Work dirs are
deleted only from the Storage tab (`POST /api/storage/clean`, `POST /api/jobs/{id}/delete`). Jobs
interrupted by a restart are requeued; paused jobs stay paused.

### Auto-retry with backoff

Transient download failures — a bad/slow archive.org node, aria2 retry codes (`ARIA2_RETRY_CODES`),
`requests` network errors, "too slow" / "bad response header" / "ended early", or a checksum mismatch —
requeue the same job with exponential backoff (60/300/900 s) up to `MAX_RETRIES` (default 3) instead of
failing permanently. Each failed run increments `jobs.attempts`; `jobs.retry_at` holds the next eligible
time and `db.claim_job` skips a job until then. Both columns are surfaced by `/api/state` (`jobs` uses
`SELECT *`). Success resets them to 0. Non-transient errors (bad URL, HTTP 404, disk full, no installable
PKG) and user cancel/pause never auto-retry; the **Retry** button always resets `attempts`/`retry_at`.

### FPKGi endpoint

`GET /fpkgi/{section}.json` publishes this shelf's own library so the FPKGi console app can browse and
install from it. Sections map to library categories:

| Section | `items.category` |
|---|---|
| `GAMES` | `game` |
| `UPDATES` | `update` |
| `DLC` | `dlc` |
| `HOMEBREW` | `homebrew` |

Each entry's key is the absolute `PUBLIC_URL/pkg/<id>/<CONTENT_ID>.pkg`, with `title_id`, `region`,
`name`, `version`, `release`, `size`, `min_fw` and `cover_url` in the same shape as the Shop catalog.

### Logging

Logs go to stdout, so `docker logs -f shelf` is the main tool. You will see job lifecycle lines from
`worker` (start/finish/failure), delivery lines from `shelf.delivery` (FTP offsets, RPI tasks), and
aria2 failure tail lines (the last ~1200 bytes of `work/dl-*/aria2.log`) when a transfer fails. Set
`--log-level`/`PYTHONUNBUFFERED` if you want more detail.

---

## API reference

All JSON unless noted. There is no authentication (see [Security](#security)).

| Method | Path | Body | Success | Errors |
|---|---|---|---|---|
| GET | `/` | — | `text/html` UI | — |
| GET | `/healthz` | — | `{"ok": true}` | — |
| GET | `/api/state` | — | items, latest 60 jobs, settings (sans `ftp_pass`), PS4 probe, disk free/total | — |
| GET | `/api/catalog` | query: `q`, `region`, `limit`, `offset` | `{"count": n, "entries": [...]}` | — |
| GET | `/api/console` | — | `{"reachable": bool, "host": str, "installed": [...], "pkg": [...], "pkg_bytes": n}` | — |
| POST | `/api/console/delete` | `{"names": ["..."]}` | `{"removed": [...]}` | 400 unsafe name |
| POST | `/api/shop/install` | `{"url": "..."}` | `{"job": id}` | 400 not in catalog; 507 no space |
| POST | `/api/download` | `{"url": "..."}` | `{"job": id}` | 400 bad URL / host not allowed; 507 no space |
| PUT | `/api/upload/{filename}` | raw bytes (streamed) | `{"ok": true}` | 400 bad filename; 413 over `MAX_UPLOAD_GB` |
| POST | `/api/items/{id}/send` | `{"method": "ftp"\|"rpi"\|"auto"}` | `{"job": id}` | 400 bad method |
| DELETE | `/api/items/{id}` | — | `{"ok": true}` | 404 not found |
| POST | `/api/jobs/{id}/retry` | — | `{"ok": true}` | 404 not found |
| POST | `/api/jobs/{id}/pause` | — | `{"ok": true}` | 404 not found |
| POST | `/api/jobs/{id}/resume` | — | `{"ok": true}` | 404 not found |
| POST | `/api/jobs/{id}/cancel` | — | `{"ok": true}` | 404 not found |
| POST | `/api/jobs/{id}/delete` | — | `{"ok": true}` | 404 not found; 409 still active |
| POST | `/api/jobs/clear` | — | `{"ok": true}` | — |
| GET | `/api/jobs` | query: `limit` (default 50, max 500) | `{"jobs": [...]}`, newest first | — |
| GET | `/api/storage` | — | free/total, `failed_bytes`, `work[]`, `library_bytes` | — |
| POST | `/api/storage/clean` | `{"ids": [..], "job_ids": [..], "all_orphans": bool}` | `{"removed": [...], "freed": n}` | 400 unsafe id |
| POST | `/api/settings` | any subset of the setting keys | `{"ok": true}` | 400 invalid value |
| GET | `/api/config/export` | — | `{"settings": {...}, "exported_at": epoch}` (`ftp_pass` omitted) | — |
| POST | `/api/config/import` | settings dict | `{"ok": true}` | 400 invalid value |
| POST | `/api/db/backup` | — | `{"file": path, "size": n}` | — |
| GET/HEAD | `/pkg/{item_id}/{name}` | — | PKG bytes (`application/octet-stream`) | 404 missing item/file |
| GET | `/icons/{content_id}.png` | — | PNG, `Cache-Control: max-age=86400` | 404 |
| GET | `/fpkgi/{section}.json` | section `GAMES\|UPDATES\|DLC\|HOMEBREW` | `{"DATA": {url: {...}}}` | 404 unknown section |

Notes:

- **`/api/catalog`** merges each entry with its job/item state: `status` is one of `queued`, `running`,
  `paused`, `installed`, or a terminal job status; `job_id` and `item_id` link to the current job/item;
  `on_console` is true when the entry's `title_id` is installed on the console (the console set is
  fetched over FTP and cached for 60 s). `count` is the total before `limit`/`offset`.
- **`/api/shop/install`** returns the existing job id for an in-flight download, or a new `send_auto`
  job id if the item is already in the library, or a new `download` job id.
- **`/api/download`** requires `http(s)://` and, unless `ALLOW_ANY_DOWNLOAD` is set, a host in
  `ALLOWED_DOWNLOAD_HOSTS`.
- **`/api/upload/{filename}`** streams to `inbox/.<name>.uploading` and renames only on success, so the
  inbox watcher never sees a partial file.
- **`/api/settings`** validates `auto_send ∈ {none, ftp, rpi, auto}`, ports `1..65535`, `ftp_dir` as an
  absolute path with no `..`, `public_url` as `http(s)://…`, `notify_webhook` as `http(s)://…` or empty,
  and `pause_download_while_sending` as a boolean (`1/true/yes/on` or `0/false/no/off`). A blank `ftp_pass`
  keeps the stored password. Only keys present in `config.DEFAULTS` are persisted.
- **`/api/config/import`** runs the same validation as `/api/settings` over the posted dict and applies
  every valid value; any invalid field is a 400 and nothing is applied. `/api/config/export` mirrors
  `/api/state`'s settings (no `ftp_pass`) plus `exported_at`.
- **`/api/db/backup`** writes a consistent snapshot with SQLite's online backup API to
  `<DATA_DIR>/backups/shelf-<UTC timestamp>.db`.
- **`/api/console`** is a best-effort FTP inventory: if the console's FTP server does not answer, it
  returns HTTP 200 with `reachable: false` and empty `installed`/`pkg` lists. `installed` lists
  `/user/appmeta` title ids (named from the library when known); `pkg` lists files in `ftp_dir` with
  sizes. `POST /api/console/delete` refuses any name containing `/`, `\` or `..` and ignores missing files.
- **`/pkg/{item_id}/{name}`** is what RPI and FPKGi fetch; `HEAD` is supported for size checks.
- **`/icons/{content_id}.png`** sanitises the path with `os.path.basename`.

---

## Job model

Rows live in the `jobs` table (`db.py`). Kinds:

| Kind | Created by | Work |
|---|---|---|
| `download` | `/api/download`, `/api/shop/install` | download a URL, then convert |
| `ingest` | inbox watcher, or a forwarded inbox entry | convert a file/folder already on disk |
| `send_ftp` | `/api/items/{id}/send`, auto-send | copy a library item to the console over FTP |
| `send_rpi` | `/api/items/{id}/send`, auto-send | ask RPI to install a library item |
| `send_auto` | `/api/items/{id}/send`, `/api/shop/install`, auto-send | RPI with FTP fallback |

Statuses:

| Status | Meaning |
|---|---|
| `hold` | Transient placeholder while the inbox watcher moves a unit into `work/` before queuing it; the UI hides these. |
| `queued` | Waiting for the appropriate worker (`claim_job`). |
| `running` | Claimed by a worker and in progress. |
| `paused` | A running/queued job was paused; transfer state is kept for resume. |
| `done` | Finished successfully. |
| `failed` | Finished with an error; `message` explains why. |
| `cancelled` | Cancellation requested on a running job; the worker finalises it. |

Transitions are guarded in `worker.loop` / `run_fetch` / `run_send`. The important invariant: **terminal
states are written with `db.finish_job`**, not `update_job`. `finish_job` is what survives a late
cancel/pause (see `tests/test_worker_lifecycle.py`). On startup, any `running` job becomes `queued`
("requeued after restart") and any `cancelled` job becomes `failed`. `parent_id` links auto-send child
jobs to the fetch job that spawned them; the `autosend` column can override the global setting for a
single job. `attempts` counts failed download runs and `retry_at` is the earliest time `claim_job` may
pick the job up again (0 = now); both are reset to 0 when a job completes.

---

## Configuration

Settings have two layers: environment variables provide the **initial defaults** (`config.DEFAULTS`),
and values saved from the UI are stored in SQLite and take precedence on subsequent starts. `ftp_pass`
is never returned by `/api/state`.

### Storage & paths

| Variable | Default | Purpose |
|---|---|---|
| `SHELF_DATA` | `./shelf-data` | Host path bind-mounted to `/data` (docker-compose interpolation only). |
| `DATA_DIR` | `/data` | In-container data root; all subdirectories and `shelf.db` live here. |
| `SHELF_PORT` | `8420` | Host port published for the UI/API; container always listens on 8420. |
| `PROXY_NETWORK` | `proxy` | External reverse-proxy network to join (docker-compose interpolation only). |
| `CATALOG_PATH` | `/app/catalog/GAMES.json` | Location of the Shop catalog; falls back to `<DATA_DIR>/catalog/GAMES.json`. |
| `PKGTOOL` | `/opt/pkgtool/PkgTool.Core` | Path to LibOrbisPkg's `PkgTool.Core` binary used to build/validate fake PKGs. |

### Console defaults

These seed the Settings form; changes in the UI persist in SQLite.

| Variable | Default | Purpose |
|---|---|---|
| `PS4_HOST` | *(empty)* | Console LAN IP. Required before anything can be sent. |
| `PS4_FTP_PORT` | `2121` | Homebrew FTP server port. |
| `PS4_FTP_DIR` | `/data/pkg` | Destination folder **on the console** for FTP uploads. |
| `PS4_FTP_USER` | `anonymous` | FTP username. |
| `PS4_FTP_PASS` | *(empty)* | FTP password (stored in plaintext; see Security). |
| `PS4_RPI_PORT` | `12800` | Remote Package Installer port. |
| `PUBLIC_URL` | *(empty)* | This server's address as the console sees it, e.g. `http://192.168.1.10:8420`. Required for RPI and FPKGi. |
| `AUTO_SEND` | `none` | What to do after a new PKG lands: `none`, `ftp`, `rpi` or `auto`. |
| `PAUSE_DOWNLOAD_WHILE_SENDING` | `1` | Hold the fetch worker while a `send_*` job runs (shared WiFi). Editable in the UI. |
| `NOTIFY_WEBHOOK` | *(empty)* | Optional http(s) webhook POSTed (`{"text","content"}`) when a job reaches `done`/`failed`. Editable in the UI. |

### Downloads & Internet Archive

| Variable | Default | Purpose |
|---|---|---|
| `ARCHIVE_USER` | *(empty)* | Internet Archive account email; needed for authenticated archive.org downloads. |
| `ARCHIVE_PASS` | *(empty)* | Internet Archive password. Quote values containing `#` or spaces. |
| `ARIA2_CONN` | `8` | Max aria2 connections per server (`--max-connection-per-server`). |
| `MIN_DOWNLOAD_RATE` | `100K` | aria2 `--lowest-speed-limit`; drop connections slower than this. `K`/`M` suffixes allowed. |
| `STALL_SECONDS` | `180` | Fail a download that allocates no new bytes for this many seconds (dead-node watchdog). |
| `FINISH_NEAR_COMPLETE_PCT` | `95` | A transfer at least this complete ignores the speed/stall guard and is allowed to finish. |
| `VERIFY_CHECKSUM` | `1` | Verify archive.org files against the item metadata's `md5`/`sha1`/`crc32`. |
| `MAX_RETRIES` | `3` | Transient download failures retried with 60/300/900 s backoff before giving up. |
| `ALLOWED_DOWNLOAD_HOSTS` | `archive.org,.archive.org` | Comma-separated allow-list for `POST /api/download`. A leading dot also matches subdomains. |
| `ALLOW_ANY_DOWNLOAD` | `0` | When truthy (`1/true/yes/on`), accept any http(s) host (disables the SSRF guard). |

### Safety limits

| Variable | Default | Purpose |
|---|---|---|
| `MAX_UPLOAD_GB` | `200` | Cap for a single browser upload; larger returns HTTP 413. |
| `EXTRACT_MAX_GB` | `300` | Reject an archive extraction larger than this and delete it (decompression-bomb guard). |

---

## Security

- **No authentication by design.** Every endpoint is open. Keep Shelf on your LAN or Tailscale and
  never port-forward it. There is no user model, session, or CSRF protection.
- **Download host allow-list (SSRF guard).** `POST /api/download` only accepts hosts matching
  `ALLOWED_DOWNLOAD_HOSTS` (matched on the parsed hostname, so `archive.org.evil.test` and
  `archive.org@example.com` do not pass). `ALLOW_ANY_DOWNLOAD=1` disables the guard and lets the
  server fetch arbitrary URLs, including internal addresses — use with care. `/api/shop/install` is
  unaffected because it only accepts URLs already present in the catalog.
- **Archive credentials.** Loaded from the environment, used for a per-download session, and never
  stored in the database or returned by the API. Cookies are written to a temporary Netscape cookie
  file inside the job's work dir for aria2 and deleted in a `finally`; they never appear on the
  command line. `.env` is plaintext (dotenv does not encrypt) — keep it `chmod 600` and out of source
  control.
- **Plaintext FTP password.** `ftp_pass` is stored in SQLite and travels to the console in cleartext
  over FTP. Use it only on a trusted LAN; prefer `anonymous` where the server allows it.
- **Upload / extraction caps.** Uploads are capped at `MAX_UPLOAD_GB`; extractions are capped at
  `EXTRACT_MAX_GB` to blunt decompression bombs. Filenames are reduced to a basename and dotfiles are
  rejected.
- **PKG parser scope.** `pkg.py` reads only the header, `param.sfo` and icon entry, and refuses
  truncated files; it is not a full validation of PKG contents.

---

## Troubleshooting

**Internet Archive returns 500s, throttles, or serves from a dead node.**
Archive.org load-balances across storage nodes; some are very slow or stop sending. The stall guard
(`MIN_DOWNLOAD_RATE` + `STALL_SECONDS`) terminates a dead node instead of hanging forever, and
`download_aria2` then rotates to the item's next storage server before giving up. A failure that is
transient requeues the job automatically with 60/300/900 s backoff (`MAX_RETRIES`), resuming from the
`.part`/`.aria2` files; non-transient errors fail immediately. Multiple connection failures retry once
with a single connection. A file that is at least `FINISH_NEAR_COMPLETE_PCT` complete is exempt from
the guards so a slow tail can finish. If a finished file fails `VERIFY_CHECKSUM`, the corrupt file is
kept for inspection and the job is retried.

**RPI accepts TCP then closes with no bytes / job times out.**
A previously-running RPI payload can wedge and accept connections without answering. `probe_http`
requires a real HTTP reply precisely to catch this, so `send_auto` falls back to FTP. For a
`send_rpi` job, restart the Remote Package Installer payload on the console and retry.

**FTP upload resumes at the wrong size or restarts every time.**
GoldHEN's `SIZE` is 32-bit and wraps above 4 GiB. Shelf reads the remote size via `MLST` (64-bit) and
only falls back to `SIZE`. If the FTP server rejects a large `REST` with `451`, Shelf restarts the
upload from 0 and deletes the partial — expected on some builds.

**HTTP 507 "not enough free space".**
`convert.need_space` requires `size * 1.05 + 512 MiB` free. Free space on `SHELF_DATA`, or delete work
directories and library items from the Storage tab. `/api/download` and `/api/shop/install` also count
the size of other queued/running catalog downloads.

**A large conversion dies or the container is killed.**
The `.NET` `PkgTool` build is memory-hungry. The compose file sets `mem_limit: 2g` / `cpus: 1.5`; raise
them for large dump rebuilds, or ingest an already-built PKG instead.

**Nothing appears after dropping a file in `inbox/`.**
The watcher waits until the entry stops changing for one 20 s interval, and ignores names ending in
`.part`, `.crdownload`, `.tmp`, `.aria2` and dotfiles. Check `docker logs -f shelf` for the ingest job.

**A file could not be converted.**
It is moved to `failed/<name>/`. Inspect it there. The job's `message` names the reason (bad magic,
truncated PKG, non-`gd` dump, PkgTool failure, extraction over the limit). `failed/` entries appear in
the Storage tab with a **Failed** badge and are deleted only when you explicitly select them; **Clean
all orphans** leaves them alone.

**Console offline.**
The header light is driven by the PS4 probe every 10 s. Check the console IP, that the console is on,
and that either GoldHEN's FTP server or RPI is running. Settings shows each service separately.

---

## Testing

The tests use a temporary `DATA_DIR` and mock all network access, so they never touch the production
database or the Internet. They are shipped in the image under `/app/tests`.

In the running container:

```bash
docker exec shelf python -m unittest discover -s /app/tests
# verbose
docker exec shelf python -m unittest discover -s /app/tests -v
```

On the host, against disposable data:

```bash
check_dir=$(mktemp -d /tmp/shelf-tests-XXXXXX)
DATA_DIR="$check_dir" PYTHONPATH=app python -m unittest discover -s app/tests -v
```

Coverage: catalog parsing, mtime-cache reload and duplicate flagging (`test_catalog.py`); Archive
login, host detection, cookie scoping, streamed download, Range resume, checksum verification and
server candidates (`test_downloads.py`); terminal-state / work-dir lifecycle, retry classification,
backoff and cancel-preserves-partial (`test_worker_lifecycle.py`); and console-delete path safety,
config-import validation, DB backup and the catalog `on_console` merge (`test_console_config.py`).

---

## Limitations / not implemented

- **No authentication, single user.** Anyone who can reach the port can read, change settings, download
  and install.
- **One job at a time per worker.** A single fetch thread and a single send thread mean long transfers
  block everything behind them.
- **RPI is fragile.** The payload can wedge, only handles one task at a time, and depends on the console
  reaching `PUBLIC_URL`.
- **Archive.org reliability** is outside our control; dead nodes are handled by the stall guard, not
  eliminated.
- **Dump rebuild is base-game only.** Patch/DLC dumps (`CATEGORY gp`/`ac`) are rejected with a clear
  error; only `gd` folders are rebuilt.
- **`failed/` entries** are shown in the Storage tab but never removed by **Clean all orphans**; delete
  them explicitly so an unconvertible input is not lost by accident.
- **No catalog bundled.** The Shop is empty until you supply and mount `GAMES.json`.
- **No transcoding, no updates patching, no saves/screenshots.** This is an install shelf, nothing more.
- **External cover art and fonts** are loaded by the browser from third-party hosts; the UI needs
  internet access for those to render.

---

## Project layout

```
app/
├── Dockerfile              .NET 8 runtime + Python venv; builds PkgTool from LibOrbisPkg, installs unar/aria2
├── requirements.txt        fastapi, uvicorn, requests, python-dotenv
├── shelf/
│   ├── __init__.py
│   ├── config.py           env vars, data paths, DEFAULTS, size limits, .env loading
│   ├── db.py               SQLite schema/migrations, q/x/settings, job lifecycle helpers
│   ├── pkg.py              minimal PS4 PKG + param.sfo reader (magic, metadata, icon)
│   ├── convert.py          Converter: validate, split-merge, unar extract, dump→GP4→PkgTool
│   ├── downloads.py        Archive login/session, cookie file, host allow-list, aria2 detection
│   ├── delivery.py         FTP upload, RPI install, auto fallback, probes, console inventory/delete, pkg URL
│   ├── notify.py           best-effort terminal-job webhook POSTs
│   ├── worker.py           inbox watcher, fetch/convert worker, send worker, aria2 runner, stall guard
│   ├── catalog.py          GAMES.json loader with mtime cache and duplicate flagging
│   ├── main.py             FastAPI app: routes, settings, storage, console, config/backup, FPKGi, PS4 probe
│   └── static/index.html   single-file web UI (library, shop, storage, settings drawer)
└── tests/
    ├── test_catalog.py          catalog parsing / reload / duplicates
    ├── test_console_config.py   console delete safety, config import, DB backup, on_console merge
    ├── test_downloads.py        Archive auth, host detection, download + resume
    └── test_worker_lifecycle.py terminal-state and work-dir behaviour
docker-compose.yml          service, volumes, ports, resource limits, optional proxy network
catalog/GAMES.json          optional operator-supplied Shop catalog (gitignored; not bundled)
.env.example                documented environment template
```

State lives in SQLite at `<DATA_DIR>/shelf.db` (WAL mode): tables `items`, `jobs`, `settings`.
