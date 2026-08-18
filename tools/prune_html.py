#!/usr/bin/env python3
# PHASE: standalone utility (no locatorforge imports)
"""Prune an HTML page down to its user-interactive skeleton.

Standalone port of the filtering rules in `locatorforge/ax_tree_processor.py`,
applied to static HTML instead of a live CDP accessibility tree:

  * drop decoration outright (script/style/svg/icons/hidden nodes/comments)
  * drop generic wrappers UNLESS labelled, hoisting their children to the
    parent (the `_collapsed` placeholder trick from the AX processor)
  * keep landmarks (form/table/nav/dialog/...) only when something survived
    inside them
  * collapse an interactive element's inner markup into its accessible name
  * whitelist only the attributes a locator can actually be built from
    (data-testid > aria-label > id > name > ... — same priority as
    `locator_resolver._build_candidates`)

Stdlib only — no bs4/lxml/selenium needed.

Usage
-----
    python prune_html.py page.html                     # pruned HTML to stdout
    python prune_html.py page.html -o clean.html --stats
    python prune_html.py https://example.com --json tree.json
    cat page.html | python prune_html.py - --include-verification

Note: URLs are fetched with urllib, so you get the *server* HTML with no
JavaScript executed. For SPA pages, save the rendered DOM first
(`document.documentElement.outerHTML`) and feed that file in.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
from typing import Any, Optional

# --------------------------------------------------------------------------
# Tag / role vocabularies
# --------------------------------------------------------------------------

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}

# Whole subtree is decoration or non-rendered — never contains automation targets.
DROP_SUBTREE_TAGS = {
    "script", "style", "noscript", "template", "head", "meta", "link", "base",
    "title", "svg", "canvas", "picture", "source", "track", "map", "area",
    "figcaption", "br", "hr", "wbr", "col", "colgroup", "param",
    # svg internals, in case a fragment is parsed without its <svg> root
    "path", "defs", "use", "symbol", "g", "circle", "rect", "polygon",
    "polyline", "ellipse", "line", "marker", "clippath", "mask", "filter",
    "lineargradient", "radialgradient", "stop", "tspan", "textpath",
}

# Always-interactive tags (input handled separately: type=hidden is dropped).
INTERACTIVE_TAGS = {
    "button", "select", "textarea", "option", "label", "summary",
}

# Mirrors ax_tree_processor.INTERACTIVE_ROLES, plus the composite-widget roles
# that show up in real component libraries.
INTERACTIVE_ROLES = {
    "button", "link", "textbox", "combobox", "checkbox", "radio",
    "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "slider",
    "switch", "searchbox", "spinbutton", "option", "treeitem",
}

# Kept only when something interactive survived inside them — they exist to
# give a locator its scope (form > table > row > cell), not as targets.
LANDMARK_TAGS = {
    "form", "dialog", "fieldset", "table", "thead", "tbody", "tfoot", "tr",
    "td", "th", "nav", "main", "header", "footer", "aside", "optgroup",
    "details", "menu",
}

# Boundaries: kept even when empty, because the real content lives elsewhere
# (another document / a shadow root / a plugin surface).
BOUNDARY_TAGS = {"iframe", "frame", "object", "embed"}

# Non-interactive nodes worth keeping for assertions (--include-verification).
VERIFICATION_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6", "legend", "caption", "output",
    "progress", "meter", "img", "th",
}
VERIFICATION_ROLES = {
    "heading", "alert", "alertdialog", "status", "tooltip", "log",
    "progressbar", "img",
}

# Attributes a locator can be built from, plus the state flags a test asserts on.
KEEP_ATTRS = {
    "id", "name", "type", "value", "placeholder", "href", "alt", "title",
    "role", "for", "action", "method", "target", "download", "src",
    "checked", "selected", "disabled", "readonly", "required", "multiple",
    "open", "contenteditable", "tabindex", "colspan", "rowspan",
    "min", "max", "step", "maxlength", "pattern", "accept", "list",
}
# Framework bookkeeping that masquerades as a data-* test hook.
NOISY_DATA_ATTRS_RE = re.compile(
    r"^data-(reactid|react-|v-|ng-|svelte|astro|emotion|styled|turbo|hydrat)",
    re.I,
)
CLASS_NOISE_RE = re.compile(r"^(css-[0-9a-z]{4,}|sc-[0-9a-zA-Z]{5,}|[a-z]+_[a-zA-Z0-9]{5,}__[0-9a-zA-Z]{4,})$")

# Tags that implicitly close an open sibling when a new one starts.
AUTO_CLOSE = {
    "li": {"li"},
    "p": {"p"},
    "option": {"option"},
    "optgroup": {"option", "optgroup"},
    "tr": {"tr", "td", "th"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "dt": {"dt", "dd"},
    "dd": {"dt", "dd"},
    "thead": {"thead", "tbody", "tfoot", "tr", "td", "th"},
    "tbody": {"thead", "tbody", "tfoot", "tr", "td", "th"},
    "tfoot": {"thead", "tbody", "tfoot", "tr", "td", "th"},
}

# Inline formatting left open across an implicit close (`<li><a>x<li>…`) must be
# popped too, or the unclosed <a> swallows the rest of the document.
INLINE_FORMATTING = {
    "a", "b", "i", "em", "strong", "span", "small", "u", "s", "code",
    "font", "big", "abbr", "mark", "sub", "sup",
}

# Any of these starting closes an open <p>.
BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "div", "dl", "fieldset",
    "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "hr", "main", "nav", "ol", "p", "pre", "section", "table", "ul",
}

TEXT = "#text"
COMMENT = "#comment"


# --------------------------------------------------------------------------
# Minimal DOM
# --------------------------------------------------------------------------

@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)
    text: str = ""                     # #text / #comment payload, or the
                                       # accessible name of an interactive node
    element_type: str = "structural"   # interactive | verification | structural

    @property
    def role(self) -> str:
        return (self.attrs.get("role") or "").strip().lower()


class _DomBuilder(HTMLParser):
    """html.parser -> Node tree, with just enough implicit-close handling to
    survive real-world markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack: list[Node] = [self.root]
        self.elements = 0

    # -- helpers ----------------------------------------------------------
    def _current(self) -> Node:
        return self.stack[-1]

    def _auto_close(self, tag: str) -> None:
        if tag in BLOCK_TAGS:
            while len(self.stack) > 1 and self.stack[-1].tag == "p":
                self.stack.pop()
        closes = AUTO_CLOSE.get(tag)
        if not closes:
            return
        # Look past any still-open inline formatting for the sibling to close.
        i = len(self.stack) - 1
        while i > 0 and self.stack[i].tag in INLINE_FORMATTING:
            i -= 1
        if i > 0 and self.stack[i].tag in closes:
            del self.stack[i:]

    # -- HTMLParser hooks -------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        self._auto_close(tag)
        node = Node(tag, {k.lower(): (v if v is not None else "") for k, v in attrs})
        self._current().children.append(node)
        self.elements += 1
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        self._auto_close(tag)
        node = Node(tag, {k.lower(): (v if v is not None else "") for k, v in attrs})
        self._current().children.append(node)
        self.elements += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in VOID_TAGS:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return
        # Stray close tag with no matching open — ignore it.

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._current().children.append(Node(TEXT, text=data))


