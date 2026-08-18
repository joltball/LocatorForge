# PHASE: 1.1.3
"""Pydantic models mirroring SPEC §6 wire schemas (output.json / status.json / command.json / ack.json)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.2"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocatorValue(BaseModel):
    strategy: str
    value: str


class ShadowHostRef(BaseModel):
    host_selector: str
    shadow_type: Literal["open", "closed"] = "open"


class Modification(BaseModel):
    action: Literal["update", "add"]
    element_name: str
    # Page identity (e.g. "CheckoutPayment"), derived from the URL the element
    # was captured on. Lets a multi-page recording be split into one POM class
    # per page instead of one flat dump.
    page: Optional[str] = None
    page_url: Optional[str] = None
    locator_format: Literal["selenium", "playwright"] = "selenium"
    old_locator: Optional[LocatorValue] = None
    new_locator: LocatorValue
    annotation_format: str
    shadow_chain: list[ShadowHostRef] = Field(default_factory=list)
    line_hint: Optional[int] = None
    insert_after: Optional[str] = None
    element_type: Literal["interactive", "verification"] = "interactive"
    access_modifier: Literal["private", "protected", "public"] = "private"
    # ADR-06: populated only when enable_code_block is true
    code_block: Optional[list[str]] = None
    insert_after_pattern: Optional[str] = None
    replace_pattern: Optional[str] = None


class OutputJson(BaseModel):
    version: str = SCHEMA_VERSION
    timestamp: str = Field(default_factory=_utc_now_iso)
    pom_file: str
    pom_framework: str = "selenium-java"
    enable_code_block: bool = False
    modifications: list[Modification] = Field(default_factory=list)


StatusName = Literal["idle", "pending", "output_ready", "error", "terminated"]


class StatusJson(BaseModel):
    status: StatusName = "idle"
    last_updated: str = Field(default_factory=_utc_now_iso)
    current_url: Optional[str] = None
    detected_pom: Optional[str] = None
    shadow_hosts_detected: int = 0
    error_message: Optional[str] = None


class CommandJson(BaseModel):
    command: Literal["refresh", "terminate", "navigate"]
    arg: Optional[str] = None


class AckJson(BaseModel):
    status: Literal["applied", "failed"]
    applied_changes: list[str] = Field(default_factory=list)
    error_message: Optional[str] = None


# --- internal tree node (not part of the on-disk wire schema) ---

class TreeNode(BaseModel):
    node_id: str
    backend_node_id: Optional[int] = None
    role: str
    name: Optional[str] = None
    # Page the node belongs to; stamped on the tree root at refresh time so the
    # UI can label locators when a session spans several pages.
    page: Optional[str] = None
    page_url: Optional[str] = None
    attributes: dict[str, str] = Field(default_factory=dict)
    shadow_ancestors: list[ShadowHostRef] = Field(default_factory=list)
    is_shadow_boundary: bool = False
    element_type: Literal["interactive", "verification", "structural"] = "interactive"
    children: list["TreeNode"] = Field(default_factory=list)


TreeNode.model_rebuild()
