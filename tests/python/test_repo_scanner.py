# PHASE: 3 (tests)
"""Smoke tests for repo_scanner — runs only if `rg` is on PATH."""

import shutil
from pathlib import Path

import pytest

from locatorforge.config import SearchCfg
from locatorforge.repo_scanner import search_poms


pytestmark = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")


def test_finds_login_page_by_url_segment(tmp_path: Path):
    pages_dir = tmp_path / "src/test/java/pages"
    pages_dir.mkdir(parents=True)
    (pages_dir / "LoginPage.java").write_text(
        "package pages;\n"
        "public class LoginPage {\n"
        "    @FindBy(id = \"username\") private WebElement usernameField;\n"
        "}\n",
        encoding="utf-8",
    )

    cfg = SearchCfg()
    cands = search_poms(tmp_path, cfg, current_url="https://app.example.com/login")
    assert cands, "expected at least one candidate"
    assert cands[0].path.endswith("LoginPage.java")
    assert cands[0].score > 0
