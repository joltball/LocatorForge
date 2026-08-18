# LocatorForge — Build Specification

**Version:** 1.2 | **Status:** Architecture finalized, ready for implementation
**Purpose of this doc:** Implementation-ready spec for an AI coding agent. All architecture decisions below are LOCKED — do not re-litigate them. Build in the phase order given in Section 9.

---

## 1. What This Tool Does

LocatorForge is a desktop utility that:
1. Launches/attaches to Chrome via CDP (debug mode)
2. Extracts a filtered, interactive element tree from the live page (including Shadow DOM)
3. Lets a human pick/edit locators in a Java Swing UI
4. Finds the matching POM file in the repo via ripgrep
5. Writes a structured JSON instruction file describing locator changes
6. An external AI coding agent (not part of this tool) polls for that file and applies the changes to the POM source file

**This tool never edits source files itself.** It only produces instructions. The calling agent does the file editing.

---

## 2. Tech Stack (Locked)

| Layer | Choice |
|---|---|
| Backend | Python 3.10+ |
| Browser control | Chrome DevTools Protocol (CDP) |
| Bridge (Python↔Java) | FastAPI REST + single WebSocket channel — **NOT Py4J, NOT gRPC** |
| Frontend UI | Java 17+, Swing, JTree, FlatLaf look-and-feel |
| Repo search | ripgrep (`rg`) via subprocess |
| Agent IPC | File-based JSON in `.locatorforge/` dir, agent polls |
| Config | YAML (`locatorforge.yaml`) |

### Rejected alternatives (do not implement)
- Py4J for the bridge (tight coupling, hard to debug)
- gRPC for the bridge (over-engineered for localhost single-machine use)
- POM class generation from scratch (deferred — modification only)

---

## 3. Architecture Decisions (ADRs) — All Locked

### ADR-01: Python↔Java Bridge = FastAPI REST + 1 WebSocket
- REST for request/response (get tree, resolve locator, search POM, highlight element)
- One WebSocket channel for server-push events only (page navigation, tree refresh, element picked)
- Both bind to `localhost` only — never expose externally
- Default ports: FastAPI `8765`, CDP `9222`

### ADR-02: Shadow DOM Support = Level 3 (full traversal)
- Detect shadow hosts → recursively traverse with `pierce:true` → render shadow boundaries as visible JTree nodes → generate shadow-piercing locators
- Max depth 5 (configurable), `traverse_closed: false` (closed shadow roots not traversed)

### ADR-03: CDP Accessibility API = Hybrid strategy
| Use case | API to call |
|---|---|
| Primary tree on page load/refresh | `Accessibility.getSnapshot({interestingOnly: true})` |
| Element picker (manual add, e.g. verification elements) | `Accessibility.getPartialAXTree({nodeId})` |
| Shadow DOM subtree enumeration | Scoped `Accessibility.getFullAXTree()` per shadow host, plus `DOM.describeNode({pierce:true})` |

Reason: `getFullAXTree` on the whole page is slow (1.5–3s) and over-fetches. `getSnapshot` is fast (<500ms) but its `interestingOnly` filter can drop non-interactive verification elements — hence `getPartialAXTree` is used specifically for the manual element picker to guarantee nothing is missed.

### ADR-04: Locator format = Selenium default, Playwright on double-click
- Default rendering and default `output.json` format: Selenium `@FindBy(...)`
- Double-clicking a locator in the editor panel toggles that element's display to Playwright style (`getByRole`, `getByTestId`, `getByLabel`, `page.locator()`)
- This is a **per-element UI toggle**, not a global setting
- Locator Resolver must compute both formats internally regardless of which is displayed

### ADR-05: POM class generation = OUT OF SCOPE for this build
- If no POM file is found for the current page, prompt user for a manual path
- Do NOT build code to generate new POM class files from scratch
- This may come in a future version (skeleton JSON handed to agent) — not now

