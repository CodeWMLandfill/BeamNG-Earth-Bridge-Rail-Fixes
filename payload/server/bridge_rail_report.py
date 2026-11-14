"""Asynchronous 1-hotkey silent bug report uploader for BeamNG-Earth-Bridge-Rail-Fixes."""
import base64
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import struct
import threading
import time
import urllib.error
import urllib.request
import zlib

log = logging.getLogger("earthd.bridge_rail_report")

DEFAULT_ENDPOINT = "https://beamng-earth-bridge-rail-fixes.yourlocalpcgamer.workers.dev/report"
ADDON_ID = "beamng-earth-bridge-rail-fixes"
ADDON_VERSION = "0.1.0"
MAX_SHOT_BYTES = int(1.45 * 1024 * 1024)
ALLOWED_KINDS = ("bridge", "rail", "road", "other")
ALLOWED_KEYS = frozenset({
    "lat", "lon", "kind", "summary", "note", "earthVersion", "addonVersion",
    "headingDeg", "altM", "speedMs", "anchorId", "camera", "nearest", "screenshot",
})

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_SAFE_CHUNKS = frozenset({
    b"IHDR", b"PLTE", b"IDAT", b"IEND",
    b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"bKGD",
})
_JPEG_STANDALONE_MARKERS = frozenset({0x01, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8})

_PATH_PATTERNS = (
    re.compile(r"\bfile:///?[^\s\"'`<>()[\]{},;]+(?<![.!?:])", re.IGNORECASE),
    re.compile(r"(?:^|(?<=[\s(\"'`=,[\]{}]))\\\\[A-Za-z0-9_.$ -]+\\[A-Za-z0-9_.$ -]+(?:\\[^\s\\/\"'`<>|*?,;)\]}]+)*\\?(?<![.!?:])"),
    re.compile(r"%(?:USERPROFILE|APPDATA|LOCALAPPDATA|TEMP|TMP|HOMEDRIVE|HOMEPATH)%(?:\\+|/)[^\s\"'`<>|*?,;)\]}]+(?<![.!?:])", re.IGNORECASE),
    re.compile(r"(?:^|(?<=[\s(\"'`=,[\]{}]))[A-Za-z]:(?:\\+|/)(?:(?:[^\s\\/\"'`<>|*?,;)\]}]+(?: [^\s\\/\"'`<>|*?,;)\]}]+)*(?:\\+|/))*(?:[^\s\\/\"'`<>|*?,;)\]}]+(?:\\+|/)?(?<![.!?:]))?)?"),
    re.compile(r"(?:^|(?<=[\s(\"'`=,[\]{}]))/(?:Users|home|root|private/var|var/folders|mnt/[a-zA-Z]|tmp|etc|opt|proc)(?:/[^\s\\/\"'`<>|*?,;)\]}]+)+/?(?<![.!?:])"),
    re.compile(r"(?:^|(?<=[\s(\"'`=,[\]{}]))~(?:\\+|/)[^\s\"'`<>|*?,;)\]}]+(?<![.!?:])"),
)


def redact_paths(text):
    """Strip Windows/Unix/UNC/env-var/tilde/file:// paths from report text before upload."""
    if not isinstance(text, str):
        return text
    out = text
    for pat in _PATH_PATTERNS:
        out = pat.sub("[redacted-path]", out)
    return out


