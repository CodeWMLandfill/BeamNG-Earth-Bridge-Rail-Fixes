/**
 * BeamNG.Earth Anonymous Issue & Bridge Report Collector (Cloudflare Worker).
 *
 * Pure ES module with zero npm or Node-specific dependencies. Compatible with
 * Cloudflare Workers runtime and Node.js >= 20 (`node --test`).
 */

const SERVICE_NAME = "bnge-bridge-report-worker";
const DEFAULT_WORKER_NAME = "beamng-earth-bridge-rail-fixes";
const DEFAULT_REPORTS_BRANCH = "reports";
const GITHUB_API_BASE = "https://api.github.com";

export const MAX_BODY_BYTES = Math.floor(2.5 * 1024 * 1024); // 2,621,440 bytes
export const MAX_SCREENSHOT_BYTES = Math.floor(1.5 * 1024 * 1024); // 1,572,864 bytes
const MAX_SUMMARY_CHARS = 200;
const MAX_NOTE_CHARS = 2000;
const MAX_SHORT_TEXT_CHARS = 128;
const MAX_VERSION_CHARS = 64;
const MAX_NEAREST_DEPTH = 4;
const MAX_NEAREST_KEYS = 24;
const MAX_NEAREST_ARRAY_LEN = 24;
const MAX_NEAREST_STRING_CHARS = 200;
const MAX_NEAREST_JSON_BYTES = 4096;
const MAX_CAMERA_JSON_BYTES = 1024;

const DEFAULT_RATE_LIMIT_MAX = 6;
const DEFAULT_RATE_LIMIT_WINDOW_SEC = 60;
const MAX_TRACKED_IPS = 4096;

const ALLOWED_KINDS = new Set(["bridge", "rail", "road", "other"]);
const ALLOWED_TOP_LEVEL_KEYS = new Set([
  "lat",
  "lon",
  "kind",
  "summary",
  "note",
  "earthVersion",
  "addonVersion",
  "headingDeg",
  "altM",
  "speedMs",
  "anchorId",
  "camera",
  "nearest",
  "screenshot",
  "loading_incomplete",
  "loading_reason",
]);
const FORBIDDEN_OBJECT_KEYS = new Set(["__proto__", "constructor", "prototype"]);