def parse_html(source: str) -> tuple[Node, int]:
    b = _DomBuilder()
    b.feed(source)
    b.close()
    return b.root, b.elements


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def _tabindex(node: Node) -> Optional[int]:
    raw = node.attrs.get("tabindex")
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def is_hidden(node: Node) -> bool:
    a = node.attrs
    if "hidden" in a or "inert" in a:
        return True
    if a.get("aria-hidden", "").strip().lower() == "true":
        return True
    if node.tag == "input" and a.get("type", "").strip().lower() == "hidden":
        return True
    style = a.get("style", "").lower().replace(" ", "")
    if "display:none" in style or "visibility:hidden" in style:
        return True
    return False


def is_interactive(node: Node) -> bool:
    tag, a = node.tag, node.attrs
    if tag in INTERACTIVE_TAGS:
        return True
    if tag == "input":
        return a.get("type", "").strip().lower() != "hidden"
    if tag == "a":
        return bool(a.get("href")) or "onclick" in a or _tabindex(node) is not None
    if tag in {"audio", "video"}:
        return "controls" in a
    if node.role in INTERACTIVE_ROLES:
        return True
    # contenteditable="" is "true" per the HTML spec.
    if "contenteditable" in a and a["contenteditable"].strip().lower() != "false":
        return True
    if "onclick" in a or "onchange" in a or "oninput" in a:
        return True
    ti = _tabindex(node)
    if ti is not None and ti >= 0:
        return True
    return False


