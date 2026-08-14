#!/usr/bin/env python3
from __future__ import annotations

import json
import sys

value = json.load(sys.stdin)
if "proposal" in value:
    json.dump({"verdict": "APPROVE", "reason": "fixture approval"}, sys.stdout)
else:
    json.dump(
        {
            "proposal_id": value["proposal_id"],
            "face": value["face"],
            "phrase_id": value["phrase_id"],
            "transition": value["transition"],
            "evidence": value.get("evidence", {}),
            "generated_at": value["generated_at"],
        },
        sys.stdout,
    )