### ADR-06: `code_block` output enhancement = build it, but disabled by default
- Add the feature to the output schema and the config
- Default config value: `enable_code_block: false`
- When `false`: omit/null the `code_block` and `insert_after_pattern`/`replace_pattern` fields; agent uses structured fields to generate code itself
- When `true`: populate `code_block` (array of literal code lines) and pattern fields so the agent can do pure mechanical find-and-insert with zero code generation
- **Build both code paths now.** Default stays off until manually flipped after assessment.

---

## 4. Component List

| Component | Responsibility | Key tech |
|---|---|---|
| `cdp_engine.py` | Launch/attach Chrome via CDP, send/receive protocol messages, detect navigation | `websockets`, raw CDP |
| `ax_tree_processor.py` | Take raw accessibility data → filter irrelevant nodes → build clean tree structure | pure Python |
| `shadow_dom_traverser.py` | Detect shadow hosts, recursively pierce, tag nodes with `shadowAncestors`, enforce max depth | CDP `DOM.describeNode`, scoped `getFullAXTree` |
| `repo_scanner.py` | ripgrep-based 3-pass POM file search | `subprocess` + `rg --json` |
| `locator_resolver.py` | For each node, compute all locator strategies (id/name/aria-label/data-testid/css/xpath), rank by uniqueness/stability, generate Selenium AND Playwright syntax, generate shadow-piercing chains when applicable | CDP `DOM.querySelector` for live validation |
| `api_server.py` | FastAPI app: REST endpoints + WebSocket | FastAPI, uvicorn |
| `agent_ipc.py` | Write `output.json`/`status.json`, read `command.json`/`ack.json`, atomic writes | file I/O (temp+rename) |
| Java Swing app | JTree display, locator editor panel, element picker trigger, toolbar, status bar | Java 17, Swing, FlatLaf, `HttpClient`, Java WebSocket client |

---

## 5. Functional Requirements (build in this order within each phase)

### FR-01 — Browser Launch / CDP Connection
- Launch Chrome with `--remote-debugging-port=9222` (configurable) OR attach if already running on that port
- Configurable user profile dir (persistent login)
- Clear error messages on failure (port busy, Chrome not found)

### FR-02 — Accessibility Tree Extraction (Hybrid)
- Primary: `getSnapshot({interestingOnly:true})`
- Filter out: `none`, `presentation`, `generic` roles (unless they carry an aria-label), redundant `StaticText` children of labeled interactive elements
- Keep: `button, link, textbox, combobox, checkbox, radio, menuitem, tab, slider, switch, searchbox, spinbutton` + landmark roles (`banner, navigation, main, contentinfo, complementary, form, region`) as structural grouping nodes
- Auto-refresh on `Page.frameNavigated` (push via WebSocket) + manual refresh button

### FR-03 — Interactive Element Tree Display (Java Swing)
- JTree node = role icon + name + current locator (Selenium format by default)
- Shadow boundaries = distinct collapsible nodes with shadow icon + host tag name
- Single click → highlight element in browser (`Overlay.highlightNode`)
- Double click → open locator editor (FR-04)
- Keyboard nav: arrows, Enter=select, F2=edit
- Search/filter bar by name/role/locator value

### FR-04 — Locator Strategy Selection/Editing
- Editor panel shows all candidate locators with uniqueness indicator (unique / non-unique+count / not-found)
- Selenium `@FindBy(...)` shown by default
- "Playwright format" toggle (or 2nd double-click) switches display for that element only
- For shadow DOM elements: show both the Selenium `getShadowRoot()` chain pattern and Playwright `>>>` combinator
- Custom locator entry with live CDP validation
- Strategy priority order configurable in YAML

### FR-05 — Manual Element Addition (Verification Points)
- "Add Element" button → CDP `Overlay.setInspectMode` (searchForNode)
- On click in browser → `Accessibility.getPartialAXTree(nodeId)` to fetch data even if Chrome considers it "uninteresting"
- Added under a "Verification Elements" tree section, distinct icon/italic
- User assigns custom name/alias
- Escape cancels; hover shows tooltip
- Correctly resolves shadow ancestry if applicable

