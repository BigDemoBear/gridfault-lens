"""Small JSON contracts shared by parsers, verification and CLI."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
from typing import Any

class CaseError(ValueError):
    """An actionable input or contract failure."""

@dataclass
class Source:
    id: str
    path: str
    kind: str
    raw_path: str
    size: int
    encoding: str | None = None
    text: str | None = None
    problem: str | None = None

@dataclass
class Claim:
    id: str
    field: str
    value: Any
    source: str
    scope: str
    evidence: dict
    method: str = "deterministic"
    unit: str | None = None
    basis: str | None = None
    status: str = "VERIFIED"
    reasons: list[str] = field(default_factory=list)
    source_scope: str | None = None

@dataclass
class Issue:
    code: str
    message: str
    source: str | None = None

def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                               allow_nan=False) + "\n", encoding="utf-8")

def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"),
                      parse_constant=lambda x: (_ for _ in ()).throw(CaseError("Non-finite JSON: " + x)))

def finite_number(raw: str) -> float:
    n = float(raw)
    if not math.isfinite(n):
        raise CaseError("Non-finite number")
    return n

def payload(items: list) -> list[dict]:
    return [asdict(x) for x in items]
