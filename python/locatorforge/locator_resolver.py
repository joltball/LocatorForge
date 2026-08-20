# PHASE: 2.1.2  (shadow-piercing extension in PHASE 3.4.2)
"""Compute every candidate locator strategy for a TreeNode, rank them by
configurable priority, validate uniqueness against the live DOM, and emit
**both** Selenium (`@FindBy(...)`) and Playwright (`page.locator(...)` /
`getByRole` / `getByTestId`) strings for every node — regardless of which the UI
currently displays (per ADR-04).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .cdp_engine import CdpEngine, CdpError
from .config import LocatorsCfg
from .schemas import ShadowHostRef, TreeNode
from .shadow_dom_traverser import playwright_shadow_chain, selenium_shadow_chain


@dataclass
class LocatorCandidate:
    strategy: str              # data-testid | aria-label | id | name | css | xpath |
                               # role | descendant-attr | descendant-text
    value: str
    selenium: str
    playwright: str
    rank: int                  # lower = better
    match_count: Optional[int] = None  # populated after validate()
    is_unique: Optional[bool] = None
    shadow_chain: list[ShadowHostRef] = field(default_factory=list)
    frame_chain: list = field(default_factory=list)   # list[FrameRef], PHASE 5
    # PHASE 6: the expression actually counted during validation. Set at build
    # time from the SAME string that goes into `selenium`, so a candidate can
    # never report a match count for something other than what it displays.
    # (Before this, `role` displayed an XPath but validated `[role=...]` CSS,
    # reporting "13 matches" next to a locator that resolved to nothing.)
    validation_expr: Optional[str] = None
    validation_kind: str = "css"       # "css" | "xpath"
    # PHASE 7: index-disambiguated variant of a non-unique locator. Unique, but
    # order-dependent — it breaks if the list is re-sorted, filtered, or paged,
    # so it must never outrank a naturally unique locator.
    is_positional: bool = False
    position_index: Optional[int] = None   # 0-based


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


# PHASE 6 — descendant identifiers.
# Component frameworks (Angular Material, PrimeNG, Vuetify, Ant) put the click
# target's ROLE on a wrapper element while the human-meaningful identity lives on
# a DESCENDANT (card title, label, heading). Looking only at the node's own
# attributes therefore finds nothing usable on exactly the elements testers care
# about most. These are the descendant attributes worth anchoring to, best first.
_DESCENDANT_ID_ATTRS = ("data-testid", "data-test", "aria-label", "id", "title")

# How deep to look for an identifying descendant. 3 covers
# mat-card > mat-card-header > div > mat-card-title without dragging in
# unrelated subtree content.
_DESCENDANT_MAX_DEPTH = 4

# Never anchor to these — build-hash / framework-generated attributes change on
# every rebuild, so a locator built on them is worse than no locator.
_UNSTABLE_ATTR_PREFIXES = ("_ngcontent", "_nghost", "ng-reflect", "data-v-")


def _is_stable_attr(name: str, value: str) -> bool:
    if not name or not value or not value.strip():
        return False
    if any(name.startswith(p) for p in _UNSTABLE_ATTR_PREFIXES):
        return False
    # Angular/React auto-ids like "mat-select-3" or "cdk-overlay-12" are
    # regenerated per render — reject trailing-digit ids on known prefixes.
    if name == "id" and re.match(r"^(mat|cdk|ng|react|ember)-[\w-]*\d+$", value):
        return False
    return True


def _dom_children(dom_node: dict) -> list[dict]:
    return dom_node.get("children") or []


def _attrs_of(dom_node: dict) -> dict[str, str]:
    flat = dom_node.get("attributes") or []
    return {flat[i]: flat[i + 1] for i in range(0, len(flat) - 1, 2)}


def _is_identity_tag(tag: str) -> bool:
    """Does this element name suggest it carries the component's IDENTITY?

    A card's `<mat-card-title>` names the thing; a sibling status `<span
    aria-label="Some accounts are not up to date">` describes transient state
    shared by every card. Both may sit at the same depth with the same
    attribute, so tag semantics is what separates them.
    """
    if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "legend", "label", "caption"):
        return True
    return any(k in tag for k in ("title", "heading", "header", "label", "name"))


def _is_state_tag(tag: str, attrs: dict[str, str]) -> bool:
    """Elements that describe transient state rather than identity."""
    cls = (attrs.get("class") or "").lower()
    return any(k in cls for k in ("icon", "tooltip", "badge", "status", "spinner"))


def _find_descendant_identifier(
    dom_node: dict,
) -> Optional[tuple[str, str, str, int]]:
    """Best identifying descendant as (tag, attr_name, attr_value, depth).

    Breadth-first so shallower (structurally more stable) identifiers win, with
    a bonus for title-like tags and a penalty for state/icon elements — those
    are typically shared across sibling components and produce non-unique
    locators.
    """
    queue: list[tuple[dict, int]] = [(c, 1) for c in _dom_children(dom_node)]
    best: Optional[tuple[str, str, str, int]] = None
    best_score = 10_000

    while queue:
        cur, depth = queue.pop(0)
        if depth > _DESCENDANT_MAX_DEPTH:
            continue
        tag = (cur.get("localName") or "").lower()
        attrs = _attrs_of(cur)
        for pref, attr_name in enumerate(_DESCENDANT_ID_ATTRS):
            val = attrs.get(attr_name)
            if val and _is_stable_attr(attr_name, val):
                score = depth * 10 + pref
                if _is_identity_tag(tag):
                    score -= 6          # outranks a same-depth generic sibling
                if _is_state_tag(tag, attrs):
                    score += 20         # last resort only
                if score < best_score:
                    best_score = score
                    best = (tag or "*", attr_name, val, depth)
                break
        for c in _dom_children(cur):
            queue.append((c, depth + 1))
    return best


def _find_descendant_text(dom_node: dict) -> Optional[str]:
    """A short, stable text fragment from a title-ish descendant.

    Deliberately NOT the whole subtree: on a data card the full text includes
    live balances ("AUD 123,456.78"), which would produce a locator that breaks
    on the next data refresh. We take the first meaningful text-bearing element
    and cap the length.
    """
    queue: list[tuple[dict, int]] = [(c, 1) for c in _dom_children(dom_node)]
    while queue:
        cur, depth = queue.pop(0)
        if depth > _DESCENDANT_MAX_DEPTH:
            continue
        tag = (cur.get("localName") or "").lower()
        # Title-ish elements carry identity; skip generic containers.
        if tag and ("title" in tag or "header" in tag or tag in ("h1","h2","h3","h4","legend","label")):
            text = _text_of(cur)
            if text and 3 <= len(text) <= 80 and not _looks_volatile(text):
                return text
        for c in _dom_children(cur):
            queue.append((c, depth + 1))
    return None


def _text_of(dom_node: dict) -> str:
    """Concatenate direct text-node children (nodeType 3), whitespace-normalized."""
    parts: list[str] = []

    def walk(n: dict, d: int) -> None:
        if d > 3:
            return
        if n.get("nodeType") == 3:
            parts.append(n.get("nodeValue") or "")
        for c in _dom_children(n):
            walk(c, d + 1)

    walk(dom_node, 0)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _looks_volatile(text: str) -> bool:
    """Reject text dominated by numbers/currency — balances, counts, dates."""
    if re.search(r"\d[\d,]*\.\d{2}", text):        # 123,456.78
        return True
    digits = sum(c.isdigit() for c in text)
    return digits > len(text) * 0.3


def _build_candidates(node: TreeNode, dom_node: Optional[dict] = None) -> list[LocatorCandidate]:
    attrs = node.attributes
    role = (node.role or "").lower()
    name = node.name or ""
    tag = _tag_for(node)
    tag_prefix = tag or "*"          # `//*[…]` when tag unknown
    out: list[LocatorCandidate] = []

    if "data-testid" in attrs:
        v = attrs["data-testid"]
        css = f"[data-testid='{_quote_css_attr(v)}']"
        out.append(LocatorCandidate(
            strategy="data-testid", value=v, rank=0,
            selenium=f"@FindBy(css = \"{css}\")",
            playwright=f"page.getByTestId('{v}')",
            validation_expr=css, validation_kind="css",
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
            validation_expr=css_sel, validation_kind="css",
        ))
    if "id" in attrs:
        v = attrs["id"]
        out.append(LocatorCandidate(
            strategy="id", value=v, rank=20,
            selenium=f"@FindBy(id = \"{v}\")",
            playwright=f"page.locator('#{v}')",
            validation_expr=f"#{v}", validation_kind="css",
        ))
    if "name" in attrs:
        v = attrs["name"]
        css = f"[name='{_quote_css_attr(v)}']"
        out.append(LocatorCandidate(
            strategy="name", value=v, rank=30,
            selenium=f"@FindBy(name = \"{v}\")",
            playwright=f"page.locator(\"{css}\")",
            validation_expr=css, validation_kind="css",
        ))

    # --- PHASE 6: descendant-anchored strategies -------------------------
    # These run BEFORE the generic role/css/xpath fallbacks because on
    # component-framework markup they are usually the only usable option.
    desc = _find_descendant_identifier(dom_node) if dom_node else None
    if desc:
        d_tag, d_attr, d_val, _depth = desc
        xp = (f"//{tag_prefix}[.//{d_tag}[@{d_attr}='{_xpath_text(d_val)}']]")
        css_has = f"{tag_prefix}:has({d_tag}[{d_attr}='{_quote_css_attr(d_val)}'])"
        out.append(LocatorCandidate(
            strategy="descendant-attr", value=f"{d_tag}[{d_attr}={d_val}]", rank=15,
            selenium=f"@FindBy(xpath = \"{xp}\")",
            playwright=f"page.locator(\"{css_has}\")",
            validation_expr=xp, validation_kind="xpath",
        ))

    desc_text = _find_descendant_text(dom_node) if dom_node else None
    if desc_text:
        xp_t = f"//{tag_prefix}[contains(., '{_xpath_text(desc_text)}')]"
        out.append(LocatorCandidate(
            strategy="descendant-text", value=desc_text, rank=25,
            selenium=f"@FindBy(xpath = \"{xp_t}\")",
            playwright=(f"page.locator('{tag_prefix}')"
                        f".filter({{ hasText: '{desc_text}' }})"),
            validation_expr=xp_t, validation_kind="xpath",
        ))

    # Role + accessible name — Playwright is primary; Selenium uses a tag-scoped
    # text XPath as the closest equivalent. Both display AND validate as XPath.
    if role and name:
        xp_role = f"//{tag_prefix}[normalize-space()='{_xpath_text(name)}']"
        out.append(LocatorCandidate(
            strategy="role", value=f"{role}|{name}", rank=40,
            selenium=f"@FindBy(xpath = \"{xp_role}\")",
            playwright=f"page.getByRole('{role}', {{ name: '{name}' }})",
            validation_expr=xp_role, validation_kind="xpath",
        ))
    # CSS fallback by real HTML tag. Skip when we couldn't derive one — an AX
    # role name like "link" isn't a valid CSS tag.
    if tag:
        out.append(LocatorCandidate(
            strategy="css", value=tag, rank=50,
            selenium=f"@FindBy(css = \"{tag}\")",
            playwright=f"page.locator('{tag}')",
            validation_expr=tag, validation_kind="css",
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
            validation_expr=xp_value, validation_kind="xpath",
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

    def pos(c: LocatorCandidate) -> int:
        # Derived `-nth` variants inherit their base strategy's configured
        # priority rather than falling to the bottom as unknown names.
        base = c.strategy[:-4] if c.strategy.endswith("-nth") else c.strategy
        return order.get(base, 999)

    cands.sort(key=lambda c: (pos(c), c.rank))
    for i, c in enumerate(cands):
        c.rank = i
    return cands


async def _frame_context_id(cdp: CdpEngine, frame_id: str) -> Optional[int]:
    """Resolve a frame's JS execution context, so validation runs in the right
    document. Uses an isolated world — stable and side-effect free."""
    try:
        res = await cdp.send("Page.createIsolatedWorld", {
            "frameId": frame_id,
            "worldName": "locatorforge_validate",
            "grantUniveralAccess": False,
        })
        return res.get("executionContextId")
    except CdpError as e:
        log_debug = getattr(__import__("logging").getLogger(__name__), "debug")
        log_debug("createIsolatedWorld failed for frame %s: %s", frame_id, e)
        return None


async def validate_uniqueness(
    cdp: CdpEngine,
    cand: LocatorCandidate,
    frame_id: Optional[str] = None,
) -> None:
    """Count live matches for a candidate; sets match_count + is_unique.

    PHASE 5: when `frame_id` is given, the check runs inside that frame's
    document. Without it, behavior is exactly as before (main frame), so
    existing main-frame nodes are unaffected.
    """
    # PHASE 6: always count the expression the candidate actually displays.
    # Falls back to the legacy derivation only for hand-built candidates
    # (e.g. the /validate endpoint) that carry no validation_expr.
    expr = cand.validation_expr
    kind = cand.validation_kind
    if expr is None:
        expr = cand.value if cand.strategy == "xpath" else _to_css(cand)
        kind = "xpath" if cand.strategy == "xpath" else "css"
    if expr is None:
        return

    try:
        selector = expr if kind == "css" else None

        # Branch on the expression KIND, never on the strategy name. Strategy
        # names don't imply a syntax (`role` displays XPath, `descendant-attr`
        # displays XPath), and branching on them is what let display and
        # validation drift apart in the first place.
        if kind == "xpath":
            js = ("document.evaluate("
                  f"{_js_str(expr)}, document, null, "
                  "XPathResult.UNORDERED_NODE_SNAPSHOT_TYPE, null).snapshotLength")
        else:
            js = f"document.querySelectorAll({_js_str(expr)}).length"

        params: dict = {"expression": js, "returnByValue": True}
        if frame_id:
            # Frame-scoped: evaluate inside the frame's own document.
            # DOM.getDocument/querySelectorAll are main-frame-only.
            ctx = await _frame_context_id(cdp, frame_id)
            if ctx is None:
                cand.match_count = None
                cand.is_unique = None
                return
            params["contextId"] = ctx

        res = await cdp.send("Runtime.evaluate", params)
        # A malformed selector throws rather than returning a number.
        if res.get("exceptionDetails"):
            cand.match_count = None
            cand.is_unique = None
            return
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


# PHASE 7 — positional disambiguation.
# Some elements are genuinely indistinguishable: the reference AUT renders four
# account cards all titled "Globex Trust", identical in tag, classes and
# attributes, differing only by (volatile) balances. No attribute locator can
# separate them, so we fall back to index — but only as a clearly-marked last
# resort, because index breaks the moment the list re-sorts or pages.

_INDEXABLE_KINDS = ("xpath",)
_BARE_TAG_RE = re.compile(r"^[a-z][a-z0-9-]*$")
# `tag[attr='value']`, `[attr='value']` — the shapes our own CSS candidates use.
_TAG_ATTR_RE = re.compile(
    r"^([a-z][a-z0-9-]*)?\[([a-zA-Z_:][\w:.-]*)=['\"]([^'\"]*)['\"]\]$"
)


def _as_xpath(expr: str, kind: str) -> Optional[str]:
    """Return an XPath equivalent of `expr`, or None if we can't safely convert.

    Deliberately narrow: only the selector shapes this module itself emits are
    converted. General CSS→XPath is not worth the failure modes.

    Converting `tag[attr='v']` matters for locator QUALITY, not just coverage —
    without it a duplicated element falls back to indexing a bare tag,
    producing `(//button)[3]` where `(//button[@aria-label='Delete'])[1]` was
    available. Both are unique; only one survives an unrelated button being
    added to the page.
    """
    if kind == "xpath":
        return expr
    if kind != "css":
        return None
    if _BARE_TAG_RE.match(expr):
        return f"//{expr}"
    m = _TAG_ATTR_RE.match(expr)
    if m:
        tag, attr, value = m.group(1) or "*", m.group(2), m.group(3)
        if "'" in value:            # would break the XPath literal
            return None
        return f"//{tag}[@{attr}='{value}']"
    return None


async def _element_index_for(
    cdp: CdpEngine,
    backend_node_id: int,
    xpath: str,
    frame_id: Optional[str] = None,
) -> Optional[tuple[int, int]]:
    """Return (index, total) of this element among `xpath` matches, else None.

    Resolves the node to a JS handle and asks the page which position it holds,
    rather than guessing from document order — the AX tree and the DOM can
    disagree about ordering.
    """
    ctx = await _frame_context_id(cdp, frame_id) if frame_id else None
    try:
        params: dict = {"backendNodeId": backend_node_id}
        if ctx is not None:
            params["executionContextId"] = ctx
        resolved = await cdp.send("DOM.resolveNode", params)
        object_id = (resolved.get("object") or {}).get("objectId")
        if not object_id:
            return None
        fn = """
        function(xp) {
          const r = document.evaluate(xp, document, null,
                    XPathResult.UNORDERED_NODE_SNAPSHOT_TYPE, null);
          for (let i = 0; i < r.snapshotLength; i++) {
            if (r.snapshotItem(i) === this) return {i: i, n: r.snapshotLength};
          }
          return {i: -1, n: r.snapshotLength};
        }
        """
        res = await cdp.send("Runtime.callFunctionOn", {
            "objectId": object_id,
            "functionDeclaration": fn,
            "arguments": [{"value": xpath}],
            "returnByValue": True,
        })
        if res.get("exceptionDetails"):
            return None
        val = (res.get("result") or {}).get("value") or {}
        idx, total = val.get("i"), val.get("n")
        if not isinstance(idx, int) or not isinstance(total, int) or idx < 0:
            return None
        return idx, total
    except CdpError:
        return None


async def _add_positional_variants(
    cdp: CdpEngine,
    node: TreeNode,
    cands: list[LocatorCandidate],
    max_variants: int = 2,
) -> None:
    """For non-unique candidates, append an index-pinned variant that IS unique."""
    if not node.backend_node_id:
        return
    # Only bother if nothing already resolves uniquely.
    if any(c.is_unique for c in cands):
        return

    added = 0
    for cand in list(cands):
        if added >= max_variants:
            break
        if cand.is_unique or not cand.match_count or cand.match_count < 2:
            continue
        base = _as_xpath(cand.validation_expr or "", cand.validation_kind)
        if not base:
            continue
        found = await _element_index_for(cdp, node.backend_node_id, base, node.frame_id)
        if not found:
            continue
        idx, total = found
        # Wrap in parentheses: `(//x)[2]` selects the 2nd match overall, while
        # `//x[2]` selects every x that is the 2nd child of its parent.
        xp = f"({base})[{idx + 1}]"
        pw = f"{cand.playwright}.nth({idx})"
        cands.append(LocatorCandidate(
            strategy=f"{cand.strategy}-nth",
            value=f"{cand.value} #{idx + 1} of {total}",
            rank=cand.rank,
            selenium=f"@FindBy(xpath = \"{xp}\")",
            playwright=pw,
            validation_expr=xp,
            validation_kind="xpath",
            match_count=1,
            is_unique=True,
            is_positional=True,
            position_index=idx,
            shadow_chain=list(cand.shadow_chain),
        ))
        added += 1


def _demote_broken(cands: list[LocatorCandidate]) -> None:
    """Re-rank by measured reality, not just configured preference.

    Config priority decides between locators that *work*; it must never promote
    one that resolves to nothing. Role/tag guessing is heuristic (an AX
    `combobox` is often a custom div, not a `<select>`), so a high-priority
    strategy can easily be dead on arrival. Order: unique → matches>1 →
    unknown → zero. Ties keep the configured order.
    """
    def bucket(c: LocatorCandidate) -> int:
        if c.is_unique and not c.is_positional:
            return 0
        if c.is_unique and c.is_positional:
            return 1      # unique, but order-dependent — prefer anything stable
        if c.match_count is not None and c.match_count > 1:
            return 2
        if c.match_count is None:
            return 3
        return 4          # match_count == 0 — cannot resolve

    cands.sort(key=lambda c: (bucket(c), c.rank))
    for i, c in enumerate(cands):
        c.rank = i


def _apply_frame_chain(cands: list[LocatorCandidate], chain: list) -> None:
    """Prefix locators with iframe traversal (PHASE 5).

    Selenium gets a switchTo() preamble; Playwright gets a frameLocator() chain.
    Shadow-piercing output is left alone — that helper already emits a full
    expression, and stacking both rewrites would produce nonsense.
    """
    if not chain:
        return
    from .frame_traverser import playwright_frame_prefix, selenium_frame_preamble

    preamble = selenium_frame_preamble(chain)
    pw_prefix = playwright_frame_prefix(chain)
    for c in cands:
        c.frame_chain = list(chain)
        # Selenium: @FindBy cannot express frame switching, so surface the
        # required preamble as a comment above the annotation. The agent (and
        # the human reading the tree) then knows a switchTo() is mandatory.
        c.selenium = (
            "// requires frame switch:\n"
            + "\n".join(f"// {line}" for line in preamble.splitlines())
            + f"\n{c.selenium}"
        )
        if c.playwright.startswith("page."):
            c.playwright = pw_prefix + c.playwright[len("page"):]


async def _enrich_from_dom(cdp: CdpEngine, node: TreeNode) -> Optional[dict]:
    """Replace guessed tag/attributes with the real ones from the DOM.

    Returns the DOM subtree (depth-limited) so PHASE 6 can search descendants
    for a stable identifier, or None when the node has no backendNodeId.

    `ROLE_TO_TAG` is a heuristic and it lies on component frameworks — an AX
    `combobox` in an Angular app is usually a styled `<div>`, not a `<select>`,
    which yields locators that look plausible and match nothing. We have the
    node's backendNodeId, so ask Chrome instead of inferring. Cheap: one call,
    and only on the node the user actually selected.
    """
    if not node.backend_node_id:
        return None
    try:
        # depth covers the node plus enough descendants for PHASE 6's
        # descendant-identifier search (mat-card > header > div > title).
        described = await cdp.send(
            "DOM.describeNode",
            {"backendNodeId": node.backend_node_id, "depth": _DESCENDANT_MAX_DEPTH + 1},
        )
    except CdpError:
        return None
    dom_node = described.get("node") or {}
    local = (dom_node.get("localName") or "").lower()
    if local:
        node.attributes["tag"] = local
    flat = dom_node.get("attributes") or []
    for i in range(0, len(flat) - 1, 2):
        key, val = flat[i], flat[i + 1]
        # Real DOM attributes are authoritative over AX-derived ones, but skip
        # framework build-hash noise so it can't pollute candidate generation.
        if key and val and not any(key.startswith(p) for p in _UNSTABLE_ATTR_PREFIXES):
            node.attributes[key] = val
    return dom_node


async def resolve_for_node(
    cdp: CdpEngine,
    node: TreeNode,
    cfg: LocatorsCfg,
    validate: bool = True,
) -> list[LocatorCandidate]:
    dom_node = await _enrich_from_dom(cdp, node)
    cands = _build_candidates(node, dom_node)
    _rank_by_config(cands, cfg)
    _apply_shadow_chain(cands, node.shadow_ancestors)
    if validate and not node.shadow_ancestors:
        # Live uniqueness check via querySelectorAll only works against light DOM.
        # Frame-scoped nodes validate inside their own document (PHASE 5).
        for c in cands:
            await validate_uniqueness(cdp, c, frame_id=node.frame_id)
        # PHASE 7: only when nothing resolved uniquely on its own merits.
        await _add_positional_variants(cdp, node, cands)
        _demote_broken(cands)
    # Frame prefixing happens AFTER validation — validation needs the bare
    # selector, the frame chain is a presentation concern.
    _apply_frame_chain(cands, node.frame_chain)
    return cands
