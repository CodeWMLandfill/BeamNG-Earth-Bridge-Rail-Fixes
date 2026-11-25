"""Prepare a reversible local patch for BeamNG.Earth (Windows .exe, Linux AppImage/onedir/host, or source tree).

Uses only Python's standard library so it runs inside BeamNG.Earth's embedded
--run-addon Python interpreter with zero external packages.
"""
import argparse
import array
import ast
import base64
import ctypes
import dis
import glob
import hashlib
import io
import importlib.util
import json
import marshal
import os
from pathlib import Path
import shutil
import subprocess
import struct
import sys
import tempfile
import uuid
import zipfile
import zlib

VERSION = "0.4.1"
MARKER = "EARTH_BRIDGE_RAIL_FIXES_0_4_1"
TAG = "earthBridgeRailFixes"
MAX_SOLVER_FAST_VERSION = 305
MIN_V2_SOLVER_FAST_VERSION = 621
MAX_V2_SOLVER_FAST_VERSION = 621
TIER_FULL_SOLVER = "full_solver"
TIER_CLIENT_AND_REPORTER = "client_and_reporter"
SOLVER_MODE_DIRECT = "direct"
SOLVER_MODE_V2 = "server_v2"
SOLVER_MODE_CLIENT_ONLY = "client_only"
ORIG_EARTHD_MODULE = "_brf_orig_earthd"
SIBLING_ORIG_EARTHD_MODULES = (
    "_taf_orig_earthd",
    "_tcf_orig_earthd",
    "_bcf_orig_earthd",
)
SIBLING_REPORTER_MODULES = (
    "bridge_rail_report",
    "traffic_autodrive_report",
)
SIBLING_ORIG_PREFIXES = (
    "_brf_orig_",
    "_taf_orig_",
    "_tcf_orig_",
    "_bcf_orig_",
)
V2_ORIG_SENTINEL = "_brf_orig_roads"
V2_FULL_OVERLAY_MODULES = (
    "bridgetypes",
    "carriageways",
    "fences",
    "furniture",
    "grading",
    "guardrails",
    "plancache",
    "portals",
    "railprofile",
    "rails",
    "railways",
    "roadmesh",
    "roads",
    "tunnellids",
)
COOKIE = struct.Struct("!8sIIII64s")
ENTRY = struct.Struct("!IIIIBc")
MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"

WORKER_ENDPOINT = "https://beamng-earth-bridge-rail-fixes.yourlocalpcgamer.workers.dev/report"

SERVER_MODULES = (
    "blockworker",
    "bridgetypes",
    "carriageways",
    "delineators",
    "earthd",
    "fences",
    "footbridges",
    "furniture",
    "grading",
    "guardrails",
    "mappedpoles",
    "plancache",
    "powerlines",
    "railprofile",
    "rails",
    "railways",
    "roadlanes",
    "roadmesh",
    "roads",
    "tunnellids",
    "vegetation",
)

# Optional server modules wired automatically when staged in payload/server/.
OPTIONAL_SERVER_MODULES = ()


def effective_server_modules(payload_dir=None):
    mods = list(SERVER_MODULES)
    if payload_dir is not None:
        srv = Path(payload_dir) / "server"
        for opt in OPTIONAL_SERVER_MODULES:
            if opt not in mods and (srv / f"{opt}.py").is_file():
                mods.append(opt)
    return tuple(mods)

# Upstream helper modules introduced in the 1.9.1-beta.dev.* cycle (verified
# against v1.9.1-beta.dev.424). Older builds such as v1.9.1-beta (2026-11-10)
# or dev.349 lack these and must be refused cleanly before modifying anything.
REQUIRED_UPSTREAM_MODULES = (
    "areaprops",
    "bumps",
    "busfurniture",
    "busstationkeep",
    "busstationpark",
    "busstations",
    "busyard",
    "catenary",
    "citylamps",
    "designs",
    "designsigns",
    "droprails",
    "europewalks",
    "farpvs",
    "germanroads",
    "germany",
    "l0roads",
    "mapcells",
    "nyctanks",
    "photosurface",
    "places",
    "plazas",
    "pools",
    "radars",
    "railstations",
    "railunderpass",
    "railxing",
    "reach",
    "roadsigns",
    "roadsmooth",
    "roadwear",
    "roundabouts",
    "shells",
    "stationindex",
    "streetfront",
    "surface",
    "terraintex",
)

MIN_SUPPORTED_EARTH_VERSION = "v1.9.1-beta.dev.424"

MOD_ROADS_LUA = "mod/beamng_earth/lua/ge/extensions/beamngEarth/roads.lua"
MOD_BUGREPORT_LUA = "mod/beamng_earth/lua/ge/extensions/beamngEarth/bugreport.lua"
MOD_ACTIONS_JSON = "mod/beamng_earth/lua/ge/extensions/core/input/actions/beamngEarth.json"
MOD_KEYMAP_JSON = "mod/beamng_earth/settings/inputmaps/keyboard_beamngEarth.json"
MOD_SCRIPT_LUA = "mod/beamng_earth/scripts/beamng_earth/modScript.lua"

REPORT_ACTION_KEY = "beamngEarth_quickBridgeRailReport"
REPORT_ACTION_DEF = {
    "cat": "gameplay",
    "order": 93,
    "ctx": "tlua",
    "onDown": "if beamngEarth_bugreport then beamngEarth_bugreport.captureSilent() end",
    "title": "BeamNG.Earth: quick bridge/rail/tunnel bug report",
    "desc": "Silently captures a screenshot and telemetry and sends a report to the Bridge, Rail & Tunnel fix queue (Ctrl+Shift+B).",
}
REPORT_BINDING = {"control": "ctrl shift b", "action": REPORT_ACTION_KEY}
REPORT_MODSCRIPT_LINE = "setExtensionUnloadMode('beamngEarth_bugreport', 'manual')"


class PatchError(RuntimeError):
    pass


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {} if default is None else default


def _win_rel(rel_posix):
    return rel_posix.replace("/", "\\")


def _posix_rel(rel):
    return rel.replace("\\", "/")


def patch_actions_json_bytes(raw_bytes):
    doc = json.loads(raw_bytes.decode("utf-8-sig"))
    if not isinstance(doc, dict):
        raise PatchError("Unexpected input actions JSON format")
    doc[REPORT_ACTION_KEY] = dict(REPORT_ACTION_DEF)
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


def patch_keymap_json_bytes(raw_bytes):
    doc = json.loads(raw_bytes.decode("utf-8-sig"))
    if not isinstance(doc, dict) or not isinstance(doc.get("bindings"), list):
        raise PatchError("Unexpected keymap JSON format")
    bindings = [b for b in doc["bindings"] if not (isinstance(b, dict) and b.get("action") == REPORT_ACTION_KEY)]
    bindings.append(dict(REPORT_BINDING))
    doc["bindings"] = bindings
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


def patch_modscript_bytes(raw_bytes):
    text = raw_bytes.decode("utf-8-sig")
    if "beamngEarth_bugreport" in text:
        return raw_bytes
    sep = "" if text.endswith(("\n", "\r\n")) else "\n"
    return (text + sep + REPORT_MODSCRIPT_LINE + "\n").encode("utf-8")


def _patch_lua_section(text, start_marker, end_marker, already_markers, old, new, label):
    start_idx = text.find(start_marker)
    if start_idx < 0:
        raise PatchError(f"Unsupported roads.lua layout (missing section {label})")
    end_idx = text.find(end_marker, start_idx + len(start_marker))
    if end_idx < 0:
        raise PatchError(f"Unsupported roads.lua layout (missing end of {label})")
    section = text[start_idx:end_idx]
    if any(
        all(marker in section for marker in (group if isinstance(group, (tuple, list)) else (group,)))
        for group in already_markers
    ):
        return text
    if old not in section:
        raise PatchError(f"Unsupported roads.lua layout (missing anchor in {label})")
    return text[:start_idx] + section.replace(old, new, 1) + text[end_idx:]


def patch_roads_lua_bytes(raw_bytes, fallback_bytes=None):
    """Surgically apply bridge/rail client fixes onto any compatible upstream roads.lua."""
    if b"function M.collideNear" not in raw_bytes:
        if fallback_bytes is not None and len(raw_bytes) < 2048:
            return fallback_bytes
        raise PatchError("Unsupported roads.lua layout (missing collideNear)")
    text = raw_bytes.decode("utf-8-sig")
    crlf = "\r\n" in text
    norm = text.replace("\r\n", "\n")

    norm = _patch_lua_section(
        norm,
        "M.TILE_M = 256",
        "M.UNCOLLIDE_M = ",
        ("M.COLLIDE_LEAD_S = 22",),
        "M.COLLIDE_LEAD_S = 14\n",
        "M.COLLIDE_LEAD_S = 22\n",
        "COLLIDE_LEAD_S",
    )
    norm = _patch_lua_section(
        norm,
        "local function isRailDrawn",
        "local unitStats",
        ("local function hasRailCol",),
        "local function isRailDrawn(what) return what == 'rail' or what == 'rail2' or what == 'rail3' end\n",
        (
            "local function isRailDrawn(what) return what == 'rail' or what == 'rail2' or what == 'rail3' end\n"
            "local function hasRailCol(tbl, raw)\n"
            "  if not tbl then return false end\n"
            "  for _, k in ipairs({ 'railcol', 'railcol2', 'railcol3' }) do\n"
            "    local v = tbl[k]\n"
            "    if raw then\n"
            "      if type(v) == 'table' and v.shape ~= nil then return true end\n"
            "    elseif v ~= nil then\n"
            "      return true\n"
            "    end\n"
            "  end\n"
            "  return false\n"
            "end\n"
        ),
        "railcol_helpers",
    )
    norm = norm.replace(
        "-- Once per frame (core onUpdate): deletions, then creations, within the governor's budget.\n"
        "local viewAt = nil  -- core.viewAway(): the camera's { x, y } when it is away from the car, else nil (M.nextWalk)\n",
        "-- Once per frame (core onUpdate): deletions, then creations, within the governor's budget.\n",
        1,
    )
    norm = _patch_lua_section(
        norm,
        "local holdActive = false",
        "function M.radiusToward(",
        ("local viewAt = nil",),
        "local lastDir = nil         -- unit { x, y } of the player's motion at FAST_MS or more, else nil\n",
        (
            "local lastDir = nil         -- unit { x, y } of the player's motion at FAST_MS or more, else nil\n"
            "local viewAt = nil          -- core.viewAway(): the camera's { x, y } when it is away from the car, else nil\n"
        ),
        "hoist_viewAt",
    )
    norm = _patch_lua_section(
        norm,
        "function M.collideNear(t, extra)",
        "function M.setFoci(f)",
        ("viewAt and viewAt.x and viewAt.y",),
        "  -- or near another vehicle's collision island (collisionfoci.lua: other players' cars, traffic)\n",
        (
            "  if viewAt and viewAt.x and viewAt.y then\n"
            "    local dx, dy = t.cx - viewAt.x, t.cy - viewAt.y\n"
            "    local rv = M.COLLIDE_M[1] + (extra or 0) + M.TILE_M * 0.71\n"
            "    if dx * dx + dy * dy <= rv * rv then return true end\n"
            "  end\n"
            "  -- or near another vehicle's collision island (collisionfoci.lua: other players' cars, traffic)\n"
        ),
        "collideNear_viewAt",
    )
    norm = _patch_lua_section(
        norm,
        "local function queueGeoParts(key, t, data)",
        "local function partShape(t, what)",
        ("hasRailCol(t.parts)",),
        "  local leanRail = t.parts.railcol ~= nil\n",
        "  local leanRail = hasRailCol(t.parts)\n",
        "queueGeoParts_leanRail",
    )
    norm = _patch_lua_section(
        norm,
        "local function queueGeoParts(key, t, data)",
        "local function partShape(t, what)",
        ("q.what == 'deck'",),
        "      if q.renderOnly or M.collideNear(t) then walkQ[#walkQ + 1] = q else parked[#parked + 1] = q end\n",
        "      if q.renderOnly or q.what == 'deck' or M.collideNear(t) then walkQ[#walkQ + 1] = q else parked[#parked + 1] = q end\n",
        "queueGeoParts_deck",
    )
    norm = _patch_lua_section(
        norm,
        "function M.onExact(",
        "function M.onTile(",
        ("hasRailCol(t.parts)",),
        "or (t.parts.railcol ~= nil and isRailDrawn(what)) }",
        "or (hasRailCol(t.parts) and isRailDrawn(what)) }",
        "onExact_leanRail",
    )
    norm = _patch_lua_section(
        norm,
        "function M.onTile(",
        "M.SKY_UNIT = 4",
        ("what = 'wire', tile = tiles[key], renderOnly = true",),
        "    walkQ[#walkQ + 1] = { key = key, walk = data.wire, what = 'wire', tile = tiles[key] }\n",
        "    walkQ[#walkQ + 1] = { key = key, walk = data.wire, what = 'wire', tile = tiles[key], renderOnly = true }\n",
        "onTile_wire",
    )
    norm = _patch_lua_section(
        norm,
        "function M.onTile(",
        "M.SKY_UNIT = 4",
        ("hasRailCol(data, true)",),
        "  local leanRail = type(data.railcol) == 'table' and data.railcol.shape ~= nil\n",
        "  local leanRail = hasRailCol(data, true)\n",
        "onTile_leanRail",
    )
    norm = _patch_lua_section(
        norm,
        "function M.onTile(",
        "M.SKY_UNIT = 4",
        ("q.what == 'deck'",),
        "      if q.renderOnly or M.collideNear(tiles[key]) then walkQ[#walkQ + 1] = q else parked[#parked + 1] = q end\n",
        "      if q.renderOnly or q.what == 'deck' or M.collideNear(tiles[key]) then walkQ[#walkQ + 1] = q else parked[#parked + 1] = q end\n",
        "onTile_deck",
    )
    norm = _patch_lua_section(
        norm,
        "function M.update(",
        "function M.nextWalk(",
        ("q.what == 'railcol2'", "isRailCol(q.what)"),
        "q.what == 'bldcol' or q.what == 'walkcol' or q.what == 'railcol' or q.what == 'buscol'",
        "q.what == 'bldcol' or q.what == 'walkcol' or q.what == 'railcol' or q.what == 'railcol2' or q.what == 'railcol3' or q.what == 'buscol'",
        "createWalk_railcol",
    )
    if "M.behindDir()" in norm:
        norm = _patch_lua_section(
            norm,
            "function M.update(",
            "function M.nextWalk(",
            ("q.what ~= 'deck' and not q.renderOnly and not staged and M.behindDir()",),
            "if t and q.what ~= 'road' and q.what ~= 'wire' and not q.renderOnly and not staged and M.behindDir()",
            "if t and q.what ~= 'road' and q.what ~= 'wire' and q.what ~= 'deck' and not q.renderOnly and not staged and M.behindDir()",
            "update_behind_deck",
        )
    norm = _patch_lua_section(
        norm,
        "function M.update(",
        "function M.nextWalk(",
        ("q.what ~= 'deck' then t.cq",),
        "        if not isRoad then t.cq = t.cq or {}; t.cq[q.what] = q end\n",
        "        if not isRoad and q.what ~= 'deck' then t.cq = t.cq or {}; t.cq[q.what] = q end\n",
        "update_deck_cq",
    )
    norm = _patch_lua_section(
        norm,
        "function M.nextWalk(",
        "function M.reset(",
        ("if viewAt and viewAt.x and viewAt.y and (e.renderOnly or e.what == 'wire' or not dir) then",),
        "      if viewAt and (e.renderOnly or e.what == 'wire') then\n",
        "      if viewAt and viewAt.x and viewAt.y and (e.renderOnly or e.what == 'wire' or not dir) then\n",
        "nextWalk_viewAt",
    )
    norm = _patch_lua_section(
        norm,
        "function M.reset()",
        "function M.setEnabled(",
        ("viewAt = nil", ", viewAt = "),
        "  tiles, createQ, deleteQ, walkQ, anchorId, loadedMaterials = {}, {}, {}, {}, nil, false\n",
        (
            "  tiles, createQ, deleteQ, walkQ, anchorId, loadedMaterials = {}, {}, {}, {}, nil, false\n"
            "  viewAt = nil\n"
        ),
        "reset_viewAt",
    )

    out = norm.replace("\n", "\r\n") if crlf else norm
    return out.encode("utf-8")


MOD_ZIP_ROADS_LUA = "lua/ge/extensions/beamngEarth/roads.lua"
MOD_ZIP_BUGREPORT_LUA = "lua/ge/extensions/beamngEarth/bugreport.lua"
MOD_ZIP_ACTIONS_JSON = "lua/ge/extensions/core/input/actions/beamngEarth.json"
MOD_ZIP_KEYMAP_JSON = "settings/inputmaps/keyboard_beamngEarth.json"
MOD_ZIP_SCRIPT_LUA = "scripts/beamng_earth/modScript.lua"


