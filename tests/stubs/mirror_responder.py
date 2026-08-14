import json
import os
import sys


def main() -> int:
    json.load(sys.stdin)
    json.dump({"response": os.environ.get("MIRROR_RESPONSE", "measured pushback")}, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