const REPO_SLUG_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const WORKER_NAME_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;
const BRANCH_NAME_RE = /^(?!\/|.*\/\/|.*\.\.|.*@\{|.*[\\~^:?*[\]\x00-\x20\x7f])[A-Za-z0-9._/-]{1,120}(?<!\/|\.lock|\.)$/;
const BASE64_RE = /^[A-Za-z0-9+/]+={0,2}$/;

// Privacy guard regexes for Windows, Unix, macOS, UNC, env-var, and file:// paths.
const FILE_URI_RE = /\bfile:\/\/\/?[^\s"'`<>()[\]{},;]+(?<![.!?:])/gi;
const UNC_PATH_RE = /(?:^|(?<=[\s("'`=,[\]{}]))\\\\[A-Za-z0-9_.$ -]+\\[A-Za-z0-9_.$ -]+(?:\\[^\s\\/"'`<>|*?,;)\]}]+)*\\?(?<![.!?:])/g;
const ENV_WIN_PATH_RE = /%(?:USERPROFILE|APPDATA|LOCALAPPDATA|TEMP|TMP|HOMEDRIVE|HOMEPATH)%(?:\\+|\/)[^\s"'`<>|*?,;)\]}]+(?<![.!?:])/gi;
const WIN_DRIVE_PATH_RE = /(?:^|(?<=[\s("'`=,[\]{}]))[A-Za-z]:(?:\\+|\/)(?:(?:[^\s\\/"'`<>|*?,;)\]}]+(?: [^\s\\/"'`<>|*?,;)\]}]+)*(?:\\+|\/))*[^\s\\/"'`<>|*?,;)\]}]+(?:\\+|\/)?(?<![.!?:]))?/g;
const UNIX_ABS_PATH_RE = /(?:^|(?<=[\s("'`=,[\]{}]))\/(?:Users|home|root|private\/var|var\/folders|mnt\/[a-zA-Z]|tmp|etc|opt|proc)(?:\/(?:[^\s\\/"'`<>|*?,;)\]}]+(?: [^\s\\/"'`<>|*?,;)\]}]+)*(?=\/)|[^\s\\/"'`<>|*?,;)\]}]+(?<![.!?:])))+\/?/g;
const TILDE_PATH_RE = /(?:^|(?<=[\s("'`=,[\]{}]))~(?:\\+|\/)[^\s"'`<>|*?,;)\]}]+(?<![.!?:])/g;

const ipWindows = new Map();
let isolateHmacKeyPromise = null;

const PNG_SIG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const PNG_SAFE_CHUNKS = new Set([
  "IHDR",
  "PLTE",
  "IDAT",
  "IEND",
  "tRNS",
  "gAMA",
  "cHRM",
  "sRGB",
  "bKGD",
]);
const JPEG_STANDALONE_MARKERS = new Set([
  0x01, 0xd0, 0xd1, 0xd2, 0xd3, 0xd4, 0xd5, 0xd6, 0xd7, 0xd8,
]);

const CRC32_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let k = 0; k < 8; k++) {
      c = (c & 1) !== 0 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    }
    table[i] = c >>> 0;
  }
  return table;
})();

function crc32(bytes) {
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) {
    c = CRC32_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  }
  return (c ^ 0xffffffff) >>> 0;
}

export function resetRateLimiter() {
  ipWindows.clear();
}

export function stripJpegMetadata(bytes) {
  if (!(bytes instanceof Uint8Array) || bytes.length < 4 || bytes[0] !== 0xff || bytes[1] !== 0xd8) {
    throw new ValidationError("Malformed JPEG header");
  }
  const n = bytes.length;
  const out = [0xff, 0xd8];
  let pos = 2;
  let sawEoi = false;

  while (pos < n) {
    if (bytes[pos] !== 0xff) {
      throw new ValidationError("Malformed JPEG marker stream");
    }
    while (pos < n && bytes[pos] === 0xff) {
      pos++;
    }
    if (pos >= n) {
      throw new ValidationError("Truncated JPEG marker");
    }

    const marker = bytes[pos++];
    if (marker === 0x00) {
      throw new ValidationError("Invalid JPEG marker 0xFF00");
    }
    if (marker === 0xd9) {
      out.push(0xff, 0xd9);
      sawEoi = true;
      break;
    }
    if (JPEG_STANDALONE_MARKERS.has(marker)) {
      out.push(0xff, marker);
      continue;
    }

    if (pos + 2 > n) {
      throw new ValidationError("Truncated JPEG segment length");
    }
    const segLen = (bytes[pos] << 8) | bytes[pos + 1];
    if (segLen < 2 || pos + segLen > n) {
      throw new ValidationError("Invalid JPEG segment length");
    }

    const isMeta = (marker >= 0xe0 && marker <= 0xef) || marker === 0xfe;
    if (!isMeta) {
      out.push(0xff, marker);
      for (let i = pos; i < pos + segLen; i++) {
        out.push(bytes[i]);
      }
    }
    pos += segLen;

    if (marker === 0xda) {
      const scanStart = pos;
      let foundNextMarker = false;
      while (pos < n) {
        const idx = bytes.indexOf(0xff, pos);
        if (idx === -1 || idx + 1 >= n) {
          throw new ValidationError("Truncated JPEG scan data without EOI");
        }
        const nxt = bytes[idx + 1];
        if (nxt === 0x00 || (nxt >= 0xd0 && nxt <= 0xd7)) {
          pos = idx + 2;
          continue;
        }
        for (let i = scanStart; i < idx; i++) {
          out.push(bytes[i]);
        }
        pos = idx;
        foundNextMarker = true;
        break;
      }
      if (!foundNextMarker) {
        throw new ValidationError("Truncated JPEG scan data without EOI");
      }
    }
  }

  if (!sawEoi) {
    throw new ValidationError("Malformed JPEG missing EOI marker");
  }
  return new Uint8Array(out);
}

export function stripPngMetadata(bytes) {
  if (!(bytes instanceof Uint8Array) || bytes.length < 33) {
    throw new ValidationError("Malformed PNG screenshot");
  }
  for (let i = 0; i < PNG_SIG.length; i++) {
    if (bytes[i] !== PNG_SIG[i]) {
      throw new ValidationError("Malformed PNG signature");
    }
  }

  const n = bytes.length;
  const out = Array.from(PNG_SIG);
  let pos = 8;
  let sawIhdr = false;
  let sawIdat = false;
  let sawIend = false;

  while (pos + 12 <= n) {
    const length =
      ((bytes[pos] << 24) |
        (bytes[pos + 1] << 16) |
        (bytes[pos + 2] << 8) |
        bytes[pos + 3]) >>>
      0;
    if (length > n - pos - 12) {
      throw new ValidationError("Truncated PNG chunk");
    }
    const typeBytes = bytes.subarray(pos + 4, pos + 8);
    const typeStr = String.fromCharCode(
      typeBytes[0],
      typeBytes[1],
      typeBytes[2],
      typeBytes[3]
    );
    const dataEnd = pos + 8 + length;
    pos += 12 + length;

    if (!sawIhdr) {
      if (typeStr !== "IHDR" || length !== 13) {
        throw new ValidationError("PNG must start with valid IHDR chunk");
      }
      sawIhdr = true;
    }
    if (typeStr === "IDAT") {
      sawIdat = true;
    }

    if (PNG_SAFE_CHUNKS.has(typeStr)) {
      const typeAndData = bytes.subarray(dataEnd - length - 4, dataEnd);
      const crc = crc32(typeAndData);
      out.push(
        (length >>> 24) & 0xff,
        (length >>> 16) & 0xff,
        (length >>> 8) & 0xff,
        length & 0xff
      );
      for (let i = 0; i < typeAndData.length; i++) {
        out.push(typeAndData[i]);
      }
      out.push((crc >>> 24) & 0xff, (crc >>> 16) & 0xff, (crc >>> 8) & 0xff, crc & 0xff);
    }

    if (typeStr === "IEND") {
      if (length !== 0) {
        throw new ValidationError("PNG IEND chunk must be empty");
      }
      sawIend = true;
      break;
    }
  }

  if (!sawIhdr || !sawIdat || !sawIend) {
    throw new ValidationError("Incomplete PNG chunk structure");
  }
  return new Uint8Array(out);
}

export function redactPaths(input) {
  if (typeof input !== "string" || input.length === 0) {
    return "";
  }
  return input
    .replace(FILE_URI_RE, "[REDACTED_PATH]")
    .replace(UNC_PATH_RE, "[REDACTED_PATH]")
    .replace(ENV_WIN_PATH_RE, "[REDACTED_PATH]")
    .replace(WIN_DRIVE_PATH_RE, "[REDACTED_PATH]")
    .replace(UNIX_ABS_PATH_RE, "[REDACTED_PATH]")
    .replace(TILDE_PATH_RE, "[REDACTED_PATH]");
}

function stripControlChars(str, allowNewlines) {
  if (allowNewlines) {
    return str.replace(/\r\n?/g, "\n").replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "");
  }
  return str.replace(/[\x00-\x1f\x7f]+/g, " ");
}

function sanitizeText(value, maxChars, fieldName, allowNewlines = false) {
  if (typeof value !== "string") {
    throw new ValidationError(`${fieldName} must be a string`);
  }
  // Bound pre-regex length so pathological strings stay O(1) inside regex passes.
  const preBounded = value.length > maxChars + 2048 ? value.slice(0, maxChars + 2048) : value;
  const cleaned = redactPaths(stripControlChars(preBounded, allowNewlines)).trim();
  if (cleaned.length <= maxChars) {
    return cleaned;
  }
  return cleaned.slice(0, maxChars).trimEnd();
}

function clampFiniteNumber(value, min, max, fieldName) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new ValidationError(`${fieldName} must be a finite number`);
  }
  if (value < min) return min;
  if (value > max) return max;
  return value;
}