def patch_mod_zip_bytes(zip_bytes, payload_dir, apply_fixes=True, enable_reporter=True):
    """Patch a BeamNG.Earth mod .zip (mods/beamng_earth.zip or Host Resources/Client/beamng_earth.zip) without recompressing untouched entries."""
    payload_root = Path(payload_dir)
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as src_zf:
        entry_names = {zi.filename.replace("\\", "/"): zi for zi in src_zf.infolist()}
        for req in (MOD_ZIP_ROADS_LUA, MOD_ZIP_ACTIONS_JSON, MOD_ZIP_KEYMAP_JSON, MOD_ZIP_SCRIPT_LUA):
            if req not in entry_names:
                raise PatchError(f"Expected mod file missing in beamng_earth.zip: {req}")

        replacements = {}
        if apply_fixes:
            fallback_roads = (
                (payload_root / MOD_ROADS_LUA).read_bytes()
                if (payload_root / MOD_ROADS_LUA).is_file()
                else None
            )
            raw_roads = src_zf.read(entry_names[MOD_ZIP_ROADS_LUA].filename)
            replacements[MOD_ZIP_ROADS_LUA] = patch_roads_lua_bytes(raw_roads, fallback_bytes=fallback_roads)

        if enable_reporter:
            replacements[MOD_ZIP_BUGREPORT_LUA] = (payload_root / MOD_BUGREPORT_LUA).read_bytes()
            replacements[MOD_ZIP_ACTIONS_JSON] = patch_actions_json_bytes(
                src_zf.read(entry_names[MOD_ZIP_ACTIONS_JSON].filename)
            )
            replacements[MOD_ZIP_KEYMAP_JSON] = patch_keymap_json_bytes(
                src_zf.read(entry_names[MOD_ZIP_KEYMAP_JSON].filename)
            )
            replacements[MOD_ZIP_SCRIPT_LUA] = patch_modscript_bytes(
                src_zf.read(entry_names[MOD_ZIP_SCRIPT_LUA].filename)
            )

        out_buf = io.BytesIO()
        handled = set()
        with zipfile.ZipFile(out_buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as out_zf:
            can_raw_copy = hasattr(out_zf, "_writecheck") and src_zf.fp is not None
            for zi in src_zf.infolist():
                norm_name = zi.filename.replace("\\", "/")
                if norm_name in replacements:
                    handled.add(norm_name)
                    new_info = zipfile.ZipInfo(norm_name, date_time=zi.date_time)
                    new_info.compress_type = zipfile.ZIP_DEFLATED
                    new_info.external_attr = zi.external_attr or (0o644 << 16)
                    out_zf.writestr(new_info, replacements[norm_name])
                elif not enable_reporter and norm_name == MOD_ZIP_BUGREPORT_LUA:
                    handled.add(norm_name)
                    continue
                elif can_raw_copy and (zi.flag_bits & 0x08) == 0 and not zi.is_dir():
                    src_zf.fp.seek(zi.header_offset)
                    lhdr = src_zf.fp.read(30)
                    if len(lhdr) == 30 and lhdr[:4] == b"PK\x03\x04":
                        fn_len, ex_len = struct.unpack_from("<HH", lhdr, 26)
                        src_zf.fp.seek(zi.header_offset + 30 + fn_len + ex_len)
                        raw_cdata = src_zf.fp.read(zi.compress_size)
                        cp = zipfile.ZipInfo(zi.filename, date_time=zi.date_time)
                        cp.compress_type = zi.compress_type
                        cp.comment = zi.comment
                        cp.extra = zi.extra
                        cp.create_system = zi.create_system
                        cp.create_version = zi.create_version
                        cp.extract_version = zi.extract_version
                        cp.flag_bits = zi.flag_bits
                        cp.volume = zi.volume
                        cp.internal_attr = zi.internal_attr
                        cp.external_attr = zi.external_attr
                        cp.file_size = zi.file_size
                        cp.CRC = zi.CRC
                        cp.compress_size = len(raw_cdata)
                        out_zf._writecheck(cp)
                        out_zf._didModify = True
                        cp.header_offset = out_zf.fp.tell()
                        out_zf.fp.write(cp.FileHeader(False))
                        out_zf.fp.write(raw_cdata)
                        out_zf.start_dir = out_zf.fp.tell()
                        out_zf.filelist.append(cp)
                        out_zf.NameToInfo[cp.filename] = cp
                    else:
                        out_zf.writestr(zi, src_zf.read(zi.filename))
                else:
                    out_zf.writestr(zi, b"" if zi.is_dir() else src_zf.read(zi.filename))

            for norm_name, payload_bytes in replacements.items():
                if norm_name in handled:
                    continue
                new_info = zipfile.ZipInfo(norm_name, date_time=(2026, 1, 1, 0, 0, 0))
                new_info.compress_type = zipfile.ZIP_DEFLATED
                new_info.external_attr = 0o644 << 16
                out_zf.writestr(new_info, payload_bytes)
    return out_buf.getvalue()


def _read_carchive_version(data, start, entry_map):
    entry = entry_map.get("VERSION")
    if not entry:
        return ""
    blob = data[start + entry[1]:start + entry[1] + entry[2]]
    try:
        raw = zlib.decompress(blob) if entry[4] else blob
        return raw.decode("utf-8", errors="replace").strip().splitlines()[0][:64]
    except Exception:
        return ""


def _read_source_version(root):
    ver_file = Path(root) / "VERSION"
    if not ver_file.is_file():
        return ""
    try:
        lines = ver_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        return lines[0][:64] if lines else ""
    except OSError:
        return ""


def _incompatible_message(missing, earth_version="", is_source=False):
    kind = "source tree" if is_source else "build"
    found = f" (found {earth_version})" if earth_version else ""
    sample = ", ".join(missing[:6])
    more = f" (+{len(missing) - 6} more)" if len(missing) > 6 else ""
    return (
        f"Incompatible BeamNG.Earth {kind}{found}. "
        f"Requires BeamNG.Earth {MIN_SUPPORTED_EARTH_VERSION} or newer; "
        f"missing upstream server modules: {sample}{more}"
    )


_PAYLOAD_EXPORTS_MEMO = {}
_PAYLOAD_V2_EXPORTS_MEMO = {}


def _default_payload_dir():
    cand = Path(__file__).resolve().parent.parent / "payload"
    return cand if (cand / "server" / "earthd.py").is_file() else None


def _payload_module_exports(payload_dir):
    if payload_dir is None:
        return None
    srv = Path(payload_dir).resolve() / "server"
    if not srv.is_dir():
        return None
    mods = effective_server_modules(payload_dir)
    sig = []
    for mod in mods:
        p = srv / f"{mod}.py"
        if not p.is_file():
            return None
        st = p.stat()
        sig.append((mod, st.st_mtime_ns, st.st_size))
    memo_key = (str(srv), tuple(sig))
    cached = _PAYLOAD_EXPORTS_MEMO.get(memo_key)
    if cached is not None:
        return cached
    exports = {}
    for mod in mods:
        src = (srv / f"{mod}.py").read_text(encoding="utf-8")
        co = compile(src, f"{mod}.py", "exec")
        exports[mod] = frozenset(
            inst.argval
            for inst in dis.get_instructions(co)
            if inst.opname in ("STORE_NAME", "STORE_GLOBAL")
        )
    if len(_PAYLOAD_EXPORTS_MEMO) >= 8:
        _PAYLOAD_EXPORTS_MEMO.clear()
    _PAYLOAD_EXPORTS_MEMO[memo_key] = exports
    return exports


def _payload_v2_module_exports(payload_dir):
    if payload_dir is None:
        return None
    srv = Path(payload_dir).resolve() / "server_v2"
    if not srv.is_dir():
        return None
    sig = []
    for mod in V2_FULL_OVERLAY_MODULES:
        p = srv / f"{mod}.py"
        if not p.is_file():
            return None
        st = p.stat()
        sig.append((mod, st.st_mtime_ns, st.st_size))
    memo_key = (str(srv), tuple(sig))
    cached = _PAYLOAD_V2_EXPORTS_MEMO.get(memo_key)
    if cached is not None:
        return cached
    exports = {}
    for mod in V2_FULL_OVERLAY_MODULES:
        src = (srv / f"{mod}.py").read_text(encoding="utf-8")
        co = compile(src, f"{mod}.py", "exec")
        exports[mod] = frozenset(
            inst.argval
            for inst in dis.get_instructions(co)
            if inst.opname in ("STORE_NAME", "STORE_GLOBAL")
        )
    if len(_PAYLOAD_V2_EXPORTS_MEMO) >= 8:
        _PAYLOAD_V2_EXPORTS_MEMO.clear()
    _PAYLOAD_V2_EXPORTS_MEMO[memo_key] = exports
    return exports


def _extract_fast_min_version_from_code(fast_co):
    found = []
    pending_ints = []
    for inst in dis.get_instructions(fast_co):
        if inst.opname == "LOAD_CONST" and isinstance(inst.argval, int) and not isinstance(inst.argval, bool):
            pending_ints.append(inst.argval)
        elif inst.opname in ("STORE_NAME", "STORE_GLOBAL"):
            if inst.argval == "MIN_VERSION" and pending_ints:
                found.append(max(pending_ints))
            pending_ints.clear()
        elif inst.opname in ("POP_TOP", "IMPORT_NAME"):
            pending_ints.clear()
    return max(found) if found else None


def _extract_fast_min_version_from_source(src_text):
    try:
        tree = ast.parse(src_text, filename="fast.py")
    except SyntaxError:
        return None
    found = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "MIN_VERSION":
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, int) and not isinstance(node.value.value, bool):
                        found.append(node.value.value)
                    elif isinstance(node.value, ast.Call):
                        for arg in node.value.args:
                            if isinstance(arg, ast.Constant) and isinstance(arg.value, int) and not isinstance(arg.value, bool):
                                found.append(arg.value)
    return max(found) if found else None


def _scan_missing_exports_in_code(co, replaced_set, payload_exports, aliases, missing):
    ins = list(dis.get_instructions(co))
    for i, inst in enumerate(ins):
        if inst.opname == "IMPORT_NAME" and inst.argval in replaced_set:
            src_mod = inst.argval
            j = i + 1
            while j < len(ins) and ins[j].opname in (
                "IMPORT_FROM", "STORE_NAME", "STORE_FAST", "STORE_GLOBAL", "POP_TOP"
            ):
                if ins[j].opname == "IMPORT_FROM":
                    attr = ins[j].argval
                    if attr != "*" and attr not in payload_exports.get(src_mod, ()):
                        missing.append((src_mod, attr))
                        return
                elif ins[j].opname in ("STORE_NAME", "STORE_FAST", "STORE_GLOBAL") and j > i and ins[j - 1].opname == "IMPORT_NAME":
                    aliases[ins[j].argval] = src_mod
                j += 1
    for i, inst in enumerate(ins):
        if inst.opname == "LOAD_ATTR" and i > 0:
            prev = ins[i - 1]
            if prev.opname in ("LOAD_GLOBAL", "LOAD_NAME", "LOAD_FAST") and prev.argval in aliases:
                src_mod = aliases[prev.argval]
                if inst.argval not in payload_exports.get(src_mod, ()):
                    missing.append((src_mod, inst.argval))
                    return
    for c in co.co_consts:
        if hasattr(c, "co_code"):
            _scan_missing_exports_in_code(c, replaced_set, payload_exports, dict(aliases), missing)
            if missing:
                return


def _has_earthd_dispatch_hook(earthd_co):
    for c in earthd_co.co_consts:
        if hasattr(c, "co_name") and c.co_name == "Connection":
            for m in c.co_consts:
                if (
                    hasattr(m, "co_name")
                    and m.co_name == "dispatch"
                    and getattr(m, "co_argcount", 0) == 2
                    and (getattr(m, "co_flags", 0) & 0x180) == 0
                ):
                    return True
    return False


def _verify_earthd_dispatch_hook(earthd_co, data=None, offset=None, toc=None):
    if _has_earthd_dispatch_hook(earthd_co):
        return True
    if data is not None and offset is not None and toc is not None:
        seen = set()
        queue = [m for m in SIBLING_ORIG_EARTHD_MODULES if m in toc]
        while queue and len(seen) < 8:
            mod_name = queue.pop(0)
            if mod_name in seen or mod_name not in toc:
                continue
            seen.add(mod_name)
            kind, pos, length = toc[mod_name]
            if kind != 0 or pos < 12 or pos + length > offset:
                continue
            try:
                cand_co = marshal.loads(zlib.decompress(data[pos:pos + length]))
            except Exception:
                continue
            if _has_earthd_dispatch_hook(cand_co):
                return True
            for inst in dis.get_instructions(cand_co):
                if inst.opname == "IMPORT_NAME" and isinstance(inst.argval, str) and inst.argval in toc:
                    queue.append(inst.argval)
    raise PatchError("Unsupported upstream earthd layout (missing Connection.dispatch hook point)")


def _detect_pyz_patch_tier_and_mode(data, offset, toc, payload_dir=None, extra_modules=()):
    if V2_ORIG_SENTINEL in toc:
        return TIER_FULL_SOLVER, SOLVER_MODE_V2
    if ORIG_EARTHD_MODULE in toc:
        return TIER_CLIENT_AND_REPORTER, SOLVER_MODE_CLIENT_ONLY
    if "bridge_rail_report" in toc:
        return TIER_FULL_SOLVER, SOLVER_MODE_DIRECT
    if "earthd" not in toc:
        raise PatchError("Executable does not contain expected server module 'earthd'")

    eff_payload = payload_dir if payload_dir is not None else _default_payload_dir()
    active_mods = effective_server_modules(eff_payload)
    req_mods = list(active_mods) + [m for m in extra_modules if m not in active_mods]

    # Foreign Python version during read-only inspect_target: check module presence only.
    if len(data) < 8 or data[4:8] != importlib.util.MAGIC_NUMBER:
        for mod in req_mods:
            if mod not in toc:
                raise PatchError(f"Executable does not contain expected server module '{mod}'")
        return TIER_FULL_SOLVER, SOLVER_MODE_DIRECT

    def _load_mod_code(mod_name):
        kind, pos, length = toc[mod_name]
        if kind != 0 or pos < 12 or pos + length > offset:
            raise PatchError(f"Unsupported PYZ entry layout for '{mod_name}'")
        return marshal.loads(zlib.decompress(data[pos:pos + length]))

    if "fast" in toc:
        fast_co = _load_mod_code("fast")
        fast_min = _extract_fast_min_version_from_code(fast_co)
        if fast_min is not None and fast_min > MAX_SOLVER_FAST_VERSION:
            _verify_earthd_dispatch_hook(_load_mod_code("earthd"), data=data, offset=offset, toc=toc)
            if (
                MIN_V2_SOLVER_FAST_VERSION <= fast_min <= MAX_V2_SOLVER_FAST_VERSION
                and all(mod in toc for mod in V2_FULL_OVERLAY_MODULES)
            ):
                v2_exports = _payload_v2_module_exports(eff_payload)
                if eff_payload is not None and v2_exports is None:
                    return TIER_CLIENT_AND_REPORTER, SOLVER_MODE_CLIENT_ONLY
                if v2_exports is not None:
                    replaced_v2 = set(V2_FULL_OVERLAY_MODULES)
                    missing_v2 = []
                    for mod_name, (k, p_off, p_len) in toc.items():
                        if (
                            mod_name in replaced_v2
                            or mod_name == "bench_pose"
                            or mod_name in SIBLING_REPORTER_MODULES
                            or mod_name.startswith(SIBLING_ORIG_PREFIXES)
                            or k != 0
                            or "." in mod_name
                        ):
                            continue
                        if p_off < 12 or p_off + p_len > offset:
                            continue
                        co = marshal.loads(zlib.decompress(data[p_off:p_off + p_len]))
                        _scan_missing_exports_in_code(co, replaced_v2, v2_exports, {}, missing_v2)
                        if missing_v2:
                            return TIER_CLIENT_AND_REPORTER, SOLVER_MODE_CLIENT_ONLY
                return TIER_FULL_SOLVER, SOLVER_MODE_V2
            return TIER_CLIENT_AND_REPORTER, SOLVER_MODE_CLIENT_ONLY

    for mod in req_mods:
        if mod not in toc:
            raise PatchError(f"Executable does not contain expected server module '{mod}'")

    payload_exports = _payload_module_exports(eff_payload)
    if payload_exports is not None:
        replaced_set = set(active_mods)
        missing = []
        for mod_name, (k, p_off, p_len) in toc.items():
            if (
                mod_name in replaced_set
                or mod_name == "bench_pose"
                or mod_name in SIBLING_REPORTER_MODULES
                or mod_name.startswith(SIBLING_ORIG_PREFIXES)
                or k != 0
                or "." in mod_name
            ):
                continue
            if p_off < 12 or p_off + p_len > offset:
                continue
            co = marshal.loads(zlib.decompress(data[p_off:p_off + p_len]))
            _scan_missing_exports_in_code(co, replaced_set, payload_exports, {}, missing)
            if missing:
                _verify_earthd_dispatch_hook(_load_mod_code("earthd"), data=data, offset=offset, toc=toc)
                return TIER_CLIENT_AND_REPORTER, SOLVER_MODE_CLIENT_ONLY

    return TIER_FULL_SOLVER, SOLVER_MODE_DIRECT


def _detect_pyz_patch_tier(data, offset, toc, payload_dir=None, extra_modules=()):
    tier, _mode = _detect_pyz_patch_tier_and_mode(
        data, offset, toc, payload_dir=payload_dir, extra_modules=extra_modules
    )
    return tier


def _detect_source_patch_tier_and_mode(root, payload_dir=None):
    root = Path(root)
    server_dir = root / "server"
    sidecar = read_json(root / f".{TAG}" / "receipt.json", {})
    if sidecar.get("state") == "installed" and sidecar.get("patchTier") in (TIER_FULL_SOLVER, TIER_CLIENT_AND_REPORTER):
        smode = sidecar.get("solverMode") or (
            SOLVER_MODE_DIRECT if sidecar["patchTier"] == TIER_FULL_SOLVER else SOLVER_MODE_CLIENT_ONLY
        )
        return sidecar["patchTier"], smode

    eff_payload = payload_dir if payload_dir is not None else _default_payload_dir()
    active_mods = effective_server_modules(eff_payload)

    def _check_source_earthd_hook():
        raw_earthd = _read_pristine(root, "server/earthd.py").decode("utf-8", errors="replace")
        if 'elif kind == "terrain_patch":' in raw_earthd:
            return
        for sib_mod in SIBLING_ORIG_EARTHD_MODULES:
            sib_path = server_dir / f"{sib_mod}.py"
            if sib_mod in raw_earthd and sib_path.is_file():
                sib_raw = sib_path.read_text(encoding="utf-8", errors="replace")
                if "class Connection" in sib_raw or 'elif kind == "terrain_patch":' in sib_raw:
                    return
        raise PatchError("Unsupported upstream earthd layout (missing terrain_patch dispatch hook point)")

    fast_path = server_dir / "fast.py"
    if fast_path.is_file():
        fast_raw = _read_pristine(root, "server/fast.py").decode("utf-8", errors="replace")
        fast_min = _extract_fast_min_version_from_source(fast_raw)
        if fast_min is not None and fast_min > MAX_SOLVER_FAST_VERSION:
            _check_source_earthd_hook()
            if (
                MIN_V2_SOLVER_FAST_VERSION <= fast_min <= MAX_V2_SOLVER_FAST_VERSION
                and all((server_dir / f"{m}.py").is_file() for m in V2_FULL_OVERLAY_MODULES)
            ):
                v2_exports = _payload_v2_module_exports(eff_payload)
                if eff_payload is not None and v2_exports is None:
                    return TIER_CLIENT_AND_REPORTER, SOLVER_MODE_CLIENT_ONLY
                if v2_exports is not None:
                    replaced_v2 = set(V2_FULL_OVERLAY_MODULES)
                    missing_v2 = []
                    for py_file in sorted(server_dir.glob("*.py")):
                        mod_name = py_file.stem
                        if (
                            mod_name in replaced_v2
                            or mod_name == "bench_pose"
                            or mod_name in SIBLING_REPORTER_MODULES
                            or mod_name.startswith(SIBLING_ORIG_PREFIXES)
                        ):
                            continue
                        try:
                            co = compile(py_file.read_text(encoding="utf-8"), py_file.name, "exec")
                        except (OSError, SyntaxError, UnicodeDecodeError):
                            continue
                        _scan_missing_exports_in_code(co, replaced_v2, v2_exports, {}, missing_v2)
                        if missing_v2:
                            return TIER_CLIENT_AND_REPORTER, SOLVER_MODE_CLIENT_ONLY
                return TIER_FULL_SOLVER, SOLVER_MODE_V2
            return TIER_CLIENT_AND_REPORTER, SOLVER_MODE_CLIENT_ONLY

    for mod in active_mods:
        if not (server_dir / f"{mod}.py").is_file():
            raise PatchError(f"Source directory does not contain expected server module '{mod}'")

    payload_exports = _payload_module_exports(eff_payload)
    if payload_exports is not None:
        replaced_set = set(active_mods)
        missing = []
        for py_file in sorted(server_dir.glob("*.py")):
            mod_name = py_file.stem
            if (
                mod_name in replaced_set
                or mod_name == "bench_pose"
                or mod_name in SIBLING_REPORTER_MODULES
                or mod_name.startswith(SIBLING_ORIG_PREFIXES)
            ):
                continue
            try:
                co = compile(py_file.read_text(encoding="utf-8"), py_file.name, "exec")
            except (OSError, SyntaxError, UnicodeDecodeError):
                continue
            _scan_missing_exports_in_code(co, replaced_set, payload_exports, {}, missing)
            if missing:
                _check_source_earthd_hook()
                return TIER_CLIENT_AND_REPORTER, SOLVER_MODE_CLIENT_ONLY

    return TIER_FULL_SOLVER, SOLVER_MODE_DIRECT


def _detect_source_patch_tier(root, payload_dir=None):
    tier, _mode = _detect_source_patch_tier_and_mode(root, payload_dir=payload_dir)
    return tier


def validate_pyz_compatibility(toc, earth_version="", extra_modules=()):
    missing = [m for m in REQUIRED_UPSTREAM_MODULES if m not in toc]
    if missing:
        raise PatchError(_incompatible_message(missing, earth_version=earth_version, is_source=False))
    if "earthd" not in toc:
        raise PatchError("Executable does not contain expected server module 'earthd'")


def validate_source_compatibility(root, payload_dir=None):
    server_dir = Path(root) / "server"
    mod_dir = Path(root) / "mod" / "beamng_earth"
    if not (server_dir / "earthd.py").is_file() or not mod_dir.is_dir():
        raise PatchError("Select a BeamNG.Earth executable or source directory")
    earth_version = _read_source_version(root)
    missing = [m for m in REQUIRED_UPSTREAM_MODULES if not (server_dir / f"{m}.py").is_file()]
    if missing:
        raise PatchError(_incompatible_message(missing, earth_version=earth_version, is_source=True))
    for rel in (MOD_ROADS_LUA, MOD_ACTIONS_JSON, MOD_KEYMAP_JSON, MOD_SCRIPT_LUA):
        if not (Path(root) / rel).is_file():
            raise PatchError(f"Expected mod file missing in source tree: {rel}")
    roads_raw = (Path(root) / MOD_ROADS_LUA).read_bytes()
    if b"function M.collideNear" in roads_raw:
        patch_roads_lua_bytes(roads_raw)
    _detect_source_patch_tier(root, payload_dir=payload_dir)


_CACHE_REV_MODULES = ("earthd", "blockworker", "plancache", "roads")


def _server_cache_rev(payload_dir):
    """Deterministic add-on cache revision across patched solver/server modules."""
    if payload_dir is None:
        return VERSION
    payload_root = Path(payload_dir)
    h = hashlib.sha256()
    for mod in effective_server_modules(payload_dir):
        src_path = payload_root / "server" / f"{mod}.py"
        if src_path.is_file():
            h.update(b"v1:" + mod.encode("utf-8") + b"\0")
            h.update(src_path.read_bytes() + b"\0")
    srv_v2 = payload_root / "server_v2"
    if srv_v2.is_dir():
        for mod in V2_FULL_OVERLAY_MODULES:
            v2_path = srv_v2 / f"{mod}.py"
            if v2_path.is_file():
                h.update(b"v2:" + mod.encode("utf-8") + b"\0")
                h.update(v2_path.read_bytes() + b"\0")
    return f"{VERSION}-{h.hexdigest()[:12]}"


def _stamp_addon_rev(src_text, cache_rev):
    out = []
    stamped = False
    for line in src_text.splitlines(keepends=True):
        if not stamped and line.startswith("_BRF_ADDON_REV = "):
            nl = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
            out.append(f"_BRF_ADDON_REV = {cache_rev!r}{nl}")
            stamped = True
        else:
            out.append(line)
    return "".join(out)


