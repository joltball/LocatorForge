# PHASE: 5.2
"""Iframe enumeration and frame-chain construction (ADR-07).

Walks `Page.getFrameTree`, resolves each child frame's owning `<iframe>` element
to a CSS selector via `DOM.getFrameOwner`, and produces an ordered `FrameRef`
chain per frame so locators can emit `switchTo().frame(...)` / `frameLocator(...)`.

Scope note (ADR-07): only same-process frames are handled — reached by passing
`frameId` to the Accessibility calls on the page session. Cross-origin OOPIFs,
which would need `Target.setAutoAttach` + `sessionId` routing, are deliberately
out of scope; the reference AUT produced none.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .cdp_engine import CdpEngine, CdpError
from .schemas import FrameRef

log = logging.getLogger(__name__)

# Frames whose URL matches these never carry test targets.
_NOISE_URL_PREFIXES = ("about:blank", "chrome-error://", "about:srcdoc")

# Owning-element ids/names that are known instrumentation, not app content.
# Substring match, case-insensitive. Keeps the tree free of a dozen empty
# boundary nodes on enterprise apps.
_NOISE_SELECTOR_HINTS = (
    "tmx_tags_iframe",          # ThreatMetrix device fingerprinting
    "fontdetectionframe",       # font enumeration probe
    "destination_publishing",   # Adobe/tag-manager publishing frame
    "tdr_",                     # content tag frames
    "google_ads",
    "googletagmanager",
    "doubleclick",
)


@dataclass
class FrameInfo:
    frame_id: str
    url: str
    depth: int
    parent_id: Optional[str] = None
    owner_backend_node_id: Optional[int] = None
    selector: Optional[str] = None
    chain: list[FrameRef] = field(default_factory=list)
    is_noise: bool = False

    def to_ref(self) -> FrameRef:
        return FrameRef(
            frame_id=self.frame_id,
            url=self.url,
            selector=self.selector,
            is_oopif=False,
        )


def _selector_from_attrs(attrs: dict[str, str], index_hint: int) -> str:
    """Best-effort CSS selector for an <iframe>, in descending stability order."""
    if attrs.get("id"):
        return f"iframe#{attrs['id']}"
    if attrs.get("name"):
        return f"iframe[name='{attrs['name']}']"
    if attrs.get("title"):
        return f"iframe[title='{attrs['title']}']"
    cls = (attrs.get("class") or "").split()
    if cls:
        return f"iframe.{cls[0]}"
    return f"iframe:nth-of-type({index_hint + 1})"


def _looks_like_noise(url: str, selector: Optional[str]) -> bool:
    if any(url.startswith(p) for p in _NOISE_URL_PREFIXES):
        return True
    hay = (selector or "").lower()
    return any(h in hay for h in _NOISE_SELECTOR_HINTS)


async def _describe_owner(cdp: CdpEngine, frame_id: str) -> tuple[Optional[int], dict[str, str]]:
    """Return (backendNodeId, attributes) for the <iframe> element owning `frame_id`."""
    try:
        owner = await cdp.send("DOM.getFrameOwner", {"frameId": frame_id})
    except CdpError as e:
        log.debug("getFrameOwner failed for %s: %s", frame_id, e)
        return None, {}
    backend_id = owner.get("backendNodeId")
    if not backend_id:
        return None, {}
    try:
        described = await cdp.send("DOM.describeNode", {"backendNodeId": backend_id})
    except CdpError:
        return backend_id, {}
    node = described.get("node") or {}
    flat = node.get("attributes") or []
    attrs = {flat[i]: flat[i + 1] for i in range(0, len(flat) - 1, 2)}
    return backend_id, attrs


async def enumerate_frames(cdp: CdpEngine, include_noise: bool = False) -> list[FrameInfo]:
    """Enumerate every child frame of the current page, deepest-last.

    The main frame is NOT included — only children, since the main frame is
    already covered by the default AX walk.
    """
    try:
        tree = await cdp.send("Page.getFrameTree")
    except CdpError as e:
        log.warning("Page.getFrameTree failed: %s", e)
        return []

    flat: list[FrameInfo] = []

    def walk(node: dict, depth: int, parent_id: Optional[str]) -> None:
        frame = node.get("frame") or {}
        fid = frame.get("id")
        if fid and depth > 0:
            flat.append(FrameInfo(
                frame_id=fid,
                url=frame.get("url") or "",
                depth=depth,
                parent_id=parent_id,
            ))
        for child in node.get("childFrames") or []:
            walk(child, depth + 1, fid)

    walk(tree.get("frameTree") or {}, 0, None)

    # Resolve owning <iframe> elements → selectors
    per_parent_counter: dict[Optional[str], int] = {}
    for info in flat:
        idx = per_parent_counter.get(info.parent_id, 0)
        per_parent_counter[info.parent_id] = idx + 1
        backend_id, attrs = await _describe_owner(cdp, info.frame_id)
        info.owner_backend_node_id = backend_id
        info.selector = _selector_from_attrs(attrs, idx) if attrs or backend_id else None
        info.is_noise = _looks_like_noise(info.url, info.selector)

    # Build ordered ancestor chains
    by_id = {f.frame_id: f for f in flat}
    for info in flat:
        chain: list[FrameRef] = []
        cursor: Optional[FrameInfo] = info
        guard = 0
        while cursor is not None and guard < 10:
            chain.append(cursor.to_ref())
            cursor = by_id.get(cursor.parent_id) if cursor.parent_id else None
            guard += 1
        info.chain = list(reversed(chain))

    kept = flat if include_noise else [f for f in flat if not f.is_noise]
    log.info(
        "[frames] %d child frame(s) found, %d kept after noise filter",
        len(flat), len(kept),
    )
    for f in kept:
        log.info("[frames]   d%d %s  selector=%s", f.depth, f.url[:60], f.selector)
    return kept


# ---- locator-generation helpers -------------------------------------------

def selenium_frame_preamble(chain: list[FrameRef]) -> str:
    """Selenium switchTo() chain for an element inside `chain`."""
    if not chain:
        return ""
    lines = ["driver.switchTo().defaultContent();"]
    for ref in chain:
        sel = ref.selector or "iframe"
        lines.append(
            f"driver.switchTo().frame(driver.findElement(By.cssSelector(\"{sel}\")));"
        )
    return "\n".join(lines)


def playwright_frame_prefix(chain: list[FrameRef]) -> str:
    """Playwright frameLocator() chain, e.g. page.frameLocator("iframe[name='Main']")."""
    if not chain:
        return "page"
    parts = ["page"]
    for ref in chain:
        sel = ref.selector or "iframe"
        parts.append(f"frameLocator(\"{sel}\")")
    return ".".join(parts)