function isPlainObject(value) {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

function sanitizeBoundedJson(value, depth, maxDepth, fieldName) {
  if (depth > maxDepth) {
    throw new ValidationError(`${fieldName} exceeds maximum nesting depth (${maxDepth})`);
  }
  if (value === null || typeof value === "boolean") {
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new ValidationError(`${fieldName} cannot contain non-finite numbers`);
    }
    return value;
  }
  if (typeof value === "string") {
    return sanitizeText(value, MAX_NEAREST_STRING_CHARS, fieldName, false);
  }
  if (Array.isArray(value)) {
    if (value.length > MAX_NEAREST_ARRAY_LEN) {
      throw new ValidationError(`${fieldName} array exceeds maximum length (${MAX_NEAREST_ARRAY_LEN})`);
    }
    return value.map((item) => sanitizeBoundedJson(item, depth + 1, maxDepth, fieldName));
  }
  if (isPlainObject(value)) {
    const keys = Object.keys(value);
    if (keys.length > MAX_NEAREST_KEYS) {
      throw new ValidationError(`${fieldName} object exceeds maximum keys (${MAX_NEAREST_KEYS})`);
    }
    const out = {};
    for (const rawKey of keys) {
      if (FORBIDDEN_OBJECT_KEYS.has(rawKey)) {
        throw new ValidationError(`${fieldName} contains disallowed key: ${rawKey}`);
      }
      const cleanKey = sanitizeText(rawKey, 64, `${fieldName} key`, false);
      if (!cleanKey || FORBIDDEN_OBJECT_KEYS.has(cleanKey)) {
        throw new ValidationError(`${fieldName} contains invalid key`);
      }
      const child = value[rawKey];
      if (child !== undefined) {
        out[cleanKey] = sanitizeBoundedJson(child, depth + 1, maxDepth, fieldName);
      }
    }
    return out;
  }
  throw new ValidationError(`${fieldName} contains unsupported JSON value`);
}

function sanitizeStructuredObject(value, maxDepth, maxBytes, fieldName) {
  if (!isPlainObject(value)) {
    throw new ValidationError(`${fieldName} must be a JSON object`);
  }
  const sanitized = sanitizeBoundedJson(value, 0, maxDepth, fieldName);
  const encodedBytes = new TextEncoder().encode(JSON.stringify(sanitized)).byteLength;
  if (encodedBytes > maxBytes) {
    throw new ValidationError(`${fieldName} exceeds maximum serialized size (${maxBytes} bytes)`);
  }
  return sanitized;
}

class ValidationError extends Error {
  constructor(message, status = 400) {
    super(message);
    this.name = "ValidationError";
    this.status = status;
  }
}

class UpstreamGitHubError extends Error {
  constructor(message, upstreamStatus) {
    super(message);
    this.name = "UpstreamGitHubError";
    this.upstreamStatus = upstreamStatus;
  }
}

function uint8ToBase64(bytes) {
  const CHUNK_SIZE = 0x8000;
  const parts = [];
  for (let offset = 0; offset < bytes.length; offset += CHUNK_SIZE) {
    const sub = bytes.subarray(offset, offset + CHUNK_SIZE);
    parts.push(String.fromCharCode(...sub));
  }
  return btoa(parts.join(""));
}

function parseScreenshot(raw) {
  if (typeof raw !== "string") {
    throw new ValidationError("screenshot must be a base64 string");
  }
  let trimmed = raw.trim();
  if (trimmed.length === 0) {
    throw new ValidationError("screenshot must not be empty when provided");
  }

  let declaredExt = null;
  if (trimmed.startsWith("data:")) {
    const commaIdx = trimmed.indexOf(",");
    if (commaIdx === -1) {
      throw new ValidationError("Invalid screenshot data URL prefix");
    }
    const header = trimmed.slice(0, commaIdx).toLowerCase();
    if (header === "data:image/jpeg;base64" || header === "data:image/jpg;base64") {
      declaredExt = "jpg";
    } else if (header === "data:image/png;base64") {
      declaredExt = "png";
    } else {
      throw new ValidationError("screenshot data URL must be image/jpeg or image/png base64");
    }
    trimmed = trimmed.slice(commaIdx + 1);
  }

  const cleanedB64 = trimmed.replace(/[\r\n\t ]+/g, "");
  if (cleanedB64.length === 0 || cleanedB64.length % 4 !== 0 || !BASE64_RE.test(cleanedB64)) {
    throw new ValidationError("screenshot is not valid base64");
  }

  const padding = cleanedB64.endsWith("==") ? 2 : cleanedB64.endsWith("=") ? 1 : 0;
  const estimatedBytes = (cleanedB64.length * 3) / 4 - padding;
  if (estimatedBytes > MAX_SCREENSHOT_BYTES) {
    throw new ValidationError("Decoded screenshot exceeds 1.5 MB limit", 413);
  }

  let binaryStr;
  try {
    binaryStr = atob(cleanedB64);
  } catch {
    throw new ValidationError("screenshot could not be base64-decoded");
  }

  if (binaryStr.length === 0) {
    throw new ValidationError("Decoded screenshot is empty");
  }
  if (binaryStr.length > MAX_SCREENSHOT_BYTES) {
    throw new ValidationError("Decoded screenshot exceeds 1.5 MB limit", 413);
  }

  const bytes = new Uint8Array(binaryStr.length);
  for (let i = 0; i < binaryStr.length; i++) {
    bytes[i] = binaryStr.charCodeAt(i);
  }

  const isJpeg =
    bytes.length >= 3 &&
    bytes[0] === 0xff &&
    bytes[1] === 0xd8 &&
    bytes[2] === 0xff;
  const isPng =
    bytes.length >= 8 &&
    bytes[0] === 0x89 &&
    bytes[1] === 0x50 &&
    bytes[2] === 0x4e &&
    bytes[3] === 0x47 &&
    bytes[4] === 0x0d &&
    bytes[5] === 0x0a &&
    bytes[6] === 0x1a &&
    bytes[7] === 0x0a;

  if (!isJpeg && !isPng) {
    throw new ValidationError("screenshot failed JPEG/PNG magic-bytes verification");
  }

  const ext = isJpeg ? "jpg" : "png";
  if (declaredExt !== null && declaredExt !== ext) {
    throw new ValidationError("screenshot data URL MIME type does not match image magic bytes");
  }

  const stripped = ext === "jpg" ? stripJpegMetadata(bytes) : stripPngMetadata(bytes);

  return {
    ext,
    byteLength: stripped.byteLength,
    base64: uint8ToBase64(stripped),
  };
}