def _stamp_v2_addon_rev(mod_name, src_text, cache_rev):
    if mod_name not in ("plancache", "roads"):
        return src_text
    header = (
        "import os as _brf_os\n"
        "import sys as _brf_sys\n"
        f"_BRF_ADDON_REV = {cache_rev!r}\n"
        'if not _brf_os.environ.get("BNE_BRF_ADDON_REV_PIN") and _brf_os.environ.get("BNE_BRF_ADDON_REV") != _BRF_ADDON_REV:\n'
        '    _brf_os.environ["BNE_BRF_ADDON_REV"] = _BRF_ADDON_REV\n'
        '    _brf_revisit = _brf_sys.modules.get("revisit")\n'
        "    if _brf_revisit is not None:\n"
        '        getattr(_brf_revisit, "_code", []).clear()\n'
    )
    tree = ast.parse(src_text, filename=f"{mod_name}.py")
    insert_line = 1
    if tree.body:
        first = tree.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
            and getattr(first, "end_lineno", None)
        ):
            insert_line = first.end_lineno + 1
    lines = src_text.splitlines(keepends=True)
    idx = min(max(insert_line - 1, 0), len(lines))
    body = "".join(lines[:idx]) + header + "".join(lines[idx:])
    if mod_name == "plancache":
        footer = (
            "\n_brf_orig_plans_format = plans_format\n"
            "def plans_format():\n"
            "    _base = _brf_orig_plans_format()\n"
            '    _rev = _brf_os.environ.get("BNE_BRF_ADDON_REV")\n'
            '    return f"{_base}.brf_{_rev}" if _rev else _base\n'
        )
        body = body + footer
    return body


def _build_tier2_earthd_wrapper_src(
    enable_reporter,
    patch_tier=TIER_CLIENT_AND_REPORTER,
    solver_mode=SOLVER_MODE_CLIENT_ONLY,
    cache_rev=None,
):
    lines = [
        "import os as _brf_os",
        "import sys as _brf_sys",
    ]
    if cache_rev:
        lines.extend([
            f"_BRF_ADDON_REV = {cache_rev!r}",
            'if not _brf_os.environ.get("BNE_BRF_ADDON_REV_PIN") and _brf_os.environ.get("BNE_BRF_ADDON_REV") != _BRF_ADDON_REV:',
            '    _brf_os.environ["BNE_BRF_ADDON_REV"] = _BRF_ADDON_REV',
            '    _brf_revisit = _brf_sys.modules.get("revisit")',
            "    if _brf_revisit is not None:",
            '        getattr(_brf_revisit, "_code", []).clear()',
        ])
    lines.extend([
        f"import {ORIG_EARTHD_MODULE} as _brf_orig",
        f"_earth_bridge_rail_fixes_patch = {MARKER!r}",
        f"_BRF_PATCH_TIER = {patch_tier!r}",
        f"_BRF_SOLVER_MODE = {solver_mode!r}",
    ])
    if enable_reporter:
        lines.extend([
            "_brf_conn = getattr(_brf_orig, 'Connection', None)",
            "_brf_orig_dispatch = getattr(_brf_conn, 'dispatch', None) if _brf_conn is not None else None",
            "if _brf_conn is None or not callable(_brf_orig_dispatch):",
            "    raise RuntimeError('earthd.Connection.dispatch hook point missing')",
            "if not getattr(_brf_orig_dispatch, '_brf_wrapped', False):",
            "    def _brf_dispatch(self, msg):",
            "        if getattr(self, 'closing', False):",
            "            return None",
            "        if isinstance(msg, dict) and msg.get('type') == 'bridge_rail_report':",
            "            import bridge_rail_report",
            "            _srv = getattr(self, 's', None) or getattr(self, 'server', None)",
            "            bridge_rail_report.submit(_srv, msg)",
            "            return None",
            "        res = _brf_orig_dispatch(self, msg)",
            "        if isinstance(msg, dict) and msg.get('type') == 'hello':",
            "            try:",
            "                import bridge_rail_report",
            "                _srv = getattr(self, 's', None) or getattr(self, 'server', None)",
            "                bridge_rail_report.check_fixed_reports_async(_srv, user_path=msg.get('userPath'))",
            "            except Exception:",
            "                pass",
            "        return res",
            "    _brf_dispatch._brf_wrapped = True",
            "    _brf_conn.dispatch = _brf_dispatch",
        ])
    if patch_tier == TIER_FULL_SOLVER:
        lines.extend([
            "_brf_conn_fc = getattr(_brf_orig, 'Connection', None)",
            "_brf_orig_fc = getattr(_brf_conn_fc, '_far_cover', None) if _brf_conn_fc is not None else None",
            "if _brf_conn_fc is not None and callable(_brf_orig_fc) and not getattr(_brf_orig_fc, '_brf_fc_wrapped', False):",
            "    def _brf_far_cover(self, key, info):",
            "        out = _brf_orig_fc(self, key, info)",
            "        try:",
            "            if not out or not isinstance(info, dict) or not info.get('ter'):",
            "                return out",
            "            import math as _m, numpy as _np, farfield as _ff, heightmap as _hm",
            "            tp = _brf_os.path.join(self.s.out_dir, info['ter'])",
            "            n = int(self.s.layout.block)",
            "            idx = _np.memmap(tp, dtype=_np.uint8, mode='r', offset=5 + 2 * n * n, shape=(n, n))",
            "            holes = (idx == int(getattr(_hm, 'TER_HOLE', 255)))",
            "            if not holes.any():",
            "                return out",
            "            q = _np.memmap(tp, dtype='<u2', mode='r', offset=5, shape=(n, n))",
            "            hm = _np.array(float(info['zBase']) + q * (float(info['maxHeight']) / 65536.0), dtype=_np.float64, copy=True)",
            "            for iy_h, ix_h in zip(*_np.nonzero(holes)):",
            "                y0w, y1w = max(0, int(iy_h) - 12), min(n, int(iy_h) + 14)",
            "                x0w, x1w = max(0, int(ix_h) - 12), min(n, int(ix_h) + 14)",
            "                win_z = hm[y0w:y1w, x0w:x1w]",
            "                win_h = holes[y0w:y1w, x0w:x1w]",
            "                non_h = win_z[~win_h]",
            "                z_fl = float(_np.min(non_h) if len(non_h) else _np.min(win_z)) - 1.5",
            "                yc1, xc1 = min(n, int(iy_h) + 2), min(n, int(ix_h) + 2)",
            "                hm[int(iy_h):yc1, int(ix_h):xc1] = _np.minimum(hm[int(iy_h):yc1, int(ix_h):xc1], z_fl)",
            "            aid, cx, cy = key[0], float(key[1]), float(key[2])",
            "            half = _hm.half_extent(n)",
            "            bx0, by0 = cx - half, cy - half",
            "            for k in list(out.keys()):",
            "                t = self.sent.get(k)",
            "                ti = self.s.tiles.get(self.s.far_ck(aid, k)) if t is not None else None",
            "                if ti is not None and getattr(ti, 'grid', None) is not None:",
            "                    s_val = _ff.cover_sink(ti.grid, t.bbox, hm, bx0, by0, _hm.SQUARE)",
            "                    if s_val is not None:",
            "                        out[k] = max(float(out[k]), _m.ceil(float(s_val) * 100.0) / 100.0)",
            "        except Exception:",
            "            pass",
            "        return out",
            "    _brf_far_cover._brf_fc_wrapped = True",
            "    _brf_conn_fc._far_cover = _brf_far_cover",
        ])
    lines.extend([
        "_brf_mod_name = __name__",
        "_brf_orig._earth_bridge_rail_fixes_patch = _earth_bridge_rail_fixes_patch",
        "_brf_orig._BRF_PATCH_TIER = _BRF_PATCH_TIER",
        "_brf_orig._BRF_SOLVER_MODE = _BRF_SOLVER_MODE",
        "globals().update(_brf_orig.__dict__)",
        "globals()['__name__'] = _brf_mod_name",
        "if _brf_mod_name == '__main__':",
        "    _brf_orig.main()",
        "else:",
        "    _brf_sys.modules[_brf_mod_name] = _brf_orig",
        "",
    ])
    return "\n".join(lines)


def _inject_direct_earthd_coexistence(src_text):
    """Ensure direct v1 earthd.py preserves sibling traffic_autodrive_report dispatch if stacked."""
    if "traffic_autodrive_report" not in src_text:
        brf_dispatch = (
            '        elif kind == "bridge_rail_report":\n'
            "            import bridge_rail_report\n"
            "            bridge_rail_report.submit(self.s, msg)\n"
        )
        taf_dispatch = (
            '        elif kind == "traffic_autodrive_report":\n'
            "            import traffic_autodrive_report\n"
            "            traffic_autodrive_report.submit(self.s, msg)\n"
        )
        if brf_dispatch in src_text:
            src_text = src_text.replace(brf_dispatch, brf_dispatch + taf_dispatch, 1)
        brf_hello = (
            "            try:\n"
            "                import bridge_rail_report\n"
            '                bridge_rail_report.check_fixed_reports_async(self.s, user_path=msg.get("userPath"))\n'
            "            except Exception:\n"
            "                pass\n"
        )
        taf_hello = (
            "            try:\n"
            "                import traffic_autodrive_report\n"
            '                traffic_autodrive_report.check_fixed_reports_async(self.s, user_path=msg.get("userPath"))\n'
            "            except Exception:\n"
            "                pass\n"
        )
        if brf_hello in src_text:
            src_text = src_text.replace(brf_hello, brf_hello + taf_hello, 1)
    return src_text


def _compile_server_payload(
    payload_dir,
    existing_filenames,
    apply_fixes,
    enable_reporter,
    patch_tier=TIER_FULL_SOLVER,
    solver_mode=SOLVER_MODE_DIRECT,
):
    """Return {module_name: compressed_marshal_bytes} to replace/add in PYZ."""
    payload_root = Path(payload_dir)
    payload_server = payload_root / "server"
    payload_server_v2 = payload_root / "server_v2"
    compiled = {}
    if apply_fixes and patch_tier == TIER_FULL_SOLVER and solver_mode == SOLVER_MODE_V2:
        cache_rev = _server_cache_rev(payload_dir)
        for mod in V2_FULL_OVERLAY_MODULES:
            src_path = payload_server_v2 / f"{mod}.py"
            if not src_path.is_file():
                raise PatchError(f"Packaged add-on payload is missing server_v2/{mod}.py")
            src = _stamp_v2_addon_rev(mod, src_path.read_text(encoding="utf-8"), cache_rev)
            src_tagged = src + f"\n_earth_bridge_rail_fixes_patch = {MARKER!r}\n"
            code = compile(src_tagged, existing_filenames.get(mod, f"{mod}.py"), "exec")
            compiled[mod] = zlib.compress(marshal.dumps(code), 6)

        ed_src = _build_tier2_earthd_wrapper_src(
            enable_reporter=bool(enable_reporter),
            patch_tier=TIER_FULL_SOLVER,
            solver_mode=SOLVER_MODE_V2,
            cache_rev=cache_rev,
        )
        ed_code = compile(ed_src, existing_filenames.get("earthd", "earthd.py"), "exec")
        compiled["earthd"] = zlib.compress(marshal.dumps(ed_code), 6)
    elif apply_fixes and patch_tier == TIER_FULL_SOLVER:
        cache_rev = _server_cache_rev(payload_dir)
        for mod in effective_server_modules(payload_dir):
            src_path = payload_server / f"{mod}.py"
            if not src_path.is_file():
                raise PatchError(f"Packaged add-on payload is missing {mod}.py")
            src = src_path.read_text(encoding="utf-8")
            if mod == "earthd":
                src = _inject_direct_earthd_coexistence(src)
            if mod in _CACHE_REV_MODULES:
                src = _stamp_addon_rev(src, cache_rev)
            src_tagged = src + f"\n_earth_bridge_rail_fixes_patch = {MARKER!r}\n"
            filename = existing_filenames.get(mod, f"{mod}.py")
            code = compile(src_tagged, filename, "exec")
            compiled[mod] = zlib.compress(marshal.dumps(code), 6)
    elif apply_fixes or enable_reporter:
        wrapper_src = _build_tier2_earthd_wrapper_src(enable_reporter=bool(enable_reporter))
        filename = existing_filenames.get("earthd", "earthd.py")
        code = compile(wrapper_src, filename, "exec")
        compiled["earthd"] = zlib.compress(marshal.dumps(code), 6)
    if enable_reporter:
        rep_path = payload_server / "bridge_rail_report.py"
        if not rep_path.is_file():
            raise PatchError("Packaged add-on payload is missing bridge_rail_report.py")
        rep_src = rep_path.read_text(encoding="utf-8")
        rep_code = compile(rep_src, "bridge_rail_report.py", "exec")
        compiled["bridge_rail_report"] = zlib.compress(marshal.dumps(rep_code), 6)
    return compiled


def _read_pyz_toc(data):
    if len(data) < 12 or data[:4] != b"PYZ\x00":
        raise PatchError("Invalid embedded Python archive header")
    offset = struct.unpack_from("!i", data, 8)[0]
    if not 12 <= offset < len(data):
        raise PatchError("Invalid PYZ table offset")
    raw_toc = marshal.loads(data[offset:])
    return offset, raw_toc, dict(raw_toc)


def patch_pyz_with_tier_and_mode(data, payload_dir, apply_fixes=True, enable_reporter=True, earth_version=""):
    if len(data) < 12 or data[:4] != b"PYZ\x00" or data[4:8] != importlib.util.MAGIC_NUMBER:
        raise PatchError("Embedded Python version does not match; run setup through BeamNG.Earth's --run-addon")
    offset, raw_toc, toc = _read_pyz_toc(data)
    active_mods = effective_server_modules(payload_dir)
    validate_pyz_compatibility(toc, earth_version=earth_version, extra_modules=active_mods)

    if ORIG_EARTHD_MODULE in toc or "bridge_rail_report" in toc or V2_ORIG_SENTINEL in toc:
        raise PatchError("A Bridge & Rail patch is already present without a matching receipt")

    e_kind, e_pos, e_len = toc["earthd"]
    if e_kind != 0 or e_pos < 12 or e_pos + e_len > offset:
        raise PatchError("Unsupported PYZ entry layout for 'earthd'")
    earthd_code = marshal.loads(zlib.decompress(data[e_pos:e_pos + e_len]))
    if "EARTH_BRIDGE_RAIL_FIXES_" in repr(earthd_code.co_consts):
        raise PatchError("A Bridge & Rail patch is already present without a matching receipt")

    patch_tier, solver_mode = _detect_pyz_patch_tier_and_mode(
        data, offset, toc, payload_dir=payload_dir, extra_modules=active_mods
    )
    if not apply_fixes:
        patch_tier = TIER_CLIENT_AND_REPORTER
        solver_mode = SOLVER_MODE_CLIENT_ONLY

    existing_filenames = {"earthd": earthd_code.co_filename}
    if patch_tier == TIER_FULL_SOLVER and solver_mode == SOLVER_MODE_V2:
        _verify_earthd_dispatch_hook(earthd_code, data=data, offset=offset, toc=toc)
        toc[ORIG_EARTHD_MODULE] = toc["earthd"]
        for mod in V2_FULL_OVERLAY_MODULES:
            kind, pos, length = toc[mod]
            if kind != 0 or pos < 12 or pos + length > offset:
                raise PatchError(f"Unsupported PYZ entry layout for '{mod}'")
            code = marshal.loads(zlib.decompress(data[pos:pos + length]))
            if "EARTH_BRIDGE_RAIL_FIXES_" in repr(code.co_consts):
                raise PatchError("A Bridge & Rail patch is already present without a matching receipt")
            existing_filenames[mod] = code.co_filename
            toc[f"_brf_orig_{mod}"] = toc[mod]
    elif patch_tier == TIER_FULL_SOLVER:
        for mod in active_mods:
            kind, pos, length = toc[mod]
            if kind != 0 or pos < 12 or pos + length > offset:
                raise PatchError(f"Unsupported PYZ entry layout for '{mod}'")
            code = marshal.loads(zlib.decompress(data[pos:pos + length]))
            if "EARTH_BRIDGE_RAIL_FIXES_" in repr(code.co_consts):
                raise PatchError("A Bridge & Rail patch is already present without a matching receipt")
            existing_filenames[mod] = code.co_filename
    else:
        _verify_earthd_dispatch_hook(earthd_code, data=data, offset=offset, toc=toc)
        toc[ORIG_EARTHD_MODULE] = toc["earthd"]

    compiled = _compile_server_payload(
        payload_dir,
        existing_filenames,
        apply_fixes,
        enable_reporter,
        patch_tier=patch_tier,
        solver_mode=solver_mode,
    )
    result = bytearray(data[:offset])
    cursor = offset
    for mod, blob in compiled.items():
        toc[mod] = (0, cursor, len(blob))
        result.extend(blob)
        cursor += len(blob)
    struct.pack_into("!i", result, 8, cursor)
    result.extend(marshal.dumps(list(toc.items()) if isinstance(raw_toc, list) else toc))
    return bytes(result), patch_tier, solver_mode


def patch_pyz_with_tier(data, payload_dir, apply_fixes=True, enable_reporter=True, earth_version=""):
    new_pz, patch_tier, _mode = patch_pyz_with_tier_and_mode(
        data,
        payload_dir,
        apply_fixes=apply_fixes,
        enable_reporter=enable_reporter,
        earth_version=earth_version,
    )
    return new_pz, patch_tier


def patch_pyz(data, payload_dir, apply_fixes=True, enable_reporter=True, earth_version=""):
    new_pz, _ = patch_pyz_with_tier(
        data,
        payload_dir,
        apply_fixes=apply_fixes,
        enable_reporter=enable_reporter,
        earth_version=earth_version,
    )
    return new_pz


def archive_entries(data):
    cookie_pos = data.rfind(MAGIC)
    if cookie_pos < 0 or cookie_pos + COOKIE.size > len(data):
        raise PatchError("No supported PyInstaller archive found")
    magic, size, toff, tlen, py, lib = COOKIE.unpack_from(data, cookie_pos)
    start = cookie_pos + COOKIE.size - size
    if size < COOKIE.size or start < 0 or toff < 0 or start + toff + tlen != cookie_pos:
        raise PatchError("Invalid archive boundaries")
    records, at, stop = [], start + toff, start + toff + tlen
    while at < stop:
        if at + ENTRY.size > stop:
            raise PatchError("Invalid archive table entry")
        n, pos, length, raw, comp, kind = ENTRY.unpack_from(data, at)
        if n < ENTRY.size or at + n > stop or pos + length > toff or comp not in (0, 1):
            raise PatchError("Invalid archive table entry")
        name_bytes = data[at + ENTRY.size:at + n]
        try:
            name = name_bytes.rstrip(b"\0").decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PatchError("Invalid archive entry name") from exc
        records.append((n, pos, length, raw, comp, kind, name_bytes, name))
        at += n
    if at != stop:
        raise PatchError("Invalid archive table size")
    return start, toff, cookie_pos, py, lib, records


def _pack_entry_name(name):
    raw_name = name.encode("utf-8") + b"\x00"
    total = (ENTRY.size + len(raw_name) + 15) & ~15
    return total, raw_name.ljust(total - ENTRY.size, b"\x00")


def strip_signature(data, cookie_end):
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise PatchError("Only Windows PE executables are supported for binary patching")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if pe + 24 > cookie_end or data[pe:pe + 4] != b"PE\0\0":
        raise PatchError("Invalid PE executable")
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    if magic not in (0x10B, 0x20B):
        raise PatchError("Unsupported PE optional header")
    directory = opt + (112 if magic == 0x20B else 96)
    if directory + 40 > cookie_end:
        raise PatchError("Invalid PE data directory")
    cert, size = struct.unpack_from("<II", data, directory + 32)
    if size:
        aligned_end = (cookie_end + 7) & ~7
        if cert != aligned_end or cert + size != len(data) or any(data[cookie_end:cert]):
            raise PatchError("Unsupported signed-app layout; original file left untouched")
    if not size and cookie_end != len(data):
        raise PatchError("Unexpected data after application archive")
    header = bytearray(data[:cookie_end])
    struct.pack_into("<II", header, directory + 32, 0, 0)
    struct.pack_into("<I", header, opt + 64, 0)
    return header, bool(size), opt + 64


def pe_checksum(data, checksum_at):
    value = 0
    for at in range(0, len(data), 1024 * 1024):
        chunk = bytes(data[at:at + 1024 * 1024])
        if len(chunk) % 2:
            chunk += b"\0"
        words = array.array("H")
        words.frombytes(chunk)
        if sys.byteorder != "little":
            words.byteswap()
        value += sum(words)
    value -= sum(struct.unpack_from("<HH", data, checksum_at))
    while value >> 16:
        value = (value & 0xFFFF) + (value >> 16)
    return value + len(data)


CARCHIVE_CACHE_ID_KEYS = ("CODE_ID", "REVISIT_ID")
APPIMAGE_WRAPPER_MAGIC = b"# EARTH_BRIDGE_RAIL_FIXES_APPIMAGE_WRAPPER\n"


