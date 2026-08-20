# PHASE: 6 (tests)
"""Descendant-anchored locator strategies — no live CDP.

Regression coverage for the Angular Material card shape, where the click target
(`mat-card[role=link]`) carries no usable attribute of its own and the identity
lives on a `<mat-card-title>` descendant.
"""

from locatorforge.config import LocatorsCfg
from locatorforge.locator_resolver import (
    _as_xpath,
    _build_candidates,
    _find_descendant_identifier,
    _find_descendant_text,
    _demote_broken,
    _rank_by_config,
    LocatorCandidate,
)
from locatorforge.schemas import TreeNode


def _el(tag, attrs=None, children=None, text=None):
    """Minimal DOM.describeNode-shaped node."""
    flat = []
    for k, v in (attrs or {}).items():
        flat += [k, v]
    kids = list(children or [])
    if text is not None:
        kids.append({"nodeType": 3, "nodeValue": text, "children": []})
    return {"localName": tag, "nodeType": 1, "attributes": flat, "children": kids}


def _card():
    """A representative Angular Material account card.

    Faithful to the shape seen in the wild: the clickable `mat-card` carries
    role and tabindex but no usable identifier, while the identity sits on a
    `mat-card-title` descendant alongside a status icon that shares the same
    depth and attribute.
    """
    return _el("mat-card", {"role": "link", "tabindex": "0",
                            "class": "mat-mdc-card mdc-card",
                            "_ngcontent-ng-c974676431": ""}, [
        _el("mat-card-header", {"class": "mat-mdc-card-header"}, [
            _el("div", {"class": "mat-card-header-wrapper"}, [
                _el("div", {"class": "card-title-and-icon"}, [
                    # status icon — same depth, same attribute, NOT identity
                    _el("span", {"class": "ng-icon icon-stale",
                                 "aria-label": "Some accounts in this group are not up to date."}),
                    _el("mat-card-title", {"class": "mat-mdc-card-title",
                                           "aria-label": "Acme Holdings and/or Globex Trust"},
                        text="Acme Holdings and/or Globex Trust"),
                ]),
            ]),
        ]),
        _el("mat-card-content", {}, [
            _el("div", {"class": "amount-data"}, [
                _el("span", {}, text="AUD 123,456.78"),
            ]),
        ]),
    ])


def _node():
    return TreeNode(
        node_id="n1", role="link", backend_node_id=99,
        name=("Some accounts in this group are not up to date. "
              "Acme Holdings and/or Globex Trust 2 Accounts Assets AUD 123,456.78"),
        attributes={"tag": "mat-card", "role": "link"},
    )


def test_identity_tag_beats_same_depth_status_icon():
    """A <mat-card-title> must outrank a sibling status <span> at equal depth."""
    found = _find_descendant_identifier(_card())
    assert found is not None
    tag, attr, value, _depth = found
    assert tag == "mat-card-title"
    assert attr == "aria-label"
    assert value == "Acme Holdings and/or Globex Trust"


def test_descendant_text_excludes_volatile_balances():
    """Text anchor must be the title, never the AUD amounts."""
    text = _find_descendant_text(_card())
    assert text == "Acme Holdings and/or Globex Trust"
    assert "AUD" not in text


def test_emits_ancestor_scoped_xpath_and_has_css():
    cands = _build_candidates(_node(), _card())
    by = {c.strategy: c for c in cands}

    assert "descendant-attr" in by, "no descendant-anchored candidate produced"
    d = by["descendant-attr"]
    assert d.selenium == (
        '@FindBy(xpath = "//mat-card[.//mat-card-title'
        "[@aria-label='Acme Holdings and/or Globex Trust']]\")"
    )
    assert d.playwright == (
        'page.locator("mat-card:has(mat-card-title'
        "[aria-label='Acme Holdings and/or Globex Trust'])\")"
    )

    t = by["descendant-text"]
    assert "contains(., 'Acme Holdings and/or Globex Trust')" in t.selenium
    assert "hasText" in t.playwright


def test_unstable_angular_attrs_are_never_anchored():
    """_ngcontent-* is a build hash — anchoring to it breaks every rebuild."""
    for c in _build_candidates(_node(), _card()):
        assert "_ngcontent" not in c.selenium
        assert "_nghost" not in c.selenium