function validateAndSanitizeReport(raw) {
  if (!isPlainObject(raw)) {
    throw new ValidationError("Report payload must be a JSON object");
  }

  for (const key of Object.keys(raw)) {
    if (FORBIDDEN_OBJECT_KEYS.has(key) || !ALLOWED_TOP_LEVEL_KEYS.has(key)) {
      const safeKey = String(key).replace(/[^\w.-]/g, "").slice(0, 40) || "unknown";
      throw new ValidationError(`Unsupported top-level field: ${safeKey}`);
    }
  }

  if (typeof raw.lat !== "number" || !Number.isFinite(raw.lat) || raw.lat < -90 || raw.lat > 90) {
    throw new ValidationError("lat must be a finite number in [-90, 90]");
  }
  if (typeof raw.lon !== "number" || !Number.isFinite(raw.lon) || raw.lon < -180 || raw.lon > 180) {
    throw new ValidationError("lon must be a finite number in [-180, 180]");
  }
  if (typeof raw.kind !== "string" || !ALLOWED_KINDS.has(raw.kind)) {
    throw new ValidationError('kind must be one of: "bridge", "rail", "road", "other"');
  }

  const report = {
    lat: Number(raw.lat.toFixed(7)),
    lon: Number(raw.lon.toFixed(7)),
    kind: raw.kind,
  };

  if (raw.summary !== undefined && raw.summary !== null) {
    const summary = sanitizeText(raw.summary, MAX_SUMMARY_CHARS, "summary", false);
    if (summary) report.summary = summary;
  }
  if (raw.note !== undefined && raw.note !== null) {
    const note = sanitizeText(raw.note, MAX_NOTE_CHARS, "note", true);
    if (note) report.note = note;
  }
  if (raw.earthVersion !== undefined && raw.earthVersion !== null) {
    const v = sanitizeText(raw.earthVersion, MAX_VERSION_CHARS, "earthVersion", false);
    if (v) report.earthVersion = v;
  }
  if (raw.addonVersion !== undefined && raw.addonVersion !== null) {
    const v = sanitizeText(raw.addonVersion, MAX_VERSION_CHARS, "addonVersion", false);
    if (v) report.addonVersion = v;
  }
  if (raw.headingDeg !== undefined && raw.headingDeg !== null) {
    report.headingDeg = Number(clampFiniteNumber(raw.headingDeg, -360, 360, "headingDeg").toFixed(2));
  }
  if (raw.altM !== undefined && raw.altM !== null) {
    report.altM = Number(clampFiniteNumber(raw.altM, -1000, 100000, "altM").toFixed(2));
  }
  if (raw.speedMs !== undefined && raw.speedMs !== null) {
    report.speedMs = Number(clampFiniteNumber(raw.speedMs, -1000, 1000, "speedMs").toFixed(2));
  }
  if (raw.anchorId !== undefined && raw.anchorId !== null) {
    const anchorId = sanitizeText(raw.anchorId, MAX_SHORT_TEXT_CHARS, "anchorId", false);
    if (anchorId) report.anchorId = anchorId;
  }
  if (raw.camera !== undefined && raw.camera !== null) {
    if (typeof raw.camera === "string") {
      const cam = sanitizeText(raw.camera, MAX_SHORT_TEXT_CHARS, "camera", false);
      if (cam) report.camera = cam;
    } else if (isPlainObject(raw.camera)) {
      report.camera = sanitizeStructuredObject(raw.camera, 3, MAX_CAMERA_JSON_BYTES, "camera");
    } else {
      throw new ValidationError("camera must be a string or JSON object");
    }
  }
  if (raw.nearest !== undefined && raw.nearest !== null) {
    report.nearest = sanitizeStructuredObject(
      raw.nearest,
      MAX_NEAREST_DEPTH,
      MAX_NEAREST_JSON_BYTES,
      "nearest"
    );
  }
  if (raw.loading_incomplete !== undefined && raw.loading_incomplete !== null) {
    if (typeof raw.loading_incomplete !== "boolean") {
      throw new ValidationError("loading_incomplete must be a boolean");
    }
    report.loading_incomplete = raw.loading_incomplete;
  }
  if (raw.loading_reason !== undefined && raw.loading_reason !== null) {
    const reason = sanitizeText(raw.loading_reason, MAX_SHORT_TEXT_CHARS, "loading_reason", false);
    if (reason) report.loading_reason = reason;
  }

  let screenshot = null;
  if (raw.screenshot !== undefined && raw.screenshot !== null) {
    screenshot = parseScreenshot(raw.screenshot);
  }

  return { report, screenshot };
}