### FR-06 — Shadow DOM Traversal (Level 3)
- Detect shadow hosts via `DOM.describeNode`, read `shadowRootType` (open/closed)
- For each shadow host: `DOM.describeNode({pierce:true})` + scoped `getFullAXTree()` to enumerate contents
- Shadow boundary = JTree node, icon ⚡, label = host tag + `[shadow-root: open|closed]`
- Each element inside carries `shadowAncestors: []` metadata (ordered host chain)
- Locator Resolver outputs:
  - Selenium: chained `driver.findElement(...).getShadowRoot().findElement(...)`
  - Playwright: `>>>` piercing combinator
- Support nesting up to 5 levels deep (configurable `max_depth`)
- `traverse_closed: false` by default — do not attempt to pierce closed shadow roots

### FR-07 — Repository-Aware POM File Detection
- Extract current URL + page title via CDP
- ripgrep 3-pass search (see Section 7)
- Rank multiple candidates, let user pick
- If none found → prompt for manual path (no auto-generation, per ADR-05)
- Detected/selected path shown in UI header, user-editable

### FR-08 — POM Modification Output + Agent Communication
- "Push to POM" → write `.locatorforge/output.json` (schema in Section 6), set `status.json` → `output_ready`
- Include `shadow_chain`, `locator_format` per modification
- If `enable_code_block: true` in config → also populate `code_block` + `insert_after_pattern`/`replace_pattern`; else these are `null`
- Agent writes `.locatorforge/ack.json` after applying → tool shows confirmation

### FR-09 — Agent Launch & Polling Protocol
- Launch command: `python -m locatorforge --repo-root <path> --port 9222`
- Creates `.locatorforge/` in repo root, `status.json` starts as `idle`
- Agent polls `status.json` every 2s (configurable)
- Agent commands via `.locatorforge/command.json`: `refresh`, `terminate`, `navigate <url>`
- On UI close: status → `terminated`, clean up temp files

### FR-10 — REST API + WebSocket Interface
REST endpoints (FastAPI, localhost only, default port 8765):
- `GET /tree` — current element tree
- `GET /locators/{nodeId}` — all locator strategies for a node
- `POST /highlight/{nodeId}` — highlight in browser
- `GET /pom/search` — trigger POM search, return ranked candidates
- `POST /element/pick` — activate element picker mode
- `GET /health` — connection status

WebSocket (`ws://localhost:{port}/ws`) — server push only:
- `page_navigated` (new URL)
- `tree_updated` (full refresh)
- `element_picked` (user picked via inspect mode)
- `browser_disconnected` / `browser_reconnected`
- Reconnect with exponential backoff: 1s → 2s → 4s → max 30s

---

## 6. Data Schemas

### 6.1 `output.json`

```json
{
  "version": "1.2",
  "timestamp": "ISO-8601",
  "pom_file": "src/test/java/pages/LoginPage.java",
  "pom_framework": "selenium-java",
  "enable_code_block": false,
  "modifications": [
    {
      "action": "update | add",
      "element_name": "usernameField",
      "locator_format": "selenium | playwright",
      "old_locator": { "strategy": "id", "value": "user-input" },
      "new_locator": { "strategy": "data-testid", "value": "login-username" },
      "annotation_format": "@FindBy(css = \"[data-testid='login-username']\")",
      "shadow_chain": [
        { "host_selector": "wm-datepicker", "shadow_type": "open" }
      ],
      "line_hint": 24,
      "insert_after": "usernameField",
      "element_type": "interactive | verification",
      "access_modifier": "private",
      "code_block": null,
      "insert_after_pattern": null,
      "replace_pattern": null
    }
  ]
}
```

