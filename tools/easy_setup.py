"""1-Click Install & Uninstall helper for BeamNG.Earth - Bridge, Rail & Tunnel Fixes.

Pure Python standard library only. Provides automatic BeamNG.Earth (Windows .exe,
Linux AppImage/onedir/host, and source tree) + native BeamNG.drive user-folder
discovery, compatibility pre-checks, running-process close/relaunch handling,
unpacked & zipped mod sync, and byte-for-byte reversible uninstallation.
"""
import argparse
import ctypes
import glob
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
import zlib

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RESOURCES_DIR = ROOT / "resources"
PAYLOAD_DIR = ROOT / "payload"

if str(RESOURCES_DIR) not in sys.path:
    sys.path.insert(0, str(RESOURCES_DIR))

import patch_server  # noqa: E402

APP_TITLE = "BeamNG.Earth - Bridge, Rail & Tunnel Fixes"
STATE_FOLDER_NAME = "BeamNG-Earth-Bridge-Rail-Fixes"
RECEIPT_FILENAME = "install_receipt.json"
LAST_TARGET_FILENAME = "last_target.txt"

MOD_REL_FILES = (
    ("lua/ge/extensions/beamngEarth/roads.lua", patch_server.MOD_ROADS_LUA),
    ("lua/ge/extensions/beamngEarth/bugreport.lua", patch_server.MOD_BUGREPORT_LUA),
    ("lua/ge/extensions/core/input/actions/beamngEarth.json", patch_server.MOD_ACTIONS_JSON),
    ("settings/inputmaps/keyboard_beamngEarth.json", patch_server.MOD_KEYMAP_JSON),
    ("scripts/beamng_earth/modScript.lua", patch_server.MOD_SCRIPT_LUA),
)

_IGNORED_EXE_MARKERS = (
    ".earthbridgerailfixes.",
    ".original",
    ".pending",
    ".swap.",
    ".tmp",
    ".part",
    "unins000",
)


class SetupCancelled(RuntimeError):
    """Raised when the user cancels a dialog or prompt."""


def _is_supported_host_os():
    return os.name == "nt" or sys.platform.startswith("linux")


def default_state_dir(local_appdata=None):
    override = os.environ.get("BNE_BRF_STATE_DIR")
    if override:
        return Path(override).resolve()
    if local_appdata is not None:
        return Path(local_appdata).resolve() / STATE_FOLDER_NAME
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base).resolve() / STATE_FOLDER_NAME
    xdg_data = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(xdg_data).resolve() / STATE_FOLDER_NAME


def _norm_key(path):
    try:
        resolved = os.path.realpath(str(path))
    except OSError:
        resolved = os.path.abspath(str(path))
    return os.path.normcase(os.path.normpath(resolved))


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def atomic_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_json_safe(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {} if default is None else default


def _is_ignored_exe_name(name):
    low = name.lower()
    if not (low.endswith(".exe") or low.endswith(".appimage")):
        return True
    return any(tok in low for tok in _IGNORED_EXE_MARKERS)


def normalize_candidate_target(candidate):
    """Normalize a file or folder path into a valid BeamNG.Earth .exe, .AppImage, onedir, host, or source root."""
    if not candidate:
        return None
    try:
        raw_p = Path(str(candidate).strip().strip('"')).expanduser()
        if not raw_p.exists():
            return None
        if raw_p.is_symlink() and raw_p.name == "AppRun":
            parent_dir = raw_p.parent.resolve()
            if getattr(patch_server, "_onedir_layout", lambda _r: None)(parent_dir) is not None:
                return parent_dir
        p = raw_p.resolve()
    except OSError:
        return None

    if p.is_file():
        low_name = p.name.lower()
        if low_name.endswith(".exe") and not _is_ignored_exe_name(p.name):
            try:
                with p.open("rb") as fh:
                    if fh.read(2) != b"MZ":
                        return None
                return p
            except OSError:
                return None
        if low_name.endswith(".appimage") and not _is_ignored_exe_name(p.name):
            try:
                with p.open("rb") as fh:
                    head = fh.read(256)
                if head[:4] == b"\x7fELF" or patch_server.APPIMAGE_WRAPPER_MAGIC in head:
                    return p
            except OSError:
                return None
        if low_name == "launcher.py" and (p.parent / "server" / "earthd.py").is_file():
            return p.parent
        if low_name in ("earthd.py", "railways.py") and p.parent.name.lower() == "server":
            root = p.parent.parent
            if (root / "mod" / "beamng_earth").is_dir():
                return root
        if p.name in ("run_host.sh", "BeamMP-Server") and (p.parent / "Resources" / "Client" / "beamng_earth.zip").is_file():
            return p.parent
        if p.name == "BeamNG.Earth":
            onedir_fn = getattr(patch_server, "_onedir_layout", None)
            if callable(onedir_fn):
                if p.parent.name == "BeamNG.Earth" and p.parent.parent.name == "lib" and onedir_fn(p.parent.parent.parent) is not None:
                    return p.parent.parent.parent
                if onedir_fn(p.parent) is not None:
                    return p.parent
        return None

    if p.is_dir():
        if (p / "server" / "earthd.py").is_file() and (p / "mod" / "beamng_earth").is_dir():
            return p
        onedir_fn = getattr(patch_server, "_onedir_layout", None)
        if callable(onedir_fn) and onedir_fn(p) is not None:
            return p
        if (p / "Resources" / "Client" / "beamng_earth.zip").is_file():
            return p
        packaged = sorted(list(p.glob("BeamNG.Earth*.exe")) + list(p.glob("BeamNG.Earth*.AppImage")))
        valid_bins = []
        for item in packaged:
            if item.is_file() and not _is_ignored_exe_name(item.name):
                norm_item = normalize_candidate_target(item)
                if norm_item is not None:
                    valid_bins.append(norm_item)
        if len(valid_bins) == 1:
            return valid_bins[0]
    return None


def _self_exe_excluded_pids(self_exe=False, exclude_pid=None, exclude_pids=None):
    """Build the set of own/bootloader PIDs to ignore when scanning running BeamNG.Earth instances."""
    excluded = {int(exclude_pid) if exclude_pid else os.getpid()}
    if exclude_pids:
        for p in exclude_pids:
            if p and int(p) > 0:
                excluded.add(int(p))
    if self_exe:
        ppid = os.getppid()
        if ppid and int(ppid) > 0:
            excluded.add(int(ppid))
        try:
            env_parent = int(os.environ.get("BNGEARTH_PARENT") or 0)
        except ValueError:
            env_parent = 0
        if env_parent > 0:
            excluded.add(env_parent)
    return excluded


def find_running_beamng_earth_processes(exclude_pid=None, *, self_exe=False, exclude_pids=None):
    """Return [(pid, exe_or_appimage_path), ...] for running BeamNG.Earth processes on Windows or Linux."""
    excluded = _self_exe_excluded_pids(self_exe=self_exe, exclude_pid=exclude_pid, exclude_pids=exclude_pids)
    found = []

    if os.name == "nt":
        kw = {"capture_output": True, "text": True, "timeout": 5}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW

        ps_cmd = (
            "Get-CimInstance Win32_Process -Filter \"Name LIKE 'BeamNG.Earth%.exe'\" | "
            "ForEach-Object { \"$($_.ProcessId)|$($_.ExecutablePath)\" }"
        )
        try:
            res = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                **kw,
            )
            if res.returncode == 0 and res.stdout.strip():
                for line in res.stdout.splitlines():
                    line = line.strip()
                    if "|" not in line:
                        continue
                    pid_str, exe_path = line.split("|", 1)
                    try:
                        pid = int(pid_str.strip())
                    except ValueError:
                        continue
                    if pid <= 0 or pid in excluded:
                        continue
                    found.append((pid, exe_path.strip()))
                return found
        except (OSError, subprocess.SubprocessError):
            pass

        try:
            res = subprocess.run(
                ["wmic", "process", "where", "name like 'BeamNG.Earth%.exe'", "get", "ExecutablePath,ProcessId", "/format:csv"],
                **kw,
            )
            if res.returncode == 0 and res.stdout.strip():
                for line in res.stdout.splitlines():
                    parts = [c.strip() for c in line.strip().split(",")]
                    if len(parts) >= 3 and parts[-1].isdigit():
                        pid = int(parts[-1])
                        if pid > 0 and pid not in excluded:
                            found.append((pid, parts[-2]))
                if found:
                    return found
        except (OSError, subprocess.SubprocessError):
            pass

        try:
            res = subprocess.run(["tasklist", "/FI", "IMAGENAME eq BeamNG.Earth*", "/FO", "CSV", "/NH"], **kw)
            if res.returncode == 0 and res.stdout.strip():
                for line in res.stdout.splitlines():
                    cols = [c.strip().strip('"') for c in line.split('","')]
                    if len(cols) >= 2 and cols[0].lower().startswith("beamng.earth") and cols[1].isdigit():
                        pid = int(cols[1])
                        if pid > 0 and pid not in excluded:
                            found.append((pid, ""))
        except (OSError, subprocess.SubprocessError):
            pass
        return found

    proc_root = Path("/proc")
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid <= 0 or pid in excluded:
                continue
            appimage_val = ""
            try:
                env_raw = (entry / "environ").read_bytes()
                for kv in env_raw.split(b"\x00"):
                    if kv.startswith(b"APPIMAGE="):
                        appimage_val = kv[9:].decode("utf-8", errors="surrogateescape").strip()
                        break
            except OSError:
                pass
            if appimage_val and "beamng.earth" in os.path.basename(appimage_val).lower():
                found.append((pid, appimage_val))
                continue
            try:
                exe_real = os.path.realpath(str(entry / "exe"))
                if os.path.basename(exe_real).lower().startswith("beamng.earth"):
                    found.append((pid, exe_real))
            except OSError:
                pass
    return found


