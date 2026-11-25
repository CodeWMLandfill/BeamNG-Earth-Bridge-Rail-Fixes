"""Detached POSIX background swapper for BeamNG.Earth - Bridge, Rail & Tunnel Fixes.

Pure Python standard library only. Waits for the companion runner and any active
BeamNG.Earth process holding the target installation to exit, acquires the
cross-addon swap lock, cooperatively re-stages if a sibling add-on swapped
first, atomically applies staged changes, updates status receipts, and
optionally relaunches BeamNG.Earth or shows a desktop notice.
"""
from contextlib import contextmanager
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _target_lock_hash(target):
    resolved = str(Path(target).resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {} if default is None else default


def write_json_atomic(path, data):
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def save_record(receipt_path, record):
    if record.get("sidecar"):
        write_json_atomic(record["sidecar"], record)
    if receipt_path and Path(receipt_path).parent.is_dir():
        write_json_atomic(receipt_path, record)


def pid_alive(pid):
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)
            if not handle:
                return ctypes.get_last_error() == 5
            try:
                if kernel32.WaitForSingleObject(handle, 0) == 0:
                    return False
                code = ctypes.c_ulong(0)
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == 259
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.is_file():
        try:
            raw = stat_path.read_text(encoding="utf-8", errors="replace")
            rparen = raw.rfind(")")
            if rparen > 0:
                state_tok = raw[rparen + 1:].strip().split(" ", 1)[0]
                if state_tok in ("Z", "X", "x"):
                    return False
        except OSError:
            pass
    return True


@contextmanager
def acquire_swap_lock(target, timeout_s=90.0, stale_s=120.0):
    lock_path = Path(tempfile.gettempdir()) / f"bne_companion_swap_{_target_lock_hash(target)}.lock"
    deadline = time.monotonic() + timeout_s
    acquired = False
    while time.monotonic() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                payload = json.dumps({"pid": os.getpid(), "ts": time.time()}).encode("utf-8")
                os.write(fd, payload)
            finally:
                os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                info = read_json(lock_path, {})
                holder = int(info.get("pid") or 0)
                ts = float(info.get("ts") or 0.0)
                now = time.time()
                stale = (ts > 0.0 and (now - ts) > stale_s) or (
                    holder > 0 and (now - ts) > 1.0 and not pid_alive(holder)
                )
                if stale:
                    lock_path.unlink(missing_ok=True)
                    continue
            except Exception:
                pass
            time.sleep(0.08)
    if not acquired:
        raise RuntimeError("Timed out waiting for cross-addon swap lock.")
    try:
        yield lock_path
    finally:
        try:
            info = read_json(lock_path, {})
            if int(info.get("pid") or 0) == os.getpid():
                lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def _target_busy_on_procfs(target):
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return False
    try:
        target_real = os.path.realpath(str(target))
    except OSError:
        target_real = os.path.abspath(str(target))
    self_pid = os.getpid()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid <= 0 or pid == self_pid:
            continue
        try:
            exe_real = os.path.realpath(str(entry / "exe"))
            if exe_real == target_real:
                return True
        except OSError:
            pass
        try:
            env_raw = (entry / "environ").read_bytes()
            for kv in env_raw.split(b"\x00"):
                if kv.startswith(b"APPIMAGE="):
                    app_val = kv[9:].decode("utf-8", errors="surrogateescape")
                    if app_val and os.path.realpath(app_val) == target_real:
                        return True
        except OSError:
            pass
    return False


def cleanup_staged(record):
    mode = record.get("mode", "")
    if mode in ("source", "onedir", "host"):
        stage_dir = record.get("stageDir")
        if stage_dir and Path(stage_dir).is_dir():
            shutil.rmtree(stage_dir, ignore_errors=True)
    else:
        pending = record.get("pending")
        if pending and Path(pending).is_file():
            try:
                if sha256_file(pending) == record.get("replacementSha"):
                    Path(pending).unlink()
            except OSError:
                pass