**Field rules:**
- `old_locator` required for `action: update`; omit for `add`
- `insert_after` + `element_type` + `access_modifier` required for `action: add`
- `shadow_chain` = `[]` when element is not inside Shadow DOM
- `code_block` / `insert_after_pattern` / `replace_pattern` are `null` unless `enable_code_block: true`

### 6.2 `output.json` when `enable_code_block: true`

Same structure, but populate:
```json
{
  "action": "update",
  "code_block": [
    "    @FindBy(css = \"[data-testid='login-username']\")",
    "    private WebElement usernameField;"
  ],
  "replace_pattern": "@FindBy(id = \"user-input\")\n    private WebElement usernameField;"
}
```
```json
{
  "action": "add",
  "code_block": [
    "",
    "    public WebElement getDateInput() {",
    "        return driver.findElement(By.cssSelector(\"wm-datepicker\"))",
    "            .getShadowRoot()",
    "            .findElement(By.id(\"date-input\"));",
    "    }"
  ],
  "insert_after_pattern": "private WebElement usernameField;"
}
```
For shadow DOM elements, `code_block` must include the full helper-method pattern, not just an annotation — the agent should not need to write any shadow-traversal logic itself.

### 6.3 `status.json`

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

### 6.4 `command.json` (agent → tool)

```json
{ "command": "refresh | terminate | navigate", "arg": "url-if-navigate" }
```

### 6.5 `ack.json` (agent → tool, after applying changes)

```json
{ "status": "applied", "applied_changes": ["usernameField", "dateInput"] }
```

### 6.6 Agent processing logic (pseudocode)

```
read output.json
if enable_code_block == false:
    for mod in modifications:
        if mod.action == "update":
            open mod.pom_file
            find element by old_locator/element_name (use line_hint to narrow)
            replace its annotation with mod.annotation_format
        if mod.action == "add":
            open mod.pom_file
            find mod.insert_after element
            insert field declaration with mod.annotation_format, mod.access_modifier
        if mod.shadow_chain is not empty:
            generate shadow-piercing accessor (Selenium: getShadowRoot() chain; Playwright: >>> combinator)
        respect mod.locator_format (selenium → @FindBy; playwright → getBy*/page.locator)
else:  # enable_code_block == true
    for mod in modifications:
        if mod.action == "update":
            find mod.replace_pattern in mod.pom_file, replace with mod.code_block joined by \n
        if mod.action == "add":
            find mod.insert_after_pattern in mod.pom_file, insert mod.code_block lines after it
        # no code generation needed — code_block is final

save file(s)
write ack.json
```

---

## 7. Repository Search Strategy (ripgrep, 3-pass)

**Pass 1 — URL-based:**
```
rg --json -l "<url-path-segment>" --type java --type ts --type py <source_dirs>
```

**Pass 2 — Annotation/decorator-based:**
```
rg --json "@PageUrl|@FindBy|page_url|BASE_URL" --type java <source_dirs>
```
Narrow by matching declared URLs against current URL.

**Pass 3 — Naming convention:**
```
rg --json -l "class <UrlSegment>.*Page" --type java <source_dirs>
```

Combine signals, rank candidates, present ranked list if ambiguous. If zero matches → ask user for manual path. **Do not auto-generate a new POM file.**

---

## 8. Config File — `locatorforge.yaml`

```yaml
cdp:
  debug_port: 9222
  user_profile_dir: null   # null = temp profile; set path for persistent login

api:
  fastapi_port: 8765
  bind_host: 127.0.0.1      # never change to 0.0.0.0

search:
  source_dirs:
    - src/test/java/pages
    - src/test/java/pageobjects
    - tests/pages
  file_patterns:
    - "*Page.java"
    - "*Page.ts"
    - "*_page.py"
  exclude_dirs:
    - node_modules
    - build
    - target
    - .git
  pom_framework: selenium-java   # selenium-java | playwright-ts | pytest-selenium

locators:
  priority:
    - data-testid
    - aria-label
    - id
    - name
    - css
    - xpath
  default_format: selenium       # selenium | playwright

shadow_dom:
  max_depth: 5
  traverse_closed: false

agent_output:
  enable_code_block: false       # flip to true only after assessing default mode
  code_style:
    indent: "    "
    access_modifier: private

agent_ipc:
  poll_dir: .locatorforge
  status_poll_interval_sec: 2
```

