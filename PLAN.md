# LocatorForge — Implementation Plan

This file mirrors the approved plan and breaks the work into resumable steps. Progress is tracked in [PROGRESS.md](PROGRESS.md). All architecture decisions are **locked** by [SPEC.md](SPEC.md) and must not be re-litigated.

> **Resumability rule:** every Python module includes a top-of-file `# PHASE: N.x` comment so a fresh agent can map code back to a step. After completing any numbered step, tick it in `PROGRESS.md`.

---

## Repository Layout (target)

```
LocatorForge/
├── SPEC.md                      (exists)
├── PLAN.md                      (this file)
├── PROGRESS.md                  (checklist, updated continuously)
├── README.md                    (Phase 4)
├── .gitignore
├── pyproject.toml               (Phase 0 skeleton; finalized Phase 4)
├── locatorforge.yaml.example    (Phase 2)
├── python/
│   └── locatorforge/
│       ├── __init__.py
│       ├── __main__.py
│       ├── cli.py
│       ├── config.py
│       ├── cdp_engine.py
│       ├── ax_tree_processor.py
│       ├── shadow_dom_traverser.py
│       ├── locator_resolver.py
│       ├── repo_scanner.py
│       ├── agent_ipc.py
│       ├── api_server.py
│       └── schemas.py
├── java-ui/
│   ├── pom.xml
│   └── src/main/java/com/citi/qa/locatorforge/ui/
│       ├── Main.java
│       ├── api/{ApiClient.java, WsClient.java}
│       ├── model/
│       ├── tree/{ElementTreePanel.java, ShadowBoundaryNode.java}
│       ├── editor/LocatorEditorPanel.java
│       └── toolbar/MainToolBar.java
└── tests/python/
```

---

## Phase 0 — Scaffolding & Tracking

1. Create `PLAN.md` (this file).
2. Create `PROGRESS.md` (unchecked checklist).
3. Create `.gitignore` (`.locatorforge/`, `dist/`, `build/`, `target/`, `*.pyc`, `__pycache__/`, `.venv/`, `*.egg-info/`, cached `*.jar`).
4. Create `python/locatorforge/__init__.py` (`__version__ = "1.2.0"`) and `__main__.py`.
5. Create `pyproject.toml` skeleton.
6. Create `java-ui/pom.xml` skeleton.

## Phase 1 — Core Loop (MVP)

### 1.1 Python — CDP & tree
- `cdp_engine.py` — launch/attach Chrome on `--remote-debugging-port=9222`; async CDP WebSocket with `send(method, params)` and `on_event(method, handler)`; typed exceptions for port-busy / chrome-not-found.
- `ax_tree_processor.py` — `Accessibility.getSnapshot({interestingOnly: true})` → filter `none|presentation|generic` (unless aria-labelled) → drop redundant `StaticText` children of labelled interactives → keep landmark roles. Output normalized `{nodeId, role, name, attributes, children}` tree.
- `schemas.py` — pydantic models for SPEC §6 (`OutputJson`, `Modification`, `StatusJson`, `CommandJson`, `AckJson`).
- `agent_ipc.py` (minimal) — atomic temp+rename writer for `output.json`/`status.json` under `<repo_root>/.locatorforge/`.

### 1.2 Python — API
- `api_server.py` (minimal) — FastAPI on `127.0.0.1:8765`; routes `GET /health`, `GET /tree`, `POST /push`.
- `cli.py` (stub) — argparse: `--repo-root`, `--port` (CDP), `--api-port`; starts CDP engine + uvicorn; prints UI launch hint.

### 1.3 Java UI
- `Main.java` — FlatLaf init, parse `--api-port`, build `JFrame` with toolbar + tree + status bar.
- `ApiClient.java` — `java.net.http.HttpClient` GET/POST for `/health`, `/tree`, `/push`.
- `ElementTreePanel.java` — `JTree` populated from `/tree`, role-name + Selenium `@FindBy(...)` label.
- Toolbar buttons: **Refresh** (re-GET `/tree`), **Push to POM** (POST `/push`).

### 1.4 Smoke verification
- `python -m locatorforge --repo-root .` → `/health` ok.
- `mvn -f java-ui/pom.xml package` → JAR present.
- Launch UI → tree populated for `https://example.com` → Push → `.locatorforge/output.json` validates.

## Phase 2 — Full Locator Suite + Live Editing + Config

### 2.1 Python
- `config.py` — YAML loader; precedence: `--config` flag → repo root → `~/.locatorforge/config.yaml` → defaults.
- `locator_resolver.py` — strategies `data-testid, aria-label, id, name, css, xpath`; rank by `locators.priority`; live-validate via `DOM.querySelectorAll`; produce **both** Selenium `@FindBy(...)` and Playwright (`getByRole`/`getByTestId`/`page.locator`) for every node.
- API additions: `GET /locators/{nodeId}`, `POST /highlight/{nodeId}`, `POST /element/pick`, `POST /validate`.
- WebSocket `/ws` — push events `page_navigated`, `tree_updated`, `element_picked`, `browser_disconnected`, `browser_reconnected`.
- Element picker uses `Accessibility.getPartialAXTree({nodeId})`.

