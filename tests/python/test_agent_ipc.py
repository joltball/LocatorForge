# PHASE: 1 (tests)
"""Smoke tests for AgentIpc atomic writes."""

import json
from pathlib import Path

from locatorforge.agent_ipc import AgentIpc
from locatorforge.schemas import LocatorValue, Modification, OutputJson


def test_writes_status_on_init(tmp_path: Path):
    ipc = AgentIpc(tmp_path)
    status_path = tmp_path / ".locatorforge" / "status.json"
    assert status_path.exists()
    data = json.loads(status_path.read_text())
    assert data["status"] == "idle"


def test_writes_output_atomic(tmp_path: Path):
    ipc = AgentIpc(tmp_path)
    out = OutputJson(
        pom_file="src/test/java/pages/LoginPage.java",
        modifications=[
            Modification(
                action="update",
                element_name="usernameField",
                new_locator=LocatorValue(strategy="data-testid", value="login-username"),
                annotation_format="@FindBy(css = \"[data-testid='login-username']\")",
            )
        ],
    )
    path = ipc.write_output(out)
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["version"] == "1.2"
    assert data["enable_code_block"] is False
    assert data["modifications"][0]["element_name"] == "usernameField"