---

## 9. Build Order (Phased)

**Phase 1 — Core loop:**
CDP launch/attach → `getSnapshot` tree → basic JTree (Selenium format only) → write `output.json` (no code_block) → FastAPI `/tree` + `/health`

**Phase 2 — Full locator suite:**
All locator strategies + ranking → Playwright toggle on double-click → element picker via `getPartialAXTree` → live CDP validation → WebSocket push events → `locatorforge.yaml` loading

**Phase 3 — Repo awareness + Shadow DOM:**
ripgrep 3-pass POM detection → update/add output generation → agent polling protocol (`status.json`/`command.json`/`ack.json`) → Level 3 Shadow DOM traversal + boundary JTree nodes + piercing locators

**Phase 4 — Polish + code_block + distribution:**
Multi-tab support → error resilience (CDP reconnect, WebSocket backoff) → FlatLaf UI pass → implement `code_block` generation path (kept disabled by default) → set up `pyproject.toml` + `pom.xml` + Artifactory publish CI jobs (Section 10) → docs

---

## 10. Distribution & Versioning (Artifactory-Based)

LocatorForge is distributed via the enterprise Artifactory instance — **not** vendored as binaries inside consuming repos, and **not** built with PyInstaller/jpackage. The Python side is a normal pip-installable package; the Java side is a normal Maven artifact, fetched on first run by the Python CLI. No manual Maven invocation is required by the end user or the calling agent.

### 10.1 Python Package (`pyproject.toml`)

Build as a standard package with a console entry point so `pip install` produces a `locatorforge` command on PATH.

```toml
[project]
name = "locatorforge"
version = "1.2.0"
description = "AI-agent driven locator management and POM maintenance utility"
requires-python = ">=3.10"
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.29",
    "websockets>=12.0",
    "pyyaml>=6.0",
    "requests>=2.31",
]

[project.scripts]
locatorforge = "locatorforge.cli:main"

[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["locatorforge*"]
```

**Publish (CI job, runs on version bump/release tag):**
```bash
python -m build
twine upload --repository-url https://artifactory.citi.internal/api/pypi/pypi-local dist/*
```

**Install (dev machine or CI agent image, one-time or baked into image):**
```bash
pip install --index-url https://artifactory.citi.internal/api/pypi/pypi-local/simple locatorforge==1.2.0
```

After install, the AI agent's launch instruction is simply:
```bash
locatorforge --repo-root . --port 9222
```

### 10.2 Java UI Artifact (Maven `pom.xml`)

Build the Swing UI as a shaded/fat JAR (all dependencies — FlatLaf, HTTP client, WebSocket client — bundled into one artifact) and publish it to the same Artifactory instance via its Maven-compatible repo.

```xml
<project>
  <groupId>com.citi.qa</groupId>
  <artifactId>locatorforge-ui</artifactId>
  <version>1.2.0</version>
  <packaging>jar</packaging>

  <properties>
    <maven.compiler.source>17</maven.compiler.source>
    <maven.compiler.target>17</maven.compiler.target>
  </properties>

  <dependencies>
    <dependency>
      <groupId>com.formdev</groupId>
      <artifactId>flatlaf</artifactId>
      <version>3.4</version>
    </dependency>
    <!-- WebSocket client, JSON parsing libs, etc. -->
  </dependencies>

  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-shade-plugin</artifactId>
        <version>3.5.1</version>
        <executions>
          <execution>
            <phase>package</phase>
            <goals><goal>shade</goal></goals>
            <configuration>
              <transformers>
                <transformer implementation="org.apache.maven.plugins.shade.resource.ManifestResourceTransformer">
                  <mainClass>com.citi.qa.locatorforge.ui.Main</mainClass>
                </transformer>
              </transformers>
            </configuration>
          </execution>
        </executions>
      </plugin>
    </plugins>
  </build>

  <distributionManagement>
    <repository>
      <id>artifactory-releases</id>
      <url>https://artifactory.citi.internal/artifactory/maven-local</url>
    </repository>
  </distributionManagement>
</project>
```