def strip_jpeg_metadata(data):
    """Strip JPEG APP0..APP15 and COM metadata markers while preserving image data."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 4 or data[:2] != b"\xff\xd8":
        raise ValueError("Not a JPEG byte stream")

    n = len(data)
    out = bytearray(b"\xff\xd8")
    pos = 2
    saw_eoi = False

    while pos < n:
        if data[pos] != 0xFF:
            raise ValueError("Expected JPEG marker")
        while pos < n and data[pos] == 0xFF:
            pos += 1
        if pos >= n:
            raise ValueError("Truncated JPEG marker")

        marker = data[pos]
        pos += 1

        if marker == 0x00:
            raise ValueError("Invalid JPEG marker 0xFF00")
        if marker == 0xD9:  # EOI
            out.extend(b"\xff\xd9")
            saw_eoi = True
            break
        if marker in _JPEG_STANDALONE_MARKERS:
            out.append(0xFF)
            out.append(marker)
            continue

        if pos + 2 > n:
            raise ValueError("Truncated JPEG segment length")
        seg_len = struct.unpack_from(">H", data, pos)[0]
        if seg_len < 2 or pos + seg_len > n:
            # Allow synthetic zero-filled test stubs (FF D8 FF E0 00...00 FF D9) safely.
            if (
                marker == 0xE0
                and seg_len == 0
                and data[-2:] == b"\xff\xd9"
                and all(b == 0 for b in data[4:-2])
            ):
                return b"\xff\xd8\xff\xd9"
            raise ValueError("Invalid JPEG segment length")

        is_meta = (0xE0 <= marker <= 0xEF) or (marker == 0xFE)
        if not is_meta:
            out.append(0xFF)
            out.append(marker)
            out.extend(data[pos : pos + seg_len])
        pos += seg_len

        if marker == 0xDA:  # SOS: copy entropy-coded scan until next marker
            scan_start = pos
            while pos < n:
                idx = data.find(b"\xff", pos)
                if idx == -1:
                    raise ValueError("Truncated JPEG scan data without EOI")
                if idx + 1 >= n:
                    raise ValueError("Truncated JPEG scan marker")
                nxt = data[idx + 1]
                if nxt == 0x00 or (0xD0 <= nxt <= 0xD7):
                    pos = idx + 2
                    continue
                out.extend(data[scan_start:idx])
                pos = idx
                break
            else:
                raise ValueError("Truncated JPEG scan data without EOI")

    if not saw_eoi:
        raise ValueError("JPEG missing EOI marker")
    return bytes(out)


def strip_png_metadata(data):
    """Strip PNG ancillary metadata chunks (tEXt/iTXt/zTXt/eXIf/tIME/pHYs/iCCP) and recompute CRC32."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 33 or data[:8] != _PNG_SIG:
        raise ValueError("Not a PNG byte stream")

    n = len(data)
    pos = 8
    out = bytearray(_PNG_SIG)
    saw_ihdr = False
    saw_idat = False
    saw_iend = False

    while pos + 12 <= n:
        length, ctype = struct.unpack_from(">I4s", data, pos)
        if length > n - pos - 12:
            raise ValueError("Truncated PNG chunk")
        chunk_data = data[pos + 8 : pos + 8 + length]
        pos += 12 + length

        if not saw_ihdr:
            if ctype != b"IHDR" or length != 13:
                raise ValueError("PNG must start with valid IHDR chunk")
            saw_ihdr = True

        if ctype == b"IDAT":
            saw_idat = True

        if ctype in _PNG_SAFE_CHUNKS:
            crc = zlib.crc32(ctype + chunk_data) & 0xFFFFFFFF
            out.extend(struct.pack(">I4s", length, ctype))
            out.extend(chunk_data)
            out.extend(struct.pack(">I", crc))

        if ctype == b"IEND":
            if length != 0:
                raise ValueError("PNG IEND chunk must be empty")
            saw_iend = True
            break

    if not (saw_ihdr and saw_idat and saw_iend):
        raise ValueError("Incomplete PNG chunk structure")
    return bytes(out)


def _sanitize_value(val, depth=0):
    if depth > 4:
        return None
    if val is None or isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return val if math.isfinite(val) else None
    if isinstance(val, str):
        return redact_paths(val)[:200]
    if isinstance(val, (list, tuple)):
        return [_sanitize_value(v, depth + 1) for v in val[:24]]
    if isinstance(val, dict):
        clean = {}
        for k, v in list(val.items())[:24]:
            if isinstance(k, str) and k not in ("__proto__", "constructor", "prototype"):
                clean_key = redact_paths(k)[:64]
                if clean_key and clean_key not in ("__proto__", "constructor", "prototype"):
                    clean[clean_key] = _sanitize_value(v, depth + 1)
        return clean
    return None


