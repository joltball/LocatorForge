# PHASE: 1.1.1
"""CDP engine: launch or attach to Chrome on the configured remote-debugging port,
open a CDP WebSocket against the first page target, and provide async helpers
`send(method, params)` and `on_event(method, handler)`.

Per SPEC §2 and ADR-01, this binds to 127.0.0.1 only. Chrome is launched with
--remote-debugging-port and a (configurable) user-data-dir.

Connection resilience (backoff per FR-10) is added in Phase 4.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import requests
import websockets
from websockets.client import WebSocketClientProtocol

log = logging.getLogger(__name__)


class CdpError(RuntimeError):
    pass


class ChromeNotFoundError(CdpError):
    pass


class CdpPortBusyError(CdpError):
    pass


def _candidate_chrome_paths() -> list[Path]:
    if sys.platform.startswith("win"):
        return [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
        ]
    if sys.platform == "darwin":
        return [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    return [Path("/usr/bin/google-chrome"), Path("/usr/bin/chromium"), Path("/usr/bin/chromium-browser")]


def find_chrome() -> Path:
    found = shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("chromium")
    if found:
        return Path(found)
    for p in _candidate_chrome_paths():
        if p.exists():
            return p
    raise ChromeNotFoundError(
        "Could not locate a Chrome / Chromium executable. "
        "Install Chrome or set the PATH to its binary."
    )


def _port_alive(port: int, host: str = "127.0.0.1") -> bool:
    try:
        r = requests.get(f"http://{host}:{port}/json/version", timeout=0.5)
        return r.status_code == 200
    except requests.RequestException:
        return False


def launch_or_attach_chrome(
    debug_port: int = 9222,
    user_profile_dir: Optional[str] = None,
) -> Optional[subprocess.Popen]:
    """If port is alive, attach (return None). Otherwise launch Chrome and return
    the subprocess handle.
    """
    if _port_alive(debug_port):
        log.info("Attaching to existing Chrome on port %s", debug_port)
        return None

    chrome = find_chrome()
    profile = user_profile_dir or tempfile.mkdtemp(prefix="locatorforge-profile-")
    args = [
        str(chrome),
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    log.info("Launching Chrome: %s", " ".join(args))
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise CdpError(f"Failed to launch Chrome: {e}") from e

    # Wait for port to come up (Chrome can take a couple seconds)
    import time
    for _ in range(40):
        if _port_alive(debug_port):
            return proc
        time.sleep(0.25)
    proc.terminate()
    raise CdpPortBusyError(
        f"Chrome launched but did not start CDP on 127.0.0.1:{debug_port} in time"
    )


# Targets that are type "page" but are never the application under test.
# DevTools windows in particular register as ordinary page targets and often
# sort FIRST, so a naive pages[0] silently attaches to DevTools instead of the
# app — the tree then comes back as DevTools' own UI.
_NON_APP_URL_PREFIXES = (
    "devtools://",
    "chrome://",
    "chrome-extension://",
    "chrome-untrusted://",
    "edge://",
)


def _is_app_page(target: dict) -> bool:
    url = (target.get("url") or "").lower()
    return not any(url.startswith(p) for p in _NON_APP_URL_PREFIXES)


def _first_page_target(debug_port: int) -> dict:
    targets = requests.get(f"http://127.0.0.1:{debug_port}/json", timeout=2).json()
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        raise CdpError("No 'page' targets available in Chrome — open a tab first.")
    app_pages = [t for t in pages if _is_app_page(t)]
    if not app_pages:
        raise CdpError(
            "Only DevTools/internal pages are open — navigate a tab to the "
            "application under test first."
        )
    # Prefer a non-blank page so a stray about:blank tab doesn't win.
    real = [t for t in app_pages if (t.get("url") or "") not in ("about:blank", "")]
    chosen = (real or app_pages)[0]
    if len(pages) != len(app_pages):
        log.info(
            "Skipped %d DevTools/internal target(s); attaching to %s",
            len(pages) - len(app_pages), (chosen.get("url") or "")[:80],
        )
    return chosen


class CdpEngine:
    """Async CDP client over a single target's WebSocket."""

    def __init__(self, debug_port: int = 9222):
        self.debug_port = debug_port
        self._ws: Optional[WebSocketClientProtocol] = None
        self._counter = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._event_handlers: dict[str, list[Callable[[dict], Awaitable[None]]]] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._target_id: Optional[str] = None
        # PHASE 8: frameId -> default execution context id. Needed to run script
        # inside a specific frame (the recorder must install its listener in
        # EVERY frame, not just the main one).
        self.frame_contexts: dict[str, int] = {}
        self._context_frames: dict[int, str] = {}
        # Callbacks fired when a frame gets a fresh default context, so long-
        # running features (recording) can re-inject into SPA-swapped frames.
        self._context_listeners: list[Callable[[str, int], Awaitable[None]]] = []
        self._context_wired = False   # guard: connect() may run many times

        # PHASE 9 — target lifecycle. A page target is not forever: apps that
        # open a popup and close the opener replace the target entirely, with a
        # different targetId. A browser-level connection watches for that so we
        # can follow the app instead of dying with the old tab.
        self._browser_ws: Optional[WebSocketClientProtocol] = None
        self._browser_pending: dict[int, asyncio.Future] = {}
        self._browser_reader: Optional[asyncio.Task] = None
        self.known_targets: dict[str, dict] = {}
        self._target_seen_order: list[str] = []      # oldest -> newest
        self._target_listeners: list[Callable[[dict], Awaitable[None]]] = []
        self._rebinding = False
        self.auto_follow = True      # follow popups automatically

    async def connect(self, target: Optional[dict] = None) -> None:
        """Attach to a page target. Picks the best available one if not given."""
        target = target or _first_page_target(self.debug_port)
        self._target_id = target.get("id")
        ws_url = target.get("webSocketDebuggerUrl") or (
            f"ws://127.0.0.1:{self.debug_port}/devtools/page/{self._target_id}"
        )
        log.info("Connecting to CDP WebSocket: %s", ws_url)
        # CDP doesn't use WS ping/pong — disable keepalive so heavy responses
        # (e.g. Accessibility.getFullAXTree on real pages) don't trip the
        # default 20 s ping timeout. Also bump max frame size for big trees.
        self._ws = await websockets.connect(
            ws_url,
            max_size=128 * 1024 * 1024,
            ping_interval=None,
            ping_timeout=None,
            close_timeout=5,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        # Register context tracking BEFORE Runtime.enable — enabling replays
        # `executionContextCreated` for every context that already exists, and
        # that replay is the only chance to learn about already-loaded frames.
        # Guarded: connect() runs again on every reconnect and rebind, and the
        # handler list persists, so re-wiring would stack duplicates.
        if not self._context_wired:
            self._wire_context_tracking()
            self._context_wired = True
        # Contexts belong to the target we just left — never carry them over.
        self.frame_contexts.clear()
        self._context_frames.clear()
        # Enable domains we always need
        for domain in ("Page", "DOM", "Accessibility", "Overlay", "Runtime"):
            try:
                await self.send(f"{domain}.enable")
            except CdpError:
                # Some domains (Accessibility) don't require enable on all versions
                pass
        # Start watching browser-level target lifecycle (idempotent).
        await self._ensure_target_watcher()

    async def close(self) -> None:
        self.auto_follow = False        # stop chasing targets during shutdown
        if self._browser_reader:
            self._browser_reader.cancel()
            self._browser_reader = None
        if self._browser_ws:
            try:
                await self._browser_ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser_ws = None
        if self._reader_task:
            self._reader_task.cancel()
        if self._ws:
            await self._ws.close()

    async def send(self, method: str, params: Optional[dict] = None) -> Any:
        if not self._ws:
            raise CdpError("CDP not connected")
        msg_id = next(self._counter)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut
        payload = {"id": msg_id, "method": method, "params": params or {}}
        await self._ws.send(json.dumps(payload))
        # Healthy CDP calls respond in ms. If we're past 15 s, Chrome is stuck
        # or our read loop is blocked — fail fast so the UI is responsive.
        try:
            result = await asyncio.wait_for(fut, timeout=15)
        except asyncio.TimeoutError as e:
            self._pending.pop(msg_id, None)
            raise CdpError(f"CDP method {method} timed out") from e
        return result

    def on_event(self, method: str, handler: Callable[[dict], Awaitable[None]]) -> None:
        self._event_handlers.setdefault(method, []).append(handler)

    # ---- PHASE 8: per-frame execution contexts ----------------------------

    # ---- PHASE 9: target lifecycle --------------------------------------

    def on_target_changed(self, handler: Callable[[dict], Awaitable[None]]) -> None:
        """Called after the engine rebinds to a different page target.

        Receives the new target info. Consumers must treat everything
        target-scoped (element caches, injected scripts) as invalidated.
        """
        self._target_listeners.append(handler)

    async def _ensure_target_watcher(self) -> None:
        """Open a browser-level CDP connection and subscribe to target events.

        This is separate from the page connection on purpose: when the page
        target dies, its socket dies with it. Target lifecycle has to be
        observed from somewhere that outlives any single page.
        """
        if self._browser_ws is not None:
            return
        try:
            ver = requests.get(
                f"http://127.0.0.1:{self.debug_port}/json/version", timeout=2
            ).json()
            browser_url = ver.get("webSocketDebuggerUrl")
            if not browser_url:
                log.warning("No browser-level debugger URL; popup following disabled")
                return
            self._browser_ws = await websockets.connect(
                browser_url, max_size=16 * 1024 * 1024,
                ping_interval=None, ping_timeout=None, close_timeout=5,
            )
            self._browser_reader = asyncio.create_task(self._browser_read_loop())
            await self._browser_send("Target.setDiscoverTargets", {"discover": True})
            log.info("Target watcher active — popup windows will be followed")
        except Exception as e:  # noqa: BLE001
            log.warning("Could not start target watcher (%s); popup following disabled", e)
            self._browser_ws = None

    async def _browser_send(self, method: str, params: Optional[dict] = None) -> Any:
        if not self._browser_ws:
            raise CdpError("browser CDP not connected")
        msg_id = next(self._counter)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._browser_pending[msg_id] = fut
        await self._browser_ws.send(
            json.dumps({"id": msg_id, "method": method, "params": params or {}})
        )
        try:
            return await asyncio.wait_for(fut, timeout=10)
        except asyncio.TimeoutError as e:
            self._browser_pending.pop(msg_id, None)
            raise CdpError(f"browser CDP {method} timed out") from e

    async def _browser_read_loop(self) -> None:
        assert self._browser_ws is not None
        try:
            async for raw in self._browser_ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if "id" in msg:
                    fut = self._browser_pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(CdpError(msg["error"].get("message", "CDP error")))
                        else:
                            fut.set_result(msg.get("result", {}))
                    continue
                method = msg.get("method")
                params = msg.get("params") or {}
                if method == "Target.targetCreated":
                    self._note_target(params.get("targetInfo") or {})
                elif method == "Target.targetInfoChanged":
                    self._note_target(params.get("targetInfo") or {}, is_new=False)
                elif method == "Target.targetDestroyed":
                    asyncio.create_task(self._on_target_destroyed(params.get("targetId")))
        except websockets.ConnectionClosed:
            log.warning("Target watcher disconnected")
            self._browser_ws = None

    def _note_target(self, info: dict, is_new: bool = True) -> None:
        tid = info.get("targetId")
        if not tid or info.get("type") != "page":
            return
        known = tid in self.known_targets
        self.known_targets[tid] = info
        if not known:
            self._target_seen_order.append(tid)
            if is_new and _is_app_page(info) and self._target_id and tid != self._target_id:
                log.info("New page target appeared: %s", (info.get("url") or "")[:80])

    async def _on_target_destroyed(self, target_id: Optional[str]) -> None:
        if not target_id:
            return
        self.known_targets.pop(target_id, None)
        if target_id in self._target_seen_order:
            self._target_seen_order.remove(target_id)
        if target_id != self._target_id:
            return
        # Our page just went away — this is the popup-replaces-opener case.
        log.info("Attached target was destroyed; looking for a replacement")
        if self.auto_follow:
            await self._follow_to_best_target()

    async def _follow_to_best_target(self, attempts: int = 12) -> bool:
        """Wait briefly for a viable page target, then rebind to it.

        The popup may be created slightly before or after the opener closes, so
        this polls rather than assuming one is already present.
        """
        for i in range(attempts):
            target = self._pick_best_target()
            if target:
                try:
                    await self.rebind_to(target)
                    return True
                except Exception as e:  # noqa: BLE001
                    log.warning("Rebind attempt failed: %s", e)
            await asyncio.sleep(0.25 if i < 8 else 1.0)
        log.error("No replacement page target appeared — is the browser closed?")
        return False

    def _pick_best_target(self) -> Optional[dict]:
        """Most recently created real page target, excluding the dead one."""
        # Prefer live HTTP discovery: it carries webSocketDebuggerUrl and never
        # goes stale, whereas our event-built map can lag a beat.
        try:
            listed = requests.get(
                f"http://127.0.0.1:{self.debug_port}/json", timeout=2
            ).json()
        except Exception:  # noqa: BLE001
            listed = []
        pages = [
            t for t in listed
            if t.get("type") == "page" and _is_app_page(t) and t.get("id") != self._target_id
        ]
        if not pages:
            return None
        # Rank by how recently we saw the target announced; unseen targets are
        # brand new (the popup we are chasing) and sort last => most recent.
        def recency(t: dict) -> int:
            tid = t.get("id")
            return self._target_seen_order.index(tid) if tid in self._target_seen_order else 10**6
        real = [t for t in pages if (t.get("url") or "") not in ("about:blank", "")]
        return sorted(real or pages, key=recency)[-1]

    async def rebind_to(self, target: dict) -> None:
        """Detach from the current page target and attach to `target`."""
        if self._rebinding:
            return
        self._rebinding = True
        try:
            old = self._target_id
            # Fail any in-flight calls rather than let them hang on a dead socket.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CdpError("target changed"))
            self._pending.clear()
            if self._reader_task:
                self._reader_task.cancel()
                self._reader_task = None
            if self._ws:
                try:
                    await self._ws.close()
                except Exception:  # noqa: BLE001
                    pass
                self._ws = None

            await self.connect(target)
            log.info(
                "Rebound to target %s -> %s (%s)",
                (old or "?")[:8], (self._target_id or "?")[:8],
                (target.get("url") or "")[:70],
            )
            info = {
                "target_id": self._target_id,
                "url": target.get("url"),
                "title": target.get("title"),
                "previous_target_id": old,
            }
            for cb in list(self._target_listeners):
                try:
                    await cb(info)
                except Exception:  # noqa: BLE001
                    log.exception("target-changed listener failed")
        finally:
            self._rebinding = False

    def frame_for_context(self, context_id: Optional[int]) -> Optional[str]:
        """Which frame does this execution context belong to? None if unknown."""
        if context_id is None:
            return None
        return self._context_frames.get(context_id)

    def on_frame_context(self, handler: Callable[[str, int], Awaitable[None]]) -> None:
        """Register a callback for (frame_id, context_id) when a frame gets a
        fresh default context — i.e. a new document loaded into that frame."""
        self._context_listeners.append(handler)

    def _wire_context_tracking(self) -> None:
        async def on_created(params: dict) -> None:
            ctx = params.get("context") or {}
            aux = ctx.get("auxData") or {}
            frame_id = aux.get("frameId")
            ctx_id = ctx.get("id")
            # Only DEFAULT worlds — isolated worlds share the DOM but not the
            # page's own globals, and the recorder binding lives in the default.
            if not (aux.get("isDefault") and frame_id and ctx_id is not None):
                return
            self.frame_contexts[frame_id] = ctx_id
            self._context_frames[ctx_id] = frame_id
            for cb in list(self._context_listeners):
                try:
                    await cb(frame_id, ctx_id)
                except Exception:  # noqa: BLE001
                    log.exception("frame-context listener failed")

        async def on_destroyed(params: dict) -> None:
            ctx_id = params.get("executionContextId")
            frame_id = self._context_frames.pop(ctx_id, None)
            if frame_id and self.frame_contexts.get(frame_id) == ctx_id:
                self.frame_contexts.pop(frame_id, None)

        async def on_cleared(_params: dict) -> None:
            self.frame_contexts.clear()
            self._context_frames.clear()

        self.on_event("Runtime.executionContextCreated", on_created)
        self.on_event("Runtime.executionContextDestroyed", on_destroyed)
        self.on_event("Runtime.executionContextsCleared", on_cleared)

    async def _safe_dispatch(self, method: str, handler, params: dict) -> None:
        try:
            await handler(params)
        except Exception:  # noqa: BLE001
            log.exception("Event handler for %s failed", method)

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if not fut:
                        continue
                    if "error" in msg:
                        fut.set_exception(CdpError(msg["error"].get("message", "CDP error")))
                    else:
                        fut.set_result(msg.get("result", {}))
                else:
                    method = msg.get("method")
                    if not method:
                        continue
                    params = msg.get("params", {})
                    for handler in self._event_handlers.get(method, []):
                        # CRITICAL: dispatch event handlers as background tasks.
                        # If we `await handler(...)` inline here, a slow handler
                        # blocks the read loop — Chrome's CDP responses queue up
                        # in the WS buffer with no one to dispatch them, so any
                        # in-flight `send()` futures never resolve and time out.
                        asyncio.create_task(self._safe_dispatch(method, handler, params))
        except websockets.ConnectionClosed:
            log.warning("CDP WebSocket closed — attempting reconnect")
            asyncio.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        """Exponential backoff reconnect: 1s → 2s → 4s → max 30s (per FR-10).

        A closed socket has two very different causes: a transient drop (same
        target still exists — reattach to it) or the target being destroyed,
        e.g. an app that closes the opener after spawning a popup. The second
        case must NOT reattach to the old targetId, so we re-pick.
        """
        if self._rebinding:
            return          # a deliberate rebind is already in flight
        delay = 1.0
        while True:
            try:
                await asyncio.sleep(delay)
                self._ws = None
                self._reader_task = None

                still_alive = False
                if self._target_id:
                    try:
                        listed = requests.get(
                            f"http://127.0.0.1:{self.debug_port}/json", timeout=2
                        ).json()
                        still_alive = any(t.get("id") == self._target_id for t in listed)
                    except Exception:  # noqa: BLE001
                        pass

                if still_alive:
                    await self.connect()
                    log.info("CDP reconnected to the same target")
                    return

                target = self._pick_best_target()
                if target is None:
                    raise CdpError("no page target available yet")
                await self.rebind_to(target)
                log.info("CDP reconnected by following to a new target")
                return
            except Exception:  # noqa: BLE001
                log.warning("CDP reconnect failed, retrying in %.1fs", delay)
                delay = min(delay * 2, 30.0)