**Publish (CI job):**
```bash
mvn deploy
```

This produces a single artifact at:
```
https://artifactory.citi.internal/artifactory/maven-local/com/citi/qa/locatorforge-ui/1.2.0/locatorforge-ui-1.2.0.jar
```

No `mvn` CLI is needed at runtime on the end user's machine — the JAR is fetched via plain HTTPS GET (see 10.3).

### 10.3 Jar-Fetch Logic in `cli.py`

The Python CLI is the single entry point the agent invokes. On startup it checks a local cache for the matching UI JAR version; if absent, it downloads it directly from Artifactory's REST API (a plain authenticated HTTPS GET — no Maven dependency required at runtime) and caches it. It then starts the FastAPI backend and launches the Java process pointing at the cached JAR.

```python
# locatorforge/cli.py

import os
import sys
import subprocess
import requests
from pathlib import Path

UI_ARTIFACT_VERSION = "1.2.0"  # kept in lockstep with the pip package version
ARTIFACTORY_BASE = "https://artifactory.citi.internal/artifactory/maven-local"
UI_GROUP_PATH = "com/citi/qa/locatorforge-ui"

def get_cache_dir() -> Path:
    cache_dir = Path(os.environ.get("LOCATORFORGE_CACHE", Path.home() / ".locatorforge" / "cache"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir

def ensure_ui_jar(version: str = UI_ARTIFACT_VERSION) -> Path:
    """Return path to the locatorforge-ui jar for `version`, downloading from
    Artifactory if not already cached locally."""
    cache_dir = get_cache_dir()
    jar_path = cache_dir / f"locatorforge-ui-{version}.jar"

    if jar_path.exists():
        return jar_path

    url = f"{ARTIFACTORY_BASE}/{UI_GROUP_PATH}/{version}/locatorforge-ui-{version}.jar"
    print(f"[locatorforge] Fetching UI jar v{version} from Artifactory...")

    api_key = os.environ.get("ARTIFACTORY_API_KEY")
    headers = {"X-JFrog-Art-Api": api_key} if api_key else {}

    response = requests.get(url, headers=headers, stream=True, timeout=30)
    response.raise_for_status()

    tmp_path = jar_path.with_suffix(".jar.tmp")
    with open(tmp_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    tmp_path.rename(jar_path)  # atomic on POSIX; near-atomic on Windows

    print(f"[locatorforge] Cached at {jar_path}")
    return jar_path

def launch_ui(jar_path: Path, api_port: int) -> subprocess.Popen:
    """Launch the Java Swing UI, pointing it at the FastAPI backend port."""
    java_cmd = ["java", "-jar", str(jar_path), f"--api-port={api_port}"]
    return subprocess.Popen(java_cmd)

def main():
    args = parse_args()  # --repo-root, --port (CDP), --api-port, etc.

    # 1. Start FastAPI backend (CDP engine, tree processor, repo scanner, agent IPC)
    backend_process = start_backend(args)  # see api_server.py

    # 2. Ensure the Java UI jar is present locally (fetch from Artifactory if needed)
    jar_path = ensure_ui_jar()

    # 3. Launch the Java Swing UI as a subprocess
    ui_process = launch_ui(jar_path, api_port=args.api_port)

    # 4. Block until UI exits, then clean up backend + IPC files
    ui_process.wait()
    shutdown_backend(backend_process, args.repo_root)

if __name__ == "__main__":
    main()
```

