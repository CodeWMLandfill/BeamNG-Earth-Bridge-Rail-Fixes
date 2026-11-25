"""Re-stage a deferred Bridge, Rail & Tunnel Fixes patch onto the current target.

Invoked inside the cross-addon swap lock when a sibling companion add-on
completed its atomic swap first during the same BeamNG.Earth close cycle.
"""
import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import patch_server  # noqa: E402

_PRESERVED_FIELDS = (
    "notify",
    "skipReadyNotice",
    "standalone",
    "cleanupOnRestore",
    "relaunchAfterSwap",
    "completionNotice",
    "installReceipt",
    "lastTargetFile",
)


def restage_receipt(receipt_path):
    receipt_path = Path(receipt_path).resolve()
    record = patch_server.read_json(receipt_path, None)
    if not isinstance(record, dict) or not record.get("target"):
        raise RuntimeError("Missing or invalid staged patch receipt")

    config_path = Path(record["config"]).resolve()
    configuration = patch_server.read_json(config_path, {})
    req_token = record.get("token", "")
    if req_token and configuration.get("requestToken") != req_token:
        return 2

    operation = record.get("operation", "install")
    if operation == "install" and not record.get("standalone"):
        mod_path = configuration.get("modPath")
        if not configuration.get("enabled") or not mod_path or not Path(mod_path).is_file():
            record["state"] = "cancelled"
            record["message"] = "The add-on was disabled or removed; patch cancelled."
            patch_server.atomic_json(record["sidecar"], record)
            patch_server.atomic_json(receipt_path, record)
            return 0

    payload_cand = HERE.parent / "payload"
    payload_dir = payload_cand if payload_cand.is_dir() else None
    new_record = patch_server.prepare(
        record["target"],
        config_path,
        receipt_path,
        payload_dir,
        operation=operation,
        token=req_token,
        server_pid=0,
        apply_fixes=bool(record.get("applyFixes", True)),
        enable_reporter=bool(record.get("enableReporter", True)),
    )
    for key in _PRESERVED_FIELDS:
        if key in record:
            new_record[key] = record[key]
    new_record["companionPid"] = 0
    patch_server.atomic_json(new_record["sidecar"], new_record)
    patch_server.atomic_json(receipt_path, new_record)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, help="Path to staged patch-status.json receipt")
    args = parser.parse_args(argv)
    try:
        return restage_receipt(args.receipt)
    except Exception as exc:
        print(exc, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
