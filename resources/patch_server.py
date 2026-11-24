"""Prepare a reversible local patch for BeamNG.Earth (packaged .exe or source tree).

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
import importlib.util
import json
import marshal
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import uuid
import zlib

VERSION = "0.4.0"
MARKER = "EARTH_BRIDGE_RAIL_FIXES_0_4_0"
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


def _verify_earthd_dispatch_hook(earthd_co):
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
            _verify_earthd_dispatch_hook(_load_mod_code("earthd"))
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
                        if mod_name in replaced_v2 or mod_name == "bench_pose" or k != 0 or "." in mod_name:
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
            if mod_name in replaced_set or mod_name == "bench_pose" or k != 0 or "." in mod_name:
                continue
            if p_off < 12 or p_off + p_len > offset:
                continue
            co = marshal.loads(zlib.decompress(data[p_off:p_off + p_len]))
            _scan_missing_exports_in_code(co, replaced_set, payload_exports, {}, missing)
            if missing:
                _verify_earthd_dispatch_hook(_load_mod_code("earthd"))
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
        if 'elif kind == "terrain_patch":' not in raw_earthd:
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
                            or mod_name in ("bench_pose", "bridge_rail_report")
                            or mod_name.startswith("_brf_orig_")
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
            if mod_name in replaced_set or mod_name in ("bench_pose", "bridge_rail_report"):
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
        _verify_earthd_dispatch_hook(earthd_code)
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
        _verify_earthd_dispatch_hook(earthd_code)
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


def _patch_executable_with_tier(data, payload_dir, apply_fixes=True, enable_reporter=True):
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
    carchive_replacements = {}
    if pyz_entry[4]:
        carchive_replacements[pyz_entry[7]] = (zlib.compress(new_pz, 6), len(new_pz), 1, b"z")
    else:
        carchive_replacements[pyz_entry[7]] = (new_pz, len(new_pz), 0, b"z")

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

        after, signed, patch_tier, solver_mode = _patch_executable_with_tier(
            pristine, payload_dir, apply_fixes=apply_fixes, enable_reporter=enable_reporter
        )
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
                raw_earthd = _read_pristine(root, "server/earthd.py").decode("utf-8")
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
                if mod in _CACHE_REV_MODULES:
                    raw_bytes = _stamp_addon_rev(raw_bytes.decode("utf-8"), cache_rev).encode("utf-8")
                desired[rel] = raw_bytes
        fallback_roads = (payload_root / MOD_ROADS_LUA).read_bytes() if (payload_root / MOD_ROADS_LUA).is_file() else None
        desired[MOD_ROADS_LUA] = patch_roads_lua_bytes(_read_pristine(root, MOD_ROADS_LUA), fallback_bytes=fallback_roads)
    if enable_reporter:
        desired["server/bridge_rail_report.py"] = (payload_root / "server/bridge_rail_report.py").read_bytes()
        if patch_tier != TIER_FULL_SOLVER or solver_mode == SOLVER_MODE_V2:
            # Hook ONLY bridge_rail_report (and BNE_BRF_ADDON_REV for server_v2) into untouched earthd.py cleanly.
            raw_earthd = _read_pristine(root, "server/earthd.py").decode("utf-8")
            if patch_tier == TIER_FULL_SOLVER and solver_mode == SOLVER_MODE_V2:
                cache_rev = _server_cache_rev(payload_dir)
                raw_earthd = (
                    "import os as _brf_os\n"
                    f"_BRF_ADDON_REV = {cache_rev!r}\n"
                    'if not _brf_os.environ.get("BNE_BRF_ADDON_REV_PIN"):\n'
                    '    _brf_os.environ["BNE_BRF_ADDON_REV"] = _BRF_ADDON_REV\n'
                ) + raw_earthd
            anchor = '        elif kind == "terrain_patch":'
            if anchor not in raw_earthd:
                raise PatchError("Could not locate earthd.dispatch anchor for standalone reporter")
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


def _resolve_target_path(target):
    path = Path(target).resolve(strict=True)
    if path.is_file() and path.suffix.lower() == ".exe":
        return path, "exe"
    if path.is_dir():
        return path, "source"
    if path.is_file() and path.name in ("earthd.py", "railways.py") and path.parent.name == "server":
        return path.parent.parent, "source"
    raise PatchError("Select a BeamNG.Earth executable or source directory")


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
    return _prepare_source(resolved_target, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter)


def apply_now(record):
    """Apply staged exe or source changes immediately (offline CLI / unit tests)."""
    state = read_json(record["config"], {})
    if record.get("token") and state.get("requestToken") != record["token"]:
        raise PatchError("A newer request superseded this patch")

    if record.get("mode") == "source":
        root = Path(record["target"])
        for item in record["items"]:
            dest = root / item["rel"]
            if item["expectedSha"]:
                if not dest.is_file() or sha256_bytes(dest.read_bytes()) != item["expectedSha"]:
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
                    if sha256_bytes(staged.read_bytes()) != item["replacementSha"]:
                        raise PatchError(f"Staged file changed ({item['rel']})")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(staged, dest)
                    if sha256_bytes(dest.read_bytes()) != item["replacementSha"]:
                        raise PatchError(f"Verification failed after write ({item['rel']})")
            except PermissionError as exc:
                raise PatchError("BeamNG.Earth is currently locking files. Close BeamNG.Earth completely and try again.") from exc
        stage_dir = record.get("stageDir")
        if stage_dir and Path(stage_dir).is_dir():
            shutil.rmtree(stage_dir, ignore_errors=True)
    else:
        target, pending = Path(record["target"]), Path(record["pending"])
        if sha256_bytes(target.read_bytes()) != record["expectedSha"] or sha256_bytes(pending.read_bytes()) != record["replacementSha"]:
            raise PatchError("Files changed after preparation; refusing to overwrite")
        try:
            os.replace(pending, target)
        except PermissionError as exc:
            raise PatchError("BeamNG.Earth is currently running. Close BeamNG.Earth completely and try again.") from exc
        except OSError as exc:
            if getattr(exc, "winerror", None) in (5, 32):
                raise PatchError("BeamNG.Earth is currently running. Close BeamNG.Earth completely and try again.") from exc
            raise

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


def inspect_target(target):
    """Read-only inspection of a BeamNG.Earth executable or source directory.

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
            info["reason"] = "Select a BeamNG.Earth executable or source directory"
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
            and all((resolved / f["rel"]).is_file() and sha256_bytes((resolved / f["rel"]).read_bytes()) == f["patchedSha"]
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

    # EXE mode (read-only)
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
        and sha256_bytes(backup_path.read_bytes()) == sidecar.get("originalSha")
    )

    patched_in_pyz = ("bridge_rail_report" in toc) or (ORIG_EARTHD_MODULE in toc)
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
        if _win_rel(rel) not in entry_map:
            info["reason"] = f"Expected bundled mod file missing in archive: {_win_rel(rel)}"
            info["state"] = "incompatible"
            return info
    try:
        roads_entry = entry_map[_win_rel(MOD_ROADS_LUA)]
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
    """Return executable paths of running BeamNG.Earth*.exe processes on Windows."""
    if os.name != "nt":
        return []
    found = []
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


