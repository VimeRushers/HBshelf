"""Minimal PS4 PKG / param.sfo reader (header, metadata and icon only)."""
import struct
from dataclasses import dataclass, field

PKG_MAGIC = b"\x7fCNT"
ENTRY_PARAM_SFO = 0x1000
ENTRY_ICON0_PNG = 0x1200

CONTENT_TYPES = {0x1A: "game", 0x1B: "addon", 0x1C: "addon", 0x1E: "theme"}
REGIONS = {"UP": "USA", "EP": "EUR", "JP": "JAP", "HP": "ASIA", "KP": "ASIA", "IP": "INT"}


class PkgError(Exception):
    pass


@dataclass
class PkgInfo:
    content_id: str
    content_type: int
    drm_type: int
    size: int
    sfo: dict = field(default_factory=dict)
    icon: bytes | None = None

    @property
    def title_id(self) -> str:
        return self.sfo.get("TITLE_ID") or self.content_id[7:16]

    @property
    def title(self) -> str:
        return self.sfo.get("TITLE") or self.content_id

    @property
    def version(self) -> str:
        return self.sfo.get("APP_VER") or self.sfo.get("VERSION") or "01.00"

    @property
    def category(self) -> str:
        """gd = game, gp = patch, ac = add-on content; homebrew uses a non-CUSA/PLAS id."""
        cat = self.sfo.get("CATEGORY", "")
        if cat.startswith("gp"):
            return "update"
        if cat == "ac" or CONTENT_TYPES.get(self.content_type) == "addon":
            return "dlc"
        if cat == "gd" and not self.title_id.startswith(("CUSA", "PLAS", "PCAS", "PCJS")):
            return "homebrew"
        if cat == "gd":
            return "game"
        return cat or "other"

    @property
    def min_fw(self) -> str:
        ver = self.sfo.get("SYSTEM_VER")
        if not isinstance(ver, int) or ver == 0:
            return ""
        return f"{ver >> 24:x}.{(ver >> 16) & 0xFF:02x}"

    @property
    def region(self) -> str:
        return REGIONS.get(self.content_id[:2], "")


def parse_sfo(data: bytes) -> dict:
    if data[:4] != b"\x00PSF":
        raise PkgError("bad param.sfo magic")
    try:
        key_tab, data_tab, count = struct.unpack_from("<III", data, 8)
        out = {}
        for i in range(count):
            k_off, fmt, length, _max, d_off = struct.unpack_from("<HHIII", data, 0x14 + i * 16)
            key = data[key_tab + k_off:data.index(b"\0", key_tab + k_off)].decode()
            raw = data[data_tab + d_off:data_tab + d_off + length]
            if fmt == 0x0404:
                out[key] = struct.unpack("<I", raw[:4])[0]
            else:
                out[key] = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
    except (ValueError, struct.error) as e:
        raise PkgError(f"malformed param.sfo: {e}") from e
    return out


def read_pkg(path: str) -> PkgInfo:
    with open(path, "rb") as f:
        head = f.read(0x80)
        if len(head) < 0x80 or head[:4] != PKG_MAGIC:
            raise PkgError("not a PS4 PKG (bad magic)")
        entry_count, = struct.unpack_from(">I", head, 0x10)
        table_off, = struct.unpack_from(">I", head, 0x18)
        content_id = head[0x40:0x64].decode("ascii", "replace").rstrip("\0")
        drm_type, content_type = struct.unpack_from(">II", head, 0x70)
        f.seek(0, 2)
        size = f.tell()
        f.seek(table_off)
        table = f.read(entry_count * 32)
        entries = {}
        for i in range(entry_count):
            eid, _name, flags1, _f2, off, sz = struct.unpack_from(">IIIIII", table, i * 32)
            entries[eid] = (off, sz, bool(flags1 & 0x80000000))

        def entry(eid):
            if eid not in entries or entries[eid][2]:
                return None
            off, sz, _ = entries[eid]
            if off + sz > size:
                raise PkgError("PKG is truncated (entry past end of file)")
            f.seek(off)
            return f.read(sz)

        sfo_raw = entry(ENTRY_PARAM_SFO)
        info = PkgInfo(content_id, content_type, drm_type, size,
                       parse_sfo(sfo_raw) if sfo_raw else {}, entry(ENTRY_ICON0_PNG))
    body_off, = struct.unpack_from(">Q", head, 0x20)
    if size < body_off:
        raise PkgError("PKG is truncated")
    return info


def is_pkg(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == PKG_MAGIC
    except OSError:
        return False


def read_sfo_file(path: str) -> dict:
    with open(path, "rb") as f:
        return parse_sfo(f.read())
