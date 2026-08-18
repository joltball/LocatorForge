# PHASE: 3 (tests)
"""Selenium / Playwright shadow-chain string generation — pure-Python, no CDP."""

from locatorforge.schemas import ShadowHostRef
from locatorforge.shadow_dom_traverser import playwright_shadow_chain, selenium_shadow_chain


def test_selenium_chain_single_host():
    chain = [ShadowHostRef(host_selector="wm-datepicker", shadow_type="open")]
    expr = selenium_shadow_chain(chain, "#date-input")
    assert "wm-datepicker" in expr
    assert ".getShadowRoot()" in expr
    assert "#date-input" in expr


def test_playwright_chain_combinator():
    chain = [
        ShadowHostRef(host_selector="my-app", shadow_type="open"),
        ShadowHostRef(host_selector="app-toolbar", shadow_type="open"),
    ]
    expr = playwright_shadow_chain(chain, "#search")
    assert expr == "page.locator('my-app >>> app-toolbar >>> #search')"