def discover_targets(search_roots=None, state_root=None, include_running=True):
    """Discover candidate BeamNG.Earth installations (.exe, .AppImage, onedir, host, or source) in priority order."""
    candidates = []
    seen = set()

    def _add(raw_path):
        norm = normalize_candidate_target(raw_path)
        if norm is None:
            return
        key = _norm_key(norm)
        if key not in seen:
            seen.add(key)
            candidates.append(norm)

    sdir = Path(state_root) if state_root else default_state_dir()
    receipt = read_json_safe(sdir / RECEIPT_FILENAME, {})
    if receipt.get("target"):
        _add(receipt["target"])
    last_target_file = sdir / LAST_TARGET_FILENAME
    if last_target_file.is_file():
        try:
            _add(last_target_file.read_text(encoding="utf-8").strip())
        except OSError:
            pass

    if search_roots is not None:
        roots = [Path(r) for r in search_roots if r]
    else:
        if include_running:
            for _pid, exe_path in find_running_beamng_earth_processes():
                if exe_path:
                    _add(exe_path)

        ps_discover = getattr(patch_server, "discover_targets", None)
        if callable(ps_discover):
            try:
                for item in ps_discover():
                    if isinstance(item, dict):
                        if item.get("target"):
                            _add(item["target"])
                    else:
                        _add(item)
            except Exception:
                pass

        home = Path.home()
        roots = [
            Path.cwd(),
            Path.cwd().parent,
            ROOT,
            ROOT.parent,
            home / "Downloads",
            home / "Desktop",
        ]
        if os.name == "nt":
            local_app = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
            prog_files = os.environ.get("ProgramFiles")
            prog_x86 = os.environ.get("ProgramFiles(x86)")
            onedrive = os.environ.get("OneDrive")
            if onedrive:
                roots.append(Path(onedrive) / "Desktop")
                roots.append(Path(onedrive) / "Downloads")
            roots.extend([
                local_app / "Programs",
                local_app / "Programs" / "BeamNG.Earth",
                local_app / "beamng-earth",
            ])
            if prog_files:
                roots.append(Path(prog_files))
                roots.append(Path(prog_files) / "BeamNG.Earth")
            if prog_x86:
                roots.append(Path(prog_x86) / "BeamNG.Earth")
        else:
            roots.extend([
                home / "Applications",
                home / "Games",
                home / ".local" / "bin",
                home / ".cache" / "beamng-earth",
                Path("/opt"),
                Path("/opt/BeamNG.Earth"),
            ])

    for folder in roots:
        try:
            folder = Path(folder)
            if folder.is_file():
                _add(folder)
                continue
            if not folder.is_dir():
                continue
        except OSError:
            continue

        _add(folder)
        try:
            bins = list(folder.glob("BeamNG.Earth*.exe")) + list(folder.glob("BeamNG.Earth*.AppImage"))
            for bin_path in sorted(bins, key=lambda p: p.stat().st_mtime, reverse=True):
                if bin_path.is_file() and not _is_ignored_exe_name(bin_path.name):
                    _add(bin_path)
        except OSError:
            pass

        try:
            for sub in sorted(folder.glob("BeamNG.Earth*")):
                if sub.is_dir() and not sub.name.endswith(f".{patch_server.TAG}.appdir"):
                    _add(sub)
                    sub_bins = list(sub.glob("BeamNG.Earth*.exe")) + list(sub.glob("BeamNG.Earth*.AppImage"))
                    for bin_path in sorted(sub_bins, key=lambda p: p.stat().st_mtime, reverse=True):
                        if bin_path.is_file() and not _is_ignored_exe_name(bin_path.name):
                            _add(bin_path)
        except OSError:
            pass

    return candidates