def _validate_pending_path(target, pending):
    expected_prefix = f"{target.name}.earthBridgeRailFixes."
    if (
        pending.parent.resolve() != target.parent.resolve()
        or not pending.name.startswith(expected_prefix)
        or not pending.name.endswith(".pending")
    ):
        raise RuntimeError("Invalid staged-file path.")


def _dir_items_match(target, items):
    for item in items or ():
        dest = target / item["rel"]
        if item.get("expectedSha"):
            if not dest.is_file() or sha256_file(dest) != item["expectedSha"]:
                return False
        elif dest.exists():
            return False
    return True


def _scrub_pyinstaller_env(env):
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
        ):
            env.pop(k, None)
    return env


def _clean_env_for_restage():
    env = _scrub_pyinstaller_env(dict(os.environ))
    env["BNGEARTH_PARENT"] = "0"
    env["BNE_COMPANION_NO_HOLD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _invoke_restage_on_current(record, target, mode, receipt_path):
    cleanup_staged(record)
    restage_py = Path(__file__).resolve().parent / "restage_on_current.py"
    if not restage_py.is_file():
        raise RuntimeError("Missing cooperative restage helper (restage_on_current.py).")
    env = _clean_env_for_restage()
    candidates = []
    override_py = env.get("BNE_RESTAGE_PYTHON")
    if override_py and Path(override_py).is_file():
        candidates.append([override_py, "-B", str(restage_py), "--receipt", str(receipt_path)])
    if mode in ("exe", "appimage", "elf") and target.is_file() and os.access(target, os.X_OK):
        candidates.append([str(target), "--run-addon", str(restage_py), "--receipt", str(receipt_path)])
    if mode == "onedir":
        for rel in ("usr/lib/BeamNG.Earth/BeamNG.Earth", "BeamNG.Earth", "AppRun"):
            cand = target / rel
            if cand.is_file() and os.access(cand, os.X_OK):
                candidates.append([str(cand), "--run-addon", str(restage_py), "--receipt", str(receipt_path)])
                break
    for py_name in ("python3.12", "python3", "python"):
        py_bin = shutil.which(py_name, path=env.get("PATH"))
        if py_bin:
            candidates.append([py_bin, "-B", str(restage_py), "--receipt", str(receipt_path)])
    if sys.executable and Path(sys.executable).is_file():
        candidates.append([sys.executable, "-B", str(restage_py), "--receipt", str(receipt_path)])

    last_rc = 1
    for cmd in candidates:
        try:
            res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=240)
            if res.returncode in (0, 2):
                return res.returncode
            last_rc = res.returncode
        except (OSError, subprocess.SubprocessError):
            continue
    return last_rc


def mod_active(configuration):
    if not isinstance(configuration, dict) or not configuration.get("enabled"):
        return False
    mod_path = configuration.get("modPath")
    return bool(mod_path and Path(mod_path).is_file())


