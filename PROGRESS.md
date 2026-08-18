# LocatorForge — Implementation Progress

Tick boxes as steps complete. A fresh agent should resume from the first unchecked item under the lowest-numbered unfinished phase.

Legend: `[ ]` pending · `[~]` in progress · `[x]` done · `[!]` blocked (see note)

---

## Phase 0 — Scaffolding & Tracking

- [x] 0.1 Create `PLAN.md`
- [x] 0.2 Create `PROGRESS.md`
- [x] 0.3 Create `.gitignore`
- [x] 0.4 Create `python/locatorforge/__init__.py` + `__main__.py`
- [x] 0.5 Create `pyproject.toml` skeleton
- [x] 0.6 Create `java-ui/pom.xml` skeleton

## Phase 1 — Core Loop (MVP)

### 1.1 Python — CDP & tree
- [x] 1.1.1 `cdp_engine.py` — launch/attach + CDP WebSocket helpers
- [x] 1.1.2 `ax_tree_processor.py` — `getSnapshot` + filtering
- [x] 1.1.3 `schemas.py` — pydantic models for SPEC §6
- [x] 1.1.4 `agent_ipc.py` minimal — atomic writer + write `output.json`/`status.json`

### 1.2 Python — API
- [x] 1.2.1 `api_server.py` — `GET /health`, `GET /tree`, `POST /push`
- [x] 1.2.2 `cli.py` stub — argparse + start backend

### 1.3 Java UI
- [x] 1.3.1 `Main.java` — JFrame, FlatLaf, args
- [x] 1.3.2 `ApiClient.java` — REST GET/POST
- [x] 1.3.3 `ElementTreePanel.java` — JTree (+ `ElementNode.java`)
- [x] 1.3.4 `MainToolBar.java` — Refresh + Push buttons

### 1.4 Smoke verification
- [~] 1.4.1 `python -m locatorforge --repo-root .` health-checks — modules `py_compile`-clean; live run requires Chrome+pip-install
- [ ] 1.4.2 UI displays tree from `https://example.com` (needs `mvn package` + Chrome)
- [ ] 1.4.3 Push writes valid `output.json` (covered by `tests/python/test_agent_ipc.py`)

## Phase 2 — Full Locator Suite + Live Editing + Config

### 2.1 Python
- [x] 2.1.1 `config.py` — YAML loader + precedence
- [x] 2.1.2 `locator_resolver.py` — all strategies + ranking + dual format
- [x] 2.1.3 API: `GET /locators/{nodeId}`
- [x] 2.1.4 API: `POST /highlight/{nodeId}`
- [x] 2.1.5 API: `POST /element/pick` + `getPartialAXTree`
- [x] 2.1.6 API: `POST /validate`
- [x] 2.1.7 WebSocket `/ws` push events

### 2.2 Java UI
- [x] 2.2.1 `LocatorEditorPanel.java`
- [x] 2.2.2 Per-element Selenium↔Playwright toggle
- [x] 2.2.3 Add Element button → element picker flow
- [x] 2.2.4 `WsClient.java` with backoff
- [x] 2.2.5 Search/filter bar
- [x] 2.2.6 `locatorforge.yaml.example`

## Phase 3 — Repo Awareness + Shadow DOM L3

### 3.1 Repo scanner
- [x] 3.1.1 `repo_scanner.py` 3-pass ripgrep
- [x] 3.1.2 API: `GET /pom/search`, `POST /pom/select`

### 3.2 Agent IPC (full)
- [x] 3.2.1 Full status state machine
- [x] 3.2.2 `command.json` watcher
- [x] 3.2.3 `ack.json` watcher + UI toast (broadcast on `agent_ack`)
- [x] 3.2.4 Auto-append `.locatorforge/` to `.gitignore`

### 3.3 Full output.json
- [x] 3.3.1 Populate full SPEC §6.1 (sans `code_block`)
- [~] 3.3.2 UI enforces `action: add` required fields — current UI emits only `update`; `add` is reachable when a verification element is selected after the picker (helper dialog deferred to Phase 4 polish)

### 3.4 Shadow DOM L3
- [x] 3.4.1 `shadow_dom_traverser.py`
- [x] 3.4.2 `locator_resolver` shadow extensions (Selenium chain + Playwright `>>>`)
- [x] 3.4.3 `ShadowBoundaryNode` in JTree

## Phase 4 — Polish, code_block, Distribution

### 4.1 Resilience
- [x] 4.1.1 CDP reconnect backoff (1s→2s→4s→max 30s)
- [x] 4.1.2 Multi-tab — `/targets` endpoint enumerates page targets via CDP HTTP discovery
- [ ] 4.1.3 (deferred) WS-client reconnect already in place from Phase 2 (`WsClient.scheduleReconnect`)

### 4.2 code_block path
- [x] 4.2.1 `update` mode emitter
- [x] 4.2.2 `add` mode emitter
- [x] 4.2.3 Shadow `add` helper-method emitter

### 4.3 FlatLaf polish
- [~] 4.3.1 Role icons resources — placeholder character icons (⚡ for shadow); image icons deferred
- [~] 4.3.2 Light/dark toggle + keyboard nav — JTree default keyboard nav active; theme toggle deferred

### 4.4 Distribution
- [x] 4.4.1 Finalize `pyproject.toml` (deps, `[project.scripts]`, `setuptools.packages.find` rooted at `python/`)
- [x] 4.4.2 Finalize `pom.xml` shade
- [x] 4.4.3 `cli.ensure_ui_jar()` Artifactory fetch + cache + atomic rename

### 4.5 Docs
- [x] 4.5.1 `README.md`

---

## Notes / Blockers

- **`tests/python/test_repo_scanner.py`** is auto-skipped on machines without `rg` on PATH. The local dev box does not have ripgrep installed; install it (`scoop install ripgrep` / `choco install ripgrep` / `apt install ripgrep`) and rerun to validate the 3-pass POM search end-to-end.
- **UI auto-launch** depends on the Artifactory mirror or `--ui-jar`. For local development run `mvn -f java-ui/pom.xml package`, then `locatorforge --repo-root . --ui-jar java-ui/target/locatorforge-ui-1.2.0.jar`.
- **FlatLaf icon polish (4.3)** is intentionally minimal — the spec's exit criteria are met with text-glyph icons. A future polish pass can drop PNGs under `java-ui/src/main/resources/icons/` and theme toggle hook into FlatLaf's `FlatDarkLaf`.
- **WebSocket DeprecationWarning** in `websockets>=14`: `WebSocketClientProtocol` import path will move. Track for a Phase 5 dep refresh; current code still functions.
- **`add`-action UI flow (3.3.2)**: today the "Push to POM" toolbar emits only `action: update`. A future pass should surface an "Add to POM" sub-menu that captures `insert_after` + `element_type` + `access_modifier` explicitly before pushing.