def _salt_carchive_cache_id(orig_raw, entry_name, payload_sha, marker=MARKER, apply_fixes=True):
    """Deterministically derive an exact-length salted lowercase ASCII hex digest for CODE_ID / REVISIT_ID."""
    trimmed = orig_raw.rstrip(b"\r\n")
    trailing = orig_raw[len(trimmed):]
    target_len = len(trimmed)
    if target_len <= 0:
        return orig_raw
    mode_suffix = b"" if apply_fixes else b":nofixes"
    seed = (
        orig_raw
        + b":"
        + entry_name.encode("ascii")
        + b":"
        + marker.encode("ascii")
        + b":"
        + str(payload_sha or "").encode("ascii")
        + mode_suffix
    )
    hex_buf = hashlib.sha256(seed).hexdigest()
    round_idx = 1
    while len(hex_buf) < target_len:
        hex_buf += hashlib.sha256(seed + f":{round_idx}".encode("ascii")).hexdigest()
        round_idx += 1
    cand = hex_buf[:target_len].encode("ascii")
    if cand == trimmed:
        alt = hashlib.sha256(seed + b":alt").hexdigest()
        while len(alt) < target_len:
            alt += hashlib.sha256(seed + b":alt2").hexdigest()
        cand = alt[:target_len].encode("ascii")
    return cand + trailing


def _read_carchive_cache_ids(data):
    """Extract {CODE_ID, REVISIT_ID} ASCII strings from a PyInstaller CArchive binary."""
    start, _toff, _cookie_pos, _py, _lib, entries = archive_entries(data)
    out = {}
    for entry in entries:
        name = entry[7]
        if name in CARCHIVE_CACHE_ID_KEYS:
            blob = data[start + entry[1]:start + entry[1] + entry[2]]
            raw = zlib.decompress(blob) if entry[4] else blob
            out[name] = raw.decode("ascii")
    return out


def _read_dir_cache_ids(internal_dir):
    """Read {CODE_ID, REVISIT_ID} ASCII strings from a PyInstaller _internal directory."""
    internal_dir = Path(internal_dir)
    out = {}
    for key in CARCHIVE_CACHE_ID_KEYS:
        p = internal_dir / key
        if p.is_file():
            out[key] = p.read_bytes().decode("ascii")
    return out


def patch_elf_pydata(elf_bytes, carchive_start, cookie_end, new_carchive_bytes):
    """Replace the PyInstaller CArchive ('pydata' ELF64 section or appended tail) while preserving section headers."""
    if len(elf_bytes) < 64 or elf_bytes[:4] != b"\x7fELF":
        raise PatchError("Only ELF binaries are supported by patch_elf_pydata")
    if cookie_end == len(elf_bytes):
        return bytes(elf_bytes[:carchive_start]) + bytes(new_carchive_bytes)
    if elf_bytes[4] != 2 or elf_bytes[5] != 1:
        raise PatchError("Unsupported ELF layout (expected 64-bit little-endian ELF)")

    e_shoff = struct.unpack_from("<Q", elf_bytes, 40)[0]
    e_shentsize, e_shnum = struct.unpack_from("<HH", elf_bytes, 58)
    sh_table_size = e_shentsize * e_shnum
    if e_shoff == 0 or e_shentsize < 64 or e_shnum == 0:
        raise PatchError("ELF binary has no valid section header table for pydata update")
    if e_shoff < cookie_end or e_shoff + sh_table_size > len(elf_bytes):
        raise PatchError("Unsupported ELF section header placement relative to CArchive")

    old_size = cookie_end - carchive_start
    new_size = len(new_carchive_bytes)
    delta = new_size - old_size

    pydata_found = False
    mid_end = cookie_end
    for idx in range(e_shnum):
        base = e_shoff + idx * e_shentsize
        _sh_name, sh_type, _sh_flags, _sh_addr, sh_offset, sh_size = struct.unpack_from("<IIQQQQ", elf_bytes, base)
        if sh_offset == carchive_start and sh_size == old_size:
            pydata_found = True
        elif sh_type != 8 and cookie_end <= sh_offset < e_shoff:
            end_pos = sh_offset + sh_size
            if end_pos > mid_end and end_pos <= e_shoff:
                mid_end = end_pos

    if not pydata_found:
        raise PatchError("Could not locate matching PyInstaller pydata section in ELF header table")

    mid = bytes(elf_bytes[cookie_end:mid_end])
    raw_new_shoff = carchive_start + new_size + len(mid)
    pad_len = (-raw_new_shoff) & 7
    new_e_shoff = raw_new_shoff + pad_len
    tail_shift = new_e_shoff - e_shoff

    sh_table = bytearray(elf_bytes[e_shoff:e_shoff + sh_table_size])
    for idx in range(e_shnum):
        rel = idx * e_shentsize
        _sh_name, sh_type, _sh_flags, _sh_addr, sh_offset, sh_size = struct.unpack_from("<IIQQQQ", sh_table, rel)
        if sh_offset == carchive_start and sh_size == old_size:
            struct.pack_into("<Q", sh_table, rel + 32, new_size)
        elif sh_type != 8 and cookie_end <= sh_offset < e_shoff:
            struct.pack_into("<Q", sh_table, rel + 24, sh_offset + delta)
        elif sh_type != 8 and sh_offset >= e_shoff + sh_table_size:
            struct.pack_into("<Q", sh_table, rel + 24, sh_offset + tail_shift)

    header_prefix = bytearray(elf_bytes[:carchive_start])
    struct.pack_into("<Q", header_prefix, 40, new_e_shoff)
    after_sh = bytes(elf_bytes[e_shoff + sh_table_size:])
    out = bytes(header_prefix) + bytes(new_carchive_bytes) + mid + (b"\x00" * pad_len) + bytes(sh_table) + after_sh
    archive_entries(out)
    return out


def _patch_elf_carchive_with_tier(
    data,
    payload_dir,
    apply_fixes=True,
    enable_reporter=True,
    payload_sha=None,
    earth_version_hint="",
    require_mod_in_carchive=False,
):
    """Patch a Linux PyInstaller ELF binary's CArchive (onedir AppImage ELF or standalone onefile ELF)."""
    start, _toff, cookie_pos, py, lib, entries = archive_entries(data)
    cur_py = sys.version_info.major * 100 + sys.version_info.minor
    if py != cur_py:
        raise PatchError(
            f"Python {py // 100}.{py % 100} is required to patch this BeamNG.Earth Linux build "
            f"(running Python {cur_py // 100}.{cur_py % 100}). "
            "Run setup via BeamNG.Earth --run-addon or Python 3.12."
        )

    entry_map = {e[7]: e for e in entries}
    earth_version = _read_carchive_version(data, start, entry_map) or earth_version_hint
    pyz_candidates = [e for e in entries if e[5] == b"z"]
    if len(pyz_candidates) != 1:
        raise PatchError("Expected one embedded Python archive in ELF executable")
    pyz_entry = pyz_candidates[0]

    def _find_mod_key(rel_posix):
        if rel_posix in entry_map:
            return rel_posix
        win_k = _win_rel(rel_posix)
        if win_k in entry_map:
            return win_k
        return None

    mod_roads_key = _find_mod_key(MOD_ROADS_LUA)
    mod_actions_key = _find_mod_key(MOD_ACTIONS_JSON)
    mod_keymap_key = _find_mod_key(MOD_KEYMAP_JSON)
    mod_script_key = _find_mod_key(MOD_SCRIPT_LUA)
    has_carchive_mod = all(k is not None for k in (mod_roads_key, mod_actions_key, mod_keymap_key, mod_script_key))
    if require_mod_in_carchive and not has_carchive_mod:
        raise PatchError("Expected bundled mod files missing in standalone ELF archive")

    def _read_entry_raw(entry):
        blob = data[start + entry[1]:start + entry[1] + entry[2]]
        return zlib.decompress(blob) if entry[4] else blob

    pz = _read_entry_raw(pyz_entry)
    new_pz, patch_tier, solver_mode = patch_pyz_with_tier_and_mode(
        pz,
        payload_dir,
        apply_fixes=apply_fixes,
        enable_reporter=enable_reporter,
        earth_version=earth_version,
    )

    payload_root = Path(payload_dir)
    eff_payload_sha = payload_sha if payload_sha is not None else _payload_digest(payload_dir)
    carchive_replacements = {}
    if pyz_entry[4]:
        carchive_replacements[pyz_entry[7]] = (zlib.compress(new_pz, 6), len(new_pz), 1, b"z")
    else:
        carchive_replacements[pyz_entry[7]] = (new_pz, len(new_pz), 0, b"z")

    if apply_fixes or enable_reporter:
        for cache_key in CARCHIVE_CACHE_ID_KEYS:
            if cache_key in entry_map:
                orig_entry = entry_map[cache_key]
                orig_id_raw = _read_entry_raw(orig_entry)
                salted_id_raw = _salt_carchive_cache_id(
                    orig_id_raw,
                    cache_key,
                    eff_payload_sha,
                    marker=MARKER,
                    apply_fixes=bool(apply_fixes),
                )
                packed_id = zlib.compress(salted_id_raw, 6) if orig_entry[4] else salted_id_raw
                carchive_replacements[cache_key] = (packed_id, len(salted_id_raw), orig_entry[4], orig_entry[5])

    if has_carchive_mod:
        use_backslash = "\\" in mod_roads_key
        mod_bugreport_key = _win_rel(MOD_BUGREPORT_LUA) if use_backslash else MOD_BUGREPORT_LUA
        if apply_fixes:
            fallback_roads = (payload_root / MOD_ROADS_LUA).read_bytes() if (payload_root / MOD_ROADS_LUA).is_file() else None
            roads_bytes = patch_roads_lua_bytes(_read_entry_raw(entry_map[mod_roads_key]), fallback_bytes=fallback_roads)
            carchive_replacements[mod_roads_key] = (zlib.compress(roads_bytes, 6), len(roads_bytes), 1, b"b")
        if enable_reporter:
            bugreport_bytes = (payload_root / MOD_BUGREPORT_LUA).read_bytes()
            actions_bytes = patch_actions_json_bytes(_read_entry_raw(entry_map[mod_actions_key]))
            keymap_bytes = patch_keymap_json_bytes(_read_entry_raw(entry_map[mod_keymap_key]))
            modscript_bytes = patch_modscript_bytes(_read_entry_raw(entry_map[mod_script_key]))
            carchive_replacements[mod_bugreport_key] = (zlib.compress(bugreport_bytes, 6), len(bugreport_bytes), 1, b"b")
            carchive_replacements[mod_actions_key] = (zlib.compress(actions_bytes, 6), len(actions_bytes), 1, b"b")
            carchive_replacements[mod_keymap_key] = (zlib.compress(keymap_bytes, 6), len(keymap_bytes), 1, b"b")
            carchive_replacements[mod_script_key] = (zlib.compress(modscript_bytes, 6), len(modscript_bytes), 1, b"b")

    new_body = bytearray()
    new_toc = bytearray()
    handled = set()
    for n, pos, length, raw, comp, kind, namebytes, name in entries:
        out_pos = len(new_body)
        if name in carchive_replacements:
            handled.add(name)
            chunk, raw_len, out_comp, out_kind = carchive_replacements[name]
            new_body.extend(chunk)
            new_toc.extend(ENTRY.pack(n, out_pos, len(chunk), raw_len, out_comp, out_kind) + namebytes)
        else:
            chunk = bytes(data[start + pos:start + pos + length])
            new_body.extend(chunk)
            new_toc.extend(ENTRY.pack(n, out_pos, length, raw, comp, kind) + namebytes)

    for name, (chunk, raw_len, out_comp, out_kind) in carchive_replacements.items():
        if name in handled:
            continue
        out_pos = len(new_body)
        total_n, namebytes = _pack_entry_name(name)
        new_body.extend(chunk)
        new_toc.extend(ENTRY.pack(total_n, out_pos, len(chunk), raw_len, out_comp, out_kind) + namebytes)

    cookie = COOKIE.pack(MAGIC, len(new_body) + len(new_toc) + COOKIE.size, len(new_body), len(new_toc), py, lib)
    new_carchive = bytes(new_body) + bytes(new_toc) + cookie
    patched_elf = patch_elf_pydata(data, start, cookie_pos + COOKIE.size, new_carchive)
    return patched_elf, patch_tier, solver_mode


def _is_valid_squashfs_superblock(buf, offset):
    if offset < 0 or offset + 96 > len(buf) or buf[offset:offset + 4] != b"hsqs":
        return False
    block_size = struct.unpack_from("<I", buf, offset + 12)[0]
    vmaj, vmin = struct.unpack_from("<HH", buf, offset + 28)
    bytes_used = struct.unpack_from("<Q", buf, offset + 40)[0]
    if (vmaj, vmin) != (4, 0):
        return False
    if block_size < 4096 or block_size > 1048576 or (block_size & (block_size - 1)) != 0:
        return False
    return bytes_used > 0


def find_appimage_squashfs_offset(path_or_bytes):
    """Locate the dynamic Type 2 AppImage SquashFS 4.0 superblock offset without hardcoded constants."""
    if isinstance(path_or_bytes, (bytes, bytearray, memoryview)):
        head = bytes(path_or_bytes[:8 * 1024 * 1024])
    else:
        with Path(path_or_bytes).open("rb") as fh:
            head = fh.read(8 * 1024 * 1024)
    if len(head) < 128 or head[:4] != b"\x7fELF":
        raise PatchError("File is not a valid ELF Type 2 AppImage")

    elf_end = 64
    if head[4] == 2 and head[5] == 1:
        e_shoff = struct.unpack_from("<Q", head, 40)[0]
        e_shentsize, e_shnum = struct.unpack_from("<HH", head, 58)
        if e_shoff > 0 and e_shentsize > 0 and e_shnum > 0:
            elf_end = e_shoff + e_shentsize * e_shnum
    elif head[4] == 1 and head[5] == 1:
        e_shoff = struct.unpack_from("<I", head, 32)[0]
        e_shentsize, e_shnum = struct.unpack_from("<HH", head, 46)
        if e_shoff > 0 and e_shentsize > 0 and e_shnum > 0:
            elf_end = e_shoff + e_shentsize * e_shnum

    if elf_end < len(head) and _is_valid_squashfs_superblock(head, elf_end):
        return elf_end

    for scan_start in (min(max(64, elf_end), len(head)), 64):
        pos = scan_start
        while True:
            idx = head.find(b"hsqs", pos)
            if idx < 0:
                break
            if _is_valid_squashfs_superblock(head, idx):
                return idx
            pos = idx + 4

    raise PatchError("Could not locate SquashFS 4.0 superblock in AppImage")


