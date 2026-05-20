"""Content hashing and cache-sidecar helpers.

All pipeline steps share these so dedup decisions are reproducible across runs.
See docs/contracts.md section 4 for the hash spec.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable

_UNIT_SEP = b"\x1f"


def content_hash(parts: Iterable[str | bytes]) -> str:
    h = hashlib.sha256()
    for p in parts:
        if isinstance(p, str):
            p = p.encode("utf-8")
        h.update(_UNIT_SEP)
        h.update(p)
    return h.hexdigest()


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def hash_sidecar_path(output_path: Path) -> Path:
    return output_path.with_suffix(output_path.suffix + ".hash")


def read_hash(output_path: Path) -> str | None:
    sidecar = hash_sidecar_path(output_path)
    if not sidecar.exists():
        return None
    return sidecar.read_text(encoding="utf-8").strip() or None


def write_hash(output_path: Path, digest: str) -> None:
    hash_sidecar_path(output_path).write_text(digest, encoding="utf-8")


def is_cached(output_path: Path, input_digest: str) -> bool:
    """True iff the output file exists AND its sidecar matches input_digest."""
    if not output_path.exists():
        return False
    return read_hash(output_path) == input_digest


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
