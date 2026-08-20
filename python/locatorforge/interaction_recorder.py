# PHASE: 5.1
"""Passive user-interaction recorder.

Unlike `/element/pick` (which uses `Overlay.setInspectMode` and *consumes* the
click), this observes the user driving the app normally and captures only the
elements they actually touched.

Mechanism
---------
`Runtime.addBinding` exposes `window.__locatorforge(json)` to page JS, and
`Page.addScriptToEvaluateOnNewDocument` installs a capture-phase listener set
that survives navigation. The listener never calls `preventDefault` — the app
behaves exactly as it would unobserved.

The listener cannot hand a DOM node across the binding (payloads are strings),
so it parks the element in `window.__lfNodes[seq]` and sends the index. Python
resolves it back with `Runtime.evaluate` → `DOM.requestNode` → `DOM.describeNode`,
which yields a `backendNodeId` — the same currency `_pick_partial()` and
`/highlight` already speak. From there the existing `resolve_for_node()` ranking
applies unchanged.

Every capture is stamped with a **page key** derived from the URL, so locators
recorded across a multi-page flow stay separable (one POM class per page).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

from .cdp_engine import CdpEngine, CdpError
from .config import Config
from .locator_resolver import resolve_for_node
from .schemas import TreeNode

log = logging.getLogger(__name__)

BINDING_NAME = "__locatorforge"
RECORDING_FILE = "recording.json"

# Path segments that are record identifiers rather than page identity —
# /orders/48213/edit and /orders/9/edit are the same page.
_VOLATILE_SEG = re.compile(
    r"^(\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{16,})$",
    re.I,
)


def page_key_from_url(url: str, title: str = "") -> str:
    """`https://shop.com/checkout/payment?step=2` -> `CheckoutPayment`.

    Query and fragment are ignored: they are almost always state, not identity.
    Falls back to the host, then the document title, then `Home`.
    """
    try:
        parsed = urlparse(url or "")
    except ValueError:
        parsed = None
    segments: list[str] = []
    if parsed and parsed.path:
        for seg in parsed.path.split("/"):
            seg = seg.strip()
            if not seg or _VOLATILE_SEG.match(seg):
                continue
            seg = re.sub(r"\.(html?|jsp|aspx?|php)$", "", seg, flags=re.I)
            segments.append(seg)
    if not segments and parsed and parsed.hostname:
        segments = [parsed.hostname.split(".")[0]]
    if not segments and title:
        segments = title.split()[:3]
    if not segments:
        return "Home"
    words: list[str] = []
    for seg in segments[-3:]:                       # deep paths stay readable
        words.extend(w for w in re.split(r"[^A-Za-z0-9]+", seg) if w)
    if not words:
        return "Home"
    # Deeply nested paths (and file:// URLs especially) would otherwise produce
    # an unusable class name.
    words = words[-4:]
    return "".join(w[:1].upper() + w[1:] for w in words)[:48]


# --------------------------------------------------------------------------
# Injected capture script
# --------------------------------------------------------------------------

_CAPTURE_JS = """
(() => {
  if (window.__lfRecorder) { window.__lfRecorder.on = true; return; }
  const R = { on: true, nodes: [], timer: null, last: '', lastAt: 0 };
  window.__lfRecorder = R;
  window.__lfNodes = R.nodes;

  const TAGS = new Set(['A','BUTTON','INPUT','SELECT','TEXTAREA','LABEL','SUMMARY','OPTION']);
  const ROLES = new Set(['button','link','textbox','combobox','checkbox','radio','menuitem',
                         'menuitemcheckbox','menuitemradio','tab','slider','switch','searchbox',
                         'spinbutton','option','treeitem']);
  // Never ship these values off the page, even to a localhost recorder.
  const SECRET = /pass|pwd|secret|cvv|cvc|ssn|card|otp|token|pin|auth|security/i;

  function interactive(el) {
    if (!el || el.nodeType !== 1) return false;
    if (TAGS.has(el.tagName)) return true;
    const r = ((el.getAttribute && el.getAttribute('role')) || '').toLowerCase();
    if (ROLES.has(r)) return true;
    if (el.hasAttribute && el.hasAttribute('onclick')) return true;
    const ti = el.getAttribute && el.getAttribute('tabindex');
    if (ti !== null && ti !== undefined && parseInt(ti, 10) >= 0) return true;
    return !!el.isContentEditable;
  }

  // composedPath() pierces OPEN shadow roots, so we get the real inner control
  // rather than the custom-element host. Closed roots stay invisible.
  function pick(e) {
    const path = (e.composedPath && e.composedPath()) || [e.target];
    for (const el of path) { if (interactive(el)) return el; }
    return (path[0] && path[0].nodeType === 1) ? path[0] : e.target;
  }

  function secretish(el) {
    if (el.tagName === 'INPUT' && String(el.type || '').toLowerCase() === 'password') return true;
    const probe = [el.name, el.id, el.getAttribute && el.getAttribute('autocomplete'),
                   el.getAttribute && el.getAttribute('aria-label')].join(' ');
    return SECRET.test(probe);
  }

  function readValue(el) {
    if (!el || !('value' in el)) return null;
    if (secretish(el)) return '***REDACTED***';
    const v = String(el.value == null ? '' : el.value);
    return v.length > 120 ? v.slice(0, 119) + '\\u2026' : v;
  }

  // Snapshot attributes here, at event time. The element may be destroyed by a
  // navigation before the Python side resolves it (clicking Submit is the
  // normal case), and a locator built from these attributes is still correct —
  // only the live uniqueness check is lost.
  function snapAttrs(el) {
    const out = {};
    if (!el.attributes) return out;
    const redact = secretish(el);
    for (const a of el.attributes) {
      if (a.name === 'style' || a.name.startsWith('on')) continue;
      let v = a.value == null ? '' : String(a.value);
      if (v.length > 200) continue;
      if (redact && a.name === 'value') v = '***REDACTED***';
      out[a.name] = v;
    }
    return out;
  }

  // An <iframe>/<frame> is never a test target. Focusing or clicking into one
  // makes the PARENT document see the frame element as the target, which would
  // record every interaction inside the app as one meaningless entry. The real
  // element is captured by that frame's own listener (we inject into all
  // frames), so drop it here.
  const FRAME_TAGS = new Set(['IFRAME', 'FRAME', 'FRAMESET', 'OBJECT', 'EMBED']);

  function emit(el, type, value) {
    if (!R.on || !el || el.nodeType !== 1) return;
    if (FRAME_TAGS.has(el.tagName)) return;
    try {
      const seq = R.nodes.push(el) - 1;
      window.__locatorforge(JSON.stringify({
        seq: seq,
        type: type,
        value: value === undefined ? null : value,
        tag: (el.tagName || '').toLowerCase(),
        attrs: snapAttrs(el),
        text: ((el.innerText || el.textContent || '').trim()).slice(0, 80),
        url: location.href,
        title: document.title,
        shadow: !!(el.getRootNode && el.getRootNode() !== document),
        ts: Date.now()
      }));
    } catch (err) { /* binding torn down mid-flight */ }
  }

  const onClick = (e) => {
    const el = pick(e);
    const t = String(el.type || '').toLowerCase();
    emit(el, 'click', (t === 'checkbox' || t === 'radio') ? String(!!el.checked) : null);
  };
  const onChange = (e) => {
    const el = pick(e);
    emit(el, el.tagName === 'SELECT' ? 'select' : 'change', readValue(el));
  };
  const onInput = (e) => {                       // debounced: one event per pause
    const el = pick(e);
    clearTimeout(R.timer);
    R.timer = setTimeout(() => emit(el, 'type', readValue(el)), 500);
  };
  const onSubmit = (e) => emit(pick(e), 'submit', null);
  const onKey = (e) => { if (e.key === 'Enter') emit(pick(e), 'press:Enter', null); };

  const opts = { capture: true, passive: true };
  document.addEventListener('click', onClick, opts);
  document.addEventListener('change', onChange, opts);
  document.addEventListener('input', onInput, opts);
  document.addEventListener('submit', onSubmit, opts);
  document.addEventListener('keydown', onKey, opts);

  R.detach = () => {
    R.on = false;
    clearTimeout(R.timer);
    document.removeEventListener('click', onClick, true);
    document.removeEventListener('change', onChange, true);
    document.removeEventListener('input', onInput, true);
    document.removeEventListener('submit', onSubmit, true);
    document.removeEventListener('keydown', onKey, true);
    R.nodes.length = 0;
  };
})();
"""

# Leave nothing of ours on the user's page. `Runtime.removeBinding` stops event
# delivery but cannot un-inject the binding function from a live context — that
# one goes away on the next navigation, inert in the meantime.
_TEARDOWN_JS = """
try {
  if (window.__lfRecorder && window.__lfRecorder.detach) window.__lfRecorder.detach();
  delete window.__lfRecorder;
  delete window.__lfNodes;
} catch (e) {}
"""


# --------------------------------------------------------------------------
# Recorder
# --------------------------------------------------------------------------

class RecordedElement:
    """One distinct element the user touched, with its ranked locators."""

    __slots__ = ("key", "page", "page_url", "page_title", "actions", "role", "name",
                 "tag", "element_type", "in_shadow", "locators", "best", "hits",
                 "first_seen", "last_seen", "last_value", "backend_node_id")

    def __init__(self, **kw: Any) -> None:
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot))

    def to_json(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "page_url": self.page_url,
            "page_title": self.page_title,
            "actions": sorted(self.actions or []),
            "role": self.role,
            "name": self.name,
            "tag": self.tag,
            "element_type": self.element_type,
            "in_shadow": self.in_shadow,
            "hits": self.hits,
            "last_value": self.last_value,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "best": self.best,
            "locators": self.locators,
        }


class InteractionRecorder:
    """Owns the injected script's lifecycle and the captured element store."""

    def __init__(
        self,
        cdp_getter: Callable[[], Awaitable[CdpEngine]],
        config: Config,
        out_dir: Path,
        broadcast: Callable[[str, dict], Awaitable[None]],
    ) -> None:
        self._cdp_getter = cdp_getter
        self._config = config
        self._out_dir = Path(out_dir)
        self._broadcast = broadcast
        self.recording = False
        self.started_at: Optional[str] = None
        self._script_id: Optional[str] = None
        self._elements: dict[str, RecordedElement] = {}
        self._wired = False
        self._ctx_hooked = False   # PHASE 8: frame-context listener registered?
        self._frame_chains: dict[str, list] = {}   # frameId -> [FrameRef]
        self._resolve_lock = asyncio.Lock()

    # -- lifecycle ------------------------------------------------------
    async def _install_all_frames(self, cdp: CdpEngine) -> int:
        """Evaluate the capture script in every frame's default context."""
        contexts = dict(getattr(cdp, "frame_contexts", {}) or {})
        installed = 0
        if not contexts:
            # Engine predates context tracking, or Runtime.enable hasn't replayed
            # yet — fall back to the main frame so recording still works.
            try:
                await cdp.send("Runtime.evaluate", {"expression": _CAPTURE_JS})
                return 1
            except CdpError as e:
                log.warning("recorder injection failed: %s", e)
                return 0
        for frame_id, ctx_id in contexts.items():
            try:
                await cdp.send("Runtime.evaluate",
                               {"expression": _CAPTURE_JS, "contextId": ctx_id})
                installed += 1
            except CdpError as e:
                # about:blank / chrome-error frames routinely refuse; harmless.
                log.debug("recorder injection skipped for frame %s: %s", frame_id[:8], e)
        return installed

    async def _on_new_frame_context(self, frame_id: str, ctx_id: int) -> None:
        """A frame loaded a new document — install the listener into it."""
        if not self.recording:
            return
        try:
            cdp = await self._cdp_getter()
            await cdp.send("Runtime.evaluate",
                           {"expression": _CAPTURE_JS, "contextId": ctx_id})
            log.debug("[recorder] re-injected into frame %s", frame_id[:8])
        except Exception:  # noqa: BLE001
            log.debug("[recorder] re-injection failed for frame %s", frame_id[:8])

    async def start(self, clear: bool = False) -> dict[str, Any]:
        cdp = await self._cdp_getter()
        if clear:
            self._elements.clear()
        if not self._wired:
            cdp.on_event("Runtime.bindingCalled", self._on_binding)
            self._wired = True
        try:
            await cdp.send("Runtime.addBinding", {"name": BINDING_NAME})
        except CdpError as e:
            log.warning("addBinding failed (may already exist): %s", e)
        try:
            res = await cdp.send(
                "Page.addScriptToEvaluateOnNewDocument", {"source": _CAPTURE_JS}
            )
            self._script_id = res.get("identifier")
        except CdpError as e:
            log.warning("addScriptToEvaluateOnNewDocument failed: %s", e)
        # addScriptToEvaluateOnNewDocument only affects *future* documents —
        # install into every frame that is already open as well.
        #
        # PHASE 8: this MUST cover child frames. Clicks inside an iframe do not
        # bubble to the parent document, so a main-frame-only listener records
        # nothing from the app's real content — on the reference AUT the entire
        # dashboard lives in `iframe[name='Main']`, and 15 of 16 frames were
        # being left without a listener.
        installed = await self._install_all_frames(cdp)
        log.info("[recorder] capture script installed in %d frame(s)", installed)

        # Re-inject whenever a frame gets a new document mid-recording (SPA
        # frame swaps are constant on this app).
        if not self._ctx_hooked:
            cdp.on_frame_context(self._on_new_frame_context)
            self._ctx_hooked = True

        self.recording = True
        self.started_at = datetime.now(timezone.utc).isoformat()
        log.info("[recorder] started (script_id=%s)", self._script_id)
        await self._broadcast("recording_state", {"recording": True, "count": len(self._elements)})
        return self.snapshot()

    async def stop(self) -> dict[str, Any]:
        self.recording = False
        try:
            cdp = await self._cdp_getter()
        except Exception:  # noqa: BLE001 - browser may already be gone
            cdp = None
        if cdp is not None:
            if self._script_id:
                try:
                    await cdp.send("Page.removeScriptToEvaluateOnNewDocument",
                                   {"identifier": self._script_id})
                except CdpError:
                    pass
                self._script_id = None
            # Tear down in every frame we injected into, not just the main one.
            contexts = dict(getattr(cdp, "frame_contexts", {}) or {})
            if contexts:
                for ctx_id in contexts.values():
                    try:
                        await cdp.send("Runtime.evaluate",
                                       {"expression": _TEARDOWN_JS, "contextId": ctx_id})
                    except CdpError:
                        pass
            else:
                try:
                    await cdp.send("Runtime.evaluate", {"expression": _TEARDOWN_JS})
                except CdpError:
                    pass
            try:
                await cdp.send("Runtime.removeBinding", {"name": BINDING_NAME})
            except CdpError:
                pass
        path = self.write_file()
        log.info("[recorder] stopped — %d element(s) -> %s", len(self._elements), path)
        await self._broadcast("recording_state", {"recording": False, "count": len(self._elements)})
        return {**self.snapshot(), "written": str(path)}

    # -- capture --------------------------------------------------------
    async def _on_binding(self, params: dict) -> None:
        if not self.recording or params.get("name") != BINDING_NAME:
            return
        try:
            data = json.loads(params.get("payload") or "{}")
        except json.JSONDecodeError:
            return
        try:
            # PHASE A — resolve the parked node NOW, unserialized. The element
            # lives only until the next navigation, and a click on Submit
            # destroys it within milliseconds. Every queued capture must grab
            # its identity before that happens; the expensive ranking work
            # waits its turn in phase B.
            cdp = await self._cdp_getter()
            seq = data.get("seq")
            if seq is None:
                return
            ctx_id = params.get("executionContextId")
            identity = await self._resolve_node(cdp, seq, ctx_id)
            # PHASE 8: which frame did this click happen in? Needed both to
            # validate uniqueness in the right document and to emit a locator
            # carrying the required switchTo().frame(...) / frameLocator(...).
            frame_id = cdp.frame_for_context(ctx_id) if hasattr(cdp, "frame_for_context") else None
        except Exception:  # noqa: BLE001
            log.exception("[recorder] node resolution failed")
            return

        # PHASE B — AX lookup + locator ranking + uniqueness validation. These
        # interleave badly (validate_uniqueness re-roots via DOM.getDocument),
        # so they run one capture at a time.
        async with self._resolve_lock:
            try:
                await self._capture(data, identity, frame_id)
            except Exception:  # noqa: BLE001
                log.exception("[recorder] capture failed")

    async def _frame_chain_for(self, cdp: CdpEngine, frame_id: Optional[str]) -> list:
        """Ordered FrameRef chain for `frame_id`, cached across captures."""
        if not frame_id:
            return []
        if frame_id in self._frame_chains:
            return self._frame_chains[frame_id]
        try:
            from .frame_traverser import enumerate_frames
            for info in await enumerate_frames(cdp, include_noise=True):
                self._frame_chains[info.frame_id] = info.chain
        except Exception:  # noqa: BLE001
            log.debug("[recorder] frame chain lookup failed", exc_info=True)
        return self._frame_chains.get(frame_id, [])

    async def _capture(self, data: dict, identity: tuple,
                       frame_id: Optional[str] = None) -> None:
        cdp = await self._cdp_getter()
        backend_id, dom_attrs, dom_tag = identity

        # Attributes snapshotted in-page win nothing over the live DOM, but they
        # are all we have once the element's document is gone.
        attrs: dict[str, str] = {**(data.get("attrs") or {}), **(dom_attrs or {})}
        tag = dom_tag or data.get("tag") or None
        alive = backend_id is not None

        role = ax_name = None
        if alive:
            role, ax_name = await self._ax_identity(cdp, backend_id)
        node = TreeNode(
            node_id=f"rec_{backend_id}" if alive else f"rec_{abs(hash(str(attrs))) % 10**9}",
            backend_node_id=backend_id,
            role=role or attrs.get("role") or tag or "unknown",
            name=ax_name or data.get("text") or None,
            attributes={**attrs, "tag": tag} if tag else attrs,
            element_type="interactive",
            frame_id=frame_id,
            frame_chain=await self._frame_chain_for(cdp, frame_id),
        )
        # Uniqueness can only be checked against a live document.
        cands = await resolve_for_node(cdp, node, self._config.locators, validate=alive)
        locators = [
            {
                "strategy": c.strategy, "value": c.value, "selenium": c.selenium,
                "playwright": c.playwright, "rank": c.rank,
                "match_count": c.match_count, "is_unique": c.is_unique,
            }
            for c in cands
        ]
        best = next((l for l in locators if l["is_unique"]), locators[0] if locators else None)

        page = page_key_from_url(data.get("url", ""), data.get("title", ""))
        if best:
            key = f"{page}::{best['strategy']}={best['value']}"
        else:
            key = f"{page}::{tag}:{(data.get('text') or '')[:40]}"
        now = datetime.now(timezone.utc).isoformat()
        action = data.get("type") or "click"
        value = data.get("value")

        existing = self._elements.get(key)
        if existing is not None:
            existing.hits = (existing.hits or 0) + 1
            existing.last_seen = now
            existing.actions = sorted(set(existing.actions or []) | {action})
            if value is not None:
                existing.last_value = value
            if alive:
                existing.backend_node_id = backend_id
                # A later, validated pass supersedes one captured post-navigation.
                if best and best.get("is_unique") is not None:
                    existing.locators, existing.best = locators, best
            rec = existing
        else:
            rec = RecordedElement(
                key=key, page=page, page_url=data.get("url"), page_title=data.get("title"),
                actions=[action], role=node.role, name=node.name, tag=tag,
                element_type="interactive", in_shadow=bool(data.get("shadow")),
                locators=locators, best=best, hits=1, first_seen=now, last_seen=now,
                last_value=value, backend_node_id=backend_id,
            )
            self._elements[key] = rec

        await self._broadcast("element_recorded", {
            "new": existing is None,
            "count": len(self._elements),
            "element": rec.to_json(),
        })

    async def _resolve_node(
        self, cdp: CdpEngine, seq: int, context_id: Optional[int]
    ) -> tuple[Optional[int], dict[str, str], Optional[str]]:
        """`window.__lfNodes[seq]` -> (backendNodeId, attributes, tag)."""
        params: dict[str, Any] = {
            "expression": f"window.__lfNodes && window.__lfNodes[{int(seq)}]",
        }
        if context_id is not None:
            params["contextId"] = context_id      # resolve inside the right frame
        try:
            res = await cdp.send("Runtime.evaluate", params)
        except CdpError:
            return None, {}, None
        obj = (res or {}).get("result") or {}
        object_id = obj.get("objectId")
        if not object_id:
            log.warning("[recorder] seq %s did not resolve to a live object", seq)
            return None, {}, None
        try:
            # describeNode takes the objectId directly — going via DOM.requestNode
            # would additionally require DOM.getDocument to have primed the node
            # map, which is not guaranteed when Record is hit before any refresh.
            descr = await cdp.send("DOM.describeNode", {"objectId": object_id})
        except CdpError as e:
            log.warning("[recorder] describeNode failed for seq %s: %s", seq, e)
            return None, {}, None
        finally:
            try:
                await cdp.send("Runtime.releaseObject", {"objectId": object_id})
            except CdpError:
                pass

        dom_node = (descr or {}).get("node") or {}
        # CDP returns attributes as a flat [name, value, name, value, ...] list.
        flat = dom_node.get("attributes") or []
        attrs = {flat[i]: flat[i + 1] for i in range(0, len(flat) - 1, 2)}
        tag = (dom_node.get("localName") or dom_node.get("nodeName") or "").lower() or None
        return dom_node.get("backendNodeId"), attrs, tag

    async def _ax_identity(self, cdp: CdpEngine, backend_id: int) -> tuple[Optional[str], Optional[str]]:
        try:
            ax = await cdp.send("Accessibility.getPartialAXTree",
                                {"backendNodeId": backend_id, "fetchRelatives": False})
        except CdpError:
            return None, None
        for n in (ax or {}).get("nodes") or []:
            if n.get("ignored"):
                continue
            role = ((n.get("role") or {}).get("value")) or None
            name = ((n.get("name") or {}).get("value")) or None
            if role:
                return role, name
        return None, None

    # -- output ---------------------------------------------------------
    def pages(self) -> list[dict[str, Any]]:
        """Recorded elements grouped by page, in first-seen order."""
        grouped: dict[str, dict[str, Any]] = {}
        for rec in self._elements.values():
            g = grouped.setdefault(rec.page, {
                "page": rec.page,
                "url": rec.page_url,
                "title": rec.page_title,
                "elements": [],
            })
            g["elements"].append(rec.to_json())
        for g in grouped.values():
            g["elements"].sort(key=lambda e: e["first_seen"] or "")
        return sorted(grouped.values(), key=lambda g: g["elements"][0]["first_seen"] or "")

    def snapshot(self) -> dict[str, Any]:
        return {
            "recording": self.recording,
            "started_at": self.started_at,
            "count": len(self._elements),
            "pages": self.pages(),
        }

    def clear(self) -> None:
        self._elements.clear()

    def write_file(self) -> Path:
        self._out_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": "1.0",
            "started_at": self.started_at,
            "stopped_at": datetime.now(timezone.utc).isoformat(),
            "element_count": len(self._elements),
            "pages": self.pages(),
        }
        target = self._out_dir / RECORDING_FILE
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        import os
        os.replace(tmp, target)
        return target
