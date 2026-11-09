"""
Satisfactory save tools - standard library only.

  - read header info, decompress / recompress the chunked body
  - body parser for the 1.2 layout (save version >= 53: TOC + data blobs, complete property tags)
  - inventory reader / editor (amounts + items), size fields patched, result re-verified
  - resource node reader (type, purity, claimed) taken from the save itself
  - save backups (timestamped copies, pruned)
  - map data: robust actor scan, layer building, pure-Python PNG renderer

Format reference: moritz-h/satisfactory-3d-map docs/SATISFACTORY_SAVE.md (Satisfactory 1.2, save
version 60). Every step validates and raises SaveError instead of guessing.

Self-check: python satsave.py --selftest
"""
import datetime
import glob
import json
import math
import os
import re
import shutil
import struct
import time
import zlib

MAGIC = b"\xC1\x83\x2A\x9E\x22\x22\x22\x22"
CHUNK_SIZE = 131072
CHUNK_HDR = 49
MAP_BOUNDS = (-324698.832031, 425301.832031, -375000.0, 375000.0)  # x0, x1, y0, y1 in cm


class SaveError(Exception):
    pass


# ----------------------------------------------------------------- reading
class Reader:
    __slots__ = ("b", "p")

    def __init__(self, b, p=0):
        self.b, self.p = b, p

    def _u(self, fmt, n):
        try:
            v = struct.unpack_from(fmt, self.b, self.p)[0]
        except struct.error:
            raise SaveError("save data ends unexpectedly")
        self.p += n
        return v

    def u32(self):
        return self._u("<I", 4)

    def u64(self):
        return self._u("<Q", 8)

    def u8(self):
        return self._u("<B", 1)

    def skip(self, n):
        self.p += n
        if self.p > len(self.b):
            raise SaveError("save data ends unexpectedly")

    def string(self):
        n = self._u("<i", 4)
        if n == 0:
            return ""
        if abs(n) > 1_000_000:
            raise SaveError(f"implausible string length {n} at byte {self.p - 4}")
        if n > 0:
            raw, enc, size = self.b[self.p:self.p + n - 1], "utf-8", n
        else:
            raw, enc, size = self.b[self.p:self.p + 2 * (-n - 1)], "utf-16-le", -2 * n
        self.p += size
        if self.p > len(self.b):
            raise SaveError("save data ends unexpectedly")
        return raw.decode(enc, "replace")


def pack_string(s):
    if not s:
        return struct.pack("<i", 0)
    raw = s.encode("utf-8") + b"\0"
    return struct.pack("<i", len(raw)) + raw


