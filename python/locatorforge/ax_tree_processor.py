# PHASE: 1.1.2
"""Accessibility-tree processor.

Primary tree extraction calls `Accessibility.getSnapshot({interestingOnly: true})`
and shapes the raw payload into a clean `TreeNode` structure with the SPEC's
filtering rules applied (drop none/presentation/generic unless labelled, drop
redundant StaticText children of labelled interactives, keep landmark roles).
"""

from __future__ import annotations

import itertools
import logging
from typing import Any, Optional

from .cdp_engine import CdpEngine, CdpError
from .schemas import TreeNode

log = logging.getLogger(__name__)

INTERACTIVE_ROLES = {
    "button", "link", "textbox", "combobox", "checkbox", "radio",
    "menuitem", "tab", "slider", "switch", "searchbox", "spinbutton",
}
STRUCTURAL_ROLES = {
    "banner", "navigation", "main", "contentinfo", "complementary",
    "form", "region",
}
DROPPABLE_ROLES = {"none", "presentation", "generic", "InlineTextBox"}

_id_counter = itertools.count(1)


def _read_value(raw: Any) -> Optional[str]:
    """CDP AX strings come as `{"type": "...", "value": "..."}`; unwrap to str."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        v = raw.get("value")
        return None if v is None else str(v)
    return str(raw)


def _properties_to_attrs(properties: list[dict]) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for p in properties or []:
        name = p.get("name")
        val = _read_value(p.get("value"))
        if name and val is not None:
            attrs[name] = val
    return attrs


def _attrs_from_ax_sources(raw: dict) -> dict[str, str]:
    """Extract HTML attributes that Chrome surfaces via `name.sources` /
    `description.sources` on each AX node.

    Chrome's Accessibility.getChildAXNodes does NOT return arbitrary DOM
    attributes with each AX node — the `properties` array carries ARIA state
    (focused, expanded, haspopup, level, …), not attributes like `aria-label`
    or `alt`. But when an attribute *contributes* to the accessible name or
    description, Chrome lists it as an AXValueSource with `type: "attribute"`.
    That's how we pull `aria-label`, `aria-labelledby`, `alt`, `title` and
    `placeholder` back onto the node so the locator resolver and the JTree
    label can use them.
    """
    out: dict[str, str] = {}
    for kind in ("name", "description"):
        info = raw.get(kind) or {}
        for src in info.get("sources") or []:
            if src.get("type") != "attribute":
                continue
            if src.get("superseded") or src.get("invalid"):
                continue
            attr_name = src.get("attribute")
            attr_val = _read_value(src.get("attributeValue"))
            if attr_name and attr_val is not None:
                out.setdefault(attr_name, attr_val)
    return out


def _has_label(name: Optional[str], attrs: dict[str, str]) -> bool:
    if name and name.strip():
        return True
    return any(k in attrs for k in ("aria-label", "aria-labelledby", "label"))


def _build_node(raw: dict, role: str, name: Optional[str], attrs: dict[str, str]) -> TreeNode:
    if role.lower() in INTERACTIVE_ROLES:
        et = "interactive"
    elif role in STRUCTURAL_ROLES:
        et = "structural"
    else:
        et = "verification"
    return TreeNode(
        node_id=f"n{next(_id_counter)}",
        backend_node_id=raw.get("backendDOMNodeId"),
        role=role,
        name=name,
        attributes=attrs,
        element_type=et,
    )


def _strip_collapsed(node: TreeNode) -> None:
    """Recursively unwrap any `_collapsed` placeholders that survived the build pass.

    Hoists their children up to the parent so the rendered tree contains only
    meaningful AX roles.
    """
    flattened: list[TreeNode] = []
    for child in node.children:
        if child.role == "_collapsed":
            _strip_collapsed(child)         # drill into the placeholder first
            flattened.extend(child.children)
        else:
            flattened.append(child)
    node.children = flattened
    for child in node.children:
        _strip_collapsed(child)


def _filter_redundant_statictexts(node: TreeNode) -> None:
    if node.role.lower() in INTERACTIVE_ROLES and _has_label(node.name, node.attributes):
        node.children = [
            c for c in node.children
            if c.role != "StaticText" or (c.name and c.name != node.name)
        ]
    for child in node.children:
        _filter_redundant_statictexts(child)


def normalize_snapshot(snapshot: dict) -> Optional[TreeNode]:
    """Transform a `Accessibility.getSnapshot` payload into a filtered TreeNode tree.

    The snapshot payload uses two shapes across Chrome versions; we handle both:
      * `nodes`: flat list with `parentId`/`childIds`/`nodeId`/`role`/`name`/`properties`
      * `documents` form: not produced by getSnapshot, ignored here
    """
    nodes = snapshot.get("nodes") or []
    log.info("[normalize] input nodes=%d", len(nodes))
    if not nodes:
        log.warning("[normalize] empty snapshot — returning None")
        return None

    by_id: dict[str, dict] = {n["nodeId"]: n for n in nodes if "nodeId" in n}
    root_raw = nodes[0]
    log.info(
        "[normalize] root role=%s name=%r childIds=%d",
        (root_raw.get("role") or {}).get("value"),
        (root_raw.get("name") or {}).get("value"),
        len(root_raw.get("childIds") or []),
    )

    def build(raw: dict) -> Optional[TreeNode]:
        role = _read_value(raw.get("role")) or "unknown"
        name = _read_value(raw.get("name"))
        attrs = _properties_to_attrs(raw.get("properties", []))
        # Merge in HTML attributes surfaced via name.sources / description.sources
        # (aria-label, alt, title, aria-labelledby, placeholder). Existing AX
        # property keys win — they never collide with HTML attribute names.
        for k, v in _attrs_from_ax_sources(raw).items():
            attrs.setdefault(k, v)

        # Filter: drop none/presentation/generic UNLESS aria-labelled.
        if role in DROPPABLE_ROLES and not _has_label(name, attrs):
            # Hoist children of dropped node up to caller via the flattened list.
            kids: list[TreeNode] = []
            for cid in raw.get("childIds", []) or []:
                child_raw = by_id.get(cid)
                if not child_raw:
                    continue
                built = build(child_raw)
                if built is None:
                    continue
                # Flatten nested collapses inline so a chain of droppable
                # generic→generic→… parents doesn't leave stacked `_collapsed`
                # placeholders for the caller to deal with.
                if built.role == "_collapsed":
                    kids.extend(built.children)
                else:
                    kids.append(built)
            if not kids:
                return None
            placeholder = TreeNode(
                node_id=f"n{next(_id_counter)}",
                role="_collapsed",
                children=kids,
            )
            return placeholder

        node = _build_node(raw, role, name, attrs)
        kids: list[TreeNode] = []
        for cid in raw.get("childIds", []) or []:
            child_raw = by_id.get(cid)
            if not child_raw:
                continue
            built = build(child_raw)
            if built is None:
                continue
            if built.role == "_collapsed":
                kids.extend(built.children)
            else:
                kids.append(built)
        node.children = kids
        return node

    root = build(root_raw)
    if root is None:
        log.warning("[normalize] root build returned None — whole tree filtered out")
        return None
    if root.role == "_collapsed":
        if not root.children:
            log.warning("[normalize] root collapsed with no surviving children")
            return None
        if len(root.children) == 1:
            root = root.children[0]
        else:
            wrapper = TreeNode(node_id=f"n{next(_id_counter)}", role="document")
            wrapper.children = root.children
            root = wrapper
    _strip_collapsed(root)
    _filter_redundant_statictexts(root)

    def _count(n: TreeNode) -> int:
        return 1 + sum(_count(c) for c in n.children)
    log.info("[normalize] output root role=%s name=%r total_nodes=%d direct_children=%d",
             root.role, root.name, _count(root), len(root.children))
    return root


MAX_NODES = 2000     # raw AX nodes (many will be ignored). Final filtered
                     # tree typically lands ~10× smaller per SPEC §11.
                     # PHASE 5: this is a PER-FRAME budget — frames must not
                     # starve each other (the reference AUT's dashboard frame
                     # alone exceeded 400 raw nodes).
MAX_DEPTH = 25


async def _lazy_walk(cdp: CdpEngine, frame_id: Optional[str] = None) -> Optional[dict]:
    """BFS-walk the AX tree via getRootAXNode + getChildAXNodes.

    `frame_id` scopes the walk to one frame's document (PHASE 5). AXNodeIds are
    frame-scoped, so every call in the walk must carry the same frameId. When
    None, Chrome uses the root frame — preserving pre-Phase-5 behavior exactly.
    """
    import time
    t0 = time.monotonic()
    tag = f"frame {frame_id[:8]}" if frame_id else "main frame"
    scope = {"frameId": frame_id} if frame_id else {}
    log.info("[lazy_walk] getRootAXNode (%s)", tag)
    try:
        root_resp = await cdp.send("Accessibility.getRootAXNode", dict(scope))
    except CdpError as e:
        log.warning("[lazy_walk] getRootAXNode failed for %s: %s", tag, e)
        return None
    root = root_resp.get("node") if isinstance(root_resp, dict) else None
    if not root:
        log.warning("[lazy_walk] getRootAXNode returned no node for %s", tag)
        return None

    root_role = (root.get("role") or {}).get("value")
    root_name = (root.get("name") or {}).get("value")
    log.info(
        "[lazy_walk] root nodeId=%s role=%s name=%r childIds=%d ignored=%s",
        root.get("nodeId"), root_role, root_name,
        len(root.get("childIds") or []), root.get("ignored"),
    )

    seen: set[str] = {root["nodeId"]}
    order: list[dict] = [root]   # root must be index 0 for normalize_snapshot
    queue: list[tuple[dict, int]] = [(root, 0)]
    round_trips = 0
    fail_count = 0

    while queue and len(order) < MAX_NODES:
        cur, depth = queue.pop(0)
        if depth >= MAX_DEPTH:
            continue
        if not (cur.get("childIds") or []):
            continue
        try:
            child_params = {"id": cur["nodeId"]}
            child_params.update(scope)   # frameId, when frame-scoped
            resp = await cdp.send("Accessibility.getChildAXNodes", child_params)
            round_trips += 1
            if round_trips % 25 == 0:
                log.info("[lazy_walk] %d round-trips, %d nodes so far", round_trips, len(order))
        except CdpError as e:
            fail_count += 1
            log.debug("getChildAXNodes failed for %s: %s", cur.get("nodeId"), e)
            continue
        for child in resp.get("nodes") or []:
            cid = child.get("nodeId")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            # Emit every node — including ignored placeholders. They are needed
            # in `by_id` so `normalize_snapshot.build()` can follow childIds
            # links through them; otherwise an ignored intermediate orphans
            # the entire real subtree. The role-based filter in
            # `normalize_snapshot` will collapse them out of the final tree.
            order.append(child)
            queue.append((child, depth + 1))
            if len(order) >= MAX_NODES:
                log.info("AX tree truncated at MAX_NODES=%d", MAX_NODES)
                break

    elapsed = time.monotonic() - t0
    log.info(
        "[lazy_walk] %s done in %.2fs — %d nodes, %d round-trips, %d failed",
        tag, elapsed, len(order), round_trips, fail_count,
    )
    return {"nodes": order}


def _tag_frame_context(node: TreeNode, frame_id: str, chain: list) -> None:
    """Stamp frame identity on an entire subtree so downstream consumers
    (locator resolution, uniqueness validation, highlight) can scope correctly."""
    node.frame_id = frame_id
    node.frame_chain = list(chain)
    for child in node.children:
        _tag_frame_context(child, frame_id, chain)


def _count_meaningful(node: TreeNode) -> int:
    """Nodes that would actually be useful to a tester (named or interactive)."""
    total = 1 if (node.element_type == "interactive" or (node.name or "").strip()) else 0
    return total + sum(_count_meaningful(c) for c in node.children)


async def fetch_tree(cdp: CdpEngine, include_frames: bool = True) -> Optional[TreeNode]:
    """Fetch the live page's accessibility tree, including iframe content.

    Strategy: lazy BFS via getRootAXNode + getChildAXNodes (avoids
    `getFullAXTree`'s multi-megabyte response and the removed `getSnapshot`).

    PHASE 5: after walking the main frame, every same-process child frame is
    walked with its own `frameId` and spliced in as a collapsible boundary node.
    Frames that yield nothing meaningful are dropped so instrumentation iframes
    (fingerprinting, tag managers) don't litter the tree.
    """
    snapshot = await _lazy_walk(cdp)
    if not snapshot:
        return None
    root = normalize_snapshot(snapshot)
    if root is None or not include_frames:
        return root

    try:
        from .frame_traverser import enumerate_frames
        frames = await enumerate_frames(cdp)
    except Exception as e:  # noqa: BLE001
        log.warning("[frames] enumeration failed, returning main frame only: %s", e)
        return root

    if not frames:
        return root

    attached = 0
    for info in frames:
        try:
            sub_snapshot = await _lazy_walk(cdp, frame_id=info.frame_id)
        except Exception as e:  # noqa: BLE001
            log.debug("[frames] walk failed for %s: %s", info.selector, e)
            continue
        if not sub_snapshot:
            continue
        sub_root = normalize_snapshot(sub_snapshot)
        if sub_root is None:
            continue

        meaningful = _count_meaningful(sub_root)
        if meaningful == 0:
            log.info("[frames] skipping %s — no meaningful nodes", info.selector)
            continue

        _tag_frame_context(sub_root, info.frame_id, info.chain)

        boundary = TreeNode(
            node_id=f"n{next(_id_counter)}",
            role="iframe",
            name=info.selector or info.url[:60],
            element_type="structural",
            is_frame_boundary=True,
            frame_id=info.frame_id,
            frame_chain=list(info.chain),
            attributes={"src": info.url[:200], "selector": info.selector or ""},
            children=[sub_root],
        )
        root.children.append(boundary)
        attached += 1
        log.info(
            "[frames] attached %s — %d meaningful node(s)",
            info.selector, meaningful,
        )

    log.info("[frames] %d frame subtree(s) spliced into the tree", attached)
    return root