async function readJsonBodyBounded(request, maxBytes) {
  const contentLengthHeader = request.headers.get("content-length");
  if (contentLengthHeader !== null) {
    const declaredLength = Number(contentLengthHeader);
    if (Number.isFinite(declaredLength) && declaredLength > maxBytes) {
      throw new ValidationError("Request body exceeds 2.5 MB limit", 413);
    }
  }

  if (!request.body) {
    throw new ValidationError("Request body is empty", 400);
  }

  const reader = request.body.getReader();
  const chunks = [];
  let totalBytes = 0;

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value && value.byteLength > 0) {
        totalBytes += value.byteLength;
        if (totalBytes > maxBytes) {
          await reader.cancel().catch(() => {});
          throw new ValidationError("Request body exceeds 2.5 MB limit", 413);
        }
        chunks.push(value);
      }
    }
  } finally {
    reader.releaseLock();
  }

  if (totalBytes === 0) {
    throw new ValidationError("Request body is empty", 400);
  }

  const combined = new Uint8Array(totalBytes);
  let offset = 0;
  for (const chunk of chunks) {
    combined.set(chunk, offset);
    offset += chunk.byteLength;
  }

  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(combined);
  } catch {
    throw new ValidationError("Request body must be valid UTF-8", 400);
  }

  try {
    return JSON.parse(text);
  } catch {
    throw new ValidationError("Invalid JSON payload", 400);
  }
}

function getClientIp(request) {
  const cfIp = request.headers.get("CF-Connecting-IP");
  if (cfIp && cfIp.trim()) {
    return cfIp.trim();
  }
  const xff = request.headers.get("X-Forwarded-For");
  if (xff && xff.trim()) {
    const first = xff.split(",")[0].trim();
    if (first) return first;
  }
  return "unknown";
}

async function getIsolateHmacKey() {
  if (!isolateHmacKeyPromise) {
    isolateHmacKeyPromise = crypto.subtle.generateKey(
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"]
    );
  }
  return isolateHmacKeyPromise;
}

async function hashClientIp(clientIp) {
  const key = await getIsolateHmacKey();
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(clientIp));
  return Array.from(new Uint8Array(sig), (b) => b.toString(16).padStart(2, "0")).join("");
}

function parsePositiveInt(val, fallback) {
  const n = Number(val);
  if (Number.isFinite(n) && n >= 1) {
    return Math.floor(n);
  }
  return fallback;
}

async function checkRateLimit(request, env) {
  const ipHash = await hashClientIp(getClientIp(request));
  const windowSec = parsePositiveInt(env?.RATE_LIMIT_WINDOW_SEC, DEFAULT_RATE_LIMIT_WINDOW_SEC);
  const maxRequests = parsePositiveInt(env?.RATE_LIMIT_MAX, DEFAULT_RATE_LIMIT_MAX);

  if (env?.RATE_LIMITER && typeof env.RATE_LIMITER.limit === "function") {
    const result = await env.RATE_LIMITER.limit({ key: `report:${ipHash}` });
    if (!result || result.success === false) {
      return { allowed: false, retryAfterSec: windowSec };
    }
    return { allowed: true, retryAfterSec: 0 };
  }

  const now = Date.now();
  const windowMs = windowSec * 1000;
  const cutoff = now - windowMs;

  if (ipWindows.size >= MAX_TRACKED_IPS && !ipWindows.has(ipHash)) {
    for (const [key, stamps] of ipWindows) {
      if (stamps.length === 0 || stamps[stamps.length - 1] <= cutoff) {
        ipWindows.delete(key);
      }
      if (ipWindows.size < MAX_TRACKED_IPS) break;
    }
    if (ipWindows.size >= MAX_TRACKED_IPS) {
      const oldestKey = ipWindows.keys().next().value;
      if (oldestKey !== undefined) ipWindows.delete(oldestKey);
    }
  }

  const existing = ipWindows.get(ipHash) || [];
  const active = existing.filter((ts) => ts > cutoff);

  if (active.length >= maxRequests) {
    ipWindows.set(ipHash, active);
    const retryAfterSec = Math.max(1, Math.ceil((active[0] + windowMs - now) / 1000));
    return { allowed: false, retryAfterSec };
  }

  active.push(now);
  ipWindows.set(ipHash, active);
  return { allowed: true, retryAfterSec: 0 };
}

function resolveBranch(env) {
  const raw = typeof env?.REPORTS_BRANCH === "string" ? env.REPORTS_BRANCH.trim() : "";
  const branch = raw || DEFAULT_REPORTS_BRANCH;
  if (!BRANCH_NAME_RE.test(branch)) {
    return DEFAULT_REPORTS_BRANCH;
  }
  return branch;
}

function resolveWorkerName(env) {
  const raw = typeof env?.WORKER_NAME === "string" ? env.WORKER_NAME.trim() : "";
  const candidate = raw || DEFAULT_WORKER_NAME;
  return WORKER_NAME_RE.test(candidate) ? candidate : DEFAULT_WORKER_NAME;
}

function resolveGitHubConfig(env) {
  const repo = typeof env?.GITHUB_REPO === "string" ? env.GITHUB_REPO.trim() : "";
  const token = typeof env?.GITHUB_TOKEN === "string" ? env.GITHUB_TOKEN.trim() : "";
  const branch = resolveBranch(env);
  const workerName = resolveWorkerName(env);
  const repoValid = REPO_SLUG_RE.test(repo);
  return {
    repo,
    token,
    branch,
    workerName,
    repoConfigured: Boolean(repoValid && token.length > 0),
  };
}