def read_header_info(data):
    """Best-effort header summary for display; never raises."""
    info = {}
    try:
        r = Reader(data)
        info["header_version"], info["save_version"], info["build"] = r.u32(), r.u32(), r.u32()
        if info["header_version"] >= 14:
            info["save_name"] = r.string()
        info["map"] = r.string()
        r.string()  # map options
        info["session"] = r.string()
        info["played_s"] = r.u32()
        ticks = r.u64()
        info["saved"] = (datetime.datetime(1, 1, 1) + datetime.timedelta(microseconds=ticks // 10)).strftime("%Y-%m-%d %H:%M")
    except (SaveError, OverflowError, ValueError):
        pass
    return info


def read_chunks(data):
    """-> (header_len, chunk_template, body). The first chunk is located by its magic bytes."""
    start = data.find(MAGIC)
    if start < 0:
        raise SaveError("no compressed body found - not a Satisfactory save?")
    tmpl, out, p = None, [], start
    while p + CHUNK_HDR <= len(data):
        if data[p:p + 8] != MAGIC:
            raise SaveError(f"unexpected chunk header at byte {p}")
        csize, usize, csize2, usize2 = struct.unpack_from("<QQQQ", data, p + 17)
        if csize != csize2 or usize != usize2:
            raise SaveError(f"inconsistent chunk sizes at byte {p}")
        if tmpl is None:
            tmpl = data[p + 8:p + 17]
        try:
            raw = zlib.decompress(data[p + CHUNK_HDR:p + CHUNK_HDR + csize])
        except zlib.error as e:
            raise SaveError(f"chunk at byte {p} failed to decompress: {e}")
        if len(raw) != usize:
            raise SaveError(f"chunk at byte {p} has the wrong size")
        out.append(raw)
        p += CHUNK_HDR + csize
    return start, tmpl, b"".join(out)


def build_file(head, tmpl, body):
    """Inverse of read_chunks: original header bytes + recompressed body."""
    out = [head]
    for i in range(0, len(body), CHUNK_SIZE):
        raw = body[i:i + CHUNK_SIZE]
        z = zlib.compress(raw, 6)
        out.append(MAGIC + tmpl + struct.pack("<QQQQ", len(z), len(raw), len(z), len(raw)) + z)
    return b"".join(out)


# ------------------------------------------------------------ body parsing
class Level:
    def __init__(self):
        self.headers = []   # (is_actor, type_path, instance, parent_actor, position)
        self.objs = []      # (data_len_offset, data_offset, size, VersionUE5)
        self.blob_len_off = 0


def _version_data(r):
    """FSaveObjectVersionData -> FileVersionUE5."""
    r.skip(8)  # SaveObjectVersionDataVersion, FileVersionUE4
    ue5 = r.u32()
    r.skip(14)  # licensee version, engine major/minor/patch, changelist
    r.string()
    r.skip(20 * r.u32())
    return ue5


def _blob(r):
    """TArray<uint8, int64> -> (length_field_offset, data_offset, length)."""
    len_off = r.p
    n = r.u64()
    off = r.p
    r.skip(n)
    return len_off, off, n


def _header(r, sv):
    kind = r.u32()
    if kind > 1:
        raise SaveError(f"unknown object kind {kind} at byte {r.p - 4}")
    typ = r.string()
    r.string()
    name = r.string()
    if sv >= 49:
        r.u32()  # object flags
    if kind:
        r.u32()  # need transform
        pos = struct.unpack_from("<3f", r.b, r.p + 16) if r.p + 40 <= len(r.b) else None
        r.skip(40)  # rotation(4f) translation(3f) scale(3f)
        r.u32()  # was placed in level
        return (True, typ, name, None, pos)
    return (False, typ, name, r.string(), None)


def _fill_level(body, lvl, toc, data, sv, level_ue5):
    r = Reader(body, toc[1])
    n = r.u32()
    for _ in range(n):
        lvl.headers.append(_header(r, sv))
    if r.p > toc[1] + toc[2]:
        raise SaveError("object table runs past its block")
    d = Reader(body, data[1])
    if d.u32() != n:
        raise SaveError("object table and object data disagree on the object count")
    for _ in range(n):
        ver = d.u32()
        d.u32()  # migrate-refs flag
        len_off = d.p
        size = d.u32()
        off = d.p
        d.skip(size)
        ue5 = level_ue5
        if ver >= 53 and d.u32():
            ue5 = _version_data(d)
        lvl.objs.append((len_off, off, size, ue5))
    if d.p != data[1] + data[2]:
        raise SaveError("object data does not line up with its block")


def parse_save(body, sv):
    """Walk all levels of a 1.2-style body (`sv` = SaveVersion from the file header)."""
    if sv < 53:
        raise SaveError(f"save version {sv} is older than this editor supports (it needs a 1.1+ save)")
    r = Reader(body)
    if r.u64() + 8 != len(body):
        raise SaveError("body size field does not match the data")
    base_ue5 = _version_data(r)
    for _ in range(r.u32()):  # world partition validation grids
        r.string()
        r.skip(8)
        for _ in range(r.u32()):
            r.string()
            r.skip(4)
    pending = []
    for _ in range(r.u32()):  # per streaming level
        r.string()
        toc, data = _blob(r), _blob(r)
        if sv >= 51:
            r.u32()
        for _ in range(r.u32()):
            r.string(), r.string()
        ue5 = base_ue5
        if sv >= 53 and r.u32():
            ue5 = _version_data(r)
        pending.append((toc, data, ue5))
    pending.append((_blob(r), _blob(r), base_ue5))  # persistent + runtime
    for _ in range(r.u32()):
        r.string()
        for _ in range(r.u32()):
            r.string(), r.string()
    for _ in range(r.u32()):
        r.string(), r.string()
    if r.p != len(body):
        raise SaveError("data left over after the last level (save layout differs from the documented one)")
    levels = []
    for toc, data, ue5 in pending:
        lvl = Level()
        lvl.blob_len_off = data[0]
        _fill_level(body, lvl, toc, data, sv, ue5)
        levels.append(lvl)
    return levels


# --------------------------------------------------------------- properties
def _tree(r, depth=0):
    if depth > 8:
        raise SaveError("property type nested too deep")
    name, count = r.string(), r.u32()
    if count > 8:
        raise SaveError("bad property type")
    return (name, [_tree(r, depth + 1) for _ in range(count)])


def _tag(r):
    """FPropertyTag (VersionUE5 >= 1012) -> (name, type_tree, size_field_offset, size, flags, payload_offset) or None."""
    name = r.string()
    if name == "None":
        return None
    tree = _tree(r)
    size_off = r.p
    size = r.u32()
    flags = r.u8()
    if flags & 1:
        r.u32()
    if flags & 2:
        r.skip(16)
    if flags & 4:
        raise SaveError("unsupported property flags")
    return name, tree, size_off, size, flags, r.p


def _object_start(r, ue5, actor):
    """Skip the class-specific prefix so `r` sits on the first property."""
    if ue5 < 1012:
        raise SaveError(f"object uses an older property format (engine version {ue5})")
    if actor:
        r.string(), r.string()
        for _ in range(r.u32()):
            r.string(), r.string()
    if ue5 >= 1011 and r.u8() != 0:
        raise SaveError("unsupported object serialization control")


# --------------------------------------------------------------- inventory
def _read_stack(r):
    st = {"item": "", "level": "", "num": 0, "item_ok": False}
    while True:
        h = _tag(r)
        if h is None:
            return st
        name, tree, size_off, psize, _flags, pay = h
        if name == "Item" and tree[0] == "StructProperty":
            r.p = pay
            level, path, has_state = r.string(), r.string(), r.u32()
            st.update(item=path, level=level, item_size_off=size_off, item_off=pay, item_len=psize,
                      item_ok=(has_state == 0 and r.p == pay + psize))
        elif name == "NumItems" and tree[0] == "IntProperty" and psize == 4:
            st["val_off"] = pay
            st["num"] = struct.unpack_from("<i", r.b, pay)[0]
        r.p = pay + psize


def _read_inventory(body, data_off, size, ue5):
    r = Reader(body, data_off)
    end = data_off + size
    _object_start(r, ue5, False)
    while r.p < end:
        h = _tag(r)
        if h is None:
            return None
        name, tree, size_off, psize, _flags, pay = h
        if name == "mInventoryStacks":
            if tree[0] != "ArrayProperty":
                raise SaveError("unexpected inventory layout")
            r.p = pay
            stacks = [_read_stack(r) for _ in range(r.u32())]
            if r.p != pay + psize:
                raise SaveError("inventory array size mismatch")
            return {"arr_size_off": size_off, "stacks": stacks}
        r.p = pay + psize
    return None


def player_inventories(body, levels, errors=None):
    """
    Every component owned by a player character that holds `mInventoryStacks` - any component class, so
    modded inventories (bigger backpacks, extra slot sets) are found too. Slot count is whatever the
    save holds. Unreadable ones are skipped; their reasons are appended to `errors` if given.
    """
    found = []
    for lvl in levels:
        for i, (actor, typ, name, parent, _pos) in enumerate(lvl.headers):
            if actor or not parent or "Char_Player" not in parent:
                continue
            len_off, data_off, size, ue5 = lvl.objs[i]
            try:
                inv = _read_inventory(body, data_off, size, ue5)
            except SaveError as e:
                if errors is not None and "Inventory" in typ + name:
                    errors.append(f"{name.rsplit('.', 1)[-1]}: {e}")
                inv = None
            if inv:
                inv.update(label=f"{parent.rsplit('.', 1)[-1]} / {name.rsplit('.', 1)[-1]} ({len(inv['stacks'])} slots)",
                           data_len_off=len_off, blob_len_off=lvl.blob_len_off)
                found.append(inv)
    return found


def apply_edits(body, levels, edits, sv):
    """
    edits: list of (inventory_index, slot, new_item_path_or_None, new_amount_or_None).
    Returns the new body. Re-parses and checks every edited slot before returning.
    """
    invs = player_inventories(body, levels)
    b = bytearray(body)
    splices = []
    for ii, slot, path, num in edits:
        inv = invs[ii]
        st = inv["stacks"][slot]
        if num is not None:
            if "val_off" not in st:
                raise SaveError("slot has no amount field")
            struct.pack_into("<i", b, st["val_off"], int(num))
        if path is not None and path != st["item"]:
            if not st["item_ok"]:
                raise SaveError(f"slot {slot + 1}: this item can't be changed (it carries extra data)")
            level = next((s["level"] for s in inv["stacks"] if s["item"]), "")
            splices.append((st["item_off"], inv, st, path, level))
    for off, inv, st, path, level in sorted(splices, key=lambda t: -t[0]):
        new = pack_string(level) + pack_string(path) + struct.pack("<I", 0)
        delta = len(new) - st["item_len"]
        b[off:off + st["item_len"]] = new
        for field, fmt in ((st["item_size_off"], "<I"), (inv["arr_size_off"], "<I"), (inv["data_len_off"], "<I"),
                           (inv["blob_len_off"], "<Q"), (0, "<Q")):
            struct.pack_into(fmt, b, field, struct.unpack_from(fmt, b, field)[0] + delta)
    out = bytes(b)
    # verify: whole body still parses and each edited slot reads back as intended
    inv2 = player_inventories(out, parse_save(out, sv))
    if len(inv2) != len(invs):
        raise SaveError("verification failed: inventory count changed")
    for ii, slot, path, num in edits:
        st = inv2[ii]["stacks"][slot]
        if (path is not None and st["item"] != path) or (num is not None and st["num"] != int(num)):
            raise SaveError(f"verification failed on slot {slot + 1}")
    return out


# ------------------------------------------------------------ resource nodes
NODE_KEYS = ("resourcenode", "frackingcore", "frackingsatellite", "geyser")
_PURITY_RE = re.compile(rb"(?i)(impure|normal|pure)")
_DESC_RE = re.compile(rb"Desc_[A-Za-z0-9_]+_C")


def _node_props(body, data_off, size, ue5):
    r = Reader(body, data_off)
    end = data_off + size
    _object_start(r, ue5, True)
    cls = purity = ""
    occ = None
    while r.p < end:
        h = _tag(r)
        if h is None:
            break
        name, tree, _so, psize, flags, pay = h
        raw = body[pay:pay + psize]
        low = name.lower()
        if tree[0] == "BoolProperty" and low == "misoccupied":
            occ = bool(flags & 0x10)
        elif "purity" in low:
            m = _PURITY_RE.search(raw)
            if m:
                purity = m.group(1).decode().lower()
            elif psize == 1 and raw[0] < 3:
                purity = ("impure", "normal", "pure")[raw[0]]
        elif not cls:
            m = _DESC_RE.search(raw)
            if m:
                cls = m.group().decode()
        r.p = pay + psize
    return cls, purity, occ


def read_nodes(body, levels):
    """[(resource_class, purity, x, y, claimed)] for every node / geyser / fracking actor in the save."""
    out = []
    for lvl in levels:
        for i, (actor, typ, _name, _parent, pos) in enumerate(lvl.headers):
            low = typ.lower()
            if not actor or pos is None or "deposit" in low or not any(k in low for k in NODE_KEYS):
                continue
            cls, purity, occ = "", "", None
            try:
                cls, purity, occ = _node_props(body, *lvl.objs[i][1:])
            except SaveError:
                pass
            if not cls and "geyser" in low:
                cls = "Geyser"
            out.append((cls, purity, pos[0], pos[1], occ))
    return out


def nodes_from_actors(actors):
    """Fallback when the strict parser can't read the save: node positions only (type unknown)."""
    return [("Geyser" if "geyser" in t.lower() else "", "", x, y, None) for t, x, y, _z in actors
            if "deposit" not in t.lower() and any(k in t.lower() for k in NODE_KEYS)]


def merge_nodes(save_nodes, json_nodes):
    """Prefer nodes from the save; fill unknown types from a JSON node list by position (within 5 m).
    With no save nodes at all, the JSON list is used as is."""
    if not save_nodes:
        return list(json_nodes)
    if not json_nodes or all(n[0] for n in save_nodes):
        return list(save_nodes)
    grid = {}
    for n in json_nodes:
        grid.setdefault((int(n[2] // 1000), int(n[3] // 1000)), []).append(n)
    out = []
    for cls, purity, x, y, occ in save_nodes:
        if not cls:
            gx, gy = int(x // 1000), int(y // 1000)
            near = [j for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j in grid.get((gx + dx, gy + dy), [])
                    if abs(j[2] - x) < 500 and abs(j[3] - y) < 500]
            if near:
                best = min(near, key=lambda j: (j[2] - x) ** 2 + (j[3] - y) ** 2)
                cls, purity = best[0], purity or best[1]
        out.append((cls, purity, x, y, occ))
    return out


_ITEM_RE = re.compile(rb"Desc_[A-Za-z0-9_]+\.Desc_[A-Za-z0-9_]+_C")


def item_paths(body):
    """Item descriptor paths that already appear in the save (guaranteed valid for this game version)."""
    seen, out = set(), {}
    for m in _ITEM_RE.finditer(body):
        name = m.group()
        if name in seen:
            continue
        seen.add(name)
        s, e = m.start(), m.end()
        for b0 in range(s, max(s - 300, 4), -1):
            if body[b0 - 4:b0] == struct.pack("<i", e - b0 + 1) and body[e:e + 1] == b"\0":
                path = body[b0:e].decode("ascii", "replace")
                if path.startswith("/") and "/Buildable/" not in path:
                    out[name.split(b".")[1].decode()] = path
                break
    return out


def friendly(class_name):
    n = re.sub(r"^Desc_|_C$", "", class_name)
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", n).replace("_", " ")


# ----------------------------------------------------------------- backups
def save_roots():
    base = os.environ.get("LOCALAPPDATA")
    return [os.path.join(base, "FactoryGame", "Saved", "SaveGames")] if base else []


def list_saves(roots):
    """[(path, mtime, size)] newest first; game backup folders are skipped."""
    out = []
    for root in roots:
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if "backup" not in d.lower()]
            for f in files:
                if f.lower().endswith(".sav"):
                    p = os.path.join(dirpath, f)
                    try:
                        st = os.stat(p)
                        out.append((p, st.st_mtime, st.st_size))
                    except OSError:
                        pass
    return sorted(out, key=lambda t: -t[1])


def backup_save(path, dest_root, tag="", keep=15):
    stem = os.path.splitext(os.path.basename(path))[0]
    d = os.path.join(dest_root, stem)
    os.makedirs(d, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst, n = os.path.join(d, f"{stem}__{stamp}{tag}.sav"), 1
    while os.path.exists(dst):
        n += 1
        dst = os.path.join(d, f"{stem}__{stamp}{tag}_{n}.sav")
    shutil.copy2(path, dst)
    files = sorted(glob.glob(os.path.join(d, "*.sav")))
    for group, cap in (([f for f in files if "__before" not in f], keep), ([f for f in files if "__before" in f], 30)):
        for old in group[:-cap] if len(group) > cap else []:
            os.remove(old)
    return dst


def list_backups(dest_root, path):
    stem = os.path.splitext(os.path.basename(path))[0]
    return sorted(glob.glob(os.path.join(dest_root, stem, "*.sav")), reverse=True)


def auto_backup(roots, dest_root, state, keep=15, min_age=15):
    """Copy every save that changed since the last pass. `state` {path: [mtime, size]} is updated in place."""
    made = []
    for path, mtime, size in list_saves(roots):
        if state.get(path) == [mtime, size] or time.time() - mtime < min_age:
            continue
        try:
            made.append(backup_save(path, dest_root, keep=keep))
            state[path] = [mtime, size]
        except OSError:
            pass
    return made


# --------------------------------------------------------------------- map
_ACTOR = re.compile(rb"\x01\x00\x00\x00([\x08-\xff])\x00\x00\x00/(?:Game|Script)/", re.S)


def scan_actors(body):
    """
    [(type_path, x, y, z)] for every actor header found by signature. Independent of the level
    layout, so it still works when the strict parser does not. Tries float32, then float64 positions.
    """
    best = []
    for fmt, pos_off, tail in (("<3f", 24, 52), ("<3d", 40, 92)):
        out, cands = [], 0
        for m in _ACTOR.finditer(body):
            cands += 1
            p = m.start() + 4
            try:
                r = Reader(body, p)
                typ, _root, inst = r.string(), r.string(), r.string()
                if ":" not in inst or not typ.startswith("/"):
                    continue
                x, y, z = struct.unpack_from(fmt, body, r.p + pos_off)
            except (SaveError, struct.error):
                continue
            if all(math.isfinite(v) and abs(v) < 2e6 for v in (x, y, z)):
                out.append((typ, x, y, z))
        if len(out) > len(best):
            best = out
        if len(best) >= max(10, cands // 2):
            break
    return best


LAYER_STYLE = {
    "Structures": ((70, 90, 115), 0), "Logistics": ((120, 120, 120), 0), "Other buildings": ((0, 200, 200), 1),
    "Storage": ((100, 160, 255), 2), "Production": ((80, 225, 120), 2), "Power": ((255, 80, 80), 2),
    "Extractors": ((255, 210, 0), 2), "Vehicles": ((255, 60, 255), 2), "Players": ((255, 255, 255), 4),
}
LAYER_ORDER = list(LAYER_STYLE)  # draw order, bottom to top


def categorize(typ):
    n = typ.lower()
    if "char_player" in n:
        return "Players"
    if "/vehicle" in n or any(k in n for k in ("truck", "tractor", "explorer", "factorycart", "golfcart", "locomotive", "freightwagon")):
        return "Vehicles"
    if "/buildable/" not in n:
        return None
    name = n.rsplit("/", 1)[-1]
    for cat, keys in (
        ("Logistics", ("conveyor", "pipeline", "pipe", "railroad", "wire", "hypertube", "powerline")),
        ("Extractors", ("miner", "oilpump", "frackingextractor", "waterpump", "resourcewell")),
        ("Power", ("generator", "powerstorage")),
        ("Production", ("constructor", "assembler", "manufacturer", "smelter", "foundry", "refinery", "blender",
                        "packager", "converter", "accelerator", "encoder")),
        ("Storage", ("storage",)),
        ("Structures", ("foundation", "wall", "ramp", "roof", "beam", "pillar", "stair", "floor", "ceiling", "fence", "quarter", "corner")),
    ):
        if any(k in name for k in keys):
            return cat
    return "Other buildings"


RES_COLORS = {
    "OreIron": (160, 160, 200), "OreCopper": (230, 130, 60), "Stone": (225, 225, 180), "Coal": (150, 110, 90),
    "OreGold": (255, 225, 60), "Sulfur": (240, 240, 0), "RawQuartz": (235, 150, 235), "OreBauxite": (205, 90, 70),
    "OreUranium": (60, 255, 60), "SAM": (160, 60, 230), "LiquidOil": (120, 120, 255), "Water": (60, 150, 255),
    "NitrogenGas": (150, 255, 255), "Geyser": (255, 150, 0),
}
PURITY_RADIUS = {"impure": 2, "normal": 3, "pure": 4}


def resource_label(cls):
    key = re.sub(r"^Desc_|_C$", "", cls)
    return {"OreIron": "Iron Ore", "OreCopper": "Copper Ore", "Stone": "Limestone", "OreGold": "Caterium Ore",
            "OreBauxite": "Bauxite", "OreUranium": "Uranium", "RawQuartz": "Raw Quartz", "LiquidOil": "Crude Oil",
            "NitrogenGas": "Nitrogen Gas"}.get(key, friendly(cls))


def load_nodes(path):
    """Resource nodes from a JSON file: [(resource_class, purity, x, y, None)]. Accepts the SatisfactoryTools
    world-data output and Ficsit Remote Monitoring radar-tower style lists (any nesting)."""
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    out = []

    def walk(o):
        if isinstance(o, dict):
            loc = o.get("location")
            if loc is not None and any(k in o for k in ("resource", "ClassName", "purity", "Purity")):
                try:
                    x, y = (loc["x"], loc["y"]) if isinstance(loc, dict) else (loc[0], loc[1])
                    out.append((str(o.get("resource") or o.get("ClassName") or ""),
                                str(o.get("purity") or o.get("Purity") or "").lower(), float(x), float(y), None))
                except (KeyError, IndexError, TypeError, ValueError):
                    pass
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return out


def _dim(rgb):
    return tuple(int(c * 0.5) for c in rgb)


def build_layers(actors, nodes=()):
    """
    {layer name: [(rgb, radius, points, purity, claimed), ...]} in draw order (Players last).
    Object layers have purity None. Ore layers carry purity ('impure'/'normal'/'pure'/'unknown') and
    claimed (True/False/None) so the UI can filter; claimed nodes are drawn dimmer.
    """
    by = {}
    for typ, x, y, _z in actors:
        c = categorize(typ)
        if c:
            by.setdefault(c, []).append((x, y))
    layers = {}
    for name in LAYER_ORDER[:-1]:
        if name in by:
            layers[name] = [(LAYER_STYLE[name][0], LAYER_STYLE[name][1], by[name], None, None)]
    ores = {}
    for cls, purity, x, y, occ in nodes:
        ores.setdefault(cls, {}).setdefault((purity or "unknown", occ), []).append((x, y))
    for cls, groups in sorted(ores.items(), key=lambda t: resource_label(t[0]) if t[0] else "~"):
        base = RES_COLORS.get(re.sub(r"^Desc_|_C$", "", cls), (200, 200, 200))
        layers["Ore: " + (resource_label(cls) if cls else "Unknown type")] = [
            (_dim(base) if occ else base, PURITY_RADIUS.get(p, 3), pts, p, occ)
            for (p, occ), pts in sorted(groups.items(), key=lambda t: (t[0][0], str(t[0][1])))]
    if "Players" in by:
        layers["Players"] = [(LAYER_STYLE["Players"][0], LAYER_STYLE["Players"][1], by["Players"], None, None)]
    return layers


def _png(w, h, rgb):
    stride = w * 3
    raw = b"".join(b"\x00" + bytes(rgb[y * stride:(y + 1) * stride]) for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


def render_png(w, h, cx, cy, span, groups):
    """Top-down view (north up) of `span` cm across the width, centred on (cx, cy). groups: [(rgb, r, pts)]."""
    buf = bytearray(bytes((14, 18, 24)) * (w * h))
    x0, y0, k = cx - span / 2, cy - span / 2, w / span

    def vline(gx, c):
        px = int((gx - x0) * k)
        if 0 <= px < w:
            for py in range(h):
                buf[(py * w + px) * 3:(py * w + px) * 3 + 3] = c

    def hline(gy, c):
        py = int((gy - y0) * k)
        if 0 <= py < h:
            buf[py * w * 3:(py + 1) * w * 3] = c * w
    grid, edge = bytes((30, 38, 48)), bytes((70, 90, 110))
    gx = math.ceil(x0 / 100000) * 100000
    while gx < x0 + span:
        vline(gx, grid)
        gx += 100000
    gy = math.ceil(y0 / 100000) * 100000
    while gy < y0 + span:
        hline(gy, grid)
        gy += 100000
    bx0, bx1, by0, by1 = MAP_BOUNDS
    vline(bx0, edge), vline(bx1, edge), hline(by0, edge), hline(by1, edge)
    for rgb, r, pts in groups:
        c = bytes(rgb)
        for x, y in pts:
            px, py = int((x - x0) * k), int((y - y0) * k)
            if r == 0:
                if 0 <= px < w and 0 <= py < h:
                    i = (py * w + px) * 3
                    buf[i:i + 3] = c
            else:
                xa, xb = max(px - r, 0), min(px + r + 1, w)
                if xa >= xb:
                    continue
                for yy in range(max(py - r, 0), min(py + r + 1, h)):
                    buf[(yy * w + xa) * 3:(yy * w + xb) * 3] = c * (xb - xa)
    return _png(w, h, buf)


# ---------------------------------------------------------------- self-test
def _selftest():
    import tempfile
    P = pack_string

    def tree(name, kids=()):
        return P(name) + struct.pack("<I", len(kids)) + b"".join(kids)

    def tag(name, t, payload, flags=0):
        return P(name) + t + struct.pack("<IB", len(payload), flags) + payload

    def prop_item(path):
        return tag("Item", tree("StructProperty", [tree("InventoryItem")]), P("") + P(path) + struct.pack("<I", 0))

    def prop_num(n):
        return tag("NumItems", tree("IntProperty"), struct.pack("<i", n))

    def inventory(slots):
        elems = b"".join(prop_item(p) + prop_num(n) + P("None") for p, n in slots)
        arr = tag("mInventoryStacks", tree("ArrayProperty", [tree("StructProperty", [tree("InventoryStack")])]),
                  struct.pack("<I", len(slots)) + elems)
        flag = tag("mFlag", tree("BoolProperty"), b"", 0x10)
        return b"\0" + flag + arr + P("None") + struct.pack("<II", 0, 0)

    iron = "/Game/FactoryGame/Resource/Parts/IronPlate/Desc_IronPlate.Desc_IronPlate_C"
    cable = "/Game/FactoryGame/Resource/Parts/Wire/Desc_Cable.Desc_Cable_C"
    ore = "/Game/FactoryGame/Resource/RawResources/OreIron/Desc_OreIron.Desc_OreIron_C"

    def node_data(path, purity, occ):
        props = tag("mResourceClass", tree("ObjectProperty"), P("") + P(path)) if path else b""
        props += tag("mPurity", tree("ByteProperty", [tree("EResourcePurity")]), P("EResourcePurity::RP_" + purity))
        props += tag("mIsOccupied", tree("BoolProperty"), b"", 0x10 if occ else 0)
        return P("") + P("") + struct.pack("<I", 0) + b"\0" + props + P("None") + struct.pack("<I", 0)

    def actor(typ, name, pos):
        return (struct.pack("<I", 1) + P(typ) + P("Persistent_Level") + P(name) + struct.pack("<II", 0, 1)
                + struct.pack("<10f", 0, 0, 0, 1, *pos, 1, 1, 1) + struct.pack("<I", 1))

    def comp(typ, name, parent):
        return struct.pack("<I", 0) + P(typ) + P("Persistent_Level") + P(name) + struct.pack("<I", 0) + P(parent)

    def blob(b):
        return struct.pack("<Q", len(b)) + b

    def blobs(headers, datas):
        toc = struct.pack("<I", len(headers)) + b"".join(headers)
        dat = struct.pack("<I", len(datas)) + b"".join(
            struct.pack("<III", 60, 0, len(d)) + d + struct.pack("<I", 0) for d in datas)
        return blob(toc) + blob(dat)

    empty_actor = P("") + P("") + struct.pack("<I", 0) + b"\0" + P("None") + struct.pack("<I", 0)
    pl = "Persistent_Level:PersistentLevel.Char_Player_C_1"
    node_t = "/Game/FactoryGame/Resource/BP_ResourceNode.BP_ResourceNode_C"
    sub = blobs([actor("/Game/FactoryGame/Buildable/Factory/MinerMk1/Build_MinerMk1.Build_MinerMk1_C", "Persistent_Level:PersistentLevel.Build_MinerMk1_C_1", (1000, 2000, 0)),
                 actor(node_t, "Persistent_Level:PersistentLevel.BP_ResourceNode1", (3000, 4000, 5)),
                 actor(node_t, "Persistent_Level:PersistentLevel.BP_ResourceNode2", (6000, 7000, 5)),
                 actor("/Game/FactoryGame/Resource/BP_Geyser.BP_Geyser_C", "Persistent_Level:PersistentLevel.BP_Geyser1", (8000, 9000, 5))],
                [empty_actor, node_data(ore, "Pure", True), node_data("", "Impure", False), node_data("", "Normal", False)])
    per = blobs([actor("/Game/FactoryGame/Character/Player/Char_Player.Char_Player_C", pl, (500, -500, 10)),
                 comp("/Script/FactoryGame.FGInventoryComponent", pl + ".inventory", pl),
                 comp("/Script/FactoryGame.FGInventoryComponent", "Persistent_Level:PersistentLevel.Storage_C_1.inv", "Persistent_Level:PersistentLevel.Storage_C_1"),
                 comp("/Script/BigPack.BigInventoryComponent", pl + ".BigPack", pl)],
                [empty_actor, inventory([(iron, 40), ("", 0), (cable, 7)]), inventory([(iron, 999)]),
                 inventory([(iron, 5) if k % 3 == 0 else ("", 0) for k in range(120)])])
    vdata = struct.pack("<III", 1, 522, 1012) + struct.pack("<IHHHI", 0, 5, 6, 1, 123) + P("++UE5+Release") + struct.pack("<I", 0)
    rest = (vdata + struct.pack("<I", 1) + P("MainGrid") + struct.pack("<III", 100, 0, 0)
            + struct.pack("<I", 1) + P("Level_x") + sub + struct.pack("<III", 60, 0, 0)
            + per + struct.pack("<I", 0) + struct.pack("<I", 0))
    body = struct.pack("<Q", len(rest)) + rest
    head = struct.pack("<III", 14, 60, 495413) + P("name") + P("Persistent_Level") + P("?opts") + P("My Session") + struct.pack("<IQ", 3600, 639000000000000000) + b"\0\0\0\0"
    tmpl = struct.pack("<IBI", CHUNK_SIZE, 0, 0x03000000)
    raw = build_file(head, tmpl, body)

    info = read_header_info(raw)
    assert info["session"] == "My Session" and info["save_version"] == 60, info
    h_len, t2, body2 = read_chunks(raw)
    assert h_len == len(head) and t2 == tmpl and body2 == body
    levels = parse_save(body2, 60)
    invs = player_inventories(body2, levels)
    assert len(invs) == 2 and [s["num"] for s in invs[0]["stacks"]] == [40, 0, 7], invs
    assert len(invs[1]["stacks"]) == 120 and invs[1]["label"].endswith("BigPack (120 slots)"), invs[1]["label"]
    assert item_paths(body2).get("Desc_IronPlate_C") == iron and item_paths(body2).get("Desc_Cable_C") == cable

    new = apply_edits(body2, levels, [(0, 0, None, 123), (0, 1, cable, 5), (0, 2, "", 0), (1, 100, None, 77), (1, 119, cable, 3), (1, 3, "", 0)], 60)
    lv3 = parse_save(new, 60)
    big = player_inventories(new, lv3)[1]["stacks"]
    assert (big[100]["num"], big[119]["item"], big[119]["num"], big[3]["item"], big[3]["num"]) == (77, cable, 3, "", 0)
    assert big[0]["item"] == iron and big[0]["num"] == 5 and big[1]["item"] == ""
    st = player_inventories(new, lv3)[0]["stacks"]
    assert [(s["item"], s["num"]) for s in st] == [(iron, 123), (cable, 5), ("", 0)], [(s["item"], s["num"]) for s in st]
    so_old, so_new = levels[-1].objs[2], lv3[-1].objs[2]  # storage-box inventory untouched
    assert new[so_new[1]:so_new[1] + so_new[2]] == body2[so_old[1]:so_old[1] + so_old[2]]
    assert read_chunks(build_file(head, tmpl, new))[2] == new
    try:
        apply_edits(body2, levels, [(0, 9, None, 1)], 60)
        raise AssertionError("bad slot should fail")
    except IndexError:
        pass
    try:
        parse_save(body2, 46)
        raise AssertionError("old save versions should be refused")
    except SaveError:
        pass

    nodes = read_nodes(body2, levels)
    assert nodes == [("Desc_OreIron_C", "pure", 3000.0, 4000.0, True), ("", "impure", 6000.0, 7000.0, False),
                     ("Geyser", "normal", 8000.0, 9000.0, False)], nodes
    merged = merge_nodes(nodes, [("Desc_OreCopper_C", "impure", 6100.0, 7050.0, None)])
    assert merged[1][0] == "Desc_OreCopper_C" and merged[0][0] == "Desc_OreIron_C", merged

    actors = scan_actors(body2)
    cats = sorted(c for c in (categorize(t) for t, *_ in actors) if c)
    assert cats == ["Extractors", "Players"], cats
    assert len(nodes_from_actors(actors)) == 3
    layers = build_layers(actors, merged)
    assert list(layers)[-1] == "Players" and "Ore: Iron Ore" in layers and "Ore: Copper Ore" in layers
    iron_group = layers["Ore: Iron Ore"][0]
    assert iron_group[3] == "pure" and iron_group[4] is True and iron_group[0] == _dim(RES_COLORS["OreIron"])
    png = render_png(64, 64, 0, 0, 800000, [g[:3] for v in layers.values() for g in v])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"

    tmp, dest = tempfile.mkdtemp(), tempfile.mkdtemp()
    sav = os.path.join(tmp, "a.sav")
    open(sav, "wb").write(raw)
    for _ in range(20):
        backup_save(sav, os.path.join(dest, "bk"), keep=5)
    assert len(list_backups(os.path.join(dest, "bk"), sav)) == 5
    state = {}
    os.utime(sav, (time.time() - 100, time.time() - 100))
    assert len(auto_backup([tmp], os.path.join(dest, "bk2"), state)) == 1 and auto_backup([tmp], os.path.join(dest, "bk2"), state) == []
    print("satsave self-test OK")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