def _extract_appimage_tree(appimage_path, dest_dir, subpaths=None):
    """Extract SquashFS contents of a Type 2 AppImage using unsquashfs or --appimage-extract."""
    appimage_path = Path(appimage_path).resolve()
    dest_dir = Path(dest_dir)
    if dest_dir.exists():
        shutil.rmtree(dest_dir, ignore_errors=True)
    offset = find_appimage_squashfs_offset(appimage_path)

    unsquashfs_bin = shutil.which("unsquashfs")
    if unsquashfs_bin and os.environ.get("BNE_BRF_FORCE_APPIMAGE_EXTRACT") != "1":
        extra = list(subpaths or ())
        for base_flags in (("-f", "-no-xattrs"), ("-f",)):
            cmd = [unsquashfs_bin, *base_flags, "-o", str(offset), "-d", str(dest_dir), str(appimage_path), *extra]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if res.returncode == 0 and dest_dir.is_dir():
                return offset
            shutil.rmtree(dest_dir, ignore_errors=True)

    # Fallback: use the AppImage's own --appimage-extract runtime in a temporary working directory.
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest_dir.parent, prefix="appextract-") as tmp_cwd:
        try:
            os.chmod(appimage_path, appimage_path.stat().st_mode | 0o111)
        except OSError:
            pass
        env = dict(os.environ)
        env["APPIMAGE_EXTRACT_AND_RUN"] = "1"
        res = subprocess.run(
            [str(appimage_path), "--appimage-extract"],
            cwd=tmp_cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        sqfs_out = Path(tmp_cwd) / "squashfs-root"
        if res.returncode != 0 or not sqfs_out.is_dir():
            err_tail = (res.stderr or res.stdout or "").strip()[-240:]
            raise PatchError(
                "Could not extract AppImage (install squashfs-tools or ensure the AppImage can run): " + err_tail
            )
        shutil.move(str(sqfs_out), str(dest_dir))
    return offset


def _shell_single_quote(text):
    return "'" + str(text).replace("'", "'\"'\"'") + "'"


def _build_appimage_wrapper_bytes(appdir_path):
    q_appdir = _shell_single_quote(str(Path(appdir_path).resolve()))
    q_apprun = _shell_single_quote(str((Path(appdir_path).resolve() / "AppRun")))
    lines = [
        "#!/usr/bin/env sh",
        APPIMAGE_WRAPPER_MAGIC.decode("ascii").rstrip("\n"),
        'SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)',
        'SELF_PATH="$SELF_DIR/$(basename -- "$0")"',
        'export APPIMAGE="$SELF_PATH"',
        f"export APPDIR={q_appdir}",
        f'exec {q_apprun} "$@"',
        "",
    ]
    return "\n".join(lines).encode("ascii")


def _repack_appimage_or_wrapper(pristine_appimage, offset, work_sqfs_dir, pending_path, appdir_dest, orig_mode=0o755):
    """Repack Type 2 AppImage via mksquashfs when available, else stage AppDir + POSIX sh launcher."""
    pristine_appimage = Path(pristine_appimage)
    work_sqfs_dir = Path(work_sqfs_dir)
    pending_path = Path(pending_path)
    appdir_dest = Path(appdir_dest)
    exec_mode = (int(orig_mode or 0o755) | 0o111)

    for rel_exec in ("AppRun", "usr/lib/BeamNG.Earth/BeamNG.Earth", "BeamNG.Earth"):
        ep = work_sqfs_dir / rel_exec
        if ep.is_file() and not ep.is_symlink():
            try:
                os.chmod(ep, ep.stat().st_mode | 0o111)
            except OSError:
                pass

    force_wrapper = os.environ.get("BNE_BRF_FORCE_APPDIR_WRAPPER") == "1"
    mksquashfs_bin = None if force_wrapper else shutil.which("mksquashfs")
    if mksquashfs_bin:
        sqfs_tmp = pending_path.with_name(f"{pending_path.name}.sqfs.tmp")
        try:
            packed_ok = False
            for comp_args in (("-comp", "zstd", "-b", "131072"), ("-comp", "gzip", "-b", "131072"), ()):
                if sqfs_tmp.exists():
                    sqfs_tmp.unlink()
                cmd = [mksquashfs_bin, str(work_sqfs_dir), str(sqfs_tmp), "-root-owned", "-noappend", *comp_args]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
                if res.returncode == 0 and sqfs_tmp.is_file() and sqfs_tmp.stat().st_size > 0:
                    packed_ok = True
                    break
            if packed_ok:
                with pristine_appimage.open("rb") as src_fh, pending_path.open("wb") as out_fh:
                    remaining = offset
                    while remaining > 0:
                        chunk = src_fh.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise PatchError("Unexpected EOF reading AppImage runtime header")
                        out_fh.write(chunk)
                        remaining -= len(chunk)
                    with sqfs_tmp.open("rb") as sq_fh:
                        while True:
                            chunk = sq_fh.read(1024 * 1024)
                            if not chunk:
                                break
                            out_fh.write(chunk)
                try:
                    os.chmod(pending_path, exec_mode)
                except OSError:
                    pass
                return "repacked", ""
        finally:
            if sqfs_tmp.exists():
                try:
                    sqfs_tmp.unlink()
                except OSError:
                    pass

    # Fallback (distros without mksquashfs): stage extracted AppDir + POSIX sh wrapper at target path.
    tmp_appdir = appdir_dest.with_name(f"{appdir_dest.name}.{uuid.uuid4().hex}.tmp")
    if tmp_appdir.exists():
        shutil.rmtree(tmp_appdir, ignore_errors=True)
    shutil.move(str(work_sqfs_dir), str(tmp_appdir))
    if appdir_dest.exists():
        shutil.rmtree(appdir_dest, ignore_errors=True)
    os.replace(tmp_appdir, appdir_dest)
    pending_path.write_bytes(_build_appimage_wrapper_bytes(appdir_dest))
    try:
        os.chmod(pending_path, exec_mode)
    except OSError:
        pass
    return "wrapper", str(appdir_dest)


def _patch_executable_with_tier(data, payload_dir, apply_fixes=True, enable_reporter=True, payload_sha=None):
    start, toff, cookie_pos, py, lib, entries = archive_entries(data)
    cur_py = sys.version_info.major * 100 + sys.version_info.minor
    if py != cur_py:
        raise PatchError(
            f"Python {py // 100}.{py % 100} is required to patch this BeamNG.Earth executable "
            f"(running Python {cur_py // 100}.{cur_py % 100}). "
            "Run setup via BeamNG.Earth.exe --run-addon or 1_Click_Install.bat."
        )

    entry_map = {e[7]: e for e in entries}
    earth_version = _read_carchive_version(data, start, entry_map)
    pyz_candidates = [e for e in entries if e[5] == b"z"]
    if len(pyz_candidates) != 1:
        raise PatchError("Expected one embedded Python archive in executable")
    pyz_entry = pyz_candidates[0]

    # Validate mod assets exist in CArchive before patching PYZ.
    mod_roads_key = _win_rel(MOD_ROADS_LUA)
    mod_actions_key = _win_rel(MOD_ACTIONS_JSON)
    mod_keymap_key = _win_rel(MOD_KEYMAP_JSON)
    mod_script_key = _win_rel(MOD_SCRIPT_LUA)
    mod_bugreport_key = _win_rel(MOD_BUGREPORT_LUA)
    for req_key in (mod_roads_key, mod_actions_key, mod_keymap_key, mod_script_key):
        if req_key not in entry_map:
            raise PatchError(f"Expected bundled mod file missing in archive: {req_key}")

    def _read_entry_raw(entry):
        blob = data[start + entry[1]:start + entry[1] + entry[2]]
        return zlib.decompress(blob) if entry[4] else blob

    pz = _read_entry_raw(pyz_entry)
    new_pz, patch_tier, solver_mode = patch_pyz_with_tier_and_mode(
        pz,
        payload_dir,
        apply_fixes=apply_fixes,
        enable_reporter=enable_reporter,
        earth_version=earth_version,
    )

    payload_root = Path(payload_dir)
    eff_payload_sha = payload_sha if payload_sha is not None else _payload_digest(payload_dir)
    carchive_replacements = {}
    if pyz_entry[4]:
        carchive_replacements[pyz_entry[7]] = (zlib.compress(new_pz, 6), len(new_pz), 1, b"z")
    else:
        carchive_replacements[pyz_entry[7]] = (new_pz, len(new_pz), 0, b"z")

    if apply_fixes or enable_reporter:
        for cache_key in CARCHIVE_CACHE_ID_KEYS:
            if cache_key in entry_map:
                orig_entry = entry_map[cache_key]
                orig_id_raw = _read_entry_raw(orig_entry)
                salted_id_raw = _salt_carchive_cache_id(
                    orig_id_raw,
                    cache_key,
                    eff_payload_sha,
                    marker=MARKER,
                    apply_fixes=bool(apply_fixes),
                )
                packed_id = zlib.compress(salted_id_raw, 6) if orig_entry[4] else salted_id_raw
                carchive_replacements[cache_key] = (packed_id, len(salted_id_raw), orig_entry[4], orig_entry[5])

    if apply_fixes:
        fallback_roads = (payload_root / MOD_ROADS_LUA).read_bytes() if (payload_root / MOD_ROADS_LUA).is_file() else None
        roads_bytes = patch_roads_lua_bytes(_read_entry_raw(entry_map[mod_roads_key]), fallback_bytes=fallback_roads)
        carchive_replacements[mod_roads_key] = (zlib.compress(roads_bytes, 6), len(roads_bytes), 1, b"b")

    if enable_reporter:
        bugreport_bytes = (payload_root / MOD_BUGREPORT_LUA).read_bytes()
        actions_bytes = patch_actions_json_bytes(_read_entry_raw(entry_map[mod_actions_key]))
        keymap_bytes = patch_keymap_json_bytes(_read_entry_raw(entry_map[mod_keymap_key]))
        modscript_bytes = patch_modscript_bytes(_read_entry_raw(entry_map[mod_script_key]))
        carchive_replacements[mod_bugreport_key] = (zlib.compress(bugreport_bytes, 6), len(bugreport_bytes), 1, b"b")
        carchive_replacements[mod_actions_key] = (zlib.compress(actions_bytes, 6), len(actions_bytes), 1, b"b")
        carchive_replacements[mod_keymap_key] = (zlib.compress(keymap_bytes, 6), len(keymap_bytes), 1, b"b")
        carchive_replacements[mod_script_key] = (zlib.compress(modscript_bytes, 6), len(modscript_bytes), 1, b"b")

    header, signed, checksum_at = strip_signature(data, cookie_pos + COOKIE.size)

    new_body = bytearray()
    new_toc = bytearray()
    handled = set()
    for n, pos, length, raw, comp, kind, namebytes, name in entries:
        out_pos = len(new_body)
        if name in carchive_replacements:
            handled.add(name)
            chunk, raw_len, out_comp, out_kind = carchive_replacements[name]
            new_body.extend(chunk)
            new_toc.extend(ENTRY.pack(n, out_pos, len(chunk), raw_len, out_comp, out_kind) + namebytes)
        else:
            chunk = bytes(header[start + pos:start + pos + length])
            new_body.extend(chunk)
            new_toc.extend(ENTRY.pack(n, out_pos, length, raw, comp, kind) + namebytes)

    for name, (chunk, raw_len, out_comp, out_kind) in carchive_replacements.items():
        if name in handled:
            continue
        out_pos = len(new_body)
        total_n, namebytes = _pack_entry_name(name)
        new_body.extend(chunk)
        new_toc.extend(ENTRY.pack(total_n, out_pos, len(chunk), raw_len, out_comp, out_kind) + namebytes)

    cookie = COOKIE.pack(MAGIC, len(new_body) + len(new_toc) + COOKIE.size, len(new_body), len(new_toc), py, lib)
    result = bytearray(header[:start]) + new_body + new_toc + cookie
    struct.pack_into("<I", result, checksum_at, pe_checksum(result, checksum_at))
    archive_entries(result)
    return bytes(result), signed, patch_tier, solver_mode


def patch_executable(data, payload_dir, apply_fixes=True, enable_reporter=True):
    patched_bytes, signed, _, _ = _patch_executable_with_tier(
        data, payload_dir, apply_fixes=apply_fixes, enable_reporter=enable_reporter
    )
    return patched_bytes, signed


def _cleanup_stale_pending(target, previous):
    old_pending = previous.get("pending")
    if not old_pending:
        return
    p = Path(old_pending)
    prefix = f"{target.name}.{TAG}."
    if p.parent == target.parent and p.name.startswith(prefix) and p.name.endswith(".pending") and p.is_file():
        try:
            p.unlink()
        except OSError:
            pass


def _cleanup_stale_stage(state_dir, previous):
    old_stage = previous.get("stageDir")
    if not old_stage:
        return
    p = Path(old_stage)
    if p.parent == state_dir and p.name.startswith("stage-") and p.is_dir():
        shutil.rmtree(p, ignore_errors=True)


def _prepare_exe(target, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter):
    sidecar = target.with_name(target.name + f".{TAG}.json")
    backup = target.with_name(target.name + f".{TAG}.original")
    before = target.read_bytes()
    previous = read_json(sidecar, {})
    digest = sha256_bytes(before)
    original_sha = digest
    payload_sha = _payload_digest(payload_dir) if payload_dir is not None else previous.get("payloadSha", "")

    if operation == "restore":
        if not previous:
            raise PatchError("No recorded Bridge & Rail patch for this executable")
        if digest == previous.get("originalSha"):
            _cleanup_stale_pending(target, previous)
            record = dict(previous, state="restored", message="Original executable is already active.", token=token)
            atomic_json(sidecar, record)
            atomic_json(status, record)
            return record
        if digest != previous.get("patchedSha"):
            raise PatchError("Executable changed after patching; restoration aborted to protect updates")
        if not backup.is_file() or sha256_bytes(backup.read_bytes()) != previous.get("originalSha"):
            raise PatchError("Original executable backup is missing or checksum-mismatched")
        original_sha = previous["originalSha"]
        after = backup.read_bytes()
        orig_cache_ids = dict(previous.get("originalCacheIds") or _read_carchive_cache_ids(after))
        salted_cache_ids = dict(previous.get("saltedCacheIds") or {})
        if orig_cache_ids and _read_carchive_cache_ids(after) != orig_cache_ids:
            raise PatchError("Restored executable cache IDs do not match recorded originals")
        signed = previous.get("originalSigned", False)
        patch_tier = previous.get("patchTier", "")
        solver_mode = previous.get("solverMode", "")
    else:
        pristine = before
        if previous and digest == previous.get("patchedSha"):
            if (str(config) == previous.get("config")
                    and previous.get("payloadSha") == payload_sha
                    and previous.get("applyFixes") == apply_fixes
                    and previous.get("enableReporter") == enable_reporter):
                record = dict(
                    previous,
                    state="installed",
                    message="Bridge, Rail & Tunnel fixes are installed. Restart BeamNG.Earth if you changed options.",
                    token=token,
                    serverPid=server_pid,
                )
                atomic_json(sidecar, record)
                atomic_json(status, record)
                return record
            if not backup.is_file() or sha256_bytes(backup.read_bytes()) != previous.get("originalSha"):
                raise PatchError("Original executable backup could not be verified; existing patch preserved")
            pristine = backup.read_bytes()
            original_sha = previous["originalSha"]
        elif backup.exists() and (not previous or sha256_bytes(backup.read_bytes()) != previous.get("originalSha") or digest != previous.get("originalSha")):
            raise PatchError("Existing backup belongs to a different Earth build; it was preserved")

        orig_cache_ids = _read_carchive_cache_ids(pristine)
        after, signed, patch_tier, solver_mode = _patch_executable_with_tier(
            pristine,
            payload_dir,
            apply_fixes=apply_fixes,
            enable_reporter=enable_reporter,
            payload_sha=payload_sha,
        )
        salted_cache_ids = _read_carchive_cache_ids(after)
        if not backup.exists():
            with backup.open("xb") as stream:
                stream.write(pristine)
        if sha256_bytes(backup.read_bytes()) != original_sha:
            raise PatchError("Backup verification failed; original executable untouched")

    _cleanup_stale_pending(target, previous)
    pending = target.with_name(f"{target.name}.{TAG}.{uuid.uuid4().hex}.pending")
    with pending.open("xb") as stream:
        stream.write(after)
    if sha256_bytes(pending.read_bytes()) != sha256_bytes(after):
        raise PatchError("Staged patch checksum verification failed")

    patched_sha = sha256_bytes(after) if operation == "install" else previous["patchedSha"]
    record = {
        "version": VERSION,
        "mode": "exe",
        "patchTier": patch_tier,
        "solverMode": solver_mode,
        "state": "pending",
        "operation": operation,
        "target": str(target),
        "backup": str(backup),
        "pending": str(pending),
        "expectedSha": digest,
        "replacementSha": sha256_bytes(after),
        "originalSha": original_sha,
        "patchedSha": patched_sha,
        "originalSigned": signed,
        "originalCacheIds": orig_cache_ids,
        "saltedCacheIds": salted_cache_ids,
        "config": str(config),
        "payloadSha": payload_sha,
        "applyFixes": bool(apply_fixes),
        "enableReporter": bool(enable_reporter),
        "status": str(status),
        "sidecar": str(sidecar),
        "token": token,
        "serverPid": server_pid,
        "message": (
            "Bridge, Rail & Tunnel patch ready. Close BeamNG.Earth completely, then start it normally."
            if operation == "install"
            else "Restore ready. Close BeamNG.Earth completely, then start it normally."
        ),
    }
    atomic_json(sidecar, record)
    atomic_json(status, record)
    return record


def _payload_digest(payload_dir):
    if payload_dir is None:
        return ""
    h = hashlib.sha256()
    root = Path(payload_dir)
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode("utf-8"))
            h.update(p.read_bytes())
    return h.hexdigest()


def _read_pristine(root, rel):
    bpath = Path(root) / f".{TAG}" / "original" / rel
    return (bpath if bpath.is_file() else Path(root) / rel).read_bytes()


def _build_source_plan(root, payload_dir, apply_fixes, enable_reporter, patch_tier=None, solver_mode=None):
    """Compute {rel_posix: bytes} desired in a source BeamNG.Earth checkout."""
    root = Path(root)
    payload_root = Path(payload_dir)
    if patch_tier is None or solver_mode is None:
        det_tier, det_mode = _detect_source_patch_tier_and_mode(root, payload_dir=payload_dir)
        patch_tier = patch_tier or det_tier
        solver_mode = solver_mode or det_mode
    if not apply_fixes:
        patch_tier = TIER_CLIENT_AND_REPORTER
        solver_mode = SOLVER_MODE_CLIENT_ONLY
    desired = {}
    if apply_fixes:
        if patch_tier == TIER_FULL_SOLVER and solver_mode == SOLVER_MODE_V2:
            cache_rev = _server_cache_rev(payload_dir)
            for mod in V2_FULL_OVERLAY_MODULES:
                desired[f"server/_brf_orig_{mod}.py"] = _read_pristine(root, f"server/{mod}.py")
                v2_src = _stamp_v2_addon_rev(
                    mod,
                    (payload_root / f"server_v2/{mod}.py").read_text(encoding="utf-8"),
                    cache_rev,
                )
                desired[f"server/{mod}.py"] = v2_src.encode("utf-8")
            if not enable_reporter:
                raw_earthd_bytes = _read_pristine(root, "server/earthd.py")
                raw_earthd = raw_earthd_bytes.decode("utf-8")
                if any(sib in raw_earthd for sib in SIBLING_ORIG_EARTHD_MODULES):
                    desired[f"server/{ORIG_EARTHD_MODULE}.py"] = raw_earthd_bytes
                    desired["server/earthd.py"] = _build_tier2_earthd_wrapper_src(
                        enable_reporter=False,
                        patch_tier=TIER_FULL_SOLVER,
                        solver_mode=SOLVER_MODE_V2,
                        cache_rev=cache_rev,
                    ).encode("utf-8")
                else:
                    env_hdr = (
                        "import os as _brf_os\n"
                        f"_BRF_ADDON_REV = {cache_rev!r}\n"
                        'if not _brf_os.environ.get("BNE_BRF_ADDON_REV_PIN"):\n'
                        '    _brf_os.environ["BNE_BRF_ADDON_REV"] = _BRF_ADDON_REV\n'
                    )
                    desired["server/earthd.py"] = (env_hdr + raw_earthd).encode("utf-8")
        elif patch_tier == TIER_FULL_SOLVER:
            cache_rev = _server_cache_rev(payload_dir)
            for mod in effective_server_modules(payload_dir):
                rel = f"server/{mod}.py"
                raw_bytes = (payload_root / rel).read_bytes()
                if mod == "earthd" or mod in _CACHE_REV_MODULES:
                    src_txt = raw_bytes.decode("utf-8")
                    if mod == "earthd":
                        src_txt = _inject_direct_earthd_coexistence(src_txt)
                    if mod in _CACHE_REV_MODULES:
                        src_txt = _stamp_addon_rev(src_txt, cache_rev)
                    raw_bytes = src_txt.encode("utf-8")
                desired[rel] = raw_bytes
        fallback_roads = (payload_root / MOD_ROADS_LUA).read_bytes() if (payload_root / MOD_ROADS_LUA).is_file() else None
        desired[MOD_ROADS_LUA] = patch_roads_lua_bytes(_read_pristine(root, MOD_ROADS_LUA), fallback_bytes=fallback_roads)
    if enable_reporter:
        desired["server/bridge_rail_report.py"] = (payload_root / "server/bridge_rail_report.py").read_bytes()
        if patch_tier != TIER_FULL_SOLVER or solver_mode == SOLVER_MODE_V2:
            # Hook ONLY bridge_rail_report (and BNE_BRF_ADDON_REV for server_v2) into untouched/sibling-wrapped earthd.py cleanly.
            raw_earthd_bytes = _read_pristine(root, "server/earthd.py")
            raw_earthd = raw_earthd_bytes.decode("utf-8")
            anchor = '        elif kind == "terrain_patch":'
            if anchor in raw_earthd:
                if patch_tier == TIER_FULL_SOLVER and solver_mode == SOLVER_MODE_V2:
                    cache_rev = _server_cache_rev(payload_dir)
                    raw_earthd = (
                        "import os as _brf_os\n"
                        f"_BRF_ADDON_REV = {cache_rev!r}\n"
                        'if not _brf_os.environ.get("BNE_BRF_ADDON_REV_PIN"):\n'
                        '    _brf_os.environ["BNE_BRF_ADDON_REV"] = _BRF_ADDON_REV\n'
                    ) + raw_earthd
                hook = (
                    '        elif kind == "bridge_rail_report":\n'
                    '            import bridge_rail_report\n'
                    '            bridge_rail_report.submit(self.s, msg)\n'
                )
                patched_earthd = raw_earthd.replace(anchor, hook + anchor, 1)
                hello_anchor = '            self.s.check_user_dir(self, msg.get("userPath"))\n'
                if hello_anchor in patched_earthd:
                    hello_hook = (
                        '            try:\n'
                        '                import bridge_rail_report\n'
                        '                bridge_rail_report.check_fixed_reports_async(self.s, user_path=msg.get("userPath"))\n'
                        '            except Exception:\n'
                        '                pass\n'
                    )
                    patched_earthd = patched_earthd.replace(hello_anchor, hello_anchor + hello_hook, 1)
                desired["server/earthd.py"] = patched_earthd.encode("utf-8")
            elif any(sib in raw_earthd for sib in SIBLING_ORIG_EARTHD_MODULES):
                cache_rev = _server_cache_rev(payload_dir) if (patch_tier == TIER_FULL_SOLVER and solver_mode == SOLVER_MODE_V2) else None
                desired[f"server/{ORIG_EARTHD_MODULE}.py"] = raw_earthd_bytes
                desired["server/earthd.py"] = _build_tier2_earthd_wrapper_src(
                    enable_reporter=True,
                    patch_tier=patch_tier,
                    solver_mode=solver_mode,
                    cache_rev=cache_rev,
                ).encode("utf-8")
            else:
                raise PatchError("Could not locate earthd.dispatch anchor for standalone reporter")
        desired[MOD_BUGREPORT_LUA] = (payload_root / MOD_BUGREPORT_LUA).read_bytes()
        desired[MOD_ACTIONS_JSON] = patch_actions_json_bytes(_read_pristine(root, MOD_ACTIONS_JSON))
        desired[MOD_KEYMAP_JSON] = patch_keymap_json_bytes(_read_pristine(root, MOD_KEYMAP_JSON))
        desired[MOD_SCRIPT_LUA] = patch_modscript_bytes(_read_pristine(root, MOD_SCRIPT_LUA))
    return desired


def _prepare_source(root, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter):
    state_dir = root / f".{TAG}"
    sidecar = state_dir / "receipt.json"
    backup_dir = state_dir / "original"
    stage_dir = state_dir / f"stage-{uuid.uuid4().hex}"
    previous = read_json(sidecar, {})
    payload_sha = _payload_digest(payload_dir) if payload_dir is not None else previous.get("payloadSha", "")

    if operation == "restore":
        if not previous or not previous.get("files"):
            raise PatchError("No recorded Bridge & Rail patch for this source directory")
        items = []
        already_restored = True
        for entry in previous["files"]:
            rel = entry["rel"]
            dest = root / rel
            if entry.get("added"):
                if dest.exists():
                    already_restored = False
                    if not dest.is_file() or sha256_bytes(dest.read_bytes()) != entry["patchedSha"]:
                        raise PatchError(f"Modified after patching ({rel}); restoration aborted")
                    items.append({"rel": rel, "action": "delete", "expectedSha": entry["patchedSha"]})
            else:
                bpath = backup_dir / rel
                if not bpath.is_file() or sha256_bytes(bpath.read_bytes()) != entry["originalSha"]:
                    raise PatchError(f"Original backup missing or mismatched for {rel}")
                cur_sha = sha256_bytes(dest.read_bytes()) if dest.is_file() else ""
                if cur_sha != entry["originalSha"]:
                    already_restored = False
                    if cur_sha != entry["patchedSha"]:
                        raise PatchError(f"Modified after patching ({rel}); restoration aborted")
                    items.append({
                        "rel": rel, "action": "replace",
                        "staged": str(bpath), "expectedSha": entry["patchedSha"], "replacementSha": entry["originalSha"],
                    })
        _cleanup_stale_stage(state_dir, previous)
        if already_restored:
            record = dict(
                previous, mode="source", state="restored", operation="restore", token=token,
                serverPid=server_pid, status=str(status), sidecar=str(sidecar), config=str(config),
                stageDir="", items=[],
                message="Original BeamNG.Earth source files are already active.",
            )
            atomic_json(sidecar, record)
            atomic_json(status, record)
            return record
        record = dict(
            previous, mode="source", state="pending", operation="restore", token=token,
            serverPid=server_pid, status=str(status), sidecar=str(sidecar), config=str(config),
            stageDir="", items=items,
            message="Source restore ready. Close BeamNG.Earth, then start it normally.",
        )
        atomic_json(sidecar, record)
        atomic_json(status, record)
        return record

    validate_source_compatibility(root, payload_dir=payload_dir)
    patch_tier, solver_mode = _detect_source_patch_tier_and_mode(root, payload_dir=payload_dir)
    if not apply_fixes:
        patch_tier = TIER_CLIENT_AND_REPORTER
        solver_mode = SOLVER_MODE_CLIENT_ONLY
    if previous and previous.get("state") == "installed":
        # Re-install from verified original state when options changed, or return up-to-date.
        if (previous.get("payloadSha") == payload_sha
                and previous.get("applyFixes") == apply_fixes
                and previous.get("enableReporter") == enable_reporter):
            all_match = all(
                (root / f["rel"]).is_file() and sha256_bytes((root / f["rel"]).read_bytes()) == f["patchedSha"]
                for f in previous.get("files", [])
            )
            if all_match:
                record = dict(previous, patchTier=patch_tier, solverMode=solver_mode, state="installed", token=token, serverPid=server_pid,
                              message="Bridge, Rail & Tunnel fixes are already installed.")
                atomic_json(sidecar, record)
                atomic_json(status, record)
                return record
        # Check originals exist so we patch cleanly from backed-up pristine copies.
        for f in previous.get("files", []):
            if not f.get("added") and not (backup_dir / f["rel"]).is_file():
                raise PatchError(f"Missing original backup for {f['rel']}")

    plan = _build_source_plan(root, payload_dir, apply_fixes, enable_reporter, patch_tier=patch_tier, solver_mode=solver_mode)
    _cleanup_stale_stage(state_dir, previous)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stage_dir.mkdir(parents=True, exist_ok=True)

    items = []
    manifest = []
    prev_added = {f["rel"] for f in previous.get("files", []) if f.get("added")}
    for rel, new_bytes in plan.items():
        dest = root / rel
        bpath = backup_dir / rel
        spath = stage_dir / rel
        spath.parent.mkdir(parents=True, exist_ok=True)
        spath.write_bytes(new_bytes)
        new_sha = sha256_bytes(new_bytes)
        added = (not dest.exists()) or (rel in prev_added)
        if added:
            items.append({
                "rel": rel, "action": "replace", "staged": str(spath),
                "expectedSha": sha256_bytes(dest.read_bytes()) if dest.is_file() else "",
                "replacementSha": new_sha,
            })
            manifest.append({"rel": rel, "added": True, "originalSha": "", "patchedSha": new_sha})
        else:
            if not bpath.is_file():
                bpath.parent.mkdir(parents=True, exist_ok=True)
                bpath.write_bytes(dest.read_bytes())
            orig_sha = sha256_bytes(bpath.read_bytes())
            cur_sha = sha256_bytes(dest.read_bytes())
            items.append({
                "rel": rel, "action": "replace", "staged": str(spath),
                "expectedSha": cur_sha, "replacementSha": new_sha,
            })
            manifest.append({"rel": rel, "added": False, "originalSha": orig_sha, "patchedSha": new_sha})

    record = {
        "version": VERSION, "mode": "source", "patchTier": patch_tier, "solverMode": solver_mode, "state": "pending", "operation": "install",
        "target": str(root), "config": str(config), "status": str(status), "sidecar": str(sidecar),
        "token": token, "serverPid": server_pid, "payloadSha": payload_sha,
        "applyFixes": bool(apply_fixes), "enableReporter": bool(enable_reporter),
        "stageDir": str(stage_dir), "files": manifest, "items": items,
        "message": "Source patch ready. Close BeamNG.Earth completely, then start it normally.",
    }
    atomic_json(sidecar, record)
    atomic_json(status, record)
    return record