def _is_candidate_exe(path):
    name = path.name.lower()
    if not (name.startswith("beamng.earth") and name.endswith(".exe")):
        return False
    if f".{TAG}." in name:
        return False
    return path.is_file()


def discover_targets(extra_paths=None, include_system=True):
    """Discover BeamNG.Earth executables and source checkouts, returning inspect_target dicts."""
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
            for child in sorted(d.iterdir()):
                if child.is_file() and _is_candidate_exe(child):
                    _add(child)
                elif recurse_earth_dirs and child.is_dir() and "beamng" in child.name.lower():
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
        for folder in ("Desktop", "Downloads", "Documents"):
            _scan_dir(home / folder, recurse_earth_dirs=True)
        local_app = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
        _scan_dir(local_app / "beamng-earth", recurse_earth_dirs=True)
        _scan_dir(local_app / "Programs", recurse_earth_dirs=True)
        for env_key in ("ProgramFiles", "ProgramFiles(x86)"):
            pf = os.environ.get(env_key)
            if pf:
                _scan_dir(Path(pf), recurse_earth_dirs=True)

    results = []
    for cand in candidates:
        info = inspect_target(cand)
        if info["mode"] in ("exe", "source") and info["state"] != "invalid":
            results.append(info)
    results.sort(key=lambda x: (not x["compatible"], x["mode"] != "exe", x["target"].lower()))
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


def discover_user_dirs(include_system=True, extra_paths=None):
    """Discover BeamNG.drive user folders on Windows in upstream priority order."""
    candidates = []
    for p in (extra_paths or ()):
        candidates.append(Path(p))
    if include_system and os.name == "nt":
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
    base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "BeamNG-Earth-Bridge-Rail-Fixes"
    digest = hashlib.sha256(str(Path(target).resolve()).encode("utf-8")).hexdigest()[:12]
    return base / f"control-{digest}.json", base / f"status-{digest}.json"


def install_target(target, payload_dir, config_path=None, status_path=None,
                   apply_fixes=True, enable_reporter=True, apply_immediately=True):
    """Verify compatibility, stage, and optionally apply patches to an exe or source dir."""
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
    if mode == "source":
        state_dir = resolved / f".{TAG}"
        sidecar = read_json(state_dir / "receipt.json", {})
        if sidecar and sidecar.get("state") not in ("restored", "cancelled"):
            for entry in sidecar.get("files", []):
                dest = resolved / entry["rel"]
                if entry.get("added") and dest.exists():
                    raise PatchError("Cannot delete backup while patched files are still present")
                if not entry.get("added") and entry.get("originalSha"):
                    if not dest.is_file() or sha256_bytes(dest.read_bytes()) != entry["originalSha"]:
                        raise PatchError("Cannot delete backup until original source files are restored")
        if state_dir.is_dir():
            shutil.rmtree(state_dir)
            removed = True
        return removed

    sidecar_path = resolved.with_name(resolved.name + f".{TAG}.json")
    backup_path = resolved.with_name(resolved.name + f".{TAG}.original")
    sidecar = read_json(sidecar_path, {})
    digest = sha256_bytes(resolved.read_bytes())
    if sidecar.get("originalSha") and digest != sidecar.get("originalSha"):
        raise PatchError("Cannot delete backup while patched executable is still active")
    if not sidecar and inspect_target(resolved).get("patched"):
        raise PatchError("Cannot delete backup while executable is patched")
    _cleanup_stale_pending(resolved, sidecar)
    for p in (backup_path, sidecar_path):
        if p.is_file():
            p.unlink()
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
