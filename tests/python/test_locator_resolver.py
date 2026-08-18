# PHASE: 2 (tests)
"""Unit tests for locator candidate generation and ranking — no live CDP."""

from locatorforge.config import LocatorsCfg
from locatorforge.locator_resolver import _build_candidates, _rank_by_config
from locatorforge.schemas import TreeNode


def _node(role, name=None, **attrs):
    return TreeNode(node_id="n1", role=role, name=name, attributes=attrs)


def test_data_testid_wins_when_priority_default():
    node = _node("button", "Login")
    node.attributes.update({"data-testid": "login-btn", "id": "loginBtn", "aria-label": "Log in"})
    cands = _build_candidates(node)
    cands = _rank_by_config(cands, LocatorsCfg())
    assert cands[0].strategy == "data-testid"
    # Selenium and Playwright both emitted regardless of UI display
    assert "@FindBy(css = \"[data-testid='login-btn']\")" == cands[0].selenium
    assert cands[0].playwright == "page.getByTestId('login-btn')"


def test_id_outranks_aria_when_priority_overridden():
    cfg = LocatorsCfg(priority=["id", "data-testid", "aria-label", "name", "css", "xpath"])
    node = _node("button", "Submit")
    node.attributes.update({"id": "submit", "data-testid": "x"})
    cands = _rank_by_config(_build_candidates(node), cfg)
    assert cands[0].strategy == "id"


def test_role_with_name_emits_getbyrole():
    node = _node("button", "Continue")
    cands = _build_candidates(node)
    role_cand = next(c for c in cands if c.strategy == "role")
    assert role_cand.playwright == "page.getByRole('button', { name: 'Continue' })"


def test_no_attrs_falls_back_to_role_css():
    node = _node("link", None)
    cands = _build_candidates(node)
    assert any(c.strategy == "css" for c in cands)