def is_verification(node: Node) -> bool:
    if node.tag in VERIFICATION_TAGS:
        return True
    return node.role in VERIFICATION_ROLES


def has_label(node: Node) -> bool:
    """Mirrors ax_tree_processor._has_label: a generic wrapper survives only if
    something gives it an identity worth locating by."""
    a = node.attrs
    if any(k in a for k in ("aria-label", "aria-labelledby", "title")):
        return True
    role = node.role
    if role and role not in ("presentation", "none", "generic"):
        return True
    return any(_is_test_attr(k) for k in a)


def _is_test_attr(name: str) -> bool:
    if not name.startswith("data-"):
        return False
    if NOISY_DATA_ATTRS_RE.match(name):
        return False
    return bool(re.search(r"(test|qa|automation|cy|e2e|selenium|track|id)", name, re.I))


# --------------------------------------------------------------------------
# Accessible name
# --------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def _subtree_text(node: Node, budget: int = 400) -> str:
    out: list[str] = []

    def walk(n: Node) -> None:
        if len("".join(out)) > budget:
            return
        if n.tag == TEXT:
            out.append(n.text)
            return
        if n.tag in DROP_SUBTREE_TAGS or is_hidden(n):
            return
        if n.tag == "img" and n.attrs.get("alt"):
            out.append(" " + n.attrs["alt"] + " ")
            return
        for c in n.children:
            walk(c)

    walk(node)
    return _WS_RE.sub(" ", "".join(out)).strip()


def accessible_name(node: Node, max_text: int) -> str:
    a = node.attrs
    candidates = [
        a.get("aria-label"),
        _subtree_text(node) if node.tag not in ("input", "img") else "",
        a.get("value") if node.tag in ("input", "button") else "",
        a.get("placeholder"),
        a.get("alt"),
        a.get("title"),
        a.get("aria-labelledby"),
    ]
    for c in candidates:
        if c and c.strip():
            name = _WS_RE.sub(" ", c).strip()
            return name if len(name) <= max_text else name[: max_text - 1].rstrip() + "…"
    return ""


# --------------------------------------------------------------------------
# Attribute filtering
# --------------------------------------------------------------------------

def filter_attrs(node: Node, opts: argparse.Namespace) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in node.attrs.items():
        if k.startswith("on"):
            continue
        if k == "style":
            continue
        if k == "class":
            if not opts.keep_class:
                continue
            kept = [c for c in v.split() if not CLASS_NOISE_RE.match(c)]
            if not kept:
                continue
            v = " ".join(kept[:6])
        elif k.startswith("data-"):
            if not _is_test_attr(k):
                continue
        elif k.startswith("aria-"):
            pass
        elif k not in KEEP_ATTRS:
            continue
        if k == "src" and node.tag not in BOUNDARY_TAGS:
            continue
        v = _WS_RE.sub(" ", v).strip()
        if len(v) > opts.max_attr:
            v = v[: opts.max_attr - 1] + "…"
        out[k] = v
    return out


# Ordering that matches locator_resolver's strategy priority, so the most
# useful attribute is the first thing you read on every line.
_ATTR_ORDER = ["data-testid", "data-test-id", "data-test", "data-qa", "aria-label",
               "id", "name", "type", "role", "placeholder", "value", "href", "for", "alt", "title"]


def _sorted_attrs(attrs: dict[str, str]) -> list[tuple[str, str]]:
    def key(item: tuple[str, str]) -> tuple[int, str]:
        k = item[0]
        if k in _ATTR_ORDER:
            return (_ATTR_ORDER.index(k), k)
        if _is_test_attr(k):
            return (0, k)
        return (len(_ATTR_ORDER) + 1, k)
    return sorted(attrs.items(), key=key)


