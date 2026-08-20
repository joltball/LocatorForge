# PHASE: 1.2.1 + 2.1.3..2.1.7  (3.x adds POM endpoints; 4.x adds reconnect)
"""FastAPI backend bound to 127.0.0.1 only.

Endpoints:
  * GET    /health                — connection status
  * GET    /tree                  — current accessibility tree (filtered)
  * GET    /locators/{node_id}    — all candidate strategies for a node
  * POST   /highlight/{node_id}   — flash the element in the browser
  * POST   /element/pick          — enter inspect mode (verification picker)
  * POST   /validate              — live-validate a custom locator
  * POST   /push                  — write output.json
  * POST   /record/start          — begin passive interaction recording
  * POST   /record/stop           — end recording, write recording.json
  * GET    /record                — current recording state + captured elements
  * WS     /ws                    — server-push events (Phase 2)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from . import __version__
from .agent_ipc import AgentIpc
from .ax_tree_processor import fetch_tree
from .cdp_engine import CdpEngine
from .config import Config
from .interaction_recorder import InteractionRecorder, page_key_from_url
from .locator_resolver import resolve_for_node, validate_uniqueness, LocatorCandidate
from .code_block import annotate_modification
from .repo_scanner import search_poms
from .schemas import AckJson, CommandJson, Modification, OutputJson, ShadowHostRef, TreeNode
from .shadow_dom_traverser import traverse as shadow_traverse

log = logging.getLogger(__name__)


# ---- broadcast helpers ----------------------------------------------------

class WsBroadcaster:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, event: str, data: dict) -> None:
        dead: list[WebSocket] = []
        payload = {"event": event, "data": data}
        for c in list(self.clients):
            try:
                await c.send_json(payload)
            except Exception:  # noqa: BLE001
                dead.append(c)
        for c in dead:
            self.clients.discard(c)


# ---- state ---------------------------------------------------------------

class _AppState:
    def __init__(self, repo_root: Path, config: Config):
        self.repo_root = repo_root
        self.config = config
        self.cdp: Optional[CdpEngine] = None
        self.ipc = AgentIpc(repo_root)
        self.tree_cache: Optional[TreeNode] = None
        self.flat_cache: dict[str, TreeNode] = {}
        self.ws = WsBroadcaster()
        self._lock = asyncio.Lock()
        # Debounce auto-refresh on Page.frameNavigated — sub-frames (iframes,
        # ads) fire this event dozens of times during a real page load.
        self._auto_refresh_pending: bool = False
        self.recorder = InteractionRecorder(
            cdp_getter=self.ensure_cdp,
            config=config,
            out_dir=self.ipc.dir,
            broadcast=self.ws.broadcast,
        )

    async def ensure_cdp(self) -> CdpEngine:
        if self.cdp is None:
            engine = CdpEngine(self.config.cdp.debug_port)
            await engine.connect()
            self._wire_cdp_events(engine)
            self.cdp = engine
        return self.cdp

    def _wire_cdp_events(self, cdp: CdpEngine) -> None:
        async def on_frame_navigated(params: dict) -> None:
            frame = params.get("frame") or {}
            # Only react to top-level navigation. Sub-frames (parentId set) fire
            # this event constantly during a real page load and would each kick
            # off a tree refresh, queueing CDP calls until they time out.
            if frame.get("parentId"):
                return
            url = frame.get("url")
            if not url or url == "about:blank":
                return
            await self.ws.broadcast("page_navigated", {"url": url})
            self.ipc.write_status(current_url=url)
            # Coalesce bursts: if a refresh is already scheduled, drop this one.
            if self._auto_refresh_pending:
                return
            self._auto_refresh_pending = True
            try:
                # Let the page settle so getRootAXNode actually has a tree
                # to return — otherwise CDP hangs on a half-built document.
                await asyncio.sleep(0.6)
                # If a foreground /tree request is already running, skip.
                # We must NOT block manual refresh while CDP is busy with our
                # auto-refresh (it can hang for tens of seconds on a still-
                # loading page).
                if self._lock.locked():
                    log.info("auto-refresh skipped — foreground tree fetch in progress")
                    await self.ws.broadcast("tree_updated", {})
                    return
                try:
                    await asyncio.wait_for(self.refresh_tree(), timeout=10)
                    await self.ws.broadcast("tree_updated", {})
                except asyncio.TimeoutError:
                    log.warning("auto-refresh: refresh_tree exceeded 10 s — abandoning so manual Refresh can proceed")
                except Exception as e:  # noqa: BLE001
                    log.warning("auto-refresh after navigation failed: %s", e)
            finally:
                self._auto_refresh_pending = False

        async def on_inspect_picked(params: dict) -> None:
            backend_id = params.get("backendNodeId")
            if backend_id is None:
                return
            payload = await self._pick_partial(backend_id)
            await self.ws.broadcast("element_picked", payload)

        cdp.on_event("Page.frameNavigated", on_frame_navigated)
        cdp.on_event("Overlay.inspectNodeRequested", on_inspect_picked)

    async def _pick_partial(self, backend_node_id: int) -> dict:
        cdp = await self.ensure_cdp()
        # getPartialAXTree bypasses interestingOnly filtering so verification
        # nodes (divs, spans) still come through.
        try:
            ax = await cdp.send(
                "Accessibility.getPartialAXTree",
                {"backendNodeId": backend_node_id, "fetchRelatives": False},
            )
        except Exception:  # noqa: BLE001
            ax = {}
        try:
            descr = await cdp.send("DOM.describeNode", {"backendNodeId": backend_node_id})
        except Exception:  # noqa: BLE001
            descr = {}
        # Stop inspect mode after a pick
        try:
            await cdp.send("Overlay.setInspectMode", {"mode": "none", "highlightConfig": {}})
        except Exception:  # noqa: BLE001
            pass
        return {"ax_tree": ax, "dom_node": descr}

    async def refresh_tree(self) -> Optional[TreeNode]:
        import time
        t0 = time.monotonic()
        log.info("[refresh_tree] waiting for lock")
        async with self._lock:
            log.info("[refresh_tree] lock acquired (+%.2fs)", time.monotonic() - t0)
            cdp = await self.ensure_cdp()
            log.info("[refresh_tree] CDP ready, calling fetch_tree")
            tree = await fetch_tree(cdp)
            log.info("[refresh_tree] fetch_tree returned %s",
                     "None" if tree is None else f"root={tree.role} children={len(tree.children)}")
            log.info("[refresh_tree] calling shadow_traverse")
            shadow_hosts = await shadow_traverse(cdp, self.config.shadow_dom)
            log.info("[refresh_tree] shadow_traverse returned %d host(s)", len(shadow_hosts) if shadow_hosts else 0)
            if tree is not None and shadow_hosts:
                self._splice_shadow(tree, shadow_hosts)
                self.ipc.write_status(shadow_hosts_detected=len(shadow_hosts))
            else:
                self.ipc.write_status(shadow_hosts_detected=0)
            self.tree_cache = tree
            self.flat_cache = {}
            if tree is not None:
                self._flatten(tree)
            try:
                target_info = await cdp.send("Target.getTargetInfo")
                info = (target_info or {}).get("targetInfo", {})
                url = info.get("url")
                if url:
                    self.ipc.write_status(current_url=url)
                    # Stamp page identity on the root; the UI inherits it down
                    # the tree so every locator shows which page it came from.
                    if tree is not None:
                        tree.page = page_key_from_url(url, info.get("title", ""))
                        tree.page_url = url
            except Exception:  # noqa: BLE001
                pass
            return tree

    def _flatten(self, node: TreeNode) -> None:
        self.flat_cache[node.node_id] = node
        for c in node.children:
            self._flatten(c)

    def _splice_shadow(self, tree: TreeNode, hosts) -> None:
        """Attach shadow-boundary nodes under each host, tagged with shadowAncestors.

        The matching is best-effort: hosts come from `DOM.getDocument` and the
        primary tree from `Accessibility.getSnapshot`; we match by `backendDOMNodeId`
        when available, otherwise we fall back to appending boundary stubs to the
        root so the UI can still surface them.
        """
        from .schemas import ShadowHostRef, TreeNode as TN
        by_backend: dict[int, TN] = {}

        def collect(n: TN) -> None:
            if n.backend_node_id is not None:
                by_backend[n.backend_node_id] = n
            for c in n.children:
                collect(c)
        collect(tree)

        for rec in hosts:
            host_node = by_backend.get(rec.backend_node_id)
            boundary = TN(
                node_id=f"shadow_{rec.backend_node_id}",
                role="_shadow_boundary",
                name=f"{rec.host_selector} [shadow-root: {rec.shadow_type}]",
                attributes={"shadow_type": rec.shadow_type, "host_selector": rec.host_selector},
                is_shadow_boundary=True,
                shadow_ancestors=list(rec.ancestors),
                element_type="structural",
            )
            ancestors_chain = list(rec.ancestors) + [
                ShadowHostRef(host_selector=rec.host_selector, shadow_type=rec.shadow_type)
            ]
            # Hoist a few interesting AX nodes from the subtree, tagged with ancestry.
            if rec.ax_subtree:
                for ax_node in (rec.ax_subtree.get("nodes") or [])[:30]:
                    role = ((ax_node.get("role") or {}).get("value") or "")
                    if role in {"none", "presentation", "generic"}:
                        continue
                    name = (ax_node.get("name") or {}).get("value")
                    boundary.children.append(TN(
                        node_id=f"sn_{ax_node.get('nodeId','?')}",
                        role=role or "unknown",
                        name=name,
                        shadow_ancestors=ancestors_chain,
                        element_type="interactive",
                    ))
            if host_node is not None:
                host_node.children.append(boundary)
            else:
                tree.children.append(boundary)


# ---- request models ------------------------------------------------------

class PushRequest(BaseModel):
    pom_file: str
    pom_framework: str = "selenium-java"
    modifications: list[Modification]


class ValidateRequest(BaseModel):
    strategy: str
    value: str
    shadow_chain: list[ShadowHostRef] = []
    # PHASE 5: validate inside a specific iframe. None = main frame.
    frame_id: Optional[str] = None


class PomSelectRequest(BaseModel):
    pom_file: str


class RecordStartRequest(BaseModel):
    # Default: start a fresh recording. Pass clear=false to append to whatever
    # was captured before the last Stop.
    clear: bool = True


# ---- factory -------------------------------------------------------------

def create_app(repo_root: Path, config: Optional[Config] = None) -> FastAPI:
    cfg = config or Config()
    app = FastAPI(title="LocatorForge", version=__version__)
    state = _AppState(repo_root=repo_root, config=cfg)
    app.state.lf = state

    @app.on_event("startup")
    async def _startup() -> None:
        async def on_command(cmd: CommandJson) -> None:
            if cmd.command == "refresh":
                await state.refresh_tree()
                await state.ws.broadcast("tree_updated", {})
            elif cmd.command == "navigate" and cmd.arg:
                try:
                    cdp = await state.ensure_cdp()
                    await cdp.send("Page.navigate", {"url": cmd.arg})
                except Exception:  # noqa: BLE001
                    log.exception("navigate failed")
            elif cmd.command == "terminate":
                state.ipc.set_status("terminated")
        async def on_ack(ack: AckJson) -> None:
            state.ipc.set_status("idle" if ack.status == "applied" else "error",
                                 error=ack.error_message)
            await state.ws.broadcast("agent_ack", ack.model_dump())
        asyncio.create_task(state.ipc.watch_inbox(on_command=on_command, on_ack=on_ack,
                                                  interval=cfg.agent_ipc.status_poll_interval_sec))

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "cdp_port": cfg.cdp.debug_port,
            "repo_root": str(state.repo_root),
            "ipc_dir": str(state.ipc.dir),
        }

    @app.get("/tree")
    async def get_tree() -> dict[str, Any]:
        import time
        t0 = time.monotonic()
        log.info("[/tree] request received")
        try:
            tree = await state.refresh_tree()
        except Exception as e:  # noqa: BLE001
            log.exception("[/tree] tree fetch failed")
            raise HTTPException(status_code=500, detail=str(e)) from e
        elapsed = time.monotonic() - t0
        if tree is None:
            log.warning("[/tree] returning tree=null (no tree built) — %.2fs", elapsed)
            return {"tree": None}
        log.info("[/tree] returning tree root=%s direct_children=%d in %.2fs",
                 tree.role, len(tree.children), elapsed)
        return {"tree": tree.model_dump(mode="json")}

    @app.get("/locators/{node_id}")
    async def get_locators(node_id: str) -> dict[str, Any]:
        node = state.flat_cache.get(node_id)
        if node is None:
            # Re-fetch in case the cache is stale
            await state.refresh_tree()
            node = state.flat_cache.get(node_id)
        if node is None:
            raise HTTPException(status_code=404, detail=f"unknown node_id {node_id}")
        cdp = await state.ensure_cdp()
        cands = await resolve_for_node(cdp, node, cfg.locators)
        return {"node_id": node_id, "candidates": [_cand_to_json(c) for c in cands]}

    @app.post("/highlight/{node_id}")
    async def highlight(node_id: str) -> dict[str, Any]:
        node = state.flat_cache.get(node_id)
        if node is None or node.backend_node_id is None:
            raise HTTPException(status_code=404, detail="node has no backendNodeId")
        cdp = await state.ensure_cdp()
        await cdp.send("Overlay.highlightNode", {
            "highlightConfig": {
                "contentColor": {"r": 0, "g": 200, "b": 100, "a": 0.4},
                "showInfo": True,
            },
            "backendNodeId": node.backend_node_id,
        })
        return {"highlighted": node_id}

    @app.post("/element/pick")
    async def pick_element() -> dict[str, Any]:
        cdp = await state.ensure_cdp()
        await cdp.send("Overlay.setInspectMode", {
            "mode": "searchForNode",
            "highlightConfig": {
                "contentColor": {"r": 0, "g": 100, "b": 200, "a": 0.4},
                "showInfo": True,
            },
        })
        return {"inspect_mode": "searchForNode"}

    @app.post("/validate")
    async def validate(req: ValidateRequest) -> dict[str, Any]:
        cdp = await state.ensure_cdp()
        # Build a temporary candidate just to reuse the resolver's validator.
        cand = LocatorCandidate(
            strategy=req.strategy,
            value=req.value,
            selenium="",
            playwright="",
            rank=0,
            shadow_chain=req.shadow_chain,
        )
        await validate_uniqueness(cdp, cand, frame_id=req.frame_id)
        return {
            "strategy": req.strategy,
            "value": req.value,
            "match_count": cand.match_count,
            "is_unique": cand.is_unique,
            "frame_id": req.frame_id,
        }

    @app.post("/record/start")
    async def record_start(req: RecordStartRequest) -> dict[str, Any]:
        try:
            return await state.recorder.start(clear=req.clear)
        except Exception as e:  # noqa: BLE001
            log.exception("[/record/start] failed")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @app.post("/record/stop")
    async def record_stop() -> dict[str, Any]:
        return await state.recorder.stop()

    @app.get("/record")
    async def record_state() -> dict[str, Any]:
        return state.recorder.snapshot()

    @app.delete("/record")
    async def record_clear() -> dict[str, Any]:
        state.recorder.clear()
        await state.ws.broadcast("recording_cleared", {})
        return state.recorder.snapshot()

    @app.get("/targets")
    async def list_targets() -> dict[str, Any]:
        """Phase 4 multi-tab: list page targets via CDP HTTP discovery endpoint."""
        import requests
        r = requests.get(f"http://127.0.0.1:{cfg.cdp.debug_port}/json", timeout=2)
        items = [t for t in r.json() if t.get("type") == "page"]
        return {"targets": [
            {"id": t.get("id"), "title": t.get("title"), "url": t.get("url")}
            for t in items
        ]}

    @app.get("/pom/search")
    async def pom_search() -> dict[str, Any]:
        current_url = state.ipc.status.current_url
        cands = search_poms(state.repo_root, cfg.search, current_url)
        return {
            "current_url": current_url,
            "candidates": [
                {"path": c.path, "score": c.score, "reasons": c.reasons}
                for c in cands
            ],
        }

    @app.post("/pom/select")
    async def pom_select(req: PomSelectRequest) -> dict[str, Any]:
        state.ipc.write_status(detected_pom=req.pom_file)
        return {"selected": req.pom_file}

    @app.post("/push")
    async def push(req: PushRequest) -> dict[str, Any]:
        mods = list(req.modifications)
        if cfg.agent_output.enable_code_block:
            for m in mods:
                annotate_modification(m, cfg.agent_output.code_style)
        out = OutputJson(
            pom_file=req.pom_file,
            pom_framework=req.pom_framework,
            enable_code_block=cfg.agent_output.enable_code_block,
            modifications=mods,
        )
        path = state.ipc.write_output(out)
        state.ipc.set_status("output_ready")
        return {"written": str(path), "count": len(mods)}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await state.ws.connect(ws)
        try:
            # Send an initial hello so the Java client knows we're up.
            await ws.send_json({"event": "hello", "data": {"version": __version__}})
            while True:
                # We never expect client→server messages; just keep the loop
                # alive until disconnect.
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            state.ws.disconnect(ws)

    return app


def _cand_to_json(c: LocatorCandidate) -> dict:
    return {
        "strategy": c.strategy,
        "value": c.value,
        "selenium": c.selenium,
        "playwright": c.playwright,
        "rank": c.rank,
        "match_count": c.match_count,
        "is_unique": c.is_unique,
        "shadow_chain": [s.model_dump() for s in c.shadow_chain],
        "frame_chain": [f.model_dump() for f in c.frame_chain],
        # PHASE 7: index-pinned locators are unique but order-dependent — the UI
        # should mark them so a tester knows the risk before adopting one.
        "is_positional": c.is_positional,
        "position_index": c.position_index,
    }


def serve(repo_root: Path, config: Optional[Config] = None, host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn
    app = create_app(repo_root=repo_root, config=config)
    # `ws="websockets"` forces uvicorn to use the `websockets` library we already
    # depend on for CDP — without it the auto-detect occasionally fails and
    # `/ws` upgrades come back as "Unsupported upgrade request" warnings.
    uvicorn.run(app, host=host, port=port, log_level="info", ws="websockets")