def show_desktop_notice(message, title="Bridge, Rail & Tunnel Fixes", is_error=False):
    if not message:
        return
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    if os.environ.get("GAMESCOPE_WAYLAND_DISPLAY") or "gamescope" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower():
        return
    notify_send = shutil.which("notify-send")
    if notify_send:
        try:
            urgency = "critical" if is_error else "normal"
            subprocess.Popen(
                [notify_send, "-u", urgency, title, message],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return
        except OSError:
            pass
    zenity = shutil.which("zenity")
    if zenity:
        try:
            flag = "--error" if is_error else "--info"
            subprocess.Popen(
                [zenity, flag, f"--title={title}", f"--text={message}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return
        except OSError:
            pass
    kdialog = shutil.which("kdialog")
    if kdialog:
        try:
            flag = "--error" if is_error else "--msgbox"
            subprocess.Popen(
                [kdialog, "--title", title, flag, message],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            pass


def relaunch_target(target, mode):
    target = Path(target)
    if mode in ("exe", "appimage", "elf") and target.is_file():
        subprocess.Popen(
            [str(target)],
            cwd=str(target.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return
    if mode == "onedir":
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
                return
    launcher_py = target / "launcher.py"
    if mode == "source" and launcher_py.is_file():
        py_bin = shutil.which("python3") or sys.executable
        subprocess.Popen(
            [py_bin, str(launcher_py)],
            cwd=str(target),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def run_swap(receipt_path):
    receipt_path = Path(receipt_path).resolve()
    record = read_json(receipt_path, None)
    if not isinstance(record, dict) or not record.get("target"):
        return 1

    try:
        target = Path(record["target"]).resolve()
        mode = record.get("mode", "")
        if mode not in ("source", "onedir", "host"):
            pending = Path(record["pending"])
            _validate_pending_path(target, pending)

        companion_pid = int(record.get("companionPid") or 0)
        if companion_pid > 0:
            comp_deadline = time.monotonic() + 30.0
            while pid_alive(companion_pid):
                if time.monotonic() > comp_deadline:
                    raise RuntimeError("Setup did not finish closing. Please try again.")
                time.sleep(0.15)

        if record.get("notify") and not record.get("skipReadyNotice"):
            show_desktop_notice(
                "Bridge, Rail & Tunnel Fixes are staged and ready. Close BeamNG.Earth completely, then start it again normally.",
                "Bridge, Rail & Tunnel Fixes - restart required",
            )

        deadline = time.monotonic() + 12.0 * 3600.0
        started = time.monotonic()
        ready = False
        configuration = {}
        while time.monotonic() < deadline:
            latest = read_json(receipt_path, {})
            configuration = read_json(record.get("config"), {})
            if latest.get("token") != record.get("token") or configuration.get("requestToken") != record.get("token"):
                cleanup_staged(record)
                return 0
            server_pid = int(record.get("serverPid") or 0)
            busy = pid_alive(server_pid)
            if not busy and mode in ("appimage", "elf", "exe") and target.is_file():
                busy = _target_busy_on_procfs(target)
            if not busy:
                ready = True
                break
            time.sleep(0.25 if (time.monotonic() - started) < 10.0 else 1.5)

        if not ready:
            raise RuntimeError("Timed out waiting for BeamNG.Earth to close. Please try again.")

        with acquire_swap_lock(target):
            latest = read_json(receipt_path, {})
            configuration = read_json(record.get("config"), {})
            if latest.get("token") != record.get("token") or configuration.get("requestToken") != record.get("token"):
                cleanup_staged(record)
                return 0

            if record.get("operation") == "install" and not record.get("standalone") and not mod_active(configuration):
                cleanup_staged(record)
                record["state"] = "cancelled"
                record["message"] = "The add-on was disabled or removed; patch cancelled."
                save_record(receipt_path, record)
                return 0

            if mode in ("source", "onedir", "host"):
                if not _dir_items_match(target, record.get("items")):
                    restage_rc = _invoke_restage_on_current(record, target, mode, receipt_path)
                    if restage_rc == 2:
                        return 0
                    if restage_rc != 0:
                        raise RuntimeError("Server files changed after preparation; cooperative restage failed.")
                    record = read_json(receipt_path, {})
                    if record.get("state") in ("installed", "restored", "cancelled"):
                        save_record(receipt_path, record)
                        return 0
                for item in record.get("items") or ():
                    dest = target / item["rel"]
                    if item.get("expectedSha"):
                        if not dest.is_file() or sha256_file(dest) != item["expectedSha"]:
                            raise RuntimeError(f"File changed before apply: {item['rel']}")
                    elif dest.exists():
                        raise RuntimeError(f"File unexpectedly appeared before apply: {item['rel']}")
                for item in record.get("items") or ():
                    dest = target / item["rel"]
                    if item.get("action") == "delete":
                        if dest.exists():
                            dest.unlink()
                    else:
                        staged = Path(item["staged"])
                        if sha256_file(staged) != item["replacementSha"]:
                            raise RuntimeError(f"Staged file checksum mismatch: {item['rel']}")
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(staged, dest)
                        if sha256_file(dest) != item["replacementSha"]:
                            raise RuntimeError(f"Post-copy checksum mismatch: {item['rel']}")
                cleanup_staged(record)
            else:
                if not target.is_file() or sha256_file(target) != record.get("expectedSha"):
                    restage_rc = _invoke_restage_on_current(record, target, mode, receipt_path)
                    if restage_rc == 2:
                        return 0
                    if restage_rc != 0:
                        raise RuntimeError("Server changed after preparation; an update was preserved.")
                    record = read_json(receipt_path, {})
                    if record.get("state") in ("installed", "restored", "cancelled"):
                        save_record(receipt_path, record)
                        return 0
                    pending = Path(record["pending"])
                    _validate_pending_path(target, pending)
                pending = Path(record["pending"])
                if not target.is_file() or sha256_file(target) != record.get("expectedSha"):
                    raise RuntimeError("Server changed after preparation; an update was preserved.")
                if not pending.is_file() or sha256_file(pending) != record.get("replacementSha"):
                    raise RuntimeError("Staged file changed; original server was preserved.")
                try:
                    orig_mode = target.stat().st_mode
                except OSError:
                    orig_mode = 0o755
                exec_mode = int(record.get("originalMode") or orig_mode or 0o755) | 0o111
                try:
                    os.chmod(pending, exec_mode)
                except OSError:
                    pass
                os.replace(pending, target)
                try:
                    os.chmod(target, exec_mode)
                except OSError:
                    pass
                if sha256_file(target) != record.get("replacementSha"):
                    raise RuntimeError("Replacement verification failed; original backup available.")
                if record.get("operation") == "restore":
                    appdir_cand = record.get("appdirPath") or str(target.with_name(f"{target.name}.earthBridgeRailFixes.appdir"))
                    if appdir_cand and Path(appdir_cand).is_dir():
                        shutil.rmtree(appdir_cand, ignore_errors=True)

            if record.get("operation") == "restore":
                record["state"] = "restored"
                record["message"] = "Original BeamNG.Earth restored. Start BeamNG.Earth normally."
            else:
                record["state"] = "installed"
                record["message"] = "Bridge, Rail & Tunnel fixes installed. Start BeamNG.Earth normally."
            save_record(receipt_path, record)

            install_receipt = record.get("installReceipt")
            if record.get("operation") == "install" and install_receipt and Path(install_receipt).is_file():
                ir_doc = read_json(install_receipt, {})
                if isinstance(ir_doc, dict):
                    ir_doc["state"] = "installed"
                    ir_doc["installedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    write_json_atomic(install_receipt, ir_doc)

            if record.get("operation") == "restore" and record.get("cleanupOnRestore"):
                for path_str in (
                    record.get("backup"),
                    record.get("sidecar"),
                    record.get("installReceipt"),
                    record.get("lastTargetFile"),
                ):
                    if path_str and Path(path_str).is_file():
                        try:
                            Path(path_str).unlink()
                        except OSError:
                            pass
                appdir_cand = record.get("appdirPath") or str(target.with_name(f"{target.name}.earthBridgeRailFixes.appdir"))
                if appdir_cand and Path(appdir_cand).is_dir():
                    shutil.rmtree(appdir_cand, ignore_errors=True)

        if record.get("relaunchAfterSwap"):
            try:
                relaunch_target(target, mode)
            except OSError:
                pass

        if record.get("completionNotice"):
            show_desktop_notice(record["completionNotice"], "Bridge, Rail & Tunnel Fixes")
        return 0
    except Exception as exc:
        record["state"] = "error"
        record["message"] = str(exc)
        try:
            save_record(receipt_path, record)
        except OSError:
            pass
        if record.get("notify") and record.get("skipReadyNotice"):
            show_desktop_notice(str(exc), "Bridge, Rail & Tunnel Fixes - setup error", is_error=True)
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, help="Path to staged patch-status.json receipt")
    args = parser.parse_args(argv)
    receipt_abs = str(Path(args.receipt).resolve())
    for k in list(os.environ):
        if k.startswith(("_PYI_", "_MEIPASS")) or k in (
            "PYTHONHOME",
            "PYTHONPATH",
            "TCL_LIBRARY",
            "TK_LIBRARY",
            "BNGEARTH_PARENT",
        ):
            os.environ.pop(k, None)
    try:
        os.chdir(tempfile.gettempdir())
    except OSError:
        pass
    return run_swap(receipt_abs)


if __name__ == "__main__":
    raise SystemExit(main())
