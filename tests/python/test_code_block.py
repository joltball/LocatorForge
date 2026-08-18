# PHASE: 4 (tests)
"""Verify the ADR-06 code_block emitter populates the right fields."""

from locatorforge.code_block import annotate_modification
from locatorforge.config import CodeStyle
from locatorforge.schemas import LocatorValue, Modification, ShadowHostRef


def _base_mod(action="update", shadow=None):
    return Modification(
        action=action,
        element_name="usernameField",
        new_locator=LocatorValue(strategy="data-testid", value="login-username"),
        annotation_format="@FindBy(css = \"[data-testid='login-username']\")",
        shadow_chain=shadow or [],
        old_locator=LocatorValue(strategy="id", value="user-input") if action == "update" else None,
        insert_after="usernameField" if action == "add" else None,
    )


def test_update_emits_replace_pattern_and_code_block():
    mod = annotate_modification(_base_mod("update"), CodeStyle())
    assert mod.replace_pattern is not None
    assert mod.code_block is not None
    assert any("data-testid='login-username'" in line for line in mod.code_block)


def test_add_emits_insert_after_and_code_block():
    mod = annotate_modification(_base_mod("add"), CodeStyle())
    assert mod.insert_after_pattern is not None
    assert mod.code_block is not None


def test_shadow_add_emits_helper_method():
    mod = annotate_modification(
        _base_mod("add", shadow=[ShadowHostRef(host_selector="wm-datepicker", shadow_type="open")]),
        CodeStyle(),
    )
    assert mod.code_block is not None
    joined = "\n".join(mod.code_block)
    assert "getShadowRoot()" in joined
    assert "wm-datepicker" in joined
