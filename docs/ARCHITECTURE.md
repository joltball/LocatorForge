# Architecture

Design reference for LocatorForge. For usage see [README.md](../README.md); for development setup see [CONTRIBUTING.md](../CONTRIBUTING.md).

---

## Contents

- [System overview](#system-overview)
- [Components](#components)
- [Architecture decisions](#architecture-decisions)
- [Element discovery pipeline](#element-discovery-pipeline)
- [Locator resolution](#locator-resolution)
- [Repository search](#repository-search)
- [Agent protocol](#agent-protocol)
- [Data schemas](#data-schemas)
- [Performance targets](#performance-targets)
- [Known limitations](#known-limitations)

---

## System overview

Three processes cooperate on one machine:

```
┌──────────────┐              ┌────────────────────┐            ┌──────────────┐
│    Chrome    │              │   Python backend   │            │  Java Swing  │
│              │◄── CDP ─────►│                    │◄── REST ──►│      UI      │
│  target app  │  WebSocket   │  FastAPI + uvicorn │◄─── WS ────│              │
│              │   :9222      │       :8765        │  (push)    │              │
└──────────────┘              └─────────┬──────────┘            └──────────────┘
                                        │
                                        │ atomic writes
                                        ▼
                              <repo-root>/.locatorforge/
                                        │
                                        ▼
                              external AI agent edits POM
```

REST carries request/response. A single WebSocket carries server-push events only: `page_navigated`, `tree_updated`, `element_picked`, `element_recorded`, `browser_disconnected` / `browser_reconnected`.

Everything binds to `127.0.0.1`.

## Components

| Module | Responsibility |
|---|---|
| `cdp_engine.py` | Launch/attach Chrome, CDP WebSocket transport, per-frame execution-context tracking, reconnect with backoff |
| `ax_tree_processor.py` | Accessibility tree extraction, filtering, multi-frame assembly |
| `frame_traverser.py` | Frame enumeration, owner-element resolution, frame-chain construction |
| `shadow_dom_traverser.py` | Shadow host detection and piercing-chain generation |
| `locator_resolver.py` | Candidate generation, live validation, ranking, Selenium/Playwright emission |
| `interaction_recorder.py` | In-page capture script, cross-frame injection, element store |
| `repo_scanner.py` | 3-pass ripgrep search for Page Object files |
| `agent_ipc.py` | Atomic JSON writes, status state machine, command/ack handling |
| `code_block.py` | Literal code emission when `enable_code_block` is on |
| `api_server.py` | FastAPI app: REST endpoints + WebSocket |
| `cli.py` | Entry point, UI JAR fetch/cache, process lifecycle |

### API surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Connection status |
| GET | `/tree` | Current element tree |
| GET | `/locators/{node_id}` | Ranked candidates for a node |
| POST | `/highlight/{node_id}` | Highlight in browser |
| POST | `/element/pick` | Enter inspect mode |
| POST | `/validate` | Validate a custom locator |
| POST/GET/DELETE | `/record`, `/record/start`, `/record/stop` | Recording lifecycle |
| GET | `/targets` | Open browser tabs, marking the attached one |
| POST | `/targets/{id}/attach` | Manually attach to a specific window |
| GET/POST | `/pom/search`, `/pom/select` | Page Object detection |
| POST | `/push` | Write `output.json` |
| WS | `/ws` | Server-push events |

## Architecture decisions

Decisions are recorded as ADRs. Each is settled; revisit only with new evidence.

### ADR-01 — Bridge is FastAPI REST + one WebSocket
REST for request/response, a single WS for server push. Rejected Py4J (tight coupling, hard to debug) and gRPC (over-engineered for localhost single-machine use).

### ADR-02 — Shadow DOM support is full traversal
Detect hosts, recursively pierce with `pierce:true`, render boundaries as tree nodes, generate piercing locators. Max depth 5. Closed roots are not traversed (`traverse_closed: false`) — Chrome does not expose them, and pretending otherwise would produce locators that fail at runtime.

### ADR-03 — Hybrid accessibility API strategy
| Use case | API |
|---|---|
| Page tree | `getRootAXNode` + `getChildAXNodes` (lazy BFS) |
| Element picker | `getPartialAXTree({backendNodeId})` |
| Shadow subtrees | Scoped `getFullAXTree` per host |

**Revised from the original design.** `Accessibility.getSnapshot` was removed from modern Chrome. `getFullAXTree` over a whole page returns a multi-megabyte response that stalls the CDP socket on real applications. Lazy BFS keeps each round-trip small and the UI responsive; the element picker uses `getPartialAXTree` because it returns nodes Chrome's `interestingOnly` filter would drop.

### ADR-04 — Selenium is the default format, Playwright on demand
Default rendering and `output.json` format is Selenium `@FindBy`. Double-clicking a locator toggles that **one element** to Playwright syntax. Both formats are always computed internally regardless of what is displayed.

### ADR-05 — Page Object generation is out of scope
If no Page Object is found, prompt for a path. The tool modifies; it does not create classes from scratch.

### ADR-06 — `code_block` output is built but disabled by default
When `enable_code_block: true`, `output.json` carries literal code lines plus find/replace patterns so an agent can apply changes with no code generation. Default remains `false`; the structured fields are sufficient for a capable agent and are far less brittle.

### ADR-07 — Iframe support uses `frameId`, not OOPIF sessions
Child frames are walked by passing `frameId` to the accessibility calls on the existing page session.

Cross-origin out-of-process iframes (OOPIF) would require `Target.setAutoAttach` plus `sessionId` demultiplexing inside the CDP read loop. **Measured on the reference application: all 15 child frames were reachable via `frameId`, zero OOPIF.** The hosts differ but share an eTLD+1, and Chrome isolates by *site*, not origin. OOPIF support was therefore descoped — it would add risk to the transport layer for no measured benefit. Revisit only if an application proves it necessary.

Instrumentation frames (fingerprinting, tag managers, `about:blank`, `chrome-error://`) are filtered out so the tree is not littered with empty boundaries.

### ADR-08 — Locators anchor to descendants, and validation cannot drift from display
Two coupled decisions.

**Descendant anchoring.** Component frameworks put the role on a wrapper and the identity on a descendant. On the reference application a `mat-card[role=link]` had no usable attribute of its own; every generated locator was either non-unique or dead. Candidates are now also built from a descendant identifier, scored breadth-first with a bonus for identity tags (`*title*`, `*label*`, `h1`–`h6`) and a penalty for state elements (`icon`, `tooltip`, `badge`, `status`). Without that weighting a shared status icon at equal depth wins on document order and yields a non-unique locator.

Unstable anchors are rejected outright: `_ngcontent-*`, `_nghost-*`, `ng-reflect-*`, `data-v-*`, and auto-generated `mat-*NNN` / `cdk-*NNN` identifiers.

Text anchors reject volatile content (currency, high digit ratios) so a locator cannot break on the next data refresh.

**Validation integrity.** Every candidate carries a `validation_expr` and `validation_kind` set from the *same string* used for its displayed locator, and validation branches on the expression **kind**, never on the strategy name. This closed a defect class where the `role` strategy displayed an XPath but validated a `[role=…]` CSS selector, confidently reporting "13 matches" for a locator that resolved to nothing. Strategy names do not imply a syntax — several strategies emit XPath — so branching on them was structurally unsound.

Real tag and attributes are read from the DOM via `DOM.describeNode` rather than inferred from the accessibility role, because role-to-tag mapping is a heuristic that misleads on component frameworks.

### ADR-09 — Positional locators are a marked last resort
Some elements are genuinely indistinguishable: the reference application renders four cards with identical titles, tags, classes and attributes, differing only by (volatile) balances. When no candidate resolves uniquely, an index-pinned variant is generated:

```
(//mat-card[.//mat-card-title[@aria-label='…']])[3]      // Selenium
…locator("mat-card:has(…)").nth(2)                        // Playwright
```

The index is **measured**, not inferred: `DOM.resolveNode` yields a JS handle and `Runtime.callFunctionOn` performs an identity comparison to learn the true position. Accessibility-tree order and DOM order can disagree, and a wrong index silently targets the wrong element.

Parenthesisation is load-bearing: `(//x)[2]` is the second match overall, while `//x[2]` is every `x` that is the second child of its parent.

Safety rails: generated only when nothing is naturally unique; ranked strictly below any stable unique locator; surfaced to the UI via `is_positional` so testers see the risk before adopting one.

### ADR-10 — The engine follows the target, not the tab
A page target is not permanent. Applications that open a popup and close the
opener replace the target entirely, with a different `targetId`. Binding to one
target for the process lifetime meant the tool died with the old window.

A **browser-level** CDP connection (separate from the page connection) subscribes
to `Target.setDiscoverTargets`. It has to be separate: when the page target dies
its socket dies with it, so target lifecycle cannot be observed from there.

On `Target.targetDestroyed` for the attached target, the engine polls briefly for
a replacement — the popup may be created slightly before or after the opener
closes — then rebinds: old socket and reader torn down, in-flight calls failed
rather than left hanging, frame contexts cleared (they are target-scoped), and
listeners notified.

Consumers must treat a target change as total invalidation. `api_server` clears
the node caches and re-runs discovery; `interaction_recorder.reinstall()`
re-registers its capture script, because `addScriptToEvaluateOnNewDocument` was
registered against the *old* target and does not carry over.

Reconnect distinguishes the two reasons a socket closes: a transient drop (same
target still listed — reattach to it) versus target destruction (re-pick). Doing
otherwise would reattach to a dead `targetId` forever.

Automatic selection prefers the most recently created real page target.
`POST /targets/{id}/attach` is the manual override for when several windows are
open and the wrong one wins.

## Element discovery pipeline

```
Accessibility.getRootAXNode ──► BFS via getChildAXNodes ──► normalize/filter
        (per frame)                  (bounded)                     │
                                                                   ▼
Page.getFrameTree ──► classify frames ──► walk each ──────► splice under
                       + resolve owner                     🖼 boundary nodes
                                                                   │
DOM.getDocument(pierce) ──► shadow hosts ──► scoped AX ────► splice under
                                                             ⚡ boundary nodes
```

**Filtering rules.** Drop `none`, `presentation`, `generic` unless aria-labelled. Drop redundant `StaticText` children of labelled interactive elements. Keep landmark roles as structural grouping nodes. Ignored placeholder nodes are retained during traversal — they are needed to follow `childIds` links — and collapsed afterwards.

**Bounds.** `MAX_NODES` is a **per-frame** budget so frames cannot starve one another; `MAX_DEPTH` guards pathological nesting.

**Auto-refresh** is triggered by top-level `Page.frameNavigated` only. Sub-frame events fire dozens of times during a normal page load; each would queue a tree rebuild.

### A note on the CDP read loop

CDP event handlers are dispatched as background tasks, never awaited inline. Awaiting a slow handler inside the read loop blocks it, Chrome's responses queue unread in the socket buffer, and every in-flight request times out. This produced a hard-to-diagnose deadlock during development and is the reason `_safe_dispatch` exists.

## Locator resolution

```
DOM.describeNode (real tag + attributes, depth-limited subtree)
        │
        ▼
build candidates ──► rank by config priority ──► apply shadow chain
        │
        ▼
validate each against the live page (frame-scoped where applicable)
        │
        ▼
add positional variants (only if nothing is unique)
        │
        ▼
re-rank by measured reality ──► apply frame chain ──► return
```

Ranking buckets, best first:

1. Unique and stable
2. Unique but positional
3. Multiple matches
4. Unknown (validation failed)
5. Zero matches

Configured `locators.priority` breaks ties **within** a bucket. It cannot promote a locator that does not resolve.

Frame-scoped validation runs inside the frame's own execution context via `Page.createIsolatedWorld`; `DOM.querySelectorAll` is main-frame only and would report zero matches for every in-frame element.

## Repository search

Three ripgrep passes, combined and ranked:

1. **URL-based** — search source directories for the current URL's path segment
2. **Annotation-based** — `@PageUrl`, `@FindBy`, `page_url`, `BASE_URL`, narrowed by declared URL
3. **Naming convention** — `class <UrlSegment>.*Page`

Ambiguous results are presented as a ranked list. Zero results prompts for a manual path; no file is generated.

## Agent protocol

```
tool: write output.json ──► status.json = output_ready
                                    │
agent: poll status.json ────────────┘
       read output.json
       apply changes to the Page Object
       write ack.json
                                    │
tool: observe ack.json ◄────────────┘
      confirm in UI, status.json = idle
```

The agent may also write `command.json` with `refresh`, `terminate`, or `navigate <url>`. Commands are consumed (deleted) after being read. All writes use temp-file-plus-rename.

## Data schemas

### `output.json`

```json
{
  "version": "1.2",
  "timestamp": "ISO-8601",
  "pom_file": "src/test/java/pages/LoginPage.java",
  "pom_framework": "selenium-java",
  "enable_code_block": false,
  "modifications": [
    {
      "action": "update",
      "element_name": "usernameField",
      "page": "LoginPage",
      "page_url": "https://app.example.com/login",
      "locator_format": "selenium",
      "old_locator": { "strategy": "id", "value": "user-input" },
      "new_locator": { "strategy": "data-testid", "value": "login-username" },
      "annotation_format": "@FindBy(css = \"[data-testid='login-username']\")",
      "shadow_chain": [],
      "frame_chain": [
        { "frame_id": "…", "url": "…", "selector": "iframe[name='Main']", "is_oopif": false }
      ],
      "line_hint": 24,
      "insert_after": "usernameField",
      "element_type": "interactive",
      "access_modifier": "private",
      "code_block": null,
      "insert_after_pattern": null,
      "replace_pattern": null
    }
  ]
}
```

**Field rules**
- `old_locator` is required for `action: update`, omitted for `add`
- `insert_after`, `element_type`, `access_modifier` are required for `action: add`
- `shadow_chain` / `frame_chain` are `[]` when not applicable
- `code_block`, `insert_after_pattern`, `replace_pattern` are `null` unless `enable_code_block: true`

### `status.json`

```json
{
  "status": "idle | pending | output_ready | error | terminated",
  "last_updated": "ISO-8601",
  "current_url": "string",
  "detected_pom": "string | null",
  "shadow_hosts_detected": 0,
  "error_message": "string | null"
}
```

### `command.json` / `ack.json`

```json
{ "command": "refresh | terminate | navigate", "arg": "url-if-navigate" }
```

```json
{ "status": "applied", "applied_changes": ["usernameField", "dateInput"] }
```

## Performance targets

| Operation | Target |
|---|---|
| Tree extraction + display, ≤500 elements | ≤ 3 s |
| ripgrep search, ≤100k files | ≤ 1 s |
| `GET /tree` | ≤ 200 ms (cached) |
| WebSocket event delivery | ≤ 100 ms |
| Shadow host traversal, ≤200 elements | ≤ 500 ms |
| UI memory, ≤1000 elements | ≤ 256 MB |

## Known limitations

**Cross-origin (OOPIF) iframes are not traversed.** See ADR-07. Content in a genuinely cross-site iframe will not appear in the tree.

**Closed Shadow DOM roots are not pierced.** Chrome does not expose them to CDP.

**Positional locators are order-dependent.** `(…)[3]` silently targets a different element if the list re-sorts, filters or pages. The real fix is asking the application team for a `data-testid`.

**`descendant-text` uses `contains()`**, a substring match. Two elements whose titles share a prefix will both match; the reported match count surfaces this, and `descendant-attr` is preferred when an anchor attribute exists.

**Selenium `@FindBy` cannot express frame switching.** In-frame locators carry the required `switchTo()` sequence as a comment; the machine-readable `frame_chain` in `output.json` is what an agent should consume. Playwright has no such limitation.

**Recording requires frames to be loaded.** The capture script is injected into every frame that exists when recording starts, and into new documents as they load. A frame that is torn down and rebuilt mid-interaction may miss the first event.

**`page` identity for in-frame elements** derives from the frame's own URL, not the top-level application route, so recorded elements from an iframe group under the frame's page key.