function encodeBranchPath(branch) {
  return branch.split("/").map((seg) => encodeURIComponent(seg)).join("/");
}

function generateReportId(now = new Date()) {
  const compactIso = now
    .toISOString()
    .replace(/[-:]/g, "")
    .replace(/\.\d{3}Z$/, "Z");
  const randBytes = new Uint8Array(4);
  crypto.getRandomValues(randBytes);
  const randHex = Array.from(randBytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${compactIso}-${randHex}`;
}

async function githubRequest(config, method, path, bodyObj = undefined) {
  const headers = {
    Authorization: `Bearer ${config.token}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": `${SERVICE_NAME}/1.0`,
  };
  const init = { method, headers };
  if (bodyObj !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(bodyObj);
  }
  const response = await globalThis.fetch(`${GITHUB_API_BASE}${path}`, init);
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { raw: text.slice(0, 300) };
    }
  }
  return { status: response.status, ok: response.ok, data };
}

async function commitArchiveToGitHub(config, reportPath, reportJsonText, shotPath, screenshot, createdAt) {
  const [owner, repo] = config.repo.split("/");
  const repoApiPrefix = `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`;
  const encodedBranch = encodeBranchPath(config.branch);
  const botIdentity = {
    name: "bnge-report-bot",
    email: "noreply@users.noreply.github.com",
    date: createdAt,
  };

  let screenshotBlobSha = null;
  if (screenshot && shotPath) {
    const blobRes = await githubRequest(config, "POST", `${repoApiPrefix}/git/blobs`, {
      content: screenshot.base64,
      encoding: "base64",
    });
    if (!blobRes.ok || !blobRes.data?.sha) {
      throw new UpstreamGitHubError("Failed to create screenshot git blob", blobRes.status);
    }
    screenshotBlobSha = blobRes.data.sha;
  }

  // Minimal non-production Wrangler config + 404 preview stub on the orphan archive branch so
  // Cloudflare Workers Builds (when non-production Preview Builds are enabled) succeeds cleanly
  // instead of failing in 0s with a missing wrangler.toml check-run error.
  const archiveWranglerToml = [
    `name = "${config.workerName || DEFAULT_WORKER_NAME}"`,
    'main = "worker/worker.js"',
    'compatibility_date = "2025-01-01"',
    "workers_dev = true",
    "",
  ].join("\n");
  const archiveWorkerStub = [
    "export default {",
    "  async fetch() {",
    '    return new Response("Archive branch preview disabled.\\n", {',
    "      status: 404,",
    '      headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" },',
    "    });",
    "  },",
    "};",
    "",
  ].join("\n");

  const buildTreeEntries = () => {
    const entries = [
      {
        path: reportPath,
        mode: "100644",
        type: "blob",
        content: reportJsonText,
      },
    ];
    if (screenshotBlobSha && shotPath) {
      entries.push({
        path: shotPath,
        mode: "100644",
        type: "blob",
        sha: screenshotBlobSha,
      });
    }
    entries.push(
      {
        path: "wrangler.toml",
        mode: "100644",
        type: "blob",
        content: archiveWranglerToml,
      },
      {
        path: "worker/worker.js",
        mode: "100644",
        type: "blob",
        content: archiveWorkerStub,
      }
    );
    return entries;
  };

  for (let attempt = 0; attempt < 3; attempt++) {
    const refRes = await githubRequest(
      config,
      "GET",
      `${repoApiPrefix}/git/ref/heads/${encodedBranch}`
    );

    if (refRes.status === 404) {
      // Orphan branch bootstrap via GitHub Git Data API (no base_tree, parents: []).
      const rootTreeRes = await githubRequest(config, "POST", `${repoApiPrefix}/git/trees`, {
        tree: buildTreeEntries(),
      });
      if (!rootTreeRes.ok || !rootTreeRes.data?.sha) {
        throw new UpstreamGitHubError("Failed to create orphan root tree", rootTreeRes.status);
      }

      const rootCommitRes = await githubRequest(config, "POST", `${repoApiPrefix}/git/commits`, {
        message: `[skip ci] chore(reports): bootstrap ${config.branch} with ${reportPath}`,
        tree: rootTreeRes.data.sha,
        parents: [],
        author: botIdentity,
        committer: botIdentity,
      });
      if (!rootCommitRes.ok || !rootCommitRes.data?.sha) {
        throw new UpstreamGitHubError("Failed to create orphan root commit", rootCommitRes.status);
      }

      const createRefRes = await githubRequest(config, "POST", `${repoApiPrefix}/git/refs`, {
        ref: `refs/heads/${config.branch}`,
        sha: rootCommitRes.data.sha,
      });
      if (createRefRes.ok) {
        return rootCommitRes.data.sha;
      }
      // Concurrent bootstrap race: another worker created the branch first; retry on head.
      if (createRefRes.status === 422 && attempt < 2) {
        continue;
      }
      throw new UpstreamGitHubError("Failed to create orphan branch ref", createRefRes.status);
    }

    if (!refRes.ok || !refRes.data?.object?.sha) {
      throw new UpstreamGitHubError("Failed to read branch ref", refRes.status);
    }

    const parentCommitSha = refRes.data.object.sha;
    const commitLookupRes = await githubRequest(
      config,
      "GET",
      `${repoApiPrefix}/git/commits/${encodeURIComponent(parentCommitSha)}`
    );
    if (!commitLookupRes.ok || !commitLookupRes.data?.tree?.sha) {
      throw new UpstreamGitHubError("Failed to read parent commit tree", commitLookupRes.status);
    }

    const treeRes = await githubRequest(config, "POST", `${repoApiPrefix}/git/trees`, {
      base_tree: commitLookupRes.data.tree.sha,
      tree: buildTreeEntries(),
    });
    if (!treeRes.ok || !treeRes.data?.sha) {
      throw new UpstreamGitHubError("Failed to create report git tree", treeRes.status);
    }

    const commitRes = await githubRequest(config, "POST", `${repoApiPrefix}/git/commits`, {
      message: `[skip ci] chore(reports): add ${reportPath}`,
      tree: treeRes.data.sha,
      parents: [parentCommitSha],
      author: botIdentity,
      committer: botIdentity,
    });
    if (!commitRes.ok || !commitRes.data?.sha) {
      throw new UpstreamGitHubError("Failed to create report git commit", commitRes.status);
    }

    const updateRefRes = await githubRequest(
      config,
      "PATCH",
      `${repoApiPrefix}/git/refs/heads/${encodedBranch}`,
      {
        sha: commitRes.data.sha,
        force: false,
      }
    );
    if (updateRefRes.ok) {
      return commitRes.data.sha;
    }
    if ((updateRefRes.status === 409 || updateRefRes.status === 422) && attempt < 2) {
      continue;
    }
    throw new UpstreamGitHubError("Failed to advance reports branch ref", updateRefRes.status);
  }

  throw new UpstreamGitHubError("Exceeded retries updating reports branch", 409);
}

