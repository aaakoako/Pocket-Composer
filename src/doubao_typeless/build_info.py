"""构建身份只读信息；不依据目录名字猜版本。"""
from pathlib import Path
import json
import sys

VERSION = "0.5.9"


def build_info() -> dict:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    try:
        value = json.loads((root / "build-info.json").read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError('build identity must be an object')
        sha = value.get("source_sha", "")
        if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
            channel = value.get("channel", "v3-private-trial")
            if channel not in {"v3-private-trial", "release-candidate", "stable"}:
                channel = "v3-private-trial"
            return {"source_sha": sha, "channel":channel, "version":VERSION,
                    "release_ready": value.get("release_ready") is True and channel == "stable"}
    except (OSError, ValueError, TypeError):
        pass
    return {"source_sha":"development", "channel":"v3-private-trial", "version":VERSION, "release_ready":False}


def release_layout() -> bool:
    return build_info()["channel"] in {"release-candidate", "stable"}