**Key behaviors:**
- JAR is cached under `~/.locatorforge/cache/` (overridable via `LOCATORFORGE_CACHE` env var) — downloaded once per version, reused across repos and runs on the same machine.
- `ARTIFACTORY_API_KEY` (or equivalent token env var already used by Lightspeed CI/CD agents) is read from environment for authenticated pulls; no credentials are hardcoded or stored in the repo.
- Download uses a temp-file-then-rename pattern for atomicity, consistent with the atomic-write principle already required for IPC files (NFR-08 equivalent).
- If the jar is already cached, startup time is unaffected by Artifactory availability — only a fresh version bump triggers a network call.
- Version of the UI jar is pinned in code (`UI_ARTIFACT_VERSION`) and should be bumped in lockstep with the pip package version on every release.

### 10.4 End-to-End Setup for a Consuming Repo

No binaries, no Git LFS, nothing vendored into the test automation repo at all. The only repo-side artifact is `locatorforge.yaml` (config) and optionally a pinned version reference:

```yaml
# locatorforge.yaml (excerpt)
tool_version: "1.2.0"   # informational; actual resolution happens via pip
```

**One-time per machine/CI image:**
```bash
pip install --index-url https://artifactory.citi.internal/api/pypi/pypi-local/simple locatorforge==1.2.0
```
(Best practice: bake this into the Lightspeed CI/CD agent image alongside other standing tools like `ripgrep`, the same way `git`/`mvn`/`node` are assumed present — rather than installing per-repo.)

**Every invocation (what the AI agent actually runs):**
```bash
locatorforge --repo-root . --port 9222
```
This single command transparently handles backend startup, UI jar fetch/cache, and UI launch — the agent does not need to know about Maven, Artifactory URLs, or JAR paths.

### 10.5 Release Process Summary

| Step | Trigger | Action |
|---|---|---|
| 1 | Version bump + tag in source repo | CI builds Python wheel + shaded JAR |
| 2 | CI publish job | `twine upload` → Artifactory PyPI-local; `mvn deploy` → Artifactory Maven-local |
| 3 | Version sync | `UI_ARTIFACT_VERSION` in `cli.py` bumped to match the pip package version in the same commit/PR |
| 4 | Consumer upgrade | Re-run `pip install locatorforge==<new version>`; next launch auto-fetches the matching new UI jar (old cached jars remain for rollback, not auto-deleted) |

---

## 11. Non-Functional Targets

| Requirement | Target |
|---|---|
| Tree extraction + display, ≤500 elements | ≤ 3s |
| ripgrep POM search, ≤100k files | ≤ 1s |
| Agent poll overhead | ≤ 50ms/cycle |
| REST `GET /tree` response | ≤ 200ms |
| WebSocket event delivery | ≤ 100ms |
| Shadow host traversal, ≤200 internal elements | ≤ 500ms |
| UI process memory, ≤1000 elements | ≤ 256MB |
| Platforms | Windows 10+, macOS 12+, Ubuntu 20.04+; Java 17+, Python 3.10+, Chrome 120+ |

---

## 12. Security Constraints (Non-negotiable)

- CDP and FastAPI/WebSocket bind to `127.0.0.1` only — never expose on network
- No page content, screenshots, or credentials are ever captured/stored/transmitted — only element metadata (role, name, attributes)
- `.locatorforge/` must be auto-added to `.gitignore`
- No telemetry, no analytics, fully offline operation
- In-memory page data cleared on navigation/tool close

---

## 13. Explicitly Out of Scope (Do Not Build)

- POM class generation from scratch (ADR-05)
- Test script/test case generation
- Non-Chromium browser support
- Test execution / results reporting
- Authentication/session handling for the target app
- PyInstaller/jpackage binary bundling, Git LFS vendoring, or any "binaries committed to the consuming repo" distribution model — superseded by Artifactory-based pip/Maven distribution (Section 10)