def _parse_ini_userfolder(ini_path):
    try:
        text = Path(ini_path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith((";", "#")):
            continue
        m = re.match(r"^(?:userFolder|userpath)\s*=\s*(.+)$", stripped, flags=re.IGNORECASE)
        if m:
            val = m.group(1).strip().strip('"').strip("'")
            if val:
                return os.path.expandvars(os.path.expanduser(val))
    return None


def discover_beamng_user_dirs(local_appdata=None):
    """Return existing BeamNG.drive user folders on Windows or native Linux in upstream priority order."""
    if local_appdata is None:
        ps_user_dirs = getattr(patch_server, "discover_user_dirs", None)
        if callable(ps_user_dirs):
            try:
                res = [Path(p) for p in ps_user_dirs() if p and Path(p).is_dir()]
                if res:
                    return res
            except Exception:
                pass

    raw_candidates = []
    if os.name != "nt" and local_appdata is None:
        native_fn = getattr(patch_server, "_native_linux_beamng_user_dirs", None)
        if callable(native_fn):
            raw_candidates.extend(native_fn())
    else:
        base = Path(local_appdata or os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        app_json = read_json_safe(base / "beamng-earth" / "app.json", {})
        game_user_dir = app_json.get("gameUserDir")
        if isinstance(game_user_dir, str) and game_user_dir.strip():
            raw_candidates.append(Path(game_user_dir.strip()))

        for ini_cand in (
            base / "BeamNG" / "BeamNG.drive.ini",
            base / "BeamNG.drive" / "startup.ini",
            base / "BeamNG.drive" / "userpath.ini",
        ):
            uf = _parse_ini_userfolder(ini_cand)
            if uf:
                uf_path = Path(uf)
                if (uf_path / "current").is_dir():
                    raw_candidates.append(uf_path / "current")
                else:
                    raw_candidates.append(uf_path)

        raw_candidates.append(base / "BeamNG" / "BeamNG.drive" / "current")

        legacy_glob = sorted(glob.glob(str(base / "BeamNG.drive" / "[0-9]*")), reverse=True)
        for d in legacy_glob:
            raw_candidates.append(Path(d))

    out = []
    seen = set()
    for cand in raw_candidates:
        try:
            if cand.is_dir():
                key = _norm_key(cand)
                if key not in seen:
                    seen.add(key)
                    out.append(cand.resolve())
        except OSError:
            continue
    return out


def _exe_embedded_py_ver(exe_path):
    data = Path(exe_path).read_bytes()
    _start, _toff, _cookie_pos, py, _lib, _entries = patch_server.archive_entries(data)
    return py


def verify_target_compatibility(target, payload_dir=PAYLOAD_DIR):
    """Verify target compatibility before modifying any files on disk."""
    target = Path(target)
    ps_verify = getattr(patch_server, "verify_compatibility", None)
    if callable(ps_verify):
        return ps_verify(target)

    resolved = target.resolve()
    if resolved.is_dir():
        patch_server.validate_source_compatibility(resolved)
        return {"mode": "source", "target": str(resolved)}

    if resolved.is_file() and resolved.suffix.lower() == ".exe":
        data = resolved.read_bytes()
        sidecar = read_json_safe(resolved.with_name(f"{resolved.name}.{patch_server.TAG}.json"), {})
        backup = resolved.with_name(f"{resolved.name}.{patch_server.TAG}.original")
        if sidecar and patch_server.sha256_bytes(data) == sidecar.get("patchedSha") and backup.is_file():
            data = backup.read_bytes()

        start, _toff, _cookie_pos, py, _lib, entries = patch_server.archive_entries(data)
        entry_map = {e[7]: e for e in entries}
        for req in (
            patch_server.MOD_ROADS_LUA,
            patch_server.MOD_ACTIONS_JSON,
            patch_server.MOD_KEYMAP_JSON,
            patch_server.MOD_SCRIPT_LUA,
        ):
            win_key = req.replace("/", "\\")
            if win_key not in entry_map:
                raise patch_server.PatchError(f"Expected bundled mod file missing in archive: {win_key}")
        pyz_candidates = [e for e in entries if e[5] == b"z"]
        if len(pyz_candidates) != 1:
            raise patch_server.PatchError("Expected one embedded Python archive in executable")
        pyz_e = pyz_candidates[0]
        raw_blob = data[start + pyz_e[1]:start + pyz_e[1] + pyz_e[2]]
        pyz_bytes = zlib.decompress(raw_blob) if pyz_e[4] else raw_blob
        cur_py = sys.version_info.major * 100 + sys.version_info.minor
        if py == cur_py and pyz_bytes[4:8] == importlib.util.MAGIC_NUMBER:
            offset = int.from_bytes(pyz_bytes[8:12], "big", signed=True)
            import marshal
            toc = dict(marshal.loads(pyz_bytes[offset:]))
            patch_server.validate_pyz_compatibility(toc)
        return {"mode": "exe", "target": str(resolved), "py": py}

    raise patch_server.PatchError("Please select a BeamNG.Earth executable, AppImage, or source folder.")


def _extract_mod_files_from_target(target):
    """Extract {rel_inside_beamng_earth: bytes} from a restored BeamNG.Earth target."""
    target = Path(target).resolve()
    extracted = {}
    if target.is_dir():
        onedir_fn = getattr(patch_server, "_onedir_layout", None)
        layout = onedir_fn(target) if callable(onedir_fn) else None
        if layout is not None:
            _elf_rel, internal_rel = layout
            for short_rel, full_rel in MOD_REL_FILES:
                p = target / internal_rel / full_rel
                if p.is_file():
                    extracted[short_rel] = p.read_bytes()
            return extracted
        host_zip = target / "Resources" / "Client" / "beamng_earth.zip"
        if host_zip.is_file():
            with zipfile.ZipFile(host_zip, "r") as zf:
                names = set(zf.namelist())
                for short_rel, _full_rel in MOD_REL_FILES:
                    if short_rel in names:
                        extracted[short_rel] = zf.read(short_rel)
            return extracted
        for short_rel, full_rel in MOD_REL_FILES:
            p = target / full_rel
            if p.is_file():
                extracted[short_rel] = p.read_bytes()
        return extracted

    if target.is_file() and target.suffix.lower() == ".exe":
        data = target.read_bytes()
        start, _toff, _cookie, _py, _lib, entries = patch_server.archive_entries(data)
        emap = {e[7].replace("\\", "/"): e for e in entries}
        for short_rel, full_rel in MOD_REL_FILES:
            e = emap.get(full_rel)
            if e is not None:
                blob = data[start + e[1]:start + e[1] + e[2]]
                extracted[short_rel] = zlib.decompress(blob) if e[4] else blob
        return extracted

    if target.is_file() and target.name.lower().endswith(".appimage"):
        extract_fn = getattr(patch_server, "_extract_appimage_tree", None)
        if callable(extract_fn):
            with tempfile.TemporaryDirectory(prefix="brf-modext-") as tmp_dir:
                sqfs_dir = Path(tmp_dir) / "sqfs"
                extract_fn(
                    target,
                    sqfs_dir,
                    subpaths=("usr/lib/BeamNG.Earth/_internal/mod/beamng_earth",),
                )
                for short_rel, full_rel in MOD_REL_FILES:
                    p = sqfs_dir / "usr/lib/BeamNG.Earth/_internal" / full_rel
                    if p.is_file():
                        extracted[short_rel] = p.read_bytes()
    return extracted


def sync_unpacked_mods_on_install(user_dirs, payload_dir, state_root, apply_fixes=True, enable_reporter=True):
    """Update <user_dir>/mods/unpacked/beamng_earth and/or <user_dir>/mods/beamng_earth.zip preserving sibling add-ons."""
    payload_dir = Path(payload_dir)
    state_root = Path(state_root)
    synced_records = []

    for udir in user_dirs:
        udir = Path(udir)
        mod_root = udir / "mods" / "unpacked" / "beamng_earth"
        mod_zip = udir / "mods" / "beamng_earth.zip"
        has_unpacked = mod_root.is_dir()
        has_zip = mod_zip.is_file()
        if not has_unpacked and not has_zip:
            continue

        dir_id = hashlib.sha256(_norm_key(mod_root if has_unpacked else udir).encode("utf-8")).hexdigest()[:12]
        bdir = state_root / "unpacked_mod_backups" / dir_id
        bdir.mkdir(parents=True, exist_ok=True)
        files_meta = []

        if has_unpacked:
            for short_rel, _full_rel in MOD_REL_FILES:
                dest = mod_root / short_rel
                bfile = bdir / short_rel
                if not (bdir / f"{short_rel}.meta.json").is_file():
                    existed = dest.is_file()
                    orig_sha = ""
                    if existed:
                        raw = dest.read_bytes()
                        if short_rel.endswith("roads.lua") and b"return false, 'not supported'" not in raw:
                            clean_raw = patch_server.unpatch_roads_lua_bytes(raw)
                        elif short_rel.endswith("beamngEarth.json"):
                            clean_raw = patch_server.unpatch_actions_json_bytes(raw)
                        elif short_rel.endswith("keyboard_beamngEarth.json"):
                            clean_raw = patch_server.unpatch_keymap_json_bytes(raw)
                        elif short_rel.endswith("modScript.lua"):
                            clean_raw = patch_server.unpatch_modscript_bytes(raw)
                        else:
                            clean_raw = raw
                        bfile.parent.mkdir(parents=True, exist_ok=True)
                        bfile.write_bytes(clean_raw)
                        orig_sha = patch_server.sha256_bytes(clean_raw)
                    atomic_write_json(
                        bdir / f"{short_rel}.meta.json",
                        {"rel": short_rel, "existed": existed, "originalSha": orig_sha},
                    )
                elif short_rel.endswith("roads.lua") and dest.is_file():
                    cur_r = dest.read_bytes()
                    if not patch_server._roads_lua_has_brf(cur_r) and b"return false, 'not supported'" not in cur_r:
                        bfile.parent.mkdir(parents=True, exist_ok=True)
                        bfile.write_bytes(cur_r)
                        atomic_write_json(
                            bdir / f"{short_rel}.meta.json",
                            {"rel": short_rel, "existed": True, "originalSha": patch_server.sha256_bytes(cur_r)},
                        )
                meta = read_json_safe(bdir / f"{short_rel}.meta.json", {"rel": short_rel, "existed": False, "originalSha": ""})
                files_meta.append(meta)

            patch_server._sync_unpacked_user_mod_dir(
                mod_root,
                payload_dir,
                state_root,
                operation="install",
                apply_fixes=apply_fixes,
                enable_reporter=enable_reporter,
            )

        zip_meta = None
        if has_zip and hasattr(patch_server, "patch_mod_zip_bytes"):
            zip_backup = bdir / "beamng_earth.zip.original"
            zip_meta_path = bdir / "beamng_earth.zip.meta.json"
            cur_zip_bytes = mod_zip.read_bytes()
            if (not zip_meta_path.is_file() or not zip_backup.is_file()) and not patch_server._mod_zip_has_sibling(cur_zip_bytes):
                clean_zip = patch_server.unpatch_mod_zip_bytes(cur_zip_bytes)
                zip_backup.write_bytes(clean_zip)
                orig_zip_sha = patch_server.sha256_bytes(clean_zip)
                atomic_write_json(zip_meta_path, {"originalSha": orig_zip_sha})
            zip_meta = read_json_safe(zip_meta_path, {})
            base_zip_bytes = (
                cur_zip_bytes
                if (patch_server._mod_zip_has_sibling(cur_zip_bytes) or not zip_backup.is_file())
                else zip_backup.read_bytes()
            )
            patched_zip_bytes = patch_server.patch_mod_zip_bytes(
                base_zip_bytes,
                payload_dir,
                apply_fixes=apply_fixes,
                enable_reporter=enable_reporter,
            )
            tmp_zip = mod_zip.with_name(f"{mod_zip.name}.{uuid.uuid4().hex}.tmp")
            tmp_zip.write_bytes(patched_zip_bytes)
            os.replace(tmp_zip, mod_zip)

        rec = {
            "userDir": str(udir),
            "modRoot": str(mod_root) if has_unpacked else "",
            "modZip": str(mod_zip) if has_zip else "",
            "backupDir": str(bdir),
            "files": files_meta,
        }
        if zip_meta:
            rec["zipOriginalSha"] = zip_meta.get("originalSha", "")
        synced_records.append(rec)

    return synced_records


def restore_unpacked_mods_on_uninstall(restored_target, receipt, discovered_user_dirs):
    """Remove injected bugreport.lua/roads.lua overrides and restore unpacked or zipped mod files preserving siblings."""
    pristine_mod_files = {}
    if restored_target and Path(restored_target).exists():
        try:
            pristine_mod_files = _extract_mod_files_from_target(restored_target)
        except Exception:
            pristine_mod_files = {}

    candidate_dirs = []
    seen = set()
    by_norm_udir = {}

    for rec in receipt.get("syncedUserDirs") or ():
        udir_str = rec.get("userDir")
        if udir_str:
            k = _norm_key(udir_str)
            by_norm_udir[k] = rec
            if k not in seen:
                seen.add(k)
                candidate_dirs.append(Path(udir_str))

    for udir in discovered_user_dirs or ():
        k = _norm_key(udir)
        if k not in seen:
            seen.add(k)
            candidate_dirs.append(Path(udir))

    cleaned = []
    for udir in candidate_dirs:
        udir = Path(udir)
        rec = by_norm_udir.get(_norm_key(udir), {})
        bdir = Path(rec["backupDir"]) if rec.get("backupDir") else None

        mod_root = udir / "mods" / "unpacked" / "beamng_earth"
        if mod_root.is_dir():
            bug_path = mod_root / "lua/ge/extensions/beamngEarth/bugreport.lua"
            if bug_path.is_file():
                bug_path.unlink()

            for short_rel, _full_rel in MOD_REL_FILES:
                if short_rel.endswith("bugreport.lua"):
                    continue
                dest = mod_root / short_rel
                cur_b = dest.read_bytes() if dest.is_file() else b""
                has_sib = patch_server._has_sibling_shared_mod_entries(
                    actions_bytes=cur_b, keymap_bytes=cur_b, modscript_bytes=cur_b
                )
                if not has_sib and short_rel in pristine_mod_files and not patch_server._has_sibling_shared_mod_entries(
                    actions_bytes=pristine_mod_files[short_rel],
                    keymap_bytes=pristine_mod_files[short_rel],
                    modscript_bytes=pristine_mod_files[short_rel],
                ):
                    if short_rel.endswith("roads.lua"):
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(patch_server.unpatch_roads_lua_bytes(pristine_mod_files[short_rel]))
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(pristine_mod_files[short_rel])
                    continue
                if not has_sib and bdir and (bdir / short_rel).is_file():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes((bdir / short_rel).read_bytes())
                    continue
                if short_rel.endswith("roads.lua") and dest.is_file():
                    dest.write_bytes(patch_server.unpatch_roads_lua_bytes(cur_b))
                elif short_rel.endswith("beamngEarth.json") and dest.is_file():
                    dest.write_bytes(patch_server.unpatch_actions_json_bytes(cur_b))
                elif short_rel.endswith("keyboard_beamngEarth.json") and dest.is_file():
                    dest.write_bytes(patch_server.unpatch_keymap_json_bytes(cur_b))
                elif short_rel.endswith("modScript.lua") and dest.is_file():
                    dest.write_bytes(patch_server.unpatch_modscript_bytes(cur_b))

            cleaned.append(str(mod_root))

        mod_zip = udir / "mods" / "beamng_earth.zip"
        zip_backup = (bdir / "beamng_earth.zip.original") if bdir else None
        if mod_zip.is_file():
            cur_z = mod_zip.read_bytes()
            if not patch_server._mod_zip_has_sibling(cur_z) and zip_backup is not None and zip_backup.is_file():
                tmp_zip = mod_zip.with_name(f"{mod_zip.name}.{uuid.uuid4().hex}.tmp")
                shutil.copy2(zip_backup, tmp_zip)
                os.replace(tmp_zip, mod_zip)
                cleaned.append(str(mod_zip))
            else:
                un_z = patch_server.unpatch_mod_zip_bytes(cur_z)
                if un_z != cur_z:
                    tmp_zip = mod_zip.with_name(f"{mod_zip.name}.{uuid.uuid4().hex}.tmp")
                    tmp_zip.write_bytes(un_z)
                    os.replace(tmp_zip, mod_zip)
                    cleaned.append(str(mod_zip))

    return cleaned


def stop_processes(pids, timeout_s=10.0):
    """Close running BeamNG.Earth PIDs cleanly, escalating to force-kill only if still open."""
    if not pids:
        return
    if os.name == "nt":
        kw = {"capture_output": True, "timeout": 5}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW

        for pid in pids:
            try:
                subprocess.run(["taskkill", "/T", "/PID", str(pid)], **kw)
            except (OSError, subprocess.SubprocessError):
                pass

        deadline = time.monotonic() + min(3.0, timeout_s * 0.4)
        while time.monotonic() < deadline:
            alive = [pid for pid, _p in find_running_beamng_earth_processes() if pid in pids]
            if not alive:
                return
            time.sleep(0.25)

        for pid in pids:
            try:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], **kw)
            except (OSError, subprocess.SubprocessError):
                pass

        end_deadline = time.monotonic() + timeout_s
        while time.monotonic() < end_deadline:
            alive = [pid for pid, _p in find_running_beamng_earth_processes() if pid in pids]
            if not alive:
                return
            time.sleep(0.25)
        return

    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass

    deadline = time.monotonic() + min(3.0, timeout_s * 0.4)
    while time.monotonic() < deadline:
        alive = []
        for pid in pids:
            try:
                os.kill(int(pid), 0)
                alive.append(pid)
            except OSError:
                pass
        if not alive:
            return
        time.sleep(0.2)

    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
    for pid in pids:
        try:
            os.kill(int(pid), sigkill)
        except OSError:
            pass