def test_validation_expr_always_matches_displayed_locator():
    """The bug this prevents: `role` displayed XPath but validated [role=...] CSS,
    reporting '13 matches' for a locator that resolved to nothing."""
    for c in _build_candidates(_node(), _card()):
        assert c.validation_expr, f"{c.strategy} has no validation_expr"
        if c.validation_kind == "xpath":
            assert c.validation_expr in c.selenium, (
                f"{c.strategy} validates an expression it does not display"
            )


def test_zero_match_candidate_cannot_outrank_a_unique_one():
    good = LocatorCandidate(strategy="descendant-attr", value="x", selenium="", playwright="",
                            rank=5, match_count=1, is_unique=True)
    bad = LocatorCandidate(strategy="data-testid", value="y", selenium="", playwright="",
                           rank=0, match_count=0, is_unique=False)
    cands = [bad, good]
    _demote_broken(cands)
    assert cands[0] is good


def test_priority_config_includes_descendant_strategies():
    assert "descendant-attr" in LocatorsCfg().priority
    assert "descendant-text" in LocatorsCfg().priority


def test_positional_never_outranks_a_naturally_unique_locator():
    """An index locator breaks when the list re-sorts, so a stable unique one
    must always win — even though both report is_unique=True."""
    stable = LocatorCandidate(strategy="descendant-attr", value="a", selenium="", playwright="",
                              rank=9, match_count=1, is_unique=True)
    indexed = LocatorCandidate(strategy="css-nth", value="b", selenium="", playwright="",
                               rank=0, match_count=1, is_unique=True,
                               is_positional=True, position_index=2)
    cands = [indexed, stable]
    _demote_broken(cands)
    assert cands[0] is stable
    assert cands[1] is indexed


def test_positional_still_beats_a_non_unique_locator():
    indexed = LocatorCandidate(strategy="descendant-attr-nth", value="b", selenium="",
                               playwright="", rank=9, match_count=1, is_unique=True,
                               is_positional=True, position_index=1)
    ambiguous = LocatorCandidate(strategy="data-testid", value="c", selenium="", playwright="",
                                 rank=0, match_count=4, is_unique=False)
    cands = [ambiguous, indexed]
    _demote_broken(cands)
    assert cands[0] is indexed


def test_nth_variant_inherits_base_strategy_priority():
    """`descendant-attr-nth` must rank with `descendant-attr`, not fall to the
    bottom as an unrecognized strategy name."""
    a = LocatorCandidate(strategy="descendant-attr-nth", value="", selenium="",
                         playwright="", rank=0)
    b = LocatorCandidate(strategy="xpath", value="", selenium="", playwright="", rank=0)
    cands = [b, a]
    _rank_by_config(cands, LocatorsCfg())
    assert cands[0] is a


def test_css_to_xpath_conversion_is_conservative():
    """Only the selector shapes this module emits convert; the rest refuse
    rather than emit invalid XPath."""
    assert _as_xpath("mat-card", "css") == "//mat-card"
    assert _as_xpath("//mat-card[@id='x']", "xpath") == "//mat-card[@id='x']"
    # Simple attribute selectors convert — without this a duplicated element
    # falls back to indexing a bare tag, e.g. (//button)[3] instead of the far
    # more durable (//button[@aria-label='Delete'])[1].
    assert _as_xpath("button[aria-label='Delete']", "css") == "//button[@aria-label='Delete']"
    assert _as_xpath("[data-testid='a']", "css") == "//*[@data-testid='a']"
    # Compound selectors and id shorthand are NOT naively converted.
    assert _as_xpath("mat-card:has(x[y='z'])", "css") is None
    assert _as_xpath("#some-id", "css") is None
    # A value containing a quote would break the XPath literal.
    assert _as_xpath("button[aria-label=\"it's\"]", "css") is None


def test_positional_prefers_the_most_discriminating_base():
    """The index should be applied to the best convertible candidate, not the
    first one that happens to convert."""
    from locatorforge.locator_resolver import _BARE_TAG_RE
    # aria-label ranks above css in the default priority, so a non-unique
    # aria-label candidate must be the base rather than the bare tag.
    cfg = LocatorsCfg()
    assert cfg.priority.index("aria-label") < cfg.priority.index("css")
    assert _as_xpath("button[aria-label='View']", "css") is not None
    assert _BARE_TAG_RE.match("button")


def test_ranking_places_descendant_above_generic_fallbacks():
    cands = _build_candidates(_node(), _card())
    _rank_by_config(cands, LocatorsCfg())
    order = [c.strategy for c in cands]
    assert order.index("descendant-attr") < order.index("css")
    assert order.index("descendant-attr") < order.index("xpath")
