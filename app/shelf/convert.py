"""Turn whatever lands in the inbox into installable PKGs in the library.

Handled inputs (recursively, so an archive of split parts works too):
  * *.pkg                        -> validated, metadata/icon read, moved to the library
  * split PKGs                   -> foo.pkg.part0.., foo.pkg.000.., foo_0.pkg.. merged
  * .zip / .7z / .rar / .tar.*   -> extracted with unar
  * dump folder (sce_sys/param.sfo at its root) -> fake PKG built with LibOrbisPkg's PkgTool
"""
import datetime
import os
import re
import shutil
import subprocess
from pathlib import Path
from xml.sax.saxutils import quoteattr

from . import db
from .config import EXTRACT_MAX_BYTES, EXTRACT_MAX_GB, ICONS, LIBRARY, PKGTOOL
from .pkg import PkgError, is_pkg, read_pkg, read_sfo_file

ARCHIVE_EXT = (".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")
SPLIT_PATTERNS = [
    re.compile(r"^(?P<base>.+\.pkg)\.part(?P<n>\d+)$", re.I),
    re.compile(r"^(?P<base>.+\.pkg)\.(?P<n>\d{3})$", re.I),
    re.compile(r"^(?P<base>.+)_(?P<n>\d+)\.pkg$", re.I),
]
# sce_sys files PkgTool generates itself; dumpers often include them, and they must not be packed twice.
GENERATED_SYS = {
    "license.dat", "license.info", "playgo-chunk.dat", "playgo-chunk.sha", "playgo-manifest.xml",
    "psreserved.dat", ".digests", ".entry_keys", ".image_key", ".general_digests", ".metas", ".entry_names",
}


class ConvertError(Exception):
    pass


def split_key(p: Path):
    for pat in SPLIT_PATTERNS:
        m = pat.match(p.name)
        if m:
            base = m["base"] if m["base"].lower().endswith(".pkg") else m["base"] + ".pkg"
            return base, int(m["n"])
    return None


def group_entries(paths):
    """Return (split_groups {base_name: [paths in order]}, singles [paths])."""
    groups, singles = {}, []
    for p in paths:
        k = p.is_file() and split_key(p)
        if k:
            groups.setdefault(k[0], []).append((k[1], p))
        else:
            singles.append(p)
    out = {}
    for base, parts in groups.items():
        parts.sort()
        files = [p for _, p in parts]
        # "foo_1.pkg" alone, or a set whose first part isn't a PKG header, isn't a split set.
        if len(files) < 2 or not is_pkg(str(files[0])) or any(is_pkg(str(f)) for f in files[1:]):
            singles.extend(files)
        else:
            out[base] = files
    return out, singles


def free_bytes(path: Path) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def need_space(path: Path, size: int):
    if free_bytes(path) < size * 1.05 + 512 * 2**20:
        raise ConvertError(f"not enough free space in {path} (need ~{size / 2**30:.1f} GiB)")


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