# --------------------------------------------------------------------------
# Pruning — returns a list so a dropped wrapper can hoist its children
# --------------------------------------------------------------------------

def prune(node: Node, opts: argparse.Namespace) -> list[Node]:
    tag = node.tag

    if tag == TEXT or tag == COMMENT:
        return []                       # text is only kept via accessible_name
    if tag in DROP_SUBTREE_TAGS:
        return []
    if tag != "#document" and is_hidden(node):
        return []

    kids: list[Node] = []
    for c in node.children:
        kids.extend(prune(c, opts))
    if opts.max_similar:
        kids = collapse_similar(kids, opts.max_similar)

    if tag == "#document" or tag in ("html", "body"):
        out = Node("#document" if tag == "#document" else tag)
        out.children = kids
        return [out] if tag == "#document" else kids

    interactive = is_interactive(node)

    if interactive:
        kept = Node(tag, filter_attrs(node, opts), element_type="interactive")
        kept.text = accessible_name(node, opts.max_text)
        # Keep only nested controls (a <select>'s options, a <label>'s input);
        # everything else inside a control is icon/span decoration.
        kept.children = [k for k in kids if k.element_type == "interactive" or k.tag in BOUNDARY_TAGS]
        if kept.children and tag in ("select", "optgroup", "details", "table"):
            # The children ARE the content — don't repeat them as a flattened name.
            kept.text = node.attrs.get("aria-label", "").strip()
        return [kept]

    if tag in BOUNDARY_TAGS:
        kept = Node(tag, filter_attrs(node, opts), element_type="structural")
        kept.children = kids
        return [kept]

    if opts.include_verification and is_verification(node):
        kept = Node(tag, filter_attrs(node, opts), element_type="verification")
        kept.text = accessible_name(node, opts.max_text)
        kept.children = kids
        if kept.text or kept.children or kept.attrs:
            return [kept]
        return kids

    if tag in LANDMARK_TAGS:
        if not kids:
            return []                   # empty landmark carries no target
        if len(kids) == 1 and tag in ("tbody", "thead", "tfoot", "menu", "optgroup"):
            return kids                 # pure grouping wrapper, no locator value
        kept = Node(tag, filter_attrs(node, opts), element_type="structural")
        kept.children = kids
        return [kept]

    # Generic element: keep it only if it is labelled AND still wraps something,
    # otherwise hoist its children (ax_tree_processor's `_collapsed` behaviour).
    if kids and has_label(node) and not opts.flat:
        kept = Node(tag, filter_attrs(node, opts), element_type="structural")
        kept.children = kids
        return [kept]
    return kids


def _signature(node: Node) -> str:
    """Structure-only fingerprint: tags + attribute names, values ignored."""
    parts = [node.tag, node.element_type, ",".join(sorted(node.attrs))]
    parts.extend(_signature(c) for c in node.children)
    return "|".join(parts)


def collapse_similar(nodes: list[Node], limit: int) -> list[Node]:
    """Trim runs of structurally identical siblings (table rows, result cards)
    down to `limit` samples — the 101st identical row teaches an automation
    author nothing."""
    out: list[Node] = []
    run_sig: Optional[str] = None
    run_kept = 0
    dropped = 0

    def flush() -> None:
        nonlocal dropped
        if dropped:
            out.append(Node(COMMENT, text=f"+{dropped} similar sibling(s) omitted"))
            dropped = 0

    for n in nodes:
        sig = _signature(n)
        if sig == run_sig:
            run_kept += 1
            if run_kept > limit:
                dropped += 1
                continue
        else:
            flush()
            run_sig, run_kept = sig, 1
        out.append(n)
    flush()
    return out


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def render_html(nodes: list[Node], indent: int = 0) -> str:
    lines: list[str] = []
    pad = "  " * indent
    for n in nodes:
        if n.tag == COMMENT:
            lines.append(f"{pad}<!-- {n.text} -->")
            continue
        attr_str = "".join(
            f' {k}="{escape(v, quote=True)}"' if v else f" {k}"
            for k, v in _sorted_attrs(n.attrs)
        )
        if n.tag in VOID_TAGS:
            lines.append(f"{pad}<{n.tag}{attr_str}>")
            continue
        body = escape(n.text) if n.text else ""
        if not n.children:
            lines.append(f"{pad}<{n.tag}{attr_str}>{body}</{n.tag}>")
        else:
            lines.append(f"{pad}<{n.tag}{attr_str}>{body}")
            lines.append(render_html(n.children, indent + 1))
            lines.append(f"{pad}</{n.tag}>")
    return "\n".join(l for l in lines if l)


