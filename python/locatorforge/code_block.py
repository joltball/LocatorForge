# PHASE: 4.2
"""Build `code_block` / `insert_after_pattern` / `replace_pattern` payloads for
modifications, per ADR-06 and SPEC §6.2.

Disabled by default — the API server only calls this when
`agent_output.enable_code_block` is true in `locatorforge.yaml`.
"""

from __future__ import annotations

from .config import CodeStyle
from .schemas import Modification


def annotate_modification(mod: Modification, style: CodeStyle) -> Modification:
    """Mutate `mod` in place to populate ADR-06 fields based on its action +
    shadow_chain. Returns the same modification for chaining."""
    indent = style.indent
    access = style.access_modifier
    if mod.action == "update":
        mod.replace_pattern = _replace_pattern_for_update(mod)
        mod.code_block = [
            f"{indent}{mod.annotation_format}",
            f"{indent}{access} WebElement {mod.element_name};",
        ]
    elif mod.action == "add":
        mod.insert_after_pattern = mod.insert_after or f"{access} WebElement"
        if mod.shadow_chain:
            mod.code_block = _shadow_helper_method(mod, indent, access)
        else:
            mod.code_block = [
                "",
                f"{indent}{mod.annotation_format}",
                f"{indent}{access} WebElement {mod.element_name};",
            ]
    return mod


def _replace_pattern_for_update(mod: Modification) -> str:
    """Build the existing-source pattern the agent should look for. We don't know
    the old annotation exactly when only structured fields are supplied — fall
    back to a marker that the agent can grep against the element name."""
    if mod.old_locator and mod.old_locator.strategy in {"id", "name"}:
        old_anno = f"@FindBy({mod.old_locator.strategy} = \"{mod.old_locator.value}\")"
        return f"{old_anno}\n    private WebElement {mod.element_name};"
    return f"private WebElement {mod.element_name};"


def _shadow_helper_method(mod: Modification, indent: str, access: str) -> list[str]:
    """Selenium-style helper method that pierces the shadow chain end-to-end —
    the agent never has to write traversal logic itself.
    """
    chain = mod.shadow_chain
    method_name = "get" + mod.element_name[:1].upper() + mod.element_name[1:]
    lines = [
        "",
        f"{indent}{access} WebElement {method_name}() {{",
        f"{indent}{indent}return driver.findElement(By.cssSelector(\"{chain[0].host_selector}\"))",
    ]
    for h in chain[1:]:
        lines.append(f"{indent}{indent}    .getShadowRoot()")
        lines.append(f"{indent}{indent}    .findElement(By.cssSelector(\"{h.host_selector}\"))")
    lines.append(f"{indent}{indent}    .getShadowRoot()")
    final_value = mod.new_locator.value if mod.new_locator else "*"
    final_css = _final_css_for(mod)
    lines.append(f"{indent}{indent}    .findElement(By.cssSelector(\"{final_css}\"));")
    lines.append(f"{indent}}}")
    return lines


def _final_css_for(mod: Modification) -> str:
    if not mod.new_locator:
        return "*"
    s = mod.new_locator.strategy
    v = mod.new_locator.value
    return {
        "id":            f"#{v}",
        "data-testid":   f"[data-testid='{v}']",
        "aria-label":    f"[aria-label='{v}']",
        "name":          f"[name='{v}']",
        "css":           v,
    }.get(s, v)