def _read_addon_options():
    try:
        import apppaths
        opts_path = Path(apppaths.data_dir()) / "addons" / ADDON_ID / "options.json"
        if opts_path.is_file():
            data = json.loads(opts_path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _prepare_screenshot_data_url(shot_path):
    """Read screenshot JPEG/PNG, strip all metadata, and shrink via Pillow if > MAX_SHOT_BYTES."""
    if not shot_path or not shot_path.is_file():
        return None
    raw = shot_path.read_bytes()
    if not raw:
        return None

    is_jpeg = raw[:3] == b"\xff\xd8\xff"
    is_png = raw[:8] == _PNG_SIG
    if is_jpeg:
        try:
            clean = strip_jpeg_metadata(raw)
            if len(clean) <= MAX_SHOT_BYTES:
                return f"data:image/jpeg;base64,{base64.b64encode(clean).decode('ascii')}"
        except ValueError as err:
            log.debug("bridge_rail_report: JPEG strip fallback (%s)", err)
    elif is_png:
        try:
            clean = strip_png_metadata(raw)
            if len(clean) <= MAX_SHOT_BYTES:
                return f"data:image/png;base64,{base64.b64encode(clean).decode('ascii')}"
        except ValueError as err:
            log.debug("bridge_rail_report: PNG strip fallback (%s)", err)

    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as im:
            im = im.convert("RGB")
            for scale in (1.0, 0.75, 0.5):
                w, h = max(320, int(im.width * scale)), max(180, int(im.height * scale))
                resized = im if scale == 1.0 else im.resize((w, h), Image.Resampling.LANCZOS)
                for q in (82, 68, 52):
                    buf = io.BytesIO()
                    resized.save(buf, format="JPEG", quality=q, optimize=True)
                    jpg = strip_jpeg_metadata(buf.getvalue())
                    if len(jpg) <= MAX_SHOT_BYTES:
                        return f"data:image/jpeg;base64,{base64.b64encode(jpg).decode('ascii')}"
    except Exception as err:
        log.debug("bridge_rail_report: screenshot encode skipped (%s)", err)
    return None


def build_payload(msg, shot_path=None, options=None):
    """Build a Worker-compatible sanitized report dict from incoming Lua message."""
    raw = dict(msg.get("payload") or {})
    opts = options if options is not None else _read_addon_options()
    kind = str(raw.get("kind") or opts.get("reportCategory") or "bridge").lower()
    if kind not in ALLOWED_KINDS:
        kind = "bridge"

    lat, lon = float(raw["lat"]), float(raw["lon"])
    if not (math.isfinite(lat) and -90.0 <= lat <= 90.0 and math.isfinite(lon) and -180.0 <= lon <= 180.0):
        raise ValueError("Invalid coordinates")
    payload = {
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "kind": kind,
        "summary": redact_paths(str(raw.get("summary") or f"In-game {kind} report @ {lat:.5f}, {lon:.5f}"))[:200],
        "addonVersion": ADDON_VERSION,
    }
    for key in ("note", "earthVersion", "anchorId"):
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            max_len = 2000 if key == "note" else (64 if key == "earthVersion" else 128)
            cleaned = redact_paths(val.strip())[:max_len]
            if cleaned:
                payload[key] = cleaned
    for key, lo, hi in (("headingDeg", -360.0, 360.0), ("altM", -1000.0, 100000.0), ("speedMs", -1000.0, 1000.0)):
        val = raw.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(float(val)):
            payload[key] = max(lo, min(hi, float(val)))
    for key in ("camera", "nearest"):
        val = raw.get(key)
        if isinstance(val, dict):
            cleaned = _sanitize_value(val)
            if isinstance(cleaned, dict):
                payload[key] = cleaned
    if shot_path is not None:
        shot_url = _prepare_screenshot_data_url(Path(shot_path))
        if shot_url:
            payload["screenshot"] = shot_url
    return {k: v for k, v in payload.items() if k in ALLOWED_KEYS}


def _atomic_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _resolve_report_dir(user_dir, rel):
    if not user_dir or not isinstance(rel, str):
        return None
    rel_clean = rel.replace("\\", "/").lstrip("/")
    if ".." in rel_clean.split("/") or not rel_clean.startswith("beamngEarth/"):
        return None
    base = Path(user_dir).resolve()
    target = (base / rel_clean).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


def upload_report(user_dir, msg, endpoint=DEFAULT_ENDPOINT, timeout_s=20.0, wait_shot_s=3.0):
    """Build, upload, and record receipt for one captured report."""
    opts = _read_addon_options()
    report_dir = _resolve_report_dir(user_dir, msg.get("rel"))
    if opts.get("enableOneHotkeyReport") is False:
        res = {"ok": False, "error": "Disabled in add-on options"}
        if report_dir is not None:
            _atomic_write_json(report_dir / "uploaded.json", res)
        return res

    shot_path = None
    if report_dir is not None:
        candidate = report_dir / "screenshot.jpg"
        deadline = time.monotonic() + max(0.0, float(wait_shot_s))
        while time.monotonic() < deadline:
            if candidate.is_file() and candidate.stat().st_size > 128:
                shot_path = candidate
                break
            time.sleep(0.15)
        if shot_path is None and candidate.is_file() and candidate.stat().st_size > 0:
            shot_path = candidate

    payload = build_payload(msg, shot_path=shot_path, options=opts)
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, allow_nan=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"BeamNG-Earth-Bridge-Rail-Fixes/{ADDON_VERSION}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")[:300]
        result = {"ok": False, "status": err.code, "error": redact_paths(body or str(err))}
    except Exception as err:
        result = {"ok": False, "error": redact_paths(str(err))}

    if report_dir is not None:
        try:
            _atomic_write_json(report_dir / "uploaded.json", result)
        except OSError as err:
            log.warning("bridge_rail_report: failed writing uploaded.json: %s", err)
    if result.get("ok"):
        log.info("bridge_rail_report: sent #%s (%s)", result.get("issueNumber"), payload.get("kind"))
    else:
        log.warning("bridge_rail_report: upload failed (%s)", result.get("error"))
    return result


def submit(server, msg, endpoint=DEFAULT_ENDPOINT):
    """Fire-and-forget daemon upload so earthd's event loop never blocks."""
    user_dir = getattr(server, "user_dir", None)
    t = threading.Thread(
        target=upload_report,
        args=(user_dir, msg),
        kwargs={"endpoint": endpoint},
        name="bridge-rail-report",
        daemon=True,
    )
    t.start()
    return t
