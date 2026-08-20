# Changelog

All notable changes to LocatorForge are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.2.0]

First broadly released version. Covers the full workflow: element discovery, locator generation, recording, and agent handoff.

### Added

**Element discovery**
- Interactive element tree from Chrome's accessibility layer, with role/name/attribute metadata
- **Iframe traversal** — same-process child frames are walked and spliced into the tree beneath 🖼 boundary nodes, with instrumentation frames (fingerprinting, tag managers, blank/error frames) filtered out
- **Shadow DOM traversal** for open roots, configurable depth, rendered beneath ⚡ boundary nodes
- Element picker for verification points Chrome's `interestingOnly` filter would otherwise drop
- Multi-tab discovery and search/filter across the tree

**Locator generation**
- Strategies: `data-testid`, `aria-label`, `id`, `name`, `descendant-attr`, `descendant-text`, `role`, `css`, `xpath`
- Live uniqueness validation against the running page, frame-scoped where applicable
- Ranking by measured result, so a non-resolving locator can never outrank a working one
- **Descendant-anchored locators** for component frameworks — `//mat-card[.//mat-card-title[@aria-label='…']]` and the CSS `:has()` equivalent
- **Positional fallback** — `(…)[3]` / `.nth(2)` when elements are genuinely indistinguishable, flagged as order-dependent
- Selenium `@FindBy` and Playwright syntax emitted for every element, with per-element toggle
- Shadow-piercing and frame-switching chains generated automatically

**Workflow**
- Interaction recorder capturing elements across frames and pages while you drive the app
- Page Object detection via a 3-pass ripgrep search
- File-based agent protocol: `output.json`, `status.json`, `command.json`, `ack.json`, all written atomically
- Optional `code_block` output mode for mechanical, generation-free application of changes
- YAML configuration with layered resolution
- Artifactory distribution — `pip install` plus automatic UI JAR fetch and cache

### Notable design decisions

- **Cross-origin (OOPIF) iframes are out of scope.** Measured against a real enterprise application, all child frames were reachable through `frameId` on the existing session; OOPIF support would have meant session demultiplexing in the CDP transport for no measured benefit. See ADR-07.
- **Validation is structurally bound to display.** Each candidate validates the exact expression it shows, and validation branches on expression *kind* rather than strategy name. See ADR-08.
- **Real tag and attributes are read from the DOM** rather than inferred from the accessibility role, which misleads on component frameworks.

### Known limitations

- Cross-origin iframe content is not traversed
- Closed Shadow DOM roots cannot be pierced
- Positional locators break if a list re-sorts, filters or pages
- `descendant-text` is a substring match, so shared title prefixes can collide
- Selenium `@FindBy` cannot express frame switching; the required `switchTo()` sequence is emitted as a comment and as a machine-readable `frame_chain`

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#known-limitations) for detail.

### Requirements

Python 3.10+, JDK 17+, Chrome 120+. ripgrep optional, required only for Page Object search. Windows 10+, macOS 12+, Ubuntu 20.04+.
