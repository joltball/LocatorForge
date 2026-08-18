# PHASE: 1.1.4 + 3.2.x
"""File-based IPC with the external coding agent.

Phase 1 scope: atomic writer + output.json / status.json.
Phase 3 adds: command.json + ack.json watchers, full state machine, and a
one-time `.gitignore` auto-append for the IPC dir.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .schemas import AckJson, CommandJson, OutputJson, StatusJson, _utc_now_iso

log = logging.getLogger(__name__)

IPC_DIRNAME = ".locatorforge"

CommandHandler = Callable[[CommandJson], Awaitable[None]]
AckHandler = Callable[[AckJson], Awaitable[None]]


class AgentIpc:
    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root).resolve()
        self.dir = self.repo_root / IPC_DIRNAME
        self.dir.mkdir(parents=True, exist_ok=True)
        self._status = StatusJson(status="idle")
        self.write_status()
        self._ensure_gitignore()

    # ---- atomic write helper ----
    def _atomic_write(self, name: str, data: str) -> Path:
        target = self.dir / name
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, target)
        return target

    # ---- public writers ----
    def write_output(self, output: OutputJson) -> Path:
        payload = output.model_dump(mode="json")
        return self._atomic_write("output.json", json.dumps(payload, indent=2))

    def write_status(self, **patch) -> Path:
        for k, v in patch.items():
            if hasattr(self._status, k):
                setattr(self._status, k, v)
        self._status.last_updated = _utc_now_iso()
        return self._atomic_write(
            "status.json", json.dumps(self._status.model_dump(mode="json"), indent=2)
        )

    @property
    def status(self) -> StatusJson:
        return self._status

    def set_status(self, status: str, error: Optional[str] = None) -> None:
        self.write_status(status=status, error_message=error)

    # ---- gitignore auto-append ----
    def _ensure_gitignore(self) -> None:
        # Only do this if the repo looks git-managed.
        if not (self.repo_root / ".git").exists():
            return
        gi = self.repo_root / ".gitignore"
        line = f"{IPC_DIRNAME}/"
        try:
            existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
            if line in existing.splitlines():
                return
            sep = "" if not existing or existing.endswith("\n") else "\n"
            gi.write_text(existing + sep + line + "\n", encoding="utf-8")
            log.info("Appended %s to %s", line, gi)
        except OSError as e:
            log.warning("Could not update .gitignore: %s", e)

    # ---- watchers (Phase 3) ----
    async def watch_inbox(
        self,
        on_command: Optional[CommandHandler] = None,
        on_ack: Optional[AckHandler] = None,
        interval: float = 2.0,
    ) -> None:
        """Poll for `command.json` / `ack.json` and invoke handlers. After consume,
        the file is deleted to acknowledge receipt."""
        cmd_path = self.dir / "command.json"
        ack_path = self.dir / "ack.json"
        while True:
            try:
                if on_command and cmd_path.exists():
                    raw = cmd_path.read_text(encoding="utf-8")
                    try:
                        msg = CommandJson(**json.loads(raw))
                    except Exception:  # noqa: BLE001
                        log.warning("Malformed command.json — deleting")
                        cmd_path.unlink(missing_ok=True)
                    else:
                        cmd_path.unlink(missing_ok=True)
                        await on_command(msg)
                if on_ack and ack_path.exists():
                    raw = ack_path.read_text(encoding="utf-8")
                    try:
                        msg = AckJson(**json.loads(raw))
                    except Exception:  # noqa: BLE001
                        log.warning("Malformed ack.json — deleting")
                        ack_path.unlink(missing_ok=True)
                    else:
                        ack_path.unlink(missing_ok=True)
                        await on_ack(msg)
            except Exception:  # noqa: BLE001
                log.exception("Error in IPC watcher")
            await asyncio.sleep(interval)