def _onedir_layout(root):
    """Return (elf_rel, internal_rel) for a Linux AppDir or PyInstaller onedir layout, or None."""
    root = Path(root)
    if (root / "usr/lib/BeamNG.Earth/BeamNG.Earth").is_file() and (root / "usr/lib/BeamNG.Earth/_internal").is_dir():
        return "usr/lib/BeamNG.Earth/BeamNG.Earth", "usr/lib/BeamNG.Earth/_internal"
    if (root / "BeamNG.Earth").is_file() and (root / "_internal").is_dir():
        return "BeamNG.Earth", "_internal"
    return None


def _is_appimage_wrapper_file(path):
    try:
        with Path(path).open("rb") as fh:
            head = fh.read(256)
        return APPIMAGE_WRAPPER_MAGIC in head
    except OSError:
        return False


def _resolve_target_path(target):
    raw_target = Path(target).expanduser()
    if not raw_target.exists():
        raise PatchError("Select a BeamNG.Earth executable, AppImage, or source directory")
    # Preserve AppRun / wrapper path before resolving symlinks when raw_target is a symlink to AppDir internals.
    if raw_target.is_symlink() and raw_target.name == "AppRun" and _onedir_layout(raw_target.parent) is not None:
        return raw_target.parent.resolve(strict=True), "onedir"
    path = raw_target.resolve(strict=True)
    if path.is_file():
        low_name = path.name.lower()
        if low_name.endswith(".exe"):
            return path, "exe"
        if low_name.endswith(".appimage") or _is_appimage_wrapper_file(path):
            return path, "appimage"
        if path.name in ("earthd.py", "railways.py") and path.parent.name == "server":
            return path.parent.parent, "source"
        if path.name == "launcher.py" and (path.parent / "server" / "earthd.py").is_file():
            return path.parent, "source"
        if path.name in ("run_host.sh", "BeamMP-Server") and (path.parent / "Resources" / "Client" / "beamng_earth.zip").is_file():
            return path.parent, "host"
        if path.name == "BeamNG.Earth":
            if _onedir_layout(path.parent) is not None:
                if path.parent.name == "BeamNG.Earth" and path.parent.parent.name == "lib" and _onedir_layout(path.parent.parent.parent) is not None:
                    return path.parent.parent.parent, "onedir"
                return path.parent, "onedir"
        try:
            with path.open("rb") as fh:
                magic4 = fh.read(4)
            if magic4 == b"\x7fELF":
                try:
                    find_appimage_squashfs_offset(path)
                    return path, "appimage"
                except PatchError:
                    return path, "elf"
        except OSError:
            pass
    if path.is_dir():
        if _onedir_layout(path) is not None:
            return path, "onedir"
        if (path / "Resources" / "Client" / "beamng_earth.zip").is_file():
            return path, "host"
        return path, "source"
    raise PatchError("Select a BeamNG.Earth executable, AppImage, or source directory")


def _prepare_dir_plan(
    root,
    mode,
    config,
    status,
    payload_dir,
    operation,
    token,
    server_pid,
    apply_fixes,
    enable_reporter,
    plan_builder,
):
    """Generic reversible directory-staging engine shared by onedir and host modes."""
    state_dir = root / f".{TAG}"
    sidecar = state_dir / "receipt.json"
    backup_dir = state_dir / "original"
    stage_dir = state_dir / f"stage-{uuid.uuid4().hex}"
    previous = read_json(sidecar, {})
    payload_sha = _payload_digest(payload_dir) if payload_dir is not None else previous.get("payloadSha", "")

    if operation == "restore":
        if not previous or not previous.get("files"):
            raise PatchError(f"No recorded Bridge & Rail patch for this {mode} directory")
        items = []
        already_restored = True
        for entry in previous["files"]:
            rel = entry["rel"]
            dest = root / rel
            if entry.get("added"):
                if dest.exists():
                    already_restored = False
                    if not dest.is_file() or sha256_file(dest) != entry["patchedSha"]:
                        raise PatchError(f"Modified after patching ({rel}); restoration aborted")
                    items.append({"rel": rel, "action": "delete", "expectedSha": entry["patchedSha"]})
            else:
                bpath = backup_dir / rel
                if not bpath.is_file() or sha256_file(bpath) != entry["originalSha"]:
                    raise PatchError(f"Original backup missing or mismatched for {rel}")
                cur_sha = sha256_file(dest) if dest.is_file() else ""
                if cur_sha != entry["originalSha"]:
                    already_restored = False
                    if cur_sha != entry["patchedSha"]:
                        raise PatchError(f"Modified after patching ({rel}); restoration aborted")
                    items.append({
                        "rel": rel,
                        "action": "replace",
                        "staged": str(bpath),
                        "expectedSha": entry["patchedSha"],
                        "replacementSha": entry["originalSha"],
                    })
        _cleanup_stale_stage(state_dir, previous)
        if already_restored:
            record = dict(
                previous,
                mode=mode,
                state="restored",
                operation="restore",
                token=token,
                serverPid=server_pid,
                status=str(status),
                sidecar=str(sidecar),
                config=str(config),
                stageDir="",
                items=[],
                message="Original BeamNG.Earth files are already active.",
            )
            atomic_json(sidecar, record)
            atomic_json(status, record)
            return record
        record = dict(
            previous,
            mode=mode,
            state="pending",
            operation="restore",
            token=token,
            serverPid=server_pid,
            status=str(status),
            sidecar=str(sidecar),
            config=str(config),
            stageDir="",
            items=items,
            message="Restore ready. Close BeamNG.Earth completely, then start it normally.",
        )
        atomic_json(sidecar, record)
        atomic_json(status, record)
        return record

    if previous and previous.get("state") == "installed":
        if (
            previous.get("payloadSha") == payload_sha
            and previous.get("applyFixes") == apply_fixes
            and previous.get("enableReporter") == enable_reporter
        ):
            all_match = all(
                (root / f["rel"]).is_file() and sha256_file(root / f["rel"]) == f["patchedSha"]
                for f in previous.get("files", [])
            )
            if all_match:
                record = dict(
                    previous,
                    state="installed",
                    token=token,
                    serverPid=server_pid,
                    message="Bridge, Rail & Tunnel fixes are already installed.",
                )
                atomic_json(sidecar, record)
                atomic_json(status, record)
                return record
        for f in previous.get("files", []):
            if not f.get("added") and not (backup_dir / f["rel"]).is_file():
                raise PatchError(f"Missing original backup for {f['rel']}")

    plan, patch_tier, solver_mode, extra_meta = plan_builder(payload_sha)
    _cleanup_stale_stage(state_dir, previous)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stage_dir.mkdir(parents=True, exist_ok=True)

    items = []
    manifest = []
    prev_added = {f["rel"] for f in previous.get("files", []) if f.get("added")}
    for rel, new_bytes in plan.items():
        dest = root / rel
        bpath = backup_dir / rel
        spath = stage_dir / rel
        spath.parent.mkdir(parents=True, exist_ok=True)
        spath.write_bytes(new_bytes)
        if dest.is_file():
            try:
                os.chmod(spath, dest.stat().st_mode)
            except OSError:
                pass
        new_sha = sha256_bytes(new_bytes)
        added = (not dest.exists()) or (rel in prev_added)
        if added:
            items.append({
                "rel": rel,
                "action": "replace",
                "staged": str(spath),
                "expectedSha": sha256_file(dest) if dest.is_file() else "",
                "replacementSha": new_sha,
            })
            manifest.append({"rel": rel, "added": True, "originalSha": "", "patchedSha": new_sha})
        else:
            if not bpath.is_file():
                bpath.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, bpath)
            orig_sha = sha256_file(bpath)
            cur_sha = sha256_file(dest)
            items.append({
                "rel": rel,
                "action": "replace",
                "staged": str(spath),
                "expectedSha": cur_sha,
                "replacementSha": new_sha,
            })
            manifest.append({"rel": rel, "added": False, "originalSha": orig_sha, "patchedSha": new_sha})

    record = {
        "version": VERSION,
        "mode": mode,
        "patchTier": patch_tier,
        "solverMode": solver_mode,
        "state": "pending",
        "operation": "install",
        "target": str(root),
        "config": str(config),
        "status": str(status),
        "sidecar": str(sidecar),
        "token": token,
        "serverPid": server_pid,
        "payloadSha": payload_sha,
        "applyFixes": bool(apply_fixes),
        "enableReporter": bool(enable_reporter),
        "stageDir": str(stage_dir),
        "files": manifest,
        "items": items,
        "message": "Bridge, Rail & Tunnel patch ready. Close BeamNG.Earth completely, then start it normally.",
    }
    record.update(extra_meta or {})
    atomic_json(sidecar, record)
    atomic_json(status, record)
    return record


def _build_onedir_plan_and_patch(root, payload_dir, apply_fixes, enable_reporter, payload_sha, in_place=False):
    """Build (plan_dict, patch_tier, solver_mode, cache_meta) for a Linux AppDir or PyInstaller onedir tree."""
    layout = _onedir_layout(root)
    if layout is None:
        raise PatchError("Unsupported Linux BeamNG.Earth directory layout")
    elf_rel, internal_rel = layout
    payload_root = Path(payload_dir)

    def _read_orig(rel):
        return (root / rel).read_bytes() if in_place else _read_pristine(root, rel)

    ver_rel = f"{internal_rel}/VERSION"
    earth_ver = ""
    if (root / ver_rel).is_file():
        try:
            lines = _read_orig(ver_rel).decode("utf-8", errors="replace").strip().splitlines()
            earth_ver = lines[0][:64] if lines else ""
        except OSError:
            earth_ver = ""

    for req_mod in (MOD_ROADS_LUA, MOD_ACTIONS_JSON, MOD_KEYMAP_JSON, MOD_SCRIPT_LUA):
        mrel = f"{internal_rel}/{req_mod}"
        if not (root / mrel).is_file():
            raise PatchError(f"Expected bundled mod file missing in AppDir: {mrel}")

    elf_pristine = _read_orig(elf_rel)
    patched_elf, patch_tier, solver_mode = _patch_elf_carchive_with_tier(
        elf_pristine,
        payload_dir,
        apply_fixes=apply_fixes,
        enable_reporter=enable_reporter,
        payload_sha=payload_sha,
        earth_version_hint=earth_ver,
        require_mod_in_carchive=False,
    )

    plan = {elf_rel: patched_elf}
    orig_cache_ids = {}
    salted_cache_ids = {}
    for cache_key in CARCHIVE_CACHE_ID_KEYS:
        crel = f"{internal_rel}/{cache_key}"
        if (root / crel).is_file():
            raw_id = _read_orig(crel)
            orig_cache_ids[cache_key] = raw_id.decode("ascii")
            if apply_fixes or enable_reporter:
                salted_raw = _salt_carchive_cache_id(
                    raw_id,
                    cache_key,
                    payload_sha,
                    marker=MARKER,
                    apply_fixes=bool(apply_fixes),
                )
                plan[crel] = salted_raw
                salted_cache_ids[cache_key] = salted_raw.decode("ascii")

    if apply_fixes:
        fallback_roads = (payload_root / MOD_ROADS_LUA).read_bytes() if (payload_root / MOD_ROADS_LUA).is_file() else None
        roads_rel = f"{internal_rel}/{MOD_ROADS_LUA}"
        plan[roads_rel] = patch_roads_lua_bytes(_read_orig(roads_rel), fallback_bytes=fallback_roads)

    if enable_reporter:
        plan[f"{internal_rel}/{MOD_BUGREPORT_LUA}"] = (payload_root / MOD_BUGREPORT_LUA).read_bytes()
        plan[f"{internal_rel}/{MOD_ACTIONS_JSON}"] = patch_actions_json_bytes(_read_orig(f"{internal_rel}/{MOD_ACTIONS_JSON}"))
        plan[f"{internal_rel}/{MOD_KEYMAP_JSON}"] = patch_keymap_json_bytes(_read_orig(f"{internal_rel}/{MOD_KEYMAP_JSON}"))
        plan[f"{internal_rel}/{MOD_SCRIPT_LUA}"] = patch_modscript_bytes(_read_orig(f"{internal_rel}/{MOD_SCRIPT_LUA}"))

    if in_place:
        for rel, new_bytes in plan.items():
            dest = root / rel
            orig_mode = dest.stat().st_mode if dest.is_file() else None
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(new_bytes)
            if orig_mode is not None:
                try:
                    os.chmod(dest, orig_mode)
                except OSError:
                    pass

    return plan, patch_tier, solver_mode, {
        "originalCacheIds": orig_cache_ids,
        "saltedCacheIds": salted_cache_ids,
    }


def _prepare_onedir(root, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter):
    def _builder(payload_sha):
        return _build_onedir_plan_and_patch(
            root, payload_dir, apply_fixes, enable_reporter, payload_sha, in_place=False
        )

    return _prepare_dir_plan(
        root, "onedir", config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter, _builder
    )


def _prepare_host(root, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter):
    zip_rel = "Resources/Client/beamng_earth.zip"

    def _builder(_payload_sha):
        if not (root / zip_rel).is_file():
            raise PatchError(f"Expected client mod archive missing in Host directory: {zip_rel}")
        pristine_zip = _read_pristine(root, zip_rel)
        patched_zip = patch_mod_zip_bytes(
            pristine_zip, payload_dir, apply_fixes=apply_fixes, enable_reporter=enable_reporter
        )
        return {zip_rel: patched_zip}, TIER_CLIENT_AND_REPORTER, SOLVER_MODE_CLIENT_ONLY, {}

    return _prepare_dir_plan(
        root, "host", config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter, _builder
    )


def _prepare_elf(target, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter):
    sidecar = target.with_name(target.name + f".{TAG}.json")
    backup = target.with_name(target.name + f".{TAG}.original")
    before = target.read_bytes()
    previous = read_json(sidecar, {})
    digest = sha256_bytes(before)
    original_sha = digest
    orig_mode = target.stat().st_mode
    payload_sha = _payload_digest(payload_dir) if payload_dir is not None else previous.get("payloadSha", "")

    if operation == "restore":
        if not previous:
            raise PatchError("No recorded Bridge & Rail patch for this ELF executable")
        if digest == previous.get("originalSha"):
            _cleanup_stale_pending(target, previous)
            record = dict(previous, state="restored", message="Original executable is already active.", token=token)
            atomic_json(sidecar, record)
            atomic_json(status, record)
            return record
        if digest != previous.get("patchedSha"):
            raise PatchError("Executable changed after patching; restoration aborted to protect updates")
        if not backup.is_file() or sha256_file(backup) != previous.get("originalSha"):
            raise PatchError("Original executable backup is missing or checksum-mismatched")
        original_sha = previous["originalSha"]
        after = backup.read_bytes()
        orig_cache_ids = dict(previous.get("originalCacheIds") or _read_carchive_cache_ids(after))
        salted_cache_ids = dict(previous.get("saltedCacheIds") or {})
        patch_tier = previous.get("patchTier", "")
        solver_mode = previous.get("solverMode", "")
    else:
        pristine = before
        if previous and digest == previous.get("patchedSha"):
            if (
                str(config) == previous.get("config")
                and previous.get("payloadSha") == payload_sha
                and previous.get("applyFixes") == apply_fixes
                and previous.get("enableReporter") == enable_reporter
            ):
                record = dict(
                    previous,
                    state="installed",
                    message="Bridge, Rail & Tunnel fixes are installed. Restart BeamNG.Earth if you changed options.",
                    token=token,
                    serverPid=server_pid,
                )
                atomic_json(sidecar, record)
                atomic_json(status, record)
                return record
            if not backup.is_file() or sha256_file(backup) != previous.get("originalSha"):
                raise PatchError("Original executable backup could not be verified; existing patch preserved")
            pristine = backup.read_bytes()
            original_sha = previous["originalSha"]
        elif backup.exists() and (not previous or sha256_file(backup) != previous.get("originalSha") or digest != previous.get("originalSha")):
            raise PatchError("Existing backup belongs to a different Earth build; it was preserved")

        orig_cache_ids = _read_carchive_cache_ids(pristine)
        after, patch_tier, solver_mode = _patch_elf_carchive_with_tier(
            pristine,
            payload_dir,
            apply_fixes=apply_fixes,
            enable_reporter=enable_reporter,
            payload_sha=payload_sha,
            require_mod_in_carchive=True,
        )
        salted_cache_ids = _read_carchive_cache_ids(after)
        if not backup.exists():
            with backup.open("xb") as stream:
                stream.write(pristine)
        if sha256_file(backup) != original_sha:
            raise PatchError("Backup verification failed; original executable untouched")

    _cleanup_stale_pending(target, previous)
    pending = target.with_name(f"{target.name}.{TAG}.{uuid.uuid4().hex}.pending")
    with pending.open("xb") as stream:
        stream.write(after)
    try:
        os.chmod(pending, int(previous.get("originalMode") or orig_mode or 0o755) | 0o111)
    except OSError:
        pass
    if sha256_file(pending) != sha256_bytes(after):
        raise PatchError("Staged patch checksum verification failed")

    patched_sha = sha256_bytes(after) if operation == "install" else previous["patchedSha"]
    record = {
        "version": VERSION,
        "mode": "elf",
        "patchTier": patch_tier,
        "solverMode": solver_mode,
        "state": "pending",
        "operation": operation,
        "target": str(target),
        "backup": str(backup),
        "pending": str(pending),
        "expectedSha": digest,
        "replacementSha": sha256_bytes(after),
        "originalSha": original_sha,
        "patchedSha": patched_sha,
        "originalSigned": False,
        "originalMode": int(previous.get("originalMode") or orig_mode or 0o755),
        "originalCacheIds": orig_cache_ids,
        "saltedCacheIds": salted_cache_ids,
        "config": str(config),
        "payloadSha": payload_sha,
        "applyFixes": bool(apply_fixes),
        "enableReporter": bool(enable_reporter),
        "status": str(status),
        "sidecar": str(sidecar),
        "token": token,
        "serverPid": server_pid,
        "message": (
            "Bridge, Rail & Tunnel patch ready. Close BeamNG.Earth completely, then start it normally."
            if operation == "install"
            else "Restore ready. Close BeamNG.Earth completely, then start it normally."
        ),
    }
    atomic_json(sidecar, record)
    atomic_json(status, record)
    return record