def to_dict(node: Node) -> dict[str, Any]:
    d: dict[str, Any] = {"tag": node.tag, "element_type": node.element_type}
    if node.role:
        d["role"] = node.role
    if node.text:
        d["name" if node.tag != COMMENT else "note"] = node.text
    if node.attrs:
        d["attributes"] = dict(_sorted_attrs(node.attrs))
    if node.children:
        d["children"] = [to_dict(c) for c in node.children]
    return d


def count(nodes: list[Node]) -> int:
    return sum(1 + count(n.children) for n in nodes if n.tag != COMMENT)


def count_interactive(nodes: list[Node]) -> int:
    return sum(
        (1 if n.element_type == "interactive" else 0) + count_interactive(n.children)
        for n in nodes
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def read_source(target: str) -> str:
    if target == "-":
        return sys.stdin.read()
    if target.startswith(("http://", "https://")):
        from urllib.request import Request, urlopen
        req = Request(target, headers={"User-Agent": "Mozilla/5.0 (prune_html.py)"})
        with urlopen(req, timeout=30) as resp:          # noqa: S310 - user-supplied URL
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")
    with open(target, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="prune_html.py",
        description="Strip an HTML page down to its user-interactive elements.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage")[-1],
    )
    p.add_argument("input", help="HTML file path, http(s) URL, or '-' for stdin")
    p.add_argument("-o", "--output", help="write pruned HTML here (default: stdout)")
    p.add_argument("--json", dest="json_out", help="also write the pruned tree as JSON")
    p.add_argument("--include-verification", action="store_true",
                   help="also keep assertion anchors (headings, images, alerts, output)")
    p.add_argument("--keep-class", action="store_true",
                   help="keep class attributes (framework-hash classes still dropped)")
    p.add_argument("--flat", action="store_true",
                   help="drop labelled generic wrappers too (flattest possible output)")
    p.add_argument("--max-similar", type=int, default=0, metavar="N",
                   help="keep at most N structurally identical siblings (0 = keep all)")
    p.add_argument("--max-text", type=int, default=80, metavar="N",
                   help="truncate accessible names to N chars (default: 80)")
    p.add_argument("--max-attr", type=int, default=120, metavar="N",
                   help="truncate attribute values to N chars (default: 120)")
    p.add_argument("--stats", action="store_true", help="print reduction stats to stderr")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    # Windows consoles default to cp1252; page text rarely is.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    opts = build_parser().parse_args(argv)

    source = read_source(opts.input)
    root, raw_elements = parse_html(source)
    pruned = prune(root, opts)
    kids = pruned[0].children if pruned else []

    html_out = render_html(kids)
    if opts.output:
        with open(opts.output, "w", encoding="utf-8") as fh:
            fh.write(html_out + "\n")
    if opts.json_out:
        tree = {"source": opts.input, "nodes": [to_dict(k) for k in kids]}
        with open(opts.json_out, "w", encoding="utf-8") as fh:
            json.dump(tree, fh, indent=2, ensure_ascii=False)
    if not opts.output:
        sys.stdout.write(html_out + "\n")

    if opts.stats:
        kept = count(kids)
        pct = (1 - kept / raw_elements) * 100 if raw_elements else 0.0
        print(
            f"[prune] elements {raw_elements} -> {kept} ({pct:.1f}% removed) | "
            f"interactive {count_interactive(kids)} | "
            f"bytes {len(source)} -> {len(html_out)} "
            f"({(1 - len(html_out) / len(source)) * 100 if source else 0:.1f}% smaller)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
