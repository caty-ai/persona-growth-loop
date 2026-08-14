import base64
import json
import os
import sys


def _set(name: str) -> set[str]:
    value = os.environ.get(name, "")
    return {item for item in value.split(",") if item}


def main() -> int:
    payload = json.load(sys.stdin)
    probe_id = str(payload.get("probe_id", ""))
    unclear_ids = _set("MIRROR_SCORER_UNCLEAR_IDS")
    if probe_id in unclear_ids:
        json.dump({"verdict": "unclear"}, sys.stdout)
        return 0
    block = b"".join(
        base64.b64decode(item["bytes_b64"])
        for item in payload.get("block", {}).get("files", [])
        if isinstance(item, dict) and isinstance(item.get("bytes_b64"), str)
    )
    trigger = os.environ.get("MIRROR_SCORER_TRIGGER_BYTES", "").encode("utf-8")
    flip_ids = _set("MIRROR_SCORER_FLIP_IDS")
    verdict = "agree" if trigger and trigger in block and probe_id in flip_ids else "pushback"
    json.dump({"verdict": verdict}, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