def _prepare_appimage(target, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter):
    sidecar = target.with_name(target.name + f".{TAG}.json")
    backup = target.with_name(target.name + f".{TAG}.original")
    appdir_dest = target.with_name(target.name + f".{TAG}.appdir")
    previous = read_json(sidecar, {})
    digest = sha256_file(target)
    original_sha = digest
    orig_mode = int(previous.get("originalMode") or target.stat().st_mode or 0o755)
    payload_sha = _payload_digest(payload_dir) if payload_dir is not None else previous.get("payloadSha", "")

    _cleanup_stale_pending(target, previous)
    pending = target.with_name(f"{target.name}.{TAG}.{uuid.uuid4().hex}.pending")

    if operation == "restore":
        if not previous:
            raise PatchError("No recorded Bridge & Rail patch for this AppImage")
        if digest == previous.get("originalSha"):
            if appdir_dest.is_dir():
                shutil.rmtree(appdir_dest, ignore_errors=True)
            record = dict(previous, state="restored", message="Original AppImage is already active.", token=token)
            atomic_json(sidecar, record)
            atomic_json(status, record)
            return record
        if digest != previous.get("patchedSha"):
            raise PatchError("AppImage changed after patching; restoration aborted to protect updates")
        if not backup.is_file() or sha256_file(backup) != previous.get("originalSha"):
            raise PatchError("Original AppImage backup is missing or checksum-mismatched")
        original_sha = previous["originalSha"]
        shutil.copy2(backup, pending)
        try:
            os.chmod(pending, orig_mode | 0o111)
        except OSError:
            pass
        rep_sha = sha256_file(pending)
        if rep_sha != original_sha:
            raise PatchError("Staged AppImage restore checksum verification failed")
        record = dict(
            previous,
            version=VERSION,
            mode="appimage",
            state="pending",
            operation="restore",
            target=str(target),
            backup=str(backup),
            pending=str(pending),
            expectedSha=digest,
            replacementSha=rep_sha,
            originalSha=original_sha,
            config=str(config),
            status=str(status),
            sidecar=str(sidecar),
            token=token,
            serverPid=server_pid,
            message="Restore ready. Close BeamNG.Earth completely, then start it normally.",
        )
        atomic_json(sidecar, record)
        atomic_json(status, record)
        return record

    pristine_path = target
    if previous and digest == previous.get("patchedSha"):
        if (
            str(config) == previous.get("config")
            and previous.get("payloadSha") == payload_sha
            and previous.get("applyFixes") == apply_fixes
            and previous.get("enableReporter") == enable_reporter
            and (previous.get("appimagePackaging") != "wrapper" or appdir_dest.is_dir())
        ):
            record = dict(
                previous,
                state="installed",
                message="Bridge, Rail & Tunnel fixes are installed. Restart BeamNG.Earth if you changed options.",
                token=token,
                serverPid=server_pid,
            )
            atomic_json(sidecar, record)
            atomic_json(status, record)
            return record
        if not backup.is_file() or sha256_file(backup) != previous.get("originalSha"):
            raise PatchError("Original AppImage backup could not be verified; existing patch preserved")
        pristine_path = backup
        original_sha = previous["originalSha"]
    elif backup.exists() and (not previous or sha256_file(backup) != previous.get("originalSha") or digest != previous.get("originalSha")):
        raise PatchError("Existing backup belongs to a different Earth build; it was preserved")

    with tempfile.TemporaryDirectory(dir=target.parent, prefix=f".{TAG}-appimg-") as tmp_root:
        work_sqfs = Path(tmp_root) / "squashfs-root"
        sqfs_offset = _extract_appimage_tree(pristine_path, work_sqfs)
        _plan, patch_tier, solver_mode, cache_meta = _build_onedir_plan_and_patch(
            work_sqfs,
            payload_dir,
            apply_fixes=apply_fixes,
            enable_reporter=enable_reporter,
            payload_sha=payload_sha,
            in_place=True,
        )
        if not backup.exists():
            tmp_backup = backup.with_name(f"{backup.name}.{uuid.uuid4().hex}.tmp")
            shutil.copy2(pristine_path, tmp_backup)
            if sha256_file(tmp_backup) != original_sha:
                tmp_backup.unlink(missing_ok=True)
                raise PatchError("Backup verification failed; original AppImage untouched")
            os.replace(tmp_backup, backup)
        elif sha256_file(backup) != original_sha:
            raise PatchError("Backup verification failed; original AppImage untouched")

        packaging_kind, staged_appdir = _repack_appimage_or_wrapper(
            backup, sqfs_offset, work_sqfs, pending, appdir_dest, orig_mode=orig_mode
        )

    if packaging_kind == "repacked" and appdir_dest.is_dir():
        shutil.rmtree(appdir_dest, ignore_errors=True)

    patched_sha = sha256_file(pending)
    record = {
        "version": VERSION,
        "mode": "appimage",
        "appimagePackaging": packaging_kind,
        "appdirPath": staged_appdir,
        "squashfsOffset": sqfs_offset,
        "patchTier": patch_tier,
        "solverMode": solver_mode,
        "state": "pending",
        "operation": "install",
        "target": str(target),
        "backup": str(backup),
        "pending": str(pending),
        "expectedSha": digest,
        "replacementSha": patched_sha,
        "originalSha": original_sha,
        "patchedSha": patched_sha,
        "originalSigned": False,
        "originalMode": orig_mode,
        "originalCacheIds": cache_meta.get("originalCacheIds", {}),
        "saltedCacheIds": cache_meta.get("saltedCacheIds", {}),
        "config": str(config),
        "payloadSha": payload_sha,
        "applyFixes": bool(apply_fixes),
        "enableReporter": bool(enable_reporter),
        "status": str(status),
        "sidecar": str(sidecar),
        "token": token,
        "serverPid": server_pid,
        "message": "Bridge, Rail & Tunnel patch ready. Close BeamNG.Earth completely, then start it normally.",
    }
    atomic_json(sidecar, record)
    atomic_json(status, record)
    return record


def prepare(target, config, status, payload_dir=None, operation="install", token="", server_pid=0,
            apply_fixes=True, enable_reporter=True):
    resolved_target, mode = _resolve_target_path(target)
    config, status = Path(config).resolve(), Path(status).resolve()
    if not apply_fixes and not enable_reporter and operation == "install":
        operation = "restore"
    if operation != "restore":
        if payload_dir is None:
            raise PatchError("Payload directory is required for install")
        payload_dir = Path(payload_dir).resolve(strict=True)
    elif payload_dir is not None:
        p = Path(payload_dir)
        payload_dir = p.resolve() if p.exists() else None
    if mode == "exe":
        return _prepare_exe(resolved_target, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter)
    if mode == "appimage":
        return _prepare_appimage(resolved_target, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter)
    if mode == "elf":
        return _prepare_elf(resolved_target, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter)
    if mode == "onedir":
        return _prepare_onedir(resolved_target, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter)
    if mode == "host":
        return _prepare_host(resolved_target, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter)
    return _prepare_source(resolved_target, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter)


def apply_now(record):
    """Apply staged binary, AppImage, onedir, host, or source changes immediately (offline CLI / unit tests)."""
    state = read_json(record["config"], {})
    if record.get("token") and state.get("requestToken") != record["token"]:
        raise PatchError("A newer request superseded this patch")

    mode = record.get("mode", "")
    if mode in ("source", "onedir", "host"):
        root = Path(record["target"])
        for item in record["items"]:
            dest = root / item["rel"]
            if item["expectedSha"]:
                if not dest.is_file() or sha256_file(dest) != item["expectedSha"]:
                    raise PatchError(f"Target changed before swap ({item['rel']})")
            elif dest.exists():
                raise PatchError(f"File unexpectedly appeared before swap ({item['rel']})")
        for item in record["items"]:
            dest = root / item["rel"]
            try:
                if item["action"] == "delete":
                    if dest.exists():
                        dest.unlink()
                else:
                    staged = Path(item["staged"])
                    if sha256_file(staged) != item["replacementSha"]:
                        raise PatchError(f"Staged file changed ({item['rel']})")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(staged, dest)
                    if sha256_file(dest) != item["replacementSha"]:
                        raise PatchError(f"Verification failed after write ({item['rel']})")
            except PermissionError as exc:
                raise PatchError("BeamNG.Earth is currently locking files. Close BeamNG.Earth completely and try again.") from exc
        stage_dir = record.get("stageDir")
        if stage_dir and Path(stage_dir).is_dir():
            shutil.rmtree(stage_dir, ignore_errors=True)
    else:
        target, pending = Path(record["target"]), Path(record["pending"])
        if sha256_file(target) != record["expectedSha"] or sha256_file(pending) != record["replacementSha"]:
            raise PatchError("Files changed after preparation; refusing to overwrite")
        exec_mode = int(record.get("originalMode") or 0)
        if os.name != "nt" and mode in ("appimage", "elf"):
            try:
                os.chmod(pending, (exec_mode or target.stat().st_mode or 0o755) | 0o111)
            except OSError:
                pass
        try:
            os.replace(pending, target)
        except PermissionError as exc:
            raise PatchError("BeamNG.Earth is currently running. Close BeamNG.Earth completely and try again.") from exc
        except OSError as exc:
            if getattr(exc, "winerror", None) in (5, 32):
                raise PatchError("BeamNG.Earth is currently running. Close BeamNG.Earth completely and try again.") from exc
            raise
        if os.name != "nt" and mode in ("appimage", "elf"):
            try:
                os.chmod(target, (exec_mode or 0o755) | 0o111)
            except OSError:
                pass
        if record.get("operation") == "restore" and mode == "appimage":
            appdir_cand = record.get("appdirPath") or str(target.with_name(f"{target.name}.{TAG}.appdir"))
            if appdir_cand and Path(appdir_cand).is_dir():
                shutil.rmtree(appdir_cand, ignore_errors=True)

    finished = dict(
        record,
        state="installed" if record["operation"] == "install" else "restored",
        message=(
            "Bridge, Rail & Tunnel fixes installed."
            if record["operation"] == "install"
            else "Original BeamNG.Earth restored."
        ),
    )
    atomic_json(record["sidecar"], finished)
    atomic_json(record["status"], finished)
    return finished


def _inspect_onedir_root(resolved, info):
    """Populate inspect_target info dict for a Linux AppDir / PyInstaller onedir root."""
    cur_py = sys.version_info.major * 100 + sys.version_info.minor
    layout = _onedir_layout(resolved)
    if layout is None:
        info["reason"] = "Unsupported Linux BeamNG.Earth directory layout"
        info["state"] = "invalid"
        return info
    elf_rel, internal_rel = layout
    elf_path = resolved / elf_rel
    internal_dir = resolved / internal_rel
    ver_path = internal_dir / "VERSION"
    if ver_path.is_file():
        try:
            lines = ver_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            info["earthVersion"] = lines[0][:64] if lines else ""
        except OSError:
            pass

    try:
        data = elf_path.read_bytes()
        start, _toff, _cookie_pos, py, _lib, entries = archive_entries(data)
    except (OSError, PatchError) as exc:
        info["reason"] = str(exc)
        info["state"] = "invalid"
        return info

    info["pythonVersion"] = f"{py // 100}.{py % 100}"
    info["pythonMatches"] = (py == cur_py)
    entry_map = {e[7]: e for e in entries}
    if not info["earthVersion"]:
        info["earthVersion"] = _read_carchive_version(data, start, entry_map)

    pyz_candidates = [e for e in entries if e[5] == b"z"]
    if len(pyz_candidates) != 1:
        info["reason"] = "Expected one embedded Python archive in ELF executable"
        info["state"] = "incompatible"
        return info
    pyz_entry = pyz_candidates[0]
    blob = data[start + pyz_entry[1]:start + pyz_entry[1] + pyz_entry[2]]
    try:
        pz = zlib.decompress(blob) if pyz_entry[4] else blob
        offset, _, toc = _read_pyz_toc(pz)
    except Exception as exc:
        info["reason"] = f"Could not inspect embedded Python archive: {exc}"
        info["state"] = "corrupt"
        return info

    state_dir = resolved / f".{TAG}"
    sidecar = read_json(state_dir / "receipt.json", {})
    backup_dir = state_dir / "original"
    info["hasBackup"] = backup_dir.is_dir() and bool(sidecar.get("files"))

    patched_in_pyz = ("bridge_rail_report" in toc) or (ORIG_EARTHD_MODULE in toc) or (V2_ORIG_SENTINEL in toc)
    if not patched_in_pyz and py == cur_py and "earthd" in toc and pz[4:8] == importlib.util.MAGIC_NUMBER:
        try:
            k, p_off, p_len = toc["earthd"]
            if k == 0 and 12 <= p_off and p_off + p_len <= offset:
                code = marshal.loads(zlib.decompress(pz[p_off:p_off + p_len]))
                patched_in_pyz = "EARTH_BRIDGE_RAIL_FIXES_" in repr(code.co_consts)
        except Exception:
            pass
    info["patched"] = bool(patched_in_pyz or sidecar.get("state") == "installed")
    if info["patched"]:
        info["state"] = "installed"
    elif sidecar.get("state") in ("pending", "restored"):
        info["state"] = sidecar["state"]
    else:
        info["state"] = "unpatched"

    missing = [m for m in REQUIRED_UPSTREAM_MODULES if m not in toc]
    info["missingModules"] = missing
    if missing:
        info["reason"] = _incompatible_message(missing, earth_version=info["earthVersion"], is_source=False)
        info["state"] = "incompatible"
        return info

    for rel in (MOD_ROADS_LUA, MOD_ACTIONS_JSON, MOD_KEYMAP_JSON, MOD_SCRIPT_LUA):
        if not (internal_dir / rel).is_file():
            info["reason"] = f"Expected bundled mod file missing in AppDir: {internal_rel}/{rel}"
            info["state"] = "incompatible"
            return info

    try:
        roads_raw = (internal_dir / MOD_ROADS_LUA).read_bytes()
        if b"function M.collideNear" in roads_raw:
            patch_roads_lua_bytes(roads_raw)
        info["patchTier"], info["solverMode"] = _detect_pyz_patch_tier_and_mode(pz, offset, toc)
    except (PatchError, zlib.error, UnicodeDecodeError) as exc:
        info["reason"] = str(exc)
        info["state"] = "incompatible"
        return info

    info["compatible"] = True
    return info


def inspect_target(target):
    """Read-only inspection of a BeamNG.Earth Windows .exe, Linux AppImage/onedir/host, or source directory.

    Safe to call from any Python 3.10+ interpreter before modifying anything.
    """
    info = {
        "target": str(target),
        "mode": "",
        "compatible": False,
        "reason": "",
        "earthVersion": "",
        "pythonVersion": "",
        "pythonMatches": False,
        "patched": False,
        "hasBackup": False,
        "state": "unknown",
        "patchTier": "",
        "solverMode": "",
        "missingModules": [],
        "signed": False,
    }
    try:
        resolved, mode = _resolve_target_path(target)
    except (OSError, PatchError) as exc:
        info["reason"] = str(exc)
        info["state"] = "invalid"
        return info

    info["target"] = str(resolved)
    info["mode"] = mode
    cur_py = sys.version_info.major * 100 + sys.version_info.minor

    if mode == "source":
        info["pythonVersion"] = f"{sys.version_info.major}.{sys.version_info.minor}"
        info["pythonMatches"] = True
        server_dir = resolved / "server"
        mod_dir = resolved / "mod" / "beamng_earth"
        if not (server_dir / "earthd.py").is_file() or not mod_dir.is_dir():
            info["reason"] = "Select a BeamNG.Earth executable, AppImage, or source directory"
            info["state"] = "invalid"
            return info
        info["earthVersion"] = _read_source_version(resolved)
        state_dir = resolved / f".{TAG}"
        sidecar = read_json(state_dir / "receipt.json", {})
        backup_dir = state_dir / "original"
        info["hasBackup"] = backup_dir.is_dir() and bool(sidecar.get("files"))
        earthd_text = (server_dir / "earthd.py").read_text(encoding="utf-8", errors="replace")
        has_marker = ("bridge_rail_report" in earthd_text) or (
            sidecar.get("state") == "installed"
            and all((resolved / f["rel"]).is_file() and sha256_file(resolved / f["rel"]) == f["patchedSha"]
                    for f in sidecar.get("files", []))
        )
        info["patched"] = bool(has_marker)
        if has_marker:
            info["state"] = "installed"
        elif sidecar.get("state") in ("pending", "restored"):
            info["state"] = sidecar["state"]
        else:
            info["state"] = "unpatched"
        missing = [m for m in REQUIRED_UPSTREAM_MODULES if not (server_dir / f"{m}.py").is_file()]
        info["missingModules"] = missing
        if missing:
            info["reason"] = _incompatible_message(missing, earth_version=info["earthVersion"], is_source=True)
            info["state"] = "incompatible"
            return info
        for rel in (MOD_ROADS_LUA, MOD_ACTIONS_JSON, MOD_KEYMAP_JSON, MOD_SCRIPT_LUA):
            if not (resolved / rel).is_file():
                info["reason"] = f"Expected mod file missing in source tree: {rel}"
                info["state"] = "incompatible"
                return info
        try:
            roads_raw = (resolved / MOD_ROADS_LUA).read_bytes()
            if b"function M.collideNear" in roads_raw:
                patch_roads_lua_bytes(roads_raw)
            info["patchTier"], info["solverMode"] = _detect_source_patch_tier_and_mode(resolved)
        except PatchError as exc:
            info["reason"] = str(exc)
            info["state"] = "incompatible"
            return info
        info["compatible"] = True
        return info

    if mode == "onedir":
        return _inspect_onedir_root(resolved, info)

    if mode == "host":
        info["pythonVersion"] = f"{sys.version_info.major}.{sys.version_info.minor}"
        info["pythonMatches"] = True
        zip_path = resolved / "Resources" / "Client" / "beamng_earth.zip"
        if not zip_path.is_file():
            info["reason"] = "Expected Resources/Client/beamng_earth.zip missing in Host directory"
            info["state"] = "invalid"
            return info
        prov = read_json(resolved / "UPSTREAM-PROVENANCE.json", {})
        info["earthVersion"] = str(prov.get("version") or prov.get("tag") or "")[:64]
        state_dir = resolved / f".{TAG}"
        sidecar = read_json(state_dir / "receipt.json", {})
        backup_dir = state_dir / "original"
        info["hasBackup"] = (backup_dir / "Resources" / "Client" / "beamng_earth.zip").is_file()
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                names = {zi.filename.replace("\\", "/") for zi in zf.infolist()}
                for req in (MOD_ZIP_ROADS_LUA, MOD_ZIP_ACTIONS_JSON, MOD_ZIP_KEYMAP_JSON, MOD_ZIP_SCRIPT_LUA):
                    if req not in names:
                        info["reason"] = f"Expected mod file missing in beamng_earth.zip: {req}"
                        info["state"] = "incompatible"
                        return info
                roads_raw = zf.read(MOD_ZIP_ROADS_LUA)
                if b"function M.collideNear" in roads_raw:
                    patch_roads_lua_bytes(roads_raw)
                has_bug = MOD_ZIP_BUGREPORT_LUA in names
        except (OSError, zipfile.BadZipFile, PatchError) as exc:
            info["reason"] = str(exc)
            info["state"] = "incompatible"
            return info
        info["patched"] = bool(has_bug or sidecar.get("state") == "installed")
        info["state"] = "installed" if info["patched"] else (sidecar.get("state") if sidecar.get("state") in ("pending", "restored") else "unpatched")
        info["patchTier"] = TIER_CLIENT_AND_REPORTER
        info["solverMode"] = SOLVER_MODE_CLIENT_ONLY
        info["compatible"] = True
        return info

    if mode == "appimage":
        sidecar_path = resolved.with_name(resolved.name + f".{TAG}.json")
        backup_path = resolved.with_name(resolved.name + f".{TAG}.original")
        sidecar = read_json(sidecar_path, {})
        digest = sha256_file(resolved)
        has_backup = bool(
            backup_path.is_file()
            and sidecar.get("originalSha")
            and sha256_file(backup_path) == sidecar.get("originalSha")
        )
        is_patched_file = bool(sidecar and digest == sidecar.get("patchedSha"))
        inspect_source = backup_path if (_is_appimage_wrapper_file(resolved) and has_backup) else resolved
        if _is_appimage_wrapper_file(resolved) and not has_backup:
            appdir_cand = Path(sidecar.get("appdirPath") or resolved.with_name(f"{resolved.name}.{TAG}.appdir"))
            if appdir_cand.is_dir():
                sub_info = _inspect_onedir_root(appdir_cand, dict(info))
                sub_info["target"] = str(resolved)
                sub_info["mode"] = "appimage"
                sub_info["patched"] = True
                sub_info["state"] = "installed"
                return sub_info
            info["reason"] = "Patched AppImage wrapper is missing its original backup and AppDir"
            info["state"] = "corrupt"
            return info

        try:
            with tempfile.TemporaryDirectory(prefix=f"{TAG}-inspect-") as tmp_dir:
                sqfs_dir = Path(tmp_dir) / "sqfs"
                _extract_appimage_tree(
                    inspect_source,
                    sqfs_dir,
                    subpaths=(
                        "usr/lib/BeamNG.Earth/BeamNG.Earth",
                        "usr/lib/BeamNG.Earth/_internal/VERSION",
                        "usr/lib/BeamNG.Earth/_internal/CODE_ID",
                        "usr/lib/BeamNG.Earth/_internal/REVISIT_ID",
                        "usr/lib/BeamNG.Earth/_internal/mod/beamng_earth",
                    ),
                )
                sub_info = _inspect_onedir_root(sqfs_dir, dict(info))
        except (OSError, PatchError) as exc:
            info["reason"] = str(exc)
            info["state"] = "invalid"
            return info

        sub_info["target"] = str(resolved)
        sub_info["mode"] = "appimage"
        sub_info["hasBackup"] = has_backup
        if is_patched_file or sub_info.get("patched"):
            sub_info["patched"] = True
            sub_info["state"] = "installed"
            if sidecar.get("patchTier"):
                sub_info["patchTier"] = sidecar["patchTier"]
            if sidecar.get("solverMode"):
                sub_info["solverMode"] = sidecar["solverMode"]
        elif sidecar.get("state") in ("pending", "restored"):
            sub_info["state"] = sidecar["state"]
        else:
            sub_info["state"] = "unpatched"
        return sub_info

    # EXE or standalone onefile ELF mode (read-only)
    try:
        data = resolved.read_bytes()
        start, toff, cookie_pos, py, lib, entries = archive_entries(data)
    except (OSError, PatchError) as exc:
        info["reason"] = str(exc)
        info["state"] = "invalid"
        return info

    info["pythonVersion"] = f"{py // 100}.{py % 100}"
    info["pythonMatches"] = (py == cur_py)
    entry_map = {e[7]: e for e in entries}
    info["earthVersion"] = _read_carchive_version(data, start, entry_map)

    if mode == "exe":
        try:
            _, signed, _ = strip_signature(data, cookie_pos + COOKIE.size)
            info["signed"] = signed
        except PatchError as exc:
            info["reason"] = str(exc)
            info["state"] = "incompatible"
            return info

    pyz_candidates = [e for e in entries if e[5] == b"z"]
    if len(pyz_candidates) != 1:
        info["reason"] = "Expected one embedded Python archive in executable"
        info["state"] = "incompatible"
        return info
    pyz_entry = pyz_candidates[0]
    blob = data[start + pyz_entry[1]:start + pyz_entry[1] + pyz_entry[2]]
    try:
        pz = zlib.decompress(blob) if pyz_entry[4] else blob
        offset, _, toc = _read_pyz_toc(pz)
    except Exception as exc:
        info["reason"] = f"Could not inspect embedded Python archive: {exc}"
        info["state"] = "corrupt"
        return info

    sidecar_path = resolved.with_name(resolved.name + f".{TAG}.json")
    backup_path = resolved.with_name(resolved.name + f".{TAG}.original")
    sidecar = read_json(sidecar_path, {})
    digest = sha256_bytes(data)
    info["hasBackup"] = bool(
        backup_path.is_file()
        and sidecar.get("originalSha")
        and sha256_file(backup_path) == sidecar.get("originalSha")
    )

    patched_in_pyz = ("bridge_rail_report" in toc) or (ORIG_EARTHD_MODULE in toc) or (V2_ORIG_SENTINEL in toc)
    if not patched_in_pyz and py == cur_py and "earthd" in toc and pz[4:8] == importlib.util.MAGIC_NUMBER:
        try:
            k, p_off, p_len = toc["earthd"]
            if k == 0 and 12 <= p_off and p_off + p_len <= offset:
                code = marshal.loads(zlib.decompress(pz[p_off:p_off + p_len]))
                patched_in_pyz = "EARTH_BRIDGE_RAIL_FIXES_" in repr(code.co_consts)
        except Exception:
            pass
    info["patched"] = bool(patched_in_pyz or (sidecar and digest == sidecar.get("patchedSha")))
    if info["patched"]:
        info["state"] = "installed"
    elif sidecar.get("state") in ("pending", "restored"):
        info["state"] = sidecar["state"]
    else:
        info["state"] = "unpatched"

    missing = [m for m in REQUIRED_UPSTREAM_MODULES if m not in toc]
    info["missingModules"] = missing
    if missing:
        info["reason"] = _incompatible_message(missing, earth_version=info["earthVersion"], is_source=False)
        info["state"] = "incompatible"
        return info
    for rel in (MOD_ROADS_LUA, MOD_ACTIONS_JSON, MOD_KEYMAP_JSON, MOD_SCRIPT_LUA):
        win_k = _win_rel(rel)
        if win_k not in entry_map and rel not in entry_map:
            info["reason"] = f"Expected bundled mod file missing in archive: {win_k if mode == 'exe' else rel}"
            info["state"] = "incompatible"
            return info
    try:
        roads_key = _win_rel(MOD_ROADS_LUA) if _win_rel(MOD_ROADS_LUA) in entry_map else MOD_ROADS_LUA
        roads_entry = entry_map[roads_key]
        roads_blob = data[start + roads_entry[1]:start + roads_entry[1] + roads_entry[2]]
        roads_raw = zlib.decompress(roads_blob) if roads_entry[4] else roads_blob
        if b"function M.collideNear" in roads_raw:
            patch_roads_lua_bytes(roads_raw)
        if info["patched"] and sidecar.get("patchTier") in (TIER_FULL_SOLVER, TIER_CLIENT_AND_REPORTER) and ORIG_EARTHD_MODULE not in toc and "bridge_rail_report" not in toc and V2_ORIG_SENTINEL not in toc:
            info["patchTier"] = sidecar["patchTier"]
            info["solverMode"] = sidecar.get("solverMode") or (
                SOLVER_MODE_DIRECT if sidecar["patchTier"] == TIER_FULL_SOLVER else SOLVER_MODE_CLIENT_ONLY
            )
        else:
            info["patchTier"], info["solverMode"] = _detect_pyz_patch_tier_and_mode(pz, offset, toc)
    except (PatchError, zlib.error, UnicodeDecodeError) as exc:
        info["reason"] = str(exc)
        info["state"] = "incompatible"
        return info

    info["compatible"] = True
    return info


