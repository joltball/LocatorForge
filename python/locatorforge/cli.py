# PHASE: 1.2.2 + 4.4
"""CLI entry point.

Per SPEC §10.3: ensures the matching Java UI JAR is cached locally (downloads
from Artifactory if necessary), starts the FastAPI backend in a thread, then
spawns the Java UI as a subprocess and waits for it to exit.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

import requests

from . import __version__
from .cdp_engine import launch_or_attach_chrome
from .config import load_config

UI_ARTIFACT_VERSION = __version__
ARTIFACTORY_BASE = os.environ.get(
    "LOCATORFORGE_ARTIFACTORY_BASE",
    "https://artifactory.citi.internal/artifactory/maven-local",
)
UI_GROUP_PATH = "com/citi/qa/locatorforge-ui"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="locatorforge", description="LocatorForge backend & UI launcher")
    p.add_argument("--repo-root", required=True, help="Path to the consuming repo root")
    p.add_argument("--port", type=int, default=None, help="Chrome CDP debug port (default 9222)")
    p.add_argument("--api-port", type=int, default=None, help="FastAPI bind port (default 8765)")
    p.add_argument("--config", default=None, help="Path to locatorforge.yaml")
    p.add_argument("--user-profile-dir", default=None, help="Persistent Chrome profile dir")
    p.add_argument("--no-launch-chrome", action="store_true")
    p.add_argument("--no-ui", action="store_true", help="Run backend only (skip UI launch)")
    p.add_argument(
        "--ui-jar",
        default=None,
        help="Path to a pre-built locatorforge-ui jar (skips Artifactory fetch)",
    )
    p.add_argument("--version", action="version", version=f"locatorforge {__version__}")
    return p.parse_args(argv)


def get_cache_dir() -> Path:
    cache_dir = Path(os.environ.get("LOCATORFORGE_CACHE", Path.home() / ".locatorforge" / "cache"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def ensure_ui_jar(version: str = UI_ARTIFACT_VERSION) -> Path:
    """Return the cached UI jar path, downloading from Artifactory if needed."""
    cache_dir = get_cache_dir()
    jar_path = cache_dir / f"locatorforge-ui-{version}.jar"
    if jar_path.exists():
        return jar_path

    url = f"{ARTIFACTORY_BASE}/{UI_GROUP_PATH}/{version}/locatorforge-ui-{version}.jar"
    api_key = os.environ.get("ARTIFACTORY_API_KEY")
    headers = {"X-JFrog-Art-Api": api_key} if api_key else {}

    print(f"[locatorforge] Fetching UI jar v{version} from {url}", file=sys.stderr)
    response = requests.get(url, headers=headers, stream=True, timeout=30)
    response.raise_for_status()

    tmp_path = jar_path.with_suffix(".jar.tmp")
    with open(tmp_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    os.replace(tmp_path, jar_path)
    print(f"[locatorforge] Cached at {jar_path}", file=sys.stderr)
    return jar_path


def launch_ui(jar_path: Path, api_port: int) -> subprocess.Popen:
    args = ["java", "-jar", str(jar_path), f"--api-port={api_port}"]
    return subprocess.Popen(args)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("LOCATORFORGE_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    repo_root = Path(args.repo_root).resolve()
    repo_root.mkdir(parents=True, exist_ok=True)

    cfg = load_config(repo_root, Path(args.config) if args.config else None)
    cdp_port = args.port or cfg.cdp.debug_port
    api_port = args.api_port or cfg.api.fastapi_port
    cfg.cdp.debug_port = cdp_port
    cfg.api.fastapi_port = api_port

    if not args.no_launch_chrome:
        try:
            launch_or_attach_chrome(cdp_port, args.user_profile_dir or cfg.cdp.user_profile_dir)
        except Exception as e:  # noqa: BLE001
            print(f"[locatorforge] ERROR: {e}", file=sys.stderr)
            return 2

    print(
        f"[locatorforge] Backend starting on http://{cfg.api.bind_host}:{api_port} "
        f"(CDP on port {cdp_port}, IPC dir {repo_root / '.locatorforge'})",
        file=sys.stderr,
    )

    from .api_server import serve

    if args.no_ui:
        serve(repo_root=repo_root, config=cfg, host=cfg.api.bind_host, port=api_port)
        return 0

    backend_thread = threading.Thread(
        target=serve,
        kwargs=dict(repo_root=repo_root, config=cfg, host=cfg.api.bind_host, port=api_port),
        daemon=True,
        name="lf-backend",
    )
    backend_thread.start()

    jar_path: Optional[Path] = Path(args.ui_jar) if args.ui_jar else None
    if jar_path is None:
        try:
            jar_path = ensure_ui_jar()
        except Exception as e:  # noqa: BLE001
            print(
                f"[locatorforge] WARN: could not fetch UI jar from Artifactory ({e})\n"
                "                 Falling back to backend-only mode. Pass --ui-jar to use a local build.",
                file=sys.stderr,
            )
            backend_thread.join()
            return 0

    ui_proc = launch_ui(jar_path, api_port)
    ui_proc.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
