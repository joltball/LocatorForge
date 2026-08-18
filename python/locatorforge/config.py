# PHASE: 2.1.1
"""Load and validate `locatorforge.yaml`.

Precedence (first hit wins):
  1. Path passed in via --config
  2. <repo_root>/locatorforge.yaml
  3. ~/.locatorforge/config.yaml
  4. Built-in defaults (this module)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field


class CdpCfg(BaseModel):
    debug_port: int = 9222
    user_profile_dir: Optional[str] = None


class ApiCfg(BaseModel):
    fastapi_port: int = 8765
    bind_host: str = "127.0.0.1"


class SearchCfg(BaseModel):
    source_dirs: list[str] = Field(default_factory=lambda: [
        "src/test/java/pages",
        "src/test/java/pageobjects",
        "tests/pages",
    ])
    file_patterns: list[str] = Field(default_factory=lambda: [
        "*Page.java", "*Page.ts", "*_page.py",
    ])
    exclude_dirs: list[str] = Field(default_factory=lambda: [
        "node_modules", "build", "target", ".git",
    ])
    pom_framework: Literal["selenium-java", "playwright-ts", "pytest-selenium"] = "selenium-java"


class LocatorsCfg(BaseModel):
    priority: list[str] = Field(default_factory=lambda: [
        "data-testid", "aria-label", "id", "name", "css", "xpath",
    ])
    default_format: Literal["selenium", "playwright"] = "selenium"


class ShadowDomCfg(BaseModel):
    max_depth: int = 5
    traverse_closed: bool = False


class CodeStyle(BaseModel):
    indent: str = "    "
    access_modifier: Literal["private", "protected", "public"] = "private"


class AgentOutputCfg(BaseModel):
    enable_code_block: bool = False
    code_style: CodeStyle = CodeStyle()


class AgentIpcCfg(BaseModel):
    poll_dir: str = ".locatorforge"
    status_poll_interval_sec: float = 2.0


class Config(BaseModel):
    cdp: CdpCfg = CdpCfg()
    api: ApiCfg = ApiCfg()
    search: SearchCfg = SearchCfg()
    locators: LocatorsCfg = LocatorsCfg()
    shadow_dom: ShadowDomCfg = ShadowDomCfg()
    agent_output: AgentOutputCfg = AgentOutputCfg()
    agent_ipc: AgentIpcCfg = AgentIpcCfg()


def _candidate_paths(repo_root: Path, explicit: Optional[Path]) -> list[Path]:
    out: list[Path] = []
    if explicit:
        out.append(Path(explicit))
    out.append(repo_root / "locatorforge.yaml")
    out.append(Path(os.path.expanduser("~/.locatorforge/config.yaml")))
    return out


def load_config(repo_root: Path, explicit: Optional[Path] = None) -> Config:
    for p in _candidate_paths(Path(repo_root), explicit):
        if p.is_file():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            return Config(**data)
    return Config()