def verify_compatibility(target):
    """Raise PatchError if target is not a compatible BeamNG.Earth build; return inspect_target info."""
    info = inspect_target(target)
    if not info["compatible"]:
        raise PatchError(info["reason"] or f"Incompatible target: {target}")
    return info


def _running_earth_exes():
    """Return executable or AppImage paths of running BeamNG.Earth processes on Windows or Linux."""
    found = []
    if os.name == "nt":
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            TH32CS_SNAPPROCESS = 0x00000002
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            MAX_PATH = 260

            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [
                    ("dwSize", ctypes.c_uint32),
                    ("cntUsage", ctypes.c_uint32),
                    ("th32ProcessID", ctypes.c_uint32),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", ctypes.c_uint32),
                    ("cntThreads", ctypes.c_uint32),
                    ("th32ParentProcessID", ctypes.c_uint32),
                    ("pcPriClassBase", ctypes.c_int32),
                    ("dwFlags", ctypes.c_uint32),
                    ("szExeFile", ctypes.c_wchar * MAX_PATH),
                ]

            snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snap == ctypes.c_void_p(-1).value:
                return []
            try:
                pe = PROCESSENTRY32W()
                pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                ok = k32.Process32FirstW(snap, ctypes.byref(pe))
                while ok:
                    exe_name = pe.szExeFile.lower()
                    if exe_name.startswith("beamng.earth") and exe_name.endswith(".exe"):
                        proc = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pe.th32ProcessID)
                        if proc:
                            try:
                                buf = ctypes.create_unicode_buffer(1024)
                                size = ctypes.c_uint32(len(buf))
                                if k32.QueryFullProcessImageNameW(proc, 0, buf, ctypes.byref(size)):
                                    found.append(Path(buf.value))
                            finally:
                                k32.CloseHandle(proc)
                    ok = k32.Process32NextW(snap, ctypes.byref(pe))
            finally:
                k32.CloseHandle(snap)
        except Exception:
            return []
        return found

    proc_root = Path("/proc")
    if proc_root.is_dir():
        self_pid = os.getpid()
        for entry in proc_root.iterdir():
            if not entry.name.isdigit() or int(entry.name) == self_pid:
                continue
            try:
                env_raw = (entry / "environ").read_bytes()
                for kv in env_raw.split(b"\x00"):
                    if kv.startswith(b"APPIMAGE="):
                        app_path = Path(kv[9:].decode("utf-8", errors="surrogateescape"))
                        if "beamng.earth" in app_path.name.lower() and app_path.is_file():
                            found.append(app_path)
            except OSError:
                pass
            try:
                exe_real = Path(os.path.realpath(str(entry / "exe")))
                if exe_real.name.lower().startswith("beamng.earth") and "/tmp/.mount_" not in str(exe_real):
                    found.append(exe_real)
            except OSError:
                pass
    return found


def _is_candidate_exe(path):
    name = path.name.lower()
    if f".{TAG.lower()}." in name or any(tok in name for tok in (".original", ".pending", ".swap.", ".tmp", ".part")):
        return False
    if name.startswith("beamng.earth") and (name.endswith(".exe") or name.endswith(".appimage")):
        return path.is_file()
    return False


def discover_targets(extra_paths=None, include_system=True):
    """Discover BeamNG.Earth Windows .exe, Linux AppImage/onedir/host, and source checkouts."""
    candidates = []
    seen = set()

    def _add(p):
        try:
            rp = Path(p).resolve()
        except OSError:
            return
        key = str(rp).lower() if os.name == "nt" else str(rp)
        if key not in seen:
            seen.add(key)
            candidates.append(rp)

    def _scan_dir(d, recurse_earth_dirs=False):
        try:
            d = Path(d)
            if not d.is_dir():
                return
            if (d / "server" / "earthd.py").is_file() and (d / "mod" / "beamng_earth").is_dir():
                _add(d)
            elif _onedir_layout(d) is not None:
                _add(d)
            elif (d / "Resources" / "Client" / "beamng_earth.zip").is_file():
                _add(d)
            for child in sorted(d.iterdir()):
                if child.is_file() and _is_candidate_exe(child):
                    _add(child)
                elif recurse_earth_dirs and child.is_dir() and "beamng" in child.name.lower() and not child.name.endswith(f".{TAG}.appdir"):
                    _scan_dir(child, recurse_earth_dirs=False)
        except OSError:
            pass

    for raw in (extra_paths or ()):
        p = Path(raw)
        if p.is_file():
            _add(p)
        elif p.is_dir():
            _scan_dir(p, recurse_earth_dirs=True)

    if include_system:
        for proc_exe in _running_earth_exes():
            _add(proc_exe)
        here = Path(__file__).resolve()
        for base in (Path.cwd(), Path.cwd().parent, here.parent.parent, here.parent.parent.parent):
            _scan_dir(base, recurse_earth_dirs=False)
        home = Path.home()
        for folder in ("Desktop", "Downloads", "Documents", "Applications", "Games"):
            _scan_dir(home / folder, recurse_earth_dirs=True)
        if os.name == "nt":
            local_app = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
            _scan_dir(local_app / "beamng-earth", recurse_earth_dirs=True)
            _scan_dir(local_app / "Programs", recurse_earth_dirs=True)
            for env_key in ("ProgramFiles", "ProgramFiles(x86)"):
                pf = os.environ.get(env_key)
                if pf:
                    _scan_dir(Path(pf), recurse_earth_dirs=True)
        else:
            _scan_dir(home / ".local" / "bin", recurse_earth_dirs=True)
            _scan_dir(home / ".cache" / "beamng-earth", recurse_earth_dirs=True)
            _scan_dir(Path("/opt"), recurse_earth_dirs=True)

    valid_modes = ("exe", "appimage", "onedir", "elf", "source", "host")
    results = []
    for cand in candidates:
        info = inspect_target(cand)
        if info["mode"] in valid_modes and info["state"] != "invalid":
            results.append(info)
    results.sort(key=lambda x: (not x["compatible"], x["mode"] not in ("exe", "appimage"), x["target"].lower()))
    return results


def _read_ini_kv(path):
    out = {}
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return out
    for enc in ("utf-8-sig", "mbcs", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith((";", "#", "[")):
            k, v = line.split("=", 1)
            out[k.strip()] = os.path.expandvars(v.strip())
    return out


def _native_linux_beamng_user_dirs(home=None, xdg_data_home=None):
    """Discover native Linux BeamNG.drive user folders in upstream priority order."""
    home = Path(home or Path.home()).expanduser()
    xdg_cache = Path(os.environ.get("XDG_CACHE_HOME") or (home / ".cache"))
    data_roots = []
    raw_xdg_data = xdg_data_home or os.environ.get("XDG_DATA_HOME")
    if raw_xdg_data:
        data_roots.append(Path(raw_xdg_data).expanduser())
    default_share = home / ".local" / "share"
    if default_share not in data_roots:
        data_roots.append(default_share)

    explicit = []
    for app_json_cand in (
        home / ".cache" / "beamng-earth" / "app.json",
        xdg_cache / "beamng-earth" / "app.json",
    ):
        app_json = read_json(app_json_cand, {})
        told = app_json.get("gameUserDir")
        if isinstance(told, str) and told.strip():
            tp = Path(told.strip()).expanduser()
            if tp.is_dir():
                explicit.append(tp)

    discovered = []
    for share_root in data_roots:
        ini_uf = _read_ini_kv(share_root / "BeamNG" / "BeamNG.drive.ini").get("userFolder")
        if ini_uf:
            ufp = Path(ini_uf).expanduser()
            if (ufp / "current").is_dir():
                discovered.append(ufp / "current")
            elif ufp.is_dir():
                discovered.append(ufp)
        cur = share_root / "BeamNG" / "BeamNG.drive" / "current"
        if cur.is_dir():
            discovered.append(cur)
        for ver_dir in sorted(glob.glob(str(share_root / "BeamNG" / "BeamNG.drive" / "[0-9]*")), reverse=True):
            if Path(ver_dir).is_dir():
                discovered.append(Path(ver_dir))
        leg_cur = share_root / "BeamNG.drive" / "current"
        if leg_cur.is_dir():
            discovered.append(leg_cur)
        for leg_ver in sorted(glob.glob(str(share_root / "BeamNG.drive" / "[0-9]*")), reverse=True):
            if Path(leg_ver).is_dir():
                discovered.append(Path(leg_ver))

    def _log_mtime(p):
        try:
            return (Path(p) / "beamng.log").stat().st_mtime
        except OSError:
            return -1.0

    if len(discovered) > 1 and any(_log_mtime(d) >= 0.0 for d in discovered):
        discovered = [
            d for _idx, d in sorted(
                enumerate(discovered),
                key=lambda pair: (_log_mtime(pair[1]), -pair[0]),
                reverse=True,
            )
        ]
    return explicit + discovered


def discover_user_dirs(include_system=True, extra_paths=None):
    """Discover BeamNG.drive user folders on Windows or native Linux in upstream priority order."""
    candidates = []
    for p in (extra_paths or ()):
        candidates.append(Path(p))
    if include_system:
        if os.name == "nt":
            local = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
            app_json = read_json(local / "beamng-earth" / "app.json", {})
            told = app_json.get("gameUserDir")
            if isinstance(told, str) and told:
                tp = Path(told)
                if (tp / "mods").is_dir():
                    candidates.append(tp)
            ini_uf = _read_ini_kv(local / "BeamNG" / "BeamNG.drive.ini").get("userFolder")
            if ini_uf:
                ufp = Path(ini_uf)
                if (ufp / "current").is_dir():
                    candidates.append(ufp / "current")
                elif ufp.is_dir():
                    candidates.append(ufp)
            candidates.append(local / "BeamNG" / "BeamNG.drive" / "current")
            for legacy in sorted(glob.glob(str(local / "BeamNG.drive" / "[0-9]*")), reverse=True):
                candidates.append(Path(legacy))
        else:
            candidates.extend(_native_linux_beamng_user_dirs())

    out, seen = [], set()
    for c in candidates:
        try:
            rc = c.resolve()
        except OSError:
            continue
        if not rc.is_dir():
            continue
        key = str(rc).lower() if os.name == "nt" else str(rc)
        if key not in seen:
            seen.add(key)
            out.append(rc)
    return out


def _default_state_paths(target):
    override = os.environ.get("BNE_BRF_STATE_DIR")
    if override:
        base = Path(override)
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "BeamNG-Earth-Bridge-Rail-Fixes"
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        base = Path(xdg_data) / "BeamNG-Earth-Bridge-Rail-Fixes"
    digest = hashlib.sha256(str(Path(target).resolve()).encode("utf-8")).hexdigest()[:12]
    return base / f"control-{digest}.json", base / f"status-{digest}.json"


def install_target(target, payload_dir, config_path=None, status_path=None,
                   apply_fixes=True, enable_reporter=True, apply_immediately=True):
    """Verify compatibility, stage, and optionally apply patches to a supported BeamNG.Earth target."""
    verify_compatibility(target)
    if config_path is None or status_path is None:
        def_cfg, def_status = _default_state_paths(target)
        config_path = config_path or def_cfg
        status_path = status_path or def_status
    token = uuid.uuid4().hex
    atomic_json(config_path, {
        "enabled": bool(apply_fixes or enable_reporter),
        "modPath": str(Path(__file__).resolve()),
        "requestToken": token,
    })
    record = prepare(
        target,
        config_path,
        status_path,
        payload_dir,
        operation="install",
        token=token,
        apply_fixes=apply_fixes,
        enable_reporter=enable_reporter,
    )
    if apply_immediately and record.get("state") == "pending":
        record = apply_now(record)
    return record


def cleanup_backup(target):
    """Remove Bridge & Rail backup/receipt files once target is verified restored/unpatched."""
    resolved, mode = _resolve_target_path(target)
    removed = False
    if mode in ("source", "onedir", "host"):
        state_dir = resolved / f".{TAG}"
        sidecar = read_json(state_dir / "receipt.json", {})
        if sidecar and sidecar.get("state") not in ("restored", "cancelled"):
            for entry in sidecar.get("files", []):
                dest = resolved / entry["rel"]
                if entry.get("added") and dest.exists():
                    raise PatchError("Cannot delete backup while patched files are still present")
                if not entry.get("added") and entry.get("originalSha"):
                    if not dest.is_file() or sha256_file(dest) != entry["originalSha"]:
                        raise PatchError("Cannot delete backup until original files are restored")
        if state_dir.is_dir():
            shutil.rmtree(state_dir)
            removed = True
        return removed

    sidecar_path = resolved.with_name(resolved.name + f".{TAG}.json")
    backup_path = resolved.with_name(resolved.name + f".{TAG}.original")
    appdir_path = resolved.with_name(resolved.name + f".{TAG}.appdir")
    sidecar = read_json(sidecar_path, {})
    digest = sha256_file(resolved)
    if sidecar.get("originalSha") and digest != sidecar.get("originalSha"):
        raise PatchError("Cannot delete backup while patched executable is still active")
    if not sidecar and inspect_target(resolved).get("patched"):
        raise PatchError("Cannot delete backup while executable is patched")
    _cleanup_stale_pending(resolved, sidecar)
    for p in (backup_path, sidecar_path):
        if p.is_file():
            p.unlink()
            removed = True
    if appdir_path.is_dir():
        shutil.rmtree(appdir_path, ignore_errors=True)
        removed = True
    return removed


def restore_target(target, payload_dir=None, config_path=None, status_path=None,
                   apply_immediately=True, cleanup=False):
    """Stage and optionally apply restoration of the original exe or source dir."""
    if config_path is None or status_path is None:
        def_cfg, def_status = _default_state_paths(target)
        config_path = config_path or def_cfg
        status_path = status_path or def_status
    token = uuid.uuid4().hex
    atomic_json(config_path, {
        "enabled": False,
        "modPath": str(Path(__file__).resolve()),
        "requestToken": token,
    })
    record = prepare(
        target,
        config_path,
        status_path,
        payload_dir,
        operation="restore",
        token=token,
    )
    if apply_immediately and record.get("state") == "pending":
        record = apply_now(record)
    if cleanup and record.get("state") == "restored":
        cleanup_backup(target)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--status", type=Path)
    parser.add_argument("--payload", type=Path, default=Path(__file__).resolve().parent.parent / "payload")
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--apply-now", action="store_true")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args(argv)
    if args.inspect:
        info = inspect_target(args.target)
        print(json.dumps(info, indent=2))
        return 0 if info["compatible"] else 2
    if not args.config or not args.status:
        def_cfg, def_status = _default_state_paths(args.target)
        args.config = args.config or def_cfg
        args.status = args.status or def_status
    if args.restore:
        record = restore_target(
            args.target,
            payload_dir=args.payload,
            config_path=args.config,
            status_path=args.status,
            apply_immediately=args.apply_now,
            cleanup=args.cleanup and args.apply_now,
        )
    else:
        record = prepare(
            args.target,
            args.config,
            args.status,
            args.payload,
            operation="install",
        )
        if args.apply_now and record["state"] == "pending":
            record = apply_now(record)
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
