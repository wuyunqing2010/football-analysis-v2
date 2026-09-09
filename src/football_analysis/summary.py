from __future__ import annotations

import json
from pathlib import Path
from typing import Any


KEYWORDS = {
    "odds": ("odds", "spf", "rqspf", "让球", "赔率", "胜平负"),
    "match": ("match", "fixture", "game", "比赛", "赛事"),
    "history": ("history", "result", "record", "历史", "赛果"),
}


def _labels(record: dict[str, Any], payload: Any) -> list[str]:
    haystack = (record.get("url", "") + " " + json.dumps(payload, ensure_ascii=False)[:12000]).lower()
    return [label for label, words in KEYWORDS.items() if any(word in haystack for word in words)]


def summarize(root: str | Path) -> dict[str, Any]:
    root_path = Path(root)
    reports = sorted(root_path.glob("*/*/report.json"))
    output: dict[str, Any] = {"reports": len(reports), "sources": {}}
    for report_path in reports:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        source = output["sources"].setdefault(
            report["source_id"], {"runs": 0, "json_responses": 0, "candidates": []}
        )
        source["runs"] += 1
        source["json_responses"] += report.get("json_response_count", 0)
        for record in report.get("responses", []):
            payload_path = report_path.parent / record["file"]
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            labels = _labels(record, payload)
            if labels:
                source["candidates"].append(
                    {
                        "labels": labels,
                        "url": record["url"],
                        "status": record["status"],
                        "bytes": record["bytes"],
                        "file": str(payload_path),
                    }
                )
    return output