function escapeTableCell(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\|/g, "\\|")
    .replace(/`/g, "'")
    .replace(/\r?\n/g, " ")
    .replace(/@(?=\S)/g, "&#64;");
}

function escapeCodeCell(value) {
  return String(value)
    .replace(/\|/g, "\\|")
    .replace(/`/g, "'")
    .replace(/\r?\n/g, " ");
}

function buildFencedBlock(text, lang = "text") {
  const matches = text.match(/`+/g);
  let maxTicks = 2;
  if (matches) {
    for (const m of matches) {
      if (m.length > maxTicks) maxTicks = m.length;
    }
  }
  const fence = "`".repeat(maxTicks + 1);
  return `${fence}${lang}\n${text}\n${fence}`;
}

function formatIssuePayload(config, id, createdAt, report, reportPath, shotPath) {
  const lat5 = report.lat.toFixed(5);
  const lon5 = report.lon.toFixed(5);
  const rawTitleSummary = typeof report.summary === "string" ? report.summary : "";
  const safeTitleSummary = rawTitleSummary.replace(/[\r\n]+/g, " ").trim();
  const autoPrefix = `In-game ${report.kind} report @ ${lat5}, ${lon5}`;
  const customSuffix = safeTitleSummary.startsWith(autoPrefix)
    ? safeTitleSummary.slice(autoPrefix.length).replace(/^[\s\-:]+/, "").trim()
    : safeTitleSummary;
  const baseTitle = `[Report][${report.kind}] ${lat5}, ${lon5}`;
  const title = customSuffix ? `${baseTitle} - ${customSuffix}` : baseTitle;

  const osmUrl = `https://www.openstreetmap.org/?mlat=${lat5}&mlon=${lon5}#map=17/${lat5}/${lon5}`;
  const branchUrlPath = encodeBranchPath(config.branch);
  const jsonArchiveUrl = `https://github.com/${config.repo}/blob/${branchUrlPath}/${reportPath}`;

  const rows = [
    ["Report ID", `\`${escapeCodeCell(id)}\``],
    ["Created (UTC)", `\`${escapeCodeCell(createdAt)}\``],
    ["Kind", `\`${escapeCodeCell(report.kind)}\``],
    ["Coordinates", `\`${lat5}, ${lon5}\``],
    ["OpenStreetMap", `[View on OSM](${osmUrl})`],
    ["Archive JSON", `[\`${reportPath}\`](${jsonArchiveUrl})`],
  ];

  if (report.earthVersion !== undefined) {
    rows.push(["Earth Version", `\`${escapeCodeCell(report.earthVersion)}\``]);
  }
  if (report.addonVersion !== undefined) {
    rows.push(["Addon Version", `\`${escapeCodeCell(report.addonVersion)}\``]);
  }
  if (report.anchorId !== undefined) {
    rows.push(["Anchor ID", `\`${escapeCodeCell(report.anchorId)}\``]);
  }
  if (report.headingDeg !== undefined) {
    rows.push(["Heading (deg)", `\`${report.headingDeg}\``]);
  }
  if (report.altM !== undefined) {
    rows.push(["Altitude (m)", `\`${report.altM}\``]);
  }
  if (report.speedMs !== undefined) {
    rows.push(["Speed (m/s)", `\`${report.speedMs}\``]);
  }
  if (typeof report.camera === "string") {
    rows.push(["Camera", `\`${escapeCodeCell(report.camera)}\``]);
  }
  if (report.loading_incomplete !== undefined) {
    const incVal = report.loading_incomplete
      ? report.loading_reason
        ? `\`true\` (\`${escapeCodeCell(report.loading_reason)}\`)`
        : "`true`"
      : "`false`";
    rows.push(["Loading Incomplete", incVal]);
  }

  const lines = [
    "## Summary",
    "",
    report.summary ? escapeTableCell(report.summary) : "_No summary provided._",
    "",
    "## Metadata",
    "",
    "| Field | Value |",
    "| --- | --- |",
    ...rows.map(([k, v]) => `| ${k} | ${v} |`),
  ];

  if (report.note) {
    lines.push("", "## Reporter Note", "", buildFencedBlock(report.note, "text"));
  }

  if (report.camera && typeof report.camera === "object") {
    lines.push(
      "",
      "## Camera Telemetry",
      "",
      buildFencedBlock(JSON.stringify(report.camera, null, 2), "json")
    );
  }

  if (report.nearest) {
    lines.push(
      "",
      "## Nearest Features",
      "",
      buildFencedBlock(JSON.stringify(report.nearest, null, 2), "json")
    );
  }

  if (shotPath) {
    const shotUrl = `https://github.com/${config.repo}/blob/${branchUrlPath}/${shotPath}?raw=true`;
    lines.push("", "## Screenshot", "", `![Report screenshot](${shotUrl})`, "", shotUrl);
  }

  return {
    title,
    body: lines.join("\n"),
    labels: ["user-report", report.kind],
  };
}

async function openGitHubIssue(config, issuePayload) {
  const [owner, repo] = config.repo.split("/");
  const issuesPath = `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/issues`;

  const initialRes = await githubRequest(config, "POST", issuesPath, issuePayload);
  if (initialRes.ok && initialRes.data?.number) {
    return initialRes.data;
  }

  // Retry without labels if GitHub rejects unknown or unauthorized labels.
  const shouldRetryWithoutLabels =
    initialRes.status === 422 ||
    (initialRes.status === 403 &&
      JSON.stringify(initialRes.data || "").toLowerCase().includes("label"));

  if (shouldRetryWithoutLabels) {
    const fallbackRes = await githubRequest(config, "POST", issuesPath, {
      title: issuePayload.title,
      body: issuePayload.body,
    });
    if (fallbackRes.ok && fallbackRes.data?.number) {
      return fallbackRes.data;
    }
    throw new UpstreamGitHubError(
      "Failed to create GitHub issue (after label-free retry)",
      fallbackRes.status
    );
  }

  throw new UpstreamGitHubError("Failed to create GitHub issue", initialRes.status);
}

function buildCorsHeaders(env) {
  const origin =
    typeof env?.CORS_ORIGIN === "string" && env.CORS_ORIGIN.trim()
      ? env.CORS_ORIGIN.trim()
      : "*";
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Cache-Control": "no-store",
  };
}

