# PHASE: 1 (tests)
"""Unit tests for ax_tree_processor filtering rules."""

from locatorforge.ax_tree_processor import normalize_snapshot


def _v(s):
    return {"type": "string", "value": s}


def test_drops_unlabeled_generic_but_keeps_children():
    snapshot = {
        "nodes": [
            {"nodeId": "1", "role": _v("WebArea"), "childIds": ["2"]},
            {"nodeId": "2", "role": _v("generic"), "childIds": ["3"]},
            {"nodeId": "3", "role": _v("button"), "name": _v("Login"), "childIds": []},
        ]
    }
    root = normalize_snapshot(snapshot)
    assert root is not None
    # generic should be collapsed away, button promoted as direct child of WebArea
    assert root.role == "WebArea"
    assert len(root.children) == 1
    assert root.children[0].role == "button"


def test_keeps_labeled_generic():
    snapshot = {
        "nodes": [
            {"nodeId": "1", "role": _v("WebArea"), "childIds": ["2"]},
            {
                "nodeId": "2",
                "role": _v("generic"),
                "properties": [{"name": "aria-label", "value": _v("Settings panel")}],
                "childIds": [],
            },
        ]
    }
    root = normalize_snapshot(snapshot)
    assert root is not None
    assert any(c.role == "generic" for c in root.children)


def test_drops_redundant_statictext_child_of_labeled_button():
    snapshot = {
        "nodes": [
            {"nodeId": "1", "role": _v("WebArea"), "childIds": ["2"]},
            {"nodeId": "2", "role": _v("button"), "name": _v("Submit"), "childIds": ["3"]},
            {"nodeId": "3", "role": _v("StaticText"), "name": _v("Submit"), "childIds": []},
        ]
    }
    root = normalize_snapshot(snapshot)
    btn = root.children[0]
    assert btn.role == "button"
    assert btn.children == []
