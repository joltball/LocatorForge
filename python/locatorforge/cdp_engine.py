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

    async def connect(self) -> None:
        target = _first_page_target(self.debug_port)
        self._target_id = target.get("id")
        ws_url = target["webSocketDebuggerUrl"]
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
        self._wire_context_tracking()
        # Enable domains we always need
        for domain in ("Page", "DOM", "Accessibility", "Overlay", "Runtime"):
            try:
                await self.send(f"{domain}.enable")
            except CdpError:
                # Some domains (Accessibility) don't require enable on all versions
                pass

    async def close(self) -> None:
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
        """Exponential backoff reconnect: 1s → 2s → 4s → max 30s (per FR-10)."""
        delay = 1.0
        while True:
            try:
                await asyncio.sleep(delay)
                self._ws = None
                self._reader_task = None
                await self.connect()
                log.info("CDP reconnected")
                return
            except Exception:  # noqa: BLE001
                log.warning("CDP reconnect failed, retrying in %.1fs", delay)
                delay = min(delay * 2, 30.0)