### 2.2 Java UI
- `LocatorEditorPanel.java` — candidates with uniqueness badges; custom-locator entry with `/validate` debounce; double-click toggles Selenium↔Playwright per element.
- **Add Element** toolbar button → `POST /element/pick`; on WS `element_picked` append italic under "Verification Elements".
- `WsClient.java` — Java 17 `WebSocket`; reconnect backoff 1→2→4→max 30s.
- Search/filter bar above tree (name/role/locator).

## Phase 3 — Repo Awareness + Shadow DOM L3

### 3.1 Repo scanner
- `repo_scanner.py` — `rg --json` 3-pass per SPEC §7; combine + rank; return ranked candidates.
- API: `GET /pom/search`, `POST /pom/select`.

### 3.2 Agent IPC (full)
- Status transitions `idle → pending → output_ready → idle` (or `error`/`terminated`).
- Watch `.locatorforge/command.json` (`refresh|terminate|navigate`); delete after consume.
- Watch `.locatorforge/ack.json`; on `applied` → UI toast + `status: idle`.
- Auto-append `.locatorforge/` to project `.gitignore` on first write.

### 3.3 Full output.json
- Populate every SPEC §6.1 field including `shadow_chain`, `line_hint`, `insert_after`, `element_type`, `access_modifier`. `code_block`/`insert_after_pattern`/`replace_pattern` stay `null`.
- UI enforces required fields for `action: add`.

### 3.4 Shadow DOM L3
- `shadow_dom_traverser.py` — `DOM.getDocument({depth:-1, pierce:true})`; detect open hosts; scoped `Accessibility.getFullAXTree({backendNodeId})`; tag descendants with ordered `shadowAncestors`; enforce `max_depth: 5`; honor `traverse_closed: false`.
- `locator_resolver` extension — Selenium chained `getShadowRoot()` and Playwright `>>>` combinator when `shadowAncestors` non-empty.
- Java UI — `ShadowBoundaryNode` with ⚡ icon, label `host-tag [shadow-root: open]`.

## Phase 4 — Polish, code_block, Distribution

### 4.1 Resilience
- CDP reconnect (1→2→4→max 30s); WebSocket client mirrors.
- Multi-tab: `Target.getTargets` + UI tab switcher.

### 4.2 `code_block` path (ADR-06)
- Built but `agent_output.enable_code_block: false` by default.
- `update` → populate `replace_pattern` + `code_block`.
- `add` → populate `insert_after_pattern` + `code_block`.
- Shadow `add` → `code_block` contains a full helper method (Selenium getter / Playwright wrapper); agent never writes traversal logic.

### 4.3 FlatLaf polish
- Role icons under `java-ui/src/main/resources/icons/`; light/dark toggle; keyboard nav (arrows, Enter, F2); tooltips.

### 4.4 Distribution (SPEC §10)
- `pyproject.toml` — finalize deps, `[project.scripts] locatorforge = "locatorforge.cli:main"`, `[tool.setuptools.packages.find]` rooted at `python/`.
- `java-ui/pom.xml` — `maven-shade-plugin` produces `locatorforge-ui-1.2.0.jar` with `Main-Class`.
- `cli.py` — implement `ensure_ui_jar()` against Artifactory; cache `~/.locatorforge/cache/`; atomic temp-rename.

### 4.5 Docs
- `README.md` — install, run, config snippet, troubleshooting.

---

## Cross-Cutting

- **Localhost only:** every `bind_host` is `127.0.0.1`.
- **No content capture:** role/name/attributes only — never page text or screenshots.
- **Atomic writes:** `agent_ipc._atomic_write` is the only IPC writer.
- **Tests** (`tests/python/`): per-phase pytest modules — filters (P1), resolver ranking + dual-format (P2), ripgrep parser + shadow chain (P3), code_block emitter (P4).
- **Perf gates:** SPEC §11 sample checks at end of Phase 3.

## Verification (per phase)

See SPEC §11 and the phase **Exit criteria** in the original plan write-up at `~/.claude/plans/create-plan-to-implement-spicy-allen.md`. The end-to-end demos:
- **P1:** `/health`+JTree+`output.json` on `example.com`.
- **P2:** ranked candidates + live validation + element picker + Selenium↔Playwright per-element toggle.
- **P3:** ripgrep finds a sample `LoginPage.java`; shadow page yields valid chain.
- **P4:** `enable_code_block: true` round-trip; clean-venv `pip install` exposes `locatorforge`; `mvn package` shaded jar.
