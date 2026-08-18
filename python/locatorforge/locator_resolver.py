# PHASE: 2.1.2  (shadow-piercing extension in PHASE 3.4.2)
"""Compute every candidate locator strategy for a TreeNode, rank them by
configurable priority, validate uniqueness against the live DOM, and emit
**both** Selenium (`@FindBy(...)`) and Playwright (`page.locator(...)` /
`getByRole` / `getByTestId`) strings for every node — regardless of which the UI
currently displays (per ADR-04).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .cdp_engine import CdpEngine, CdpError
from .config import LocatorsCfg
from .schemas import ShadowHostRef, TreeNode
from .shadow_dom_traverser import playwright_shadow_chain, selenium_shadow_chain


@dataclass
class LocatorCandidate:
    strategy: str              # data-testid | aria-label | id | name | css | xpath | role
    value: str
    selenium: str
    playwright: str
    rank: int                  # lower = better
    match_count: Optional[int] = None  # populated after validate()
    is_unique: Optional[bool] = None
    shadow_chain: list[ShadowHostRef] = field(default_factory=list)


def _quote_css_attr(v: str) -> str:
    return v.replace('"', '\\"')


def _xpath_text(v: str) -> str:
    # Only strings destined for XPath text literals — apostrophes are the risk.
    return v.replace("'", "&apos;")


# Conservative AX-role → HTML tag mapping. Only entries where the tag is
# unambiguous. Missing entries fall back to `*` in XPath and to the raw role
# name being suppressed from CSS.
ROLE_TO_TAG = {
    "link": "a",
    "button": "button",
    "textbox": "input",
    "searchbox": "input",
    "checkbox": "input",
    "radio": "input",
    "combobox": "select",
    "spinbutton": "input",
    "slider": "input",
    "switch": "input",
    "img": "img",
    "image": "img",
    "form": "form",
    "navigation": "nav",
    "banner": "header",
    "main": "main",
    "contentinfo": "footer",
    "complementary": "aside",
    "article": "article",
    "region": "section",
    "list": "ul",
    "listitem": "li",
    "table": "table",
    "row": "tr",
    "cell": "td",
    "columnheader": "th",
    "rowheader": "th",
}


def _tag_for(node: TreeNode) -> Optional[str]:
    """Best-effort HTML tag for an AX node. Prefer explicit `tag` attribute,
    otherwise map from role. Returns None when unknown."""
    raw = node.attributes.get("tag")
    if raw:
        return raw.lower()
    role = (node.role or "").lower()
    return ROLE_TO_TAG.get(role)


def _build_candidates(node: TreeNode) -> list[LocatorCandidate]:
    attrs = node.attributes
    role = (node.role or "").lower()
    name = node.name or ""
    tag = _tag_for(node)
    tag_prefix = tag or "*"          # `//*[…]` when tag unknown
    out: list[LocatorCandidate] = []

    if "data-testid" in attrs:
        v = attrs["data-testid"]
        out.append(LocatorCandidate(
            strategy="data-testid", value=v, rank=0,
            selenium=f"@FindBy(css = \"[data-testid='{_quote_css_attr(v)}']\")",
            playwright=f"page.getByTestId('{v}')",
        ))
    if "aria-label" in attrs:
        v = attrs["aria-label"]
        # Tag-scope the CSS when we know the tag — more discriminating and
        # matches what a human would write.
        css_sel = (
            f"{tag}[aria-label='{_quote_css_attr(v)}']"
            if tag else f"[aria-label='{_quote_css_attr(v)}']"
        )
        out.append(LocatorCandidate(
            strategy="aria-label", value=v, rank=10,
            selenium=f"@FindBy(css = \"{css_sel}\")",
            playwright=f"page.getByLabel('{v}')",
        ))
    if "id" in attrs:
        v = attrs["id"]
        out.append(LocatorCandidate(
            strategy="id", value=v, rank=20,
            selenium=f"@FindBy(id = \"{v}\")",
            playwright=f"page.locator('#{v}')",
        ))
    if "name" in attrs:
        v = attrs["name"]
        out.append(LocatorCandidate(
            strategy="name", value=v, rank=30,
            selenium=f"@FindBy(name = \"{v}\")",
            playwright=f"page.locator(\"[name='{_quote_css_attr(v)}']\")",
        ))
    # Role + accessible name — Playwright is primary; Selenium uses a tag-scoped
    # text XPath as the closest equivalent.
    if role and name:
        xp_role = f"//{tag_prefix}[normalize-space()='{_xpath_text(name)}']"
        out.append(LocatorCandidate(
            strategy="role", value=f"{role}|{name}", rank=40,
            selenium=f"@FindBy(xpath = \"{xp_role}\")",
            playwright=f"page.getByRole('{role}', {{ name: '{name}' }})",
        ))
    # CSS fallback by real HTML tag. Skip when we couldn't derive one — an AX
    # role name like "link" isn't a valid CSS tag.
    if tag:
        out.append(LocatorCandidate(
            strategy="css", value=tag, rank=50,
            selenium=f"@FindBy(css = \"{tag}\")",
            playwright=f"page.locator('{tag}')",
        ))
    # XPath — best-effort. Prefer an attribute-scoped form (aria-label > id >
    # name > data-testid) over text matching; only fall back to
    # `normalize-space()` when no discriminating attribute is available.
    xp_value = _best_xpath(tag_prefix, attrs, name)
    if xp_value:
        out.append(LocatorCandidate(
            strategy="xpath", value=xp_value, rank=60,
            selenium=f"@FindBy(xpath = \"{xp_value}\")",
            playwright=f"page.locator(\"xpath={xp_value}\")",
        ))
    return out


def _best_xpath(tag_prefix: str, attrs: dict[str, str], name: str) -> Optional[str]:
    for a in ("aria-label", "id", "name", "data-testid"):
        v = attrs.get(a)
        if v:
            return f"//{tag_prefix}[@{a}='{_xpath_text(v)}']"
    if name:
        return f"//{tag_prefix}[normalize-space()='{_xpath_text(name)}']"
    return None


def _rank_by_config(cands: list[LocatorCandidate], cfg: LocatorsCfg) -> list[LocatorCandidate]:
    order = {s: i for i, s in enumerate(cfg.priority)}
    cands.sort(key=lambda c: (order.get(c.strategy, 999), c.rank))
    for i, c in enumerate(cands):
        c.rank = i
    return cands


async def validate_uniqueness(cdp: CdpEngine, cand: LocatorCandidate) -> None:
    """Run DOM.querySelectorAll for CSS-shaped strategies; for XPath use
    Runtime.evaluate. Stores match_count + is_unique on the candidate."""
    try:
        if cand.strategy in {"data-testid", "aria-label", "id", "name", "css", "role"}:
            selector = _to_css(cand)
            if selector is None:
                return
            doc = await cdp.send("DOM.getDocument", {"depth": 0})
            root_id = doc["root"]["nodeId"]
            res = await cdp.send("DOM.querySelectorAll", {"nodeId": root_id, "selector": selector})
            ids = res.get("nodeIds") or []
            cand.match_count = len(ids)
            cand.is_unique = len(ids) == 1
        elif cand.strategy == "xpath":
            res = await cdp.send("Runtime.evaluate", {
                "expression": (
                    "document.evaluate("
                    f"{_js_str(cand.value)}, document, null, "
                    "XPathResult.UNORDERED_NODE_SNAPSHOT_TYPE, null).snapshotLength"
                ),
                "returnByValue": True,
            })
            n = (res.get("result") or {}).get("value")
            if isinstance(n, int):
                cand.match_count = n
                cand.is_unique = n == 1
    except CdpError:
        cand.match_count = None
        cand.is_unique = None


def _to_css(c: LocatorCandidate) -> Optional[str]:
    """Return a CSS selector that mirrors this candidate for live uniqueness
    validation. Kept in sync with the CSS shown in `_build_candidates`."""
    if c.strategy == "data-testid":
        return f"[data-testid='{c.value}']"
    if c.strategy == "aria-label":
        # The candidate's Selenium string is the source of truth for whether
        # the CSS is tag-scoped; re-derive the same selector for validation.
        return _extract_css_from_findby(c.selenium) or f"[aria-label='{c.value}']"
    if c.strategy == "id":
        return f"#{c.value}"
    if c.strategy == "name":
        return f"[name='{c.value}']"
    if c.strategy == "css":
        return c.value
    if c.strategy == "role":
        role = c.value.split("|", 1)[0]
        return f"[role='{role}']" if role else None
    return None


def _extract_css_from_findby(annotation: str) -> Optional[str]:
    """`@FindBy(css = "…")` → `…` (or None if not that shape)."""
    prefix = '@FindBy(css = "'
    if not annotation.startswith(prefix) or not annotation.endswith('")'):
        return None
    return annotation[len(prefix):-2]


def _js_str(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _apply_shadow_chain(cands: list[LocatorCandidate], chain: list[ShadowHostRef]) -> None:
    if not chain:
        return
    for c in cands:
        final_css = _to_css(c) or c.value
        c.selenium = selenium_shadow_chain(chain, final_css)
        c.playwright = playwright_shadow_chain(chain, final_css)
        c.shadow_chain = list(chain)


async def resolve_for_node(
    cdp: CdpEngine,
    node: TreeNode,
    cfg: LocatorsCfg,
    validate: bool = True,
) -> list[LocatorCandidate]:
    cands = _build_candidates(node)
    _rank_by_config(cands, cfg)
    _apply_shadow_chain(cands, node.shadow_ancestors)
    if validate and not node.shadow_ancestors:
        # Live uniqueness check via querySelectorAll only works against light DOM.
        for c in cands:
            await validate_uniqueness(cdp, c)
    return cands