class Converter:
    def __init__(self, report):
        self.report = report          # report(progress 0..1 or None, message)
        self.added: list[int] = []
        self.skipped: list[str] = []

    def process(self, path: Path):
        if path.is_dir():
            if (path / "sce_sys" / "param.sfo").is_file():
                return self.add_to_library(self.build_from_dump(path))
            groups, singles = group_entries(sorted(path.iterdir()))
            for base, parts in groups.items():
                self.process(self.merge(base, parts))
            for p in singles:
                self.process(p)
        elif path.name.lower().endswith(ARCHIVE_EXT):
            self.process(self.extract(path))
        elif is_pkg(str(path)):
            self.add_to_library(path)
        elif path.name.startswith(".") or path.name.endswith(".aria2"):
            pass
        else:
            self.skipped.append(path.name)

    def extract(self, archive: Path) -> Path:
        dest = archive.with_name(archive.name + ".extracted")
        need_space(archive.parent, archive.stat().st_size * 2)
        self.report(None, f"extracting {archive.name}")
        r = subprocess.run(["unar", "-q", "-f", "-D", "-o", str(dest), str(archive)], capture_output=True, text=True)
        if r.returncode != 0:
            raise ConvertError(f"extract {archive.name} failed: {(r.stderr or r.stdout).strip()[-300:]}")
        total = dir_size(dest)
        if total > EXTRACT_MAX_BYTES:
            # Decompression-bomb guard: reject an extraction larger than the configured ceiling.
            shutil.rmtree(dest, ignore_errors=True)
            raise ConvertError(f"extract {archive.name} produced {total / 2**30:.1f} GiB, over the {EXTRACT_MAX_GB:g} GiB extraction limit")
        archive.unlink()
        return dest

    def merge(self, base: str, parts: list[Path]) -> Path:
        total = sum(p.stat().st_size for p in parts)
        need_space(parts[0].parent, total)
        out = parts[0].with_name(base)
        done = 0
        self.report(0, f"merging {len(parts)} parts -> {base}")
        with open(out, "wb") as dst:
            for p in parts:
                with open(p, "rb") as src:
                    while chunk := src.read(8 * 2**20):
                        dst.write(chunk)
                        done += len(chunk)
                        self.report(done / total, f"merging {p.name}")
                p.unlink()
        return out

    def build_from_dump(self, folder: Path) -> Path:
        sfo = read_sfo_file(str(folder / "sce_sys" / "param.sfo"))
        content_id = sfo.get("CONTENT_ID", "")
        category = sfo.get("CATEGORY", "")
        if len(content_id) != 36:
            raise ConvertError(f"{folder.name}: param.sfo has no valid CONTENT_ID")
        if category != "gd":
            raise ConvertError(f"{folder.name}: only base-game/app dumps (CATEGORY gd) can be rebuilt, got '{category}'")
        need_space(folder.parent, dir_size(folder))
        self.report(None, f"building fake PKG for {sfo.get('TITLE', content_id)} ({content_id})")
        gp4 = folder / ".shelf.gp4"
        gp4.write_text(make_gp4(folder, content_id), encoding="utf-8")
        outdir = folder.with_name(folder.name + ".built")
        outdir.mkdir(exist_ok=True)
        r = subprocess.run([PKGTOOL, "pkg_build", str(gp4), str(outdir)], capture_output=True, text=True)
        built = outdir / f"{content_id}.pkg"
        if r.returncode != 0 or not built.is_file():
            raise ConvertError(f"PkgTool failed: {(r.stderr or r.stdout).strip()[-400:]}")
        v = subprocess.run([PKGTOOL, "pkg_validate", str(built)], capture_output=True, text=True)
        if "[FAIL]" in v.stdout or v.returncode != 0:
            raise ConvertError(f"built PKG failed validation: {v.stdout.strip()[-400:]}")
        shutil.rmtree(folder)
        return built

    def add_to_library(self, pkg_path: Path) -> int:
        try:
            info = read_pkg(str(pkg_path))
        except PkgError as e:
            raise ConvertError(f"{pkg_path.name}: {e}") from e
        dest_dir = LIBRARY / re.sub(r"[^\w-]", "_", info.title_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{info.content_id}.pkg"
        self.report(None, f"adding {info.title} to library")
        shutil.move(str(pkg_path), dest)
        if info.icon:
            (ICONS / f"{info.content_id}.png").write_bytes(info.icon)
        db.x("""INSERT INTO items(content_id, title_id, title, version, category, region, min_fw, size, path, has_icon, added)
                VALUES(?,?,?,?,?,?,?,?,?,?,strftime('%s','now'))
                ON CONFLICT(content_id) DO UPDATE SET title_id=excluded.title_id, title=excluded.title,
                  version=excluded.version, category=excluded.category, region=excluded.region, min_fw=excluded.min_fw,
                  size=excluded.size, path=excluded.path, has_icon=excluded.has_icon, added=excluded.added""",
             info.content_id, info.title_id, info.title, info.version, info.category, info.region,
             info.min_fw, info.size, str(dest), int(bool(info.icon)))
        item_id = db.q("SELECT id FROM items WHERE content_id=?", info.content_id)[0]["id"]
        self.added.append(item_id)
        return item_id


def make_gp4(folder: Path, content_id: str) -> str:
    """GP4 project listing every file in a dump folder, same shape PkgTool's pkg_makegp4 emits."""
    files, dirs = [], {}
    for p in sorted(folder.rglob("*")):
        rel = p.relative_to(folder).as_posix()
        if p.is_dir():
            node = dirs
            for part in rel.split("/"):
                node = node.setdefault(part, {})
            continue
        if rel == ".shelf.gp4" or (rel.startswith("sce_sys/") and rel[8:] in GENERATED_SYS):
            continue
        files.append(f'    <file targ_path={quoteattr(rel)} orig_path={quoteattr(rel)} />')

    def render(node, depth):
        out = []
        for name, children in node.items():
            pad = "  " * depth
            if children:
                out += [f"{pad}<dir targ_name={quoteattr(name)}>", *render(children, depth + 1), f"{pad}</dir>"]
            else:
                out.append(f"{pad}<dir targ_name={quoteattr(name)} />")
        return out

    now = datetime.datetime.now(datetime.UTC)
    return "\n".join([
        '<?xml version="1.0" encoding="utf-8"?>',
        '<psproject fmt="gp4" version="1000">',
        "  <volume>",
        "    <volume_type>pkg_ps4_app</volume_type>",
        f"    <volume_ts>{now:%Y-%m-%d %H:%M:%S}</volume_ts>",
        f'    <package content_id="{content_id}" passcode="{"0" * 32}" storage_type="digital50" app_type="full" c_date="{now:%Y-%m-%d}" />',
        '    <chunk_info chunk_count="1" scenario_count="1">',
        '      <chunks><chunk id="0" layer_no="0" label="Chunk #0" /></chunks>',
        '      <scenarios default_id="0"><scenario id="0" type="sp" initial_chunk_count="1" label="Scenario #0">0</scenario></scenarios>',
        "    </chunk_info>",
        "  </volume>",
        '  <files img_no="0">', *files, "  </files>",
        "  <rootdir>", *render(dirs, 2), "  </rootdir>",
        "</psproject>",
    ])
