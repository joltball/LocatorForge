# PHASE: 3.1.1
"""ripgrep-based POM file discovery (3-pass strategy from SPEC §7).

Returns a ranked list of `PomCandidate` for the current page URL.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from .config import SearchCfg

log = logging.getLogger(__name__)


@dataclass
class PomCandidate:
    path: str
    score: int = 0
    reasons: list[str] = field(default_factory=list)


def _have_rg() -> bool:
    return shutil.which("rg") is not None


def _rg_l(query: str, root: Path, source_dirs: list[str], exclude_dirs: list[str]) -> list[str]:
    """Run `rg --json -l <query>` scoped to source dirs and return matched paths."""
    if not _have_rg():
        log.warning("ripgrep (rg) not on PATH; repo_scanner cannot run.")
        return []
    args = ["rg", "--json", "-l", query]
    for ex in exclude_dirs:
        args.extend(["-g", f"!{ex}"])
    # Only include configured source dirs if any of them exist
    targets: list[str] = []
    for d in source_dirs:
        p = (root / d).resolve()
        if p.exists():
            targets.append(str(p))
    if not targets:
        targets.append(str(root))
    args.extend(targets)
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return []
    paths: list[str] = []
    for line in proc.stdout.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("type") == "begin":
            p = msg.get("data", {}).get("path", {}).get("text")
            if p:
                paths.append(p)
    return paths


def _url_path_segments(url: str) -> list[str]:
    try:
        path = urlparse(url).path
    except Exception:  # noqa: BLE001
        return []
    parts = [p for p in re.split(r"[/_\-]+", path) if p and not p.isdigit()]
    return parts


def _segment_to_class_prefix(seg: str) -> str:
    return seg[:1].upper() + seg[1:]


def search_poms(
    repo_root: Path,
    cfg: SearchCfg,
    current_url: Optional[str],
) -> list[PomCandidate]:
    by_path: dict[str, PomCandidate] = {}

    def add(path: str, score: int, reason: str) -> None:
        c = by_path.setdefault(path, PomCandidate(path=path))
        c.score += score
        c.reasons.append(reason)

    segs = _url_path_segments(current_url or "")

    # Pass 1: URL path segments
    for seg in segs:
        if len(seg) < 3:
            continue
        for p in _rg_l(re.escape(seg), repo_root, cfg.source_dirs, cfg.exclude_dirs):
            add(p, 3, f"url:{seg}")

    # Pass 2: annotation / decorator presence
    for p in _rg_l(r"@PageUrl|@FindBy|page_url|BASE_URL", repo_root, cfg.source_dirs, cfg.exclude_dirs):
        add(p, 1, "annotation")

    # Pass 3: naming convention (class XxxPage)
    for seg in segs:
        if len(seg) < 3:
            continue
        pat = rf"class\s+{_segment_to_class_prefix(seg)}\w*Page"
        for p in _rg_l(pat, repo_root, cfg.source_dirs, cfg.exclude_dirs):
            add(p, 4, f"naming:{seg}")

    # Bonus: file-name pattern matches (handled here as a soft signal)
    suffixes = tuple([fp.lstrip("*") for fp in cfg.file_patterns])
    for c in by_path.values():
        if any(c.path.endswith(s) for s in suffixes):
            c.score += 1
            c.reasons.append("file-pattern")

    return sorted(by_path.values(), key=lambda c: c.score, reverse=True)
