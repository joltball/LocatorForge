# PHASE: 3.4.1
"""Shadow DOM detection + traversal (ADR-02, Level 3).

Walks the live DOM with `DOM.getDocument({depth:-1, pierce:true})`, locates
shadow hosts, and produces a flat list of shadow-host records plus the
descendant accessibility subtrees, tagged with ordered `shadowAncestors`.

This module is used by the API server's tree refresh to splice shadow subtrees
into the primary accessibility tree at the host node's location.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .cdp_engine import CdpEngine, CdpError
from .config import ShadowDomCfg
from .schemas import ShadowHostRef

log = logging.getLogger(__name__)


@dataclass
class ShadowHostRecord:
    backend_node_id: int
    host_selector: str
    shadow_type: str           # "open" | "closed"
    depth: int
    ancestors: list[ShadowHostRef] = field(default_factory=list)
    ax_subtree: Optional[dict] = None


def _host_selector_for(node: dict) -> str:
    """Best-effort selector for a shadow host node."""
    tag = (node.get("localName") or node.get("nodeName") or "").lower()
    attrs = node.get("attributes") or []
    # attributes is a flat [k1, v1, k2, v2, ...]
    a: dict[str, str] = {}
    for i in range(0, len(attrs), 2):
        if i + 1 < len(attrs):
            a[attrs[i]] = attrs[i + 1]
    if "id" in a:
        return f"{tag}#{a['id']}"
    if "data-testid" in a:
        return f"{tag}[data-testid='{a['data-testid']}']"
    return tag or "*"


async def traverse(
    cdp: CdpEngine,
    cfg: ShadowDomCfg,
) -> list[ShadowHostRecord]:
    """Walk DOM pierced through shadow boundaries and emit one record per host."""
    try:
        doc = await cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
    except CdpError as e:
        log.warning("DOM.getDocument failed: %s", e)
        return []

    out: list[ShadowHostRecord] = []

    def visit(node: dict, depth: int, ancestors: list[ShadowHostRef]) -> None:
        if depth > cfg.max_depth:
            return
        # Did this element bring its own shadow root in the pierce?
        shadow_roots = node.get("shadowRoots") or []
        if shadow_roots:
            for sr in shadow_roots:
                stype = sr.get("shadowRootType") or "open"
                if stype != "open" and not cfg.traverse_closed:
                    continue
                host_sel = _host_selector_for(node)
                rec = ShadowHostRecord(
                    backend_node_id=node.get("backendNodeId") or 0,
                    host_selector=host_sel,
                    shadow_type=stype,
                    depth=depth,
                    ancestors=list(ancestors),
                )
                out.append(rec)
                # Descend into shadow children, extending the ancestor chain.
                new_anc = ancestors + [ShadowHostRef(host_selector=host_sel, shadow_type=stype)]
                for child in sr.get("children") or []:
                    visit(child, depth + 1, new_anc)
        for child in node.get("children") or []:
            visit(child, depth, ancestors)

    root_node = (doc or {}).get("root")
    if root_node:
        visit(root_node, 0, [])

    # Populate AX subtree per host
    for rec in out:
        try:
            ax = await cdp.send(
                "Accessibility.getFullAXTree",
                {"backendNodeId": rec.backend_node_id},
            )
            rec.ax_subtree = ax
        except CdpError as e:
            log.debug("getFullAXTree failed for host %s: %s", rec.host_selector, e)

    return out


# ---- locator-generation helpers used by locator_resolver --------------------

def selenium_shadow_chain(host_chain: list[ShadowHostRef], final_css: str) -> str:
    """Return a multi-line Selenium expression that pierces the host chain."""
    parts = ["driver.findElement(By.cssSelector(\"" + host_chain[0].host_selector + "\"))"]
    for h in host_chain[1:]:
        parts.append(f"    .getShadowRoot()\n    .findElement(By.cssSelector(\"{h.host_selector}\"))")
    parts.append(f"    .getShadowRoot()\n    .findElement(By.cssSelector(\"{final_css}\"))")
    return "\n".join(parts)


def playwright_shadow_chain(host_chain: list[ShadowHostRef], final_css: str) -> str:
    """Return a Playwright `>>>` piercing combinator chain."""
    segs = [h.host_selector for h in host_chain]
    segs.append(final_css)
    return "page.locator('" + " >>> ".join(segs) + "')"