def relaunch_beamng_earth(target):
    target = Path(target).resolve()
    if os.name == "nt":
        flags = 0x00000200 | 0x00000008
        if target.is_file() and target.suffix.lower() == ".exe":
            subprocess.Popen(
                [str(target)],
                cwd=str(target.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            return True
        launcher_py = target / "launcher.py"
        if target.is_dir() and launcher_py.is_file():
            subprocess.Popen(
                [sys.executable, str(launcher_py)],
                cwd=str(target),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            return True
        return False

    if target.is_file() and (target.name.lower().endswith(".appimage") or os.access(target, os.X_OK)):
        subprocess.Popen(
            [str(target)],
            cwd=str(target.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    if target.is_dir():
        for rel in ("AppRun", "usr/lib/BeamNG.Earth/BeamNG.Earth", "BeamNG.Earth"):
            cand = target / rel
            if cand.is_file():
                subprocess.Popen(
                    [str(cand)],
                    cwd=str(target),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return True
        launcher_py = target / "launcher.py"
        if launcher_py.is_file():
            py_bin = shutil.which("python3") or sys.executable
            subprocess.Popen(
                [py_bin, str(launcher_py)],
                cwd=str(target),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
    return False


def _prepare_via_target_run_addon(target_exe, config_path, status_path, payload_dir, operation):
    """Delegate patch_server.prepare() to target --run-addon when system Python != embedded Python."""
    patch_script = RESOURCES_DIR / "patch_server.py"
    cmd = [
        str(target_exe),
        "--run-addon",
        str(patch_script),
        "--target",
        str(target_exe),
        "--config",
        str(config_path),
        "--status",
        str(status_path),
        "--payload",
        str(payload_dir),
    ]
    if operation == "restore":
        cmd.append("--restore")

    kw = {"capture_output": True, "text": True, "timeout": 180}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW

    res = subprocess.run(cmd, **kw)
    if res.returncode != 0:
        err_msg = (res.stderr or res.stdout or "").strip()
        lines = [ln.strip() for ln in err_msg.splitlines() if ln.strip()]
        reason = lines[-1] if lines else f"Helper exited with code {res.returncode}"
        raise patch_server.PatchError(reason)

    record = read_json_safe(status_path, None)
    if not isinstance(record, dict) or not record.get("state"):
        raise patch_server.PatchError("Helper did not produce a valid staging record.")
    cfg = read_json_safe(config_path, {})
    if cfg.get("requestToken") and not record.get("token"):
        record["token"] = cfg["requestToken"]
        atomic_write_json(status_path, record)
        if record.get("sidecar"):
            atomic_write_json(record["sidecar"], record)
    return record


def _clean_deferred_env():
    env = dict(os.environ)
    if "LD_LIBRARY_PATH_ORIG" in env:
        orig_ld = env.pop("LD_LIBRARY_PATH_ORIG")
        if orig_ld:
            env["LD_LIBRARY_PATH"] = orig_ld
        else:
            env.pop("LD_LIBRARY_PATH", None)
    elif getattr(sys, "frozen", False):
        env.pop("LD_LIBRARY_PATH", None)
    for k in list(env):
        if k.startswith(("_PYI_", "_MEIPASS")) or k in (
            "PYTHONHOME",
            "PYTHONPATH",
            "TCL_LIBRARY",
            "TK_LIBRARY",
            "BNGEARTH_PARENT",
        ):
            env.pop(k, None)
    return env


def _start_deferred_swap(status_path, helper_ps1):
    safe_cwd = tempfile.gettempdir()
    env = _clean_deferred_env()
    if os.name != "nt":
        posix_helper = Path(helper_ps1).parent / "deferred_swap.py"
        py_bin = (
            shutil.which("python3.12", path=env.get("PATH"))
            or shutil.which("python3", path=env.get("PATH"))
            or shutil.which("python", path=env.get("PATH"))
            or sys.executable
        )
        subprocess.Popen(
            [py_bin, "-B", str(posix_helper), "--receipt", str(status_path)],
            cwd=safe_cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return

    command = "& '{}' -Receipt '{}'".format(
        str(helper_ps1).replace("'", "''"),
        str(status_path).replace("'", "''"),
    )
    encoded = __import__("base64").b64encode(command.encode("utf-16le")).decode("ascii")
    tramp = subprocess.Popen(
        [
            os.environ.get("COMSPEC") or "cmd.exe",
            "/d",
            "/c",
            "start",
            "",
            "/b",
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        cwd=safe_cwd,
        env=env,
        creationflags=0x08000000 | 0x00000200,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        tramp.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _cleanup_after_restore(target, mode, state_root):
    """Delete backup, sidecar, stage dirs, and install receipt once original bytes are verified."""
    target = Path(target)
    state_root = Path(state_root)

    ps_cleanup = getattr(patch_server, "cleanup_backup", None)
    if callable(ps_cleanup):
        try:
            ps_cleanup(target)
        except Exception:
            pass

    if mode in ("exe", "appimage", "elf"):
        backup = target.with_name(f"{target.name}.{patch_server.TAG}.original")
        sidecar = target.with_name(f"{target.name}.{patch_server.TAG}.json")
        appdir = target.with_name(f"{target.name}.{patch_server.TAG}.appdir")
        for f in (backup, sidecar):
            if f.is_file():
                f.unlink()
        if appdir.is_dir():
            shutil.rmtree(appdir, ignore_errors=True)
        for pend in target.parent.glob(f"{target.name}.{patch_server.TAG}.*.pending"):
            try:
                pend.unlink()
            except OSError:
                pass
    elif mode in ("source", "onedir", "host"):
        tag_dir = target / f".{patch_server.TAG}"
        if tag_dir.is_dir():
            shutil.rmtree(tag_dir, ignore_errors=True)

    for rel in (RECEIPT_FILENAME, LAST_TARGET_FILENAME, "control.json", "patch-status.json"):
        p = state_root / rel
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass
    mod_backups = state_root / "mod_backups"
    if mod_backups.is_dir():
        shutil.rmtree(mod_backups, ignore_errors=True)
    try:
        if state_root.is_dir() and not any(state_root.iterdir()):
            state_root.rmdir()
    except OSError:
        pass


def perform_install(
    target,
    *,
    state_root=None,
    payload_dir=PAYLOAD_DIR,
    user_dirs=None,
    local_appdata=None,
    apply_fixes=True,
    enable_reporter=True,
    self_exe=False,
    relaunch_after=False,
    notify_on_defer=False,
):
    """Install fixes onto target (.exe, .AppImage, onedir, host, or source dir) and sync BeamNG.drive mod."""
    target = normalize_candidate_target(target)
    if target is None:
        raise patch_server.PatchError("Could not find a valid BeamNG.Earth executable, AppImage, or source directory at that path.")

    state_root = Path(state_root) if state_root else default_state_dir(local_appdata=local_appdata)
    state_root.mkdir(parents=True, exist_ok=True)
    payload_dir = Path(payload_dir).resolve()

    info = verify_target_compatibility(target, payload_dir=payload_dir)

    control_path = state_root / "control.json"
    status_path = state_root / "patch-status.json"
    req_token = uuid.uuid4().hex
    atomic_write_json(
        control_path,
        {
            "enabled": bool(apply_fixes or enable_reporter),
            "modPath": str(Path(__file__).resolve()),
            "requestToken": req_token,
        },
    )

    need_child_addon = False
    if target.is_file() and not self_exe:
        if target.suffix.lower() == ".exe":
            cur_py = sys.version_info.major * 100 + sys.version_info.minor
            exe_py = _exe_embedded_py_ver(target)
            if exe_py != cur_py:
                need_child_addon = True
        elif target.name.lower().endswith(".appimage") and isinstance(info, dict) and info.get("pythonMatches") is False:
            need_child_addon = True

    if need_child_addon:
        record = _prepare_via_target_run_addon(target, control_path, status_path, payload_dir, "install")
    else:
        record = patch_server.prepare(
            target,
            control_path,
            status_path,
            payload_dir,
            operation="install",
            token=req_token,
            apply_fixes=apply_fixes,
            enable_reporter=enable_reporter,
        )

    discovered_udirs = list(user_dirs) if user_dirs is not None else discover_beamng_user_dirs(local_appdata=local_appdata)
    synced_mods = sync_unpacked_mods_on_install(
        discovered_udirs,
        payload_dir,
        state_root,
        apply_fixes=apply_fixes,
        enable_reporter=enable_reporter,
    )

    receipt_path = state_root / RECEIPT_FILENAME
    last_target_path = state_root / LAST_TARGET_FILENAME
    last_target_path.write_text(str(target) + "\n", encoding="utf-8")

    if record["state"] == "pending":
        if self_exe:
            record.update(
                standalone=True,
                companionPid=0,
                notify=bool(notify_on_defer),
                skipReadyNotice=True,
                relaunchAfterSwap=bool(relaunch_after),
                installReceipt=str(receipt_path),
                lastTargetFile=str(last_target_path),
                completionNotice=(
                    "Bridge, Rail & Tunnel Fixes installed! Press Ctrl+Shift+B anytime in game to report a broken bridge, rail, or tunnel."
                    if notify_on_defer
                    else ""
                ),
            )
            atomic_write_json(record["sidecar"], record)
            atomic_write_json(status_path, record)
            atomic_write_json(
                receipt_path,
                {
                    "version": patch_server.VERSION,
                    "state": "pending",
                    "target": str(target),
                    "mode": record.get("mode", "exe"),
                    "originalSha": record.get("originalSha", ""),
                    "patchedSha": record.get("patchedSha", ""),
                    "backup": record.get("backup", ""),
                    "sidecar": record.get("sidecar", ""),
                    "controlPath": str(control_path),
                    "statusPath": str(status_path),
                    "syncedUserDirs": synced_mods,
                },
            )
            _start_deferred_swap(status_path, RESOURCES_DIR / "deferred.ps1")
            return record

        last_err = None
        for _ in range(10):
            try:
                record = patch_server.apply_now(record)
                last_err = None
                break
            except PermissionError as exc:
                last_err = exc
                time.sleep(0.5)
        if last_err is not None:
            raise patch_server.PatchError(
                "BeamNG.Earth is still open. Close BeamNG.Earth completely and run setup again."
            ) from last_err

    receipt_data = {
        "version": patch_server.VERSION,
        "state": "installed",
        "installedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "target": str(target),
        "mode": record.get("mode", "exe" if target.is_file() else "source"),
        "originalSha": record.get("originalSha", ""),
        "patchedSha": record.get("patchedSha", ""),
        "backup": record.get("backup", ""),
        "sidecar": record.get("sidecar", ""),
        "controlPath": str(control_path),
        "statusPath": str(status_path),
        "syncedUserDirs": synced_mods,
    }
    atomic_write_json(receipt_path, receipt_data)

    if relaunch_after:
        relaunch_beamng_earth(target)

    return dict(record, syncedUserDirs=synced_mods, receiptPath=str(receipt_path))


def perform_uninstall(
    target=None,
    *,
    state_root=None,
    payload_dir=PAYLOAD_DIR,
    user_dirs=None,
    local_appdata=None,
    self_exe=False,
    relaunch_after=False,
    notify_on_defer=False,
):
    """Restore original BeamNG.Earth byte-for-byte and remove mod overrides + backups + receipts."""
    state_root = Path(state_root) if state_root else default_state_dir(local_appdata=local_appdata)
    receipt_path = state_root / RECEIPT_FILENAME
    receipt = read_json_safe(receipt_path, {})

    if target is None and receipt.get("target"):
        target = Path(receipt["target"])
    target = normalize_candidate_target(target)
    if target is None:
        raise patch_server.PatchError("Could not locate a patched BeamNG.Earth installation to restore.")

    state_root.mkdir(parents=True, exist_ok=True)
    control_path = state_root / "control.json"
    status_path = state_root / "patch-status.json"
    req_token = uuid.uuid4().hex
    atomic_write_json(
        control_path,
        {
            "enabled": False,
            "modPath": str(Path(__file__).resolve()),
            "requestToken": req_token,
        },
    )

    record = patch_server.prepare(
        target,
        control_path,
        status_path,
        payload_dir,
        operation="restore",
        token=req_token,
    )

    discovered_udirs = list(user_dirs) if user_dirs is not None else discover_beamng_user_dirs(local_appdata=local_appdata)

    if record["state"] == "pending":
        if self_exe:
            cleaned_mods = restore_unpacked_mods_on_uninstall(None, receipt, discovered_udirs)
            record.update(
                standalone=True,
                companionPid=0,
                notify=bool(notify_on_defer),
                skipReadyNotice=True,
                cleanupOnRestore=True,
                relaunchAfterSwap=bool(relaunch_after),
                installReceipt=str(receipt_path),
                lastTargetFile=str(state_root / LAST_TARGET_FILENAME),
                completionNotice="Original BeamNG.Earth has been restored cleanly." if notify_on_defer else "",
            )
            atomic_write_json(record["sidecar"], record)
            atomic_write_json(status_path, record)
            _start_deferred_swap(status_path, RESOURCES_DIR / "deferred.ps1")
            return dict(record, cleanedMods=cleaned_mods)

        last_err = None
        for _ in range(10):
            try:
                record = patch_server.apply_now(record)
                last_err = None
                break
            except PermissionError as exc:
                last_err = exc
                time.sleep(0.5)
        if last_err is not None:
            raise patch_server.PatchError(
                "BeamNG.Earth is still open. Close BeamNG.Earth completely and run uninstall again."
            ) from last_err

    mode = record.get("mode", "exe" if target.is_file() else "source")
    if mode in ("exe", "appimage", "elf"):
        expected_after_sha = record.get("replacementSha") or record.get("originalSha")
        if expected_after_sha and sha256_file(target) != expected_after_sha:
            raise patch_server.PatchError("Restored executable checksum mismatch; backup kept for safety.")
        if patch_server.inspect_target(target).get("patched"):
            raise patch_server.PatchError("Executable still contains Bridge & Rail patch; backup kept for safety.")

    cleaned_mods = restore_unpacked_mods_on_uninstall(target, receipt, discovered_udirs)
    _cleanup_after_restore(target, mode, state_root)

    if relaunch_after:
        relaunch_beamng_earth(target)

    return dict(record, state="restored", cleanedMods=cleaned_mods)


def _linux_dialog_tool():
    if os.name == "nt":
        return None
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None
    if os.environ.get("GAMESCOPE_WAYLAND_DISPLAY") or "gamescope" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower():
        return None
    order = ("kdialog", "zenity") if "kde" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower() else ("zenity", "kdialog")
    for tool in order:
        if shutil.which(tool):
            return tool
    return None


class SetupUI:
    """Friendly GUI (tkinter / Win32 MessageBox / Linux zenity-kdialog) with automatic console fallback."""

    def __init__(self, use_gui=True, assume_yes=False):
        self.assume_yes = bool(assume_yes)
        self.use_gui = bool(use_gui and not assume_yes and os.environ.get("BNE_BRF_TEST_NO_PROMPT") != "1")
        self._tk_available = None

    def _has_tk(self):
        if self._tk_available is not None:
            return self._tk_available
        if not self.use_gui:
            self._tk_available = False
            return False
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            root.destroy()
            self._tk_available = True
        except Exception:
            self._tk_available = False
        return self._tk_available

    def say(self, text=""):
        try:
            print(f"  {text}" if text else "", flush=True)
        except OSError:
            pass

    def banner(self, subtitle):
        self.say("")
        self.say("=" * 72)
        self.say(f"{APP_TITLE} - {subtitle}")
        self.say("=" * 72)
        self.say("")

    def info(self, message, title=APP_TITLE):
        self.say(message)
        if not self.use_gui:
            return
        if self._has_tk():
            try:
                import tkinter as tk
                from tkinter import messagebox
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                messagebox.showinfo(title, message, parent=root)
                root.destroy()
                return
            except Exception:
                pass
        if os.name == "nt":
            try:
                ctypes.windll.user32.MessageBoxW(None, message, title, 0x40 | 0x10000 | 0x40000)
            except Exception:
                pass
            return
        tool = _linux_dialog_tool()
        if tool == "zenity":
            subprocess.run(["zenity", "--info", f"--title={title}", f"--text={message}"], check=False)
        elif tool == "kdialog":
            subprocess.run(["kdialog", "--title", title, "--msgbox", message], check=False)

    def error(self, message, title=f"{APP_TITLE} - Setup Problem"):
        self.say("")
        self.say("PROBLEM: " + message)
        if not self.use_gui:
            return
        if self._has_tk():
            try:
                import tkinter as tk
                from tkinter import messagebox
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                messagebox.showerror(title, message, parent=root)
                root.destroy()
                return
            except Exception:
                pass
        if os.name == "nt":
            try:
                ctypes.windll.user32.MessageBoxW(None, message, title, 0x10 | 0x10000 | 0x40000)
            except Exception:
                pass
            return
        tool = _linux_dialog_tool()
        if tool == "zenity":
            subprocess.run(["zenity", "--error", f"--title={title}", f"--text={message}"], check=False)
        elif tool == "kdialog":
            subprocess.run(["kdialog", "--title", title, "--error", message], check=False)

    def ask_yes_no(self, question, default=True):
        if self.assume_yes:
            return True
        if self.use_gui:
            if self._has_tk():
                try:
                    import tkinter as tk
                    from tkinter import messagebox
                    root = tk.Tk()
                    root.withdraw()
                    root.attributes("-topmost", True)
                    ans = messagebox.askyesno(APP_TITLE, question, parent=root)
                    root.destroy()
                    return bool(ans)
                except Exception:
                    pass
            if os.name == "nt":
                try:
                    res = ctypes.windll.user32.MessageBoxW(None, question, APP_TITLE, 0x04 | 0x20 | 0x10000 | 0x40000)
                    return res == 6
                except Exception:
                    pass
            else:
                tool = _linux_dialog_tool()
                if tool == "zenity":
                    res = subprocess.run(["zenity", "--question", f"--title={APP_TITLE}", f"--text={question}"], check=False)
                    return res.returncode == 0
                if tool == "kdialog":
                    res = subprocess.run(["kdialog", "--title", APP_TITLE, "--yesno", question], check=False)
                    return res.returncode == 0
        suffix = "[Y/n]" if default else "[y/N]"
        try:
            raw = input(f"  {question} {suffix}: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if not raw:
            return default
        return raw in ("y", "yes")

    def pick_target_when_missing(self):
        """Ask the user to locate BeamNG.Earth (.exe, .AppImage, or source folder) via file dialog or console."""
        if self.assume_yes:
            return None
        app_label = "BeamNG.Earth.exe" if os.name == "nt" else "BeamNG.Earth AppImage"
        self.say("BeamNG.Earth was not found in your standard Downloads, Desktop, or Applications folders.")
        if self._has_tk():
            try:
                import tkinter as tk
                from tkinter import filedialog, messagebox
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                ok = messagebox.askokcancel(
                    APP_TITLE,
                    "Could not find BeamNG.Earth automatically.\n\n"
                    f"Click OK to select your {app_label} file (for example in the folder where you downloaded it).",
                    parent=root,
                )
                if not ok:
                    root.destroy()
                    raise SetupCancelled("Setup cancelled.")
                filetypes = (
                    [("BeamNG.Earth Executable", "BeamNG.Earth*.exe *.exe"), ("All Files", "*.*")]
                    if os.name == "nt"
                    else [("BeamNG.Earth AppImage", "BeamNG.Earth*.AppImage *.AppImage"), ("All Files", "*.*")]
                )
                picked = filedialog.askopenfilename(
                    parent=root,
                    title=f"Select {app_label}",
                    filetypes=filetypes,
                )
                if not picked:
                    picked = filedialog.askdirectory(
                        parent=root,
                        title="Or select a BeamNG.Earth source folder (Cancel to quit)",
                    )
                root.destroy()
                if not picked:
                    raise SetupCancelled("No BeamNG.Earth location was selected.")
                return normalize_candidate_target(picked)
            except SetupCancelled:
                raise
            except Exception:
                pass
        elif os.name != "nt":
            tool = _linux_dialog_tool()
            if tool == "zenity":
                res = subprocess.run(
                    ["zenity", "--file-selection", f"--title=Select {app_label}"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if res.returncode == 0 and res.stdout.strip():
                    return normalize_candidate_target(res.stdout.strip())
            elif tool == "kdialog":
                res = subprocess.run(
                    ["kdialog", "--title", f"Select {app_label}", "--getopenfilename", str(Path.home())],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if res.returncode == 0 and res.stdout.strip():
                    return normalize_candidate_target(res.stdout.strip())

        self.say(f"Paste the path to your {app_label} (or source folder), then press Enter:")
        try:
            typed = input("  Path: ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise SetupCancelled("Setup cancelled.") from exc
        if not typed:
            raise SetupCancelled("No path entered.")
        return normalize_candidate_target(typed)

    def choose_from_multiple_targets(self, targets):
        """Let user pick when more than one BeamNG.Earth installation exists."""
        if self.assume_yes or len(targets) == 1:
            return targets[0]

        if self._has_tk():
            try:
                import tkinter as tk
                selected = {"index": None}
                win = tk.Tk()
                win.title(APP_TITLE)
                win.attributes("-topmost", True)
                win.resizable(False, False)

                tk.Label(
                    win,
                    text="More than one BeamNG.Earth copy was found.\nChoose which one to use:",
                    justify="left",
                    padx=16,
                    pady=12,
                ).pack(anchor="w")

                choice_var = tk.IntVar(master=win, value=0)
                for idx, path in enumerate(targets):
                    tk.Radiobutton(
                        win,
                        text=str(path),
                        variable=choice_var,
                        value=idx,
                        justify="left",
                        padx=16,
                    ).pack(anchor="w")

                btn_frame = tk.Frame(win, pady=12, padx=16)
                btn_frame.pack(fill="x")

                def _confirm():
                    selected["index"] = int(choice_var.get())
                    win.destroy()

                def _cancel():
                    win.destroy()

                tk.Button(btn_frame, text="Continue", width=14, command=_confirm).pack(side="right", padx=(8, 0))
                tk.Button(btn_frame, text="Cancel", width=12, command=_cancel).pack(side="right")
                win.mainloop()
                if selected["index"] is None:
                    raise SetupCancelled("Setup cancelled.")
                return targets[selected["index"]]
            except SetupCancelled:
                raise
            except Exception:
                pass

        self.say("Found more than one BeamNG.Earth installation:")
        for idx, t in enumerate(targets, 1):
            self.say(f"  {idx}. {t}")
        try:
            raw = input(f"  Choose [1-{len(targets)}, Enter = 1]: ").strip() or "1"
        except (EOFError, KeyboardInterrupt) as exc:
            raise SetupCancelled("Setup cancelled.") from exc
        if raw.isdigit() and 1 <= int(raw) <= len(targets):
            return targets[int(raw) - 1]
        return targets[0]


def _matching_running_pids(target, self_exe=False):
    target_norm = _norm_key(target)
    excluded = _self_exe_excluded_pids(self_exe=self_exe)
    try:
        running = find_running_beamng_earth_processes(self_exe=self_exe, exclude_pids=excluded)
    except TypeError:
        running = find_running_beamng_earth_processes()
    pids = []
    for pid, exe_path in running:
        if pid in excluded:
            continue
        if not exe_path:
            if Path(target).is_file():
                pids.append(pid)
        elif _norm_key(exe_path) == target_norm:
            pids.append(pid)
    return pids


def run_interactive(action, args, ui=None):
    ui = ui or SetupUI(use_gui=not args.no_gui, assume_yes=args.yes)
    state_root = Path(args.state_dir).resolve() if args.state_dir else default_state_dir()
    user_dirs = [Path(p).resolve() for p in args.user_dir] if args.user_dir else None

    ui.banner("1-Click Install" if action == "install" else "1-Click Uninstall")

    self_exe = False
    if args.self_exe:
        target = normalize_candidate_target(args.self_exe)
        self_exe = target is not None
    elif args.target:
        target = normalize_candidate_target(args.target)
    else:
        if action == "uninstall":
            receipt = read_json_safe(state_root / RECEIPT_FILENAME, {})
            if receipt.get("target") and normalize_candidate_target(receipt["target"]):
                target = normalize_candidate_target(receipt["target"])
            else:
                found = discover_targets(state_root=state_root)
                target = ui.choose_from_multiple_targets(found) if len(found) > 1 else (found[0] if found else None)
        else:
            found = discover_targets(state_root=state_root)
            if len(found) == 1:
                target = found[0]
            elif len(found) > 1:
                target = ui.choose_from_multiple_targets(found)
            else:
                target = None

        if target is None:
            target = ui.pick_target_when_missing()

    if target is None:
        app_hint = "BeamNG.Earth.exe" if os.name == "nt" else "BeamNG.Earth-*-x86_64.AppImage (or source folder)"
        raise patch_server.PatchError(
            f"Could not find {app_hint}.\n\n"
            "Make sure BeamNG.Earth (v1.9.1-beta.dev.424 or newer) is downloaded on this machine, then try again."
        )

    ui.say(f"Target BeamNG.Earth : {target}")

    if action == "install":
        verify_target_compatibility(target, payload_dir=PAYLOAD_DIR)

    running_pids = _matching_running_pids(target, self_exe=self_exe)
    relaunch_after = bool(args.relaunch)

    if running_pids:
        verb = "install Bridge, Rail & Tunnel Fixes" if action == "install" else "restore original BeamNG.Earth"
        ok = ui.ask_yes_no(
            f"BeamNG.Earth is currently open.\n\n"
            f"Close BeamNG.Earth automatically now, {verb}, and start it again when finished?",
            default=True,
        )
        if not ok:
            raise SetupCancelled("Setup cancelled so your running BeamNG.Earth session stays open.")
        ui.say("Closing running BeamNG.Earth window...")
        stop_processes(running_pids)
        if not args.no_relaunch:
            relaunch_after = True

    if action == "install":
        ui.say("Applying Bridge, Rail & Tunnel Fixes and verifying SHA-256 backups...")
        result = perform_install(
            target,
            state_root=state_root,
            payload_dir=PAYLOAD_DIR,
            user_dirs=user_dirs,
            self_exe=self_exe,
            relaunch_after=relaunch_after,
            notify_on_defer=ui.use_gui,
        )
        if result.get("state") == "pending" and self_exe:
            ui.say("Finalizing swap in the background (1-2 seconds)...")
            return 0
        synced_n = len(result.get("syncedUserDirs") or ())
        msg = (
            f"Success! Bridge, Rail & Tunnel Fixes is now installed.\n\n"
            f"Target: {target}\n"
            + (f"Synced BeamNG.drive mod folder(s): {synced_n}\n" if synced_n else "")
            + "\nIn-game hotkey: Press Ctrl+Shift+B anytime while driving to silently report a broken bridge, rail, tunnel, or road.\n"
            + ("BeamNG.Earth is starting up now." if relaunch_after else "You can now start BeamNG.Earth normally.")
        )
        ui.info(msg)
        return 0

    ui.say("Restoring original BeamNG.Earth from verified backup...")
    result = perform_uninstall(
        target,
        state_root=state_root,
        payload_dir=PAYLOAD_DIR,
        user_dirs=user_dirs,
        self_exe=self_exe,
        relaunch_after=relaunch_after,
        notify_on_defer=ui.use_gui,
    )
    if result.get("state") == "pending" and self_exe:
        ui.say("Finalizing restore in the background (1-2 seconds)...")
        return 0

    if not args.no_relaunch and not relaunch_after and not args.yes and ui.use_gui:
        if ui.ask_yes_no(
            "Original BeamNG.Earth has been restored byte-for-byte and all add-on overrides were removed.\n\n"
            "Would you like to start BeamNG.Earth now?",
            default=False,
        ):
            relaunch_beamng_earth(target)
            return 0

    ui.info(
        f"Original BeamNG.Earth has been restored cleanly!\n\n"
        f"Restored: {target}\n"
        f"All Bridge, Rail & Tunnel Fixes mod overrides, backups, and receipts have been removed."
    )
    return 0


def build_arg_parser():
    parser = argparse.ArgumentParser(description="1-Click Install & Uninstall for BeamNG.Earth - Bridge, Rail & Tunnel Fixes.")
    parser.add_argument("action", nargs="?", choices=("install", "uninstall", "auto"), default="auto")
    parser.add_argument("--uninstall", action="store_true", help="Restore original BeamNG.Earth and remove overrides")
    parser.add_argument("--target", type=Path, help="Explicit path to BeamNG.Earth (.exe, .AppImage, or source folder)")
    parser.add_argument("--self-exe", type=Path, help="Internal flag when invoked via BeamNG.Earth --run-addon")
    parser.add_argument("--user-dir", action="append", default=[], help="Explicit BeamNG.drive user folder(s)")
    parser.add_argument("--state-dir", type=Path, help="Override receipt/backup metadata directory")
    parser.add_argument("--yes", "-y", action="store_true", help="Accept prompts non-interactively")
    parser.add_argument("--no-gui", action="store_true", help="Console text output only (no popup dialogs)")
    parser.add_argument("--relaunch", action="store_true", help="Start BeamNG.Earth after setup finishes")
    parser.add_argument("--no-relaunch", action="store_true", help="Never start BeamNG.Earth automatically")
    return parser


def main(argv=None):
    if not _is_supported_host_os() and os.environ.get("BNE_BRF_ALLOW_NON_WINDOWS_TEST") != "1":
        print("This installer supports BeamNG.Earth on Windows (10/11) and Linux (x86_64).", file=sys.stderr)
        return 1

    parser = build_arg_parser()
    args = parser.parse_args(argv)
    ui = SetupUI(use_gui=not args.no_gui, assume_yes=args.yes)

    action = "uninstall" if args.uninstall else args.action
    if action == "auto":
        state_root = Path(args.state_dir).resolve() if args.state_dir else default_state_dir()
        receipt = read_json_safe(state_root / RECEIPT_FILENAME, {})
        if receipt.get("state") == "installed" and not args.yes:
            uninstall_now = ui.ask_yes_no(
                "Bridge, Rail & Tunnel Fixes is already installed.\n\n"
                "Click Yes to UNINSTALL and restore original BeamNG.Earth, or No to RE-INSTALL / repair.",
                default=True,
            )
            action = "uninstall" if uninstall_now else "install"
        else:
            action = "install"

    try:
        return run_interactive(action, args, ui=ui)
    except SetupCancelled as cancel_exc:
        ui.say(str(cancel_exc))
        return 0
    except patch_server.PatchError as patch_err:
        ui.error(str(patch_err))
        return 1
    except Exception as exc:
        ui.error(f"Unexpected setup problem: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