function jsonResponse(payload, status, env, extraHeaders = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...buildCorsHeaders(env),
      ...extraHeaders,
    },
  });
}

export default {
  async fetch(request, env = {}) {
    let url;
    try {
      url = new URL(request.url);
    } catch {
      return jsonResponse({ ok: false, error: "Invalid request URL" }, 400, env);
    }

    const pathname = url.pathname.length > 1 ? url.pathname.replace(/\/+$/, "") : url.pathname;
    const method = request.method.toUpperCase();

    if (method === "OPTIONS" && (pathname === "/health" || pathname === "/report")) {
      return new Response(null, { status: 204, headers: buildCorsHeaders(env) });
    }

    if (pathname === "/health") {
      if (method !== "GET") {
        return jsonResponse(
          { ok: false, error: "Method not allowed" },
          405,
          env,
          { Allow: "GET, OPTIONS" }
        );
      }
      const gh = resolveGitHubConfig(env);
      return jsonResponse(
        {
          ok: true,
          service: SERVICE_NAME,
          branch: gh.branch,
          repoConfigured: gh.repoConfigured,
        },
        200,
        env
      );
    }

    if (pathname === "/report") {
      if (method !== "POST") {
        return jsonResponse(
          { ok: false, error: "Method not allowed" },
          405,
          env,
          { Allow: "POST, OPTIONS" }
        );
      }

      const rl = await checkRateLimit(request, env);
      if (!rl.allowed) {
        return jsonResponse(
          { ok: false, error: "Rate limit exceeded", retryAfter: rl.retryAfterSec },
          429,
          env,
          { "Retry-After": String(rl.retryAfterSec) }
        );
      }

      const ghConfig = resolveGitHubConfig(env);
      if (!ghConfig.repoConfigured) {
        return jsonResponse(
          { ok: false, error: "Collector GitHub repository is not configured" },
          503,
          env
        );
      }

      try {
        const rawBody = await readJsonBodyBounded(request, MAX_BODY_BYTES);
        const { report, screenshot } = validateAndSanitizeReport(rawBody);

        const now = new Date();
        const createdAt = now.toISOString();
        const dateDir = createdAt.slice(0, 10);
        const id = generateReportId(now);
        const reportPath = `reports/${dateDir}/${id}.json`;
        const shotPath = screenshot ? `screenshots/${dateDir}/${id}.${screenshot.ext}` : null;

        const archiveRecord = {
          schemaVersion: 1,
          id,
          createdAt,
          ...report,
          ...(shotPath ? { screenshotPath: shotPath } : {}),
        };
        const archiveJsonText = `${JSON.stringify(archiveRecord, null, 2)}\n`;

        await commitArchiveToGitHub(
          ghConfig,
          reportPath,
          archiveJsonText,
          shotPath,
          screenshot,
          createdAt
        );

        const issuePayload = formatIssuePayload(
          ghConfig,
          id,
          createdAt,
          report,
          reportPath,
          shotPath
        );
        const issue = await openGitHubIssue(ghConfig, issuePayload);

        return jsonResponse(
          {
            ok: true,
            id,
            issueNumber: issue.number,
            issueUrl: issue.html_url || issue.url || "",
          },
          201,
          env
        );
      } catch (err) {
        if (err instanceof ValidationError) {
          return jsonResponse({ ok: false, error: err.message }, err.status, env);
        }
        if (err instanceof UpstreamGitHubError) {
          return jsonResponse(
            {
              ok: false,
              error: err.message,
              upstreamStatus: err.upstreamStatus,
            },
            502,
            env
          );
        }
        return jsonResponse({ ok: false, error: "Internal collector error" }, 500, env);
      }
    }

    return jsonResponse({ ok: false, error: "Not found" }, 404, env);
  },
};
