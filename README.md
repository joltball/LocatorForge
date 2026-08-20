# LocatorForge

**A desktop tool for building and maintaining Page Object Model locators against a live web application.**

LocatorForge attaches to a running Chrome instance, extracts an interactive element tree from the page (including Shadow DOM and iframes), and lets you pick or edit locators in a Java Swing UI. It then writes a structured JSON instruction file describing the changes you want.

> **LocatorForge never edits your source files.** It produces instructions. An external AI coding agent — or a person — applies them to the Page Object. This keeps the tool safe to run against any repository.

---

## Contents

- [Why it exists](#why-it-exists)
- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Using the tool](#using-the-tool)
- [Configuration](#configuration)
- [Output files](#output-files)
- [Troubleshooting](#troubleshooting)
- [Security and scope](#security-and-scope)
- [Further reading](#further-reading)

---

## Why it exists

Keeping Page Object locators healthy is repetitive and error-prone:

- Finding a stable locator means hand-inspecting the DOM in DevTools.
- Modern component frameworks (Angular Material, PrimeNG, Vuetify) put the identity on a *descendant* of the clickable element, so the obvious locator is often useless.
- Content inside iframes and Shadow DOM is invisible to naive tooling.
- A locator that *looks* right frequently matches zero or twenty elements, and you only find out when the test fails.

LocatorForge shows you every candidate locator for an element, **validates each one against the live page**, and ranks them by what actually works — not by what should theoretically work.

## Features

**Element discovery**
- Interactive element tree extracted from Chrome's accessibility layer
- **Iframe traversal** — content inside same-process frames is spied and spliced into the tree
- **Shadow DOM traversal** (open roots, configurable depth)
- Element picker for verification points that Chrome considers "uninteresting"
- Search/filter across name, role and locator

**Locator generation**
- Strategies: `data-testid`, `aria-label`, `id`, `name`, `descendant-attr`, `descendant-text`, `role`, `css`, `xpath`
- **Live uniqueness validation** — every candidate is counted against the real page
- **Ranked by measured reality**, so a locator that resolves to nothing can never outrank one that works
- **Descendant-anchored locators** for component frameworks, e.g. `//mat-card[.//mat-card-title[@aria-label='…']]`
- **Positional fallback** (`(…)[3]`) when elements are genuinely indistinguishable — clearly flagged as order-dependent
- Both **Selenium** (`@FindBy`) and **Playwright** syntax for every element
- Frame- and shadow-piercing chains generated automatically

**Workflow**
- **Interaction recorder** — drive the app normally; LocatorForge captures the elements you touch, across frames and pages
- POM file detection in your repo via a 3-pass `ripgrep` search
- Structured JSON handoff to an AI agent, with a file-based command/ack protocol

## How it works

```
┌─────────────┐   CDP    ┌──────────────────┐   REST + WS   ┌───────────────┐
│   Chrome    │◄────────►│  Python backend  │◄─────────────►│  Java Swing   │
│ (your app)  │  :9222   │  (FastAPI :8765) │               │      UI       │
└─────────────┘          └────────┬─────────┘               └───────────────┘
                                  │ writes
                                  ▼
                         .locatorforge/*.json  ──►  AI agent edits your POM
```

Both the CDP connection and the API bind to `127.0.0.1` only.

## Requirements

| | Minimum | Notes |
|---|---|---|
| Python | 3.10+ | Backend |
| JDK | 17+ | Swing UI |
| Maven | 3.9+ | Only to build the UI from source |
| Chrome | 120+ | The application under test runs here |
| ripgrep | any | Optional — required only for POM file search |

Supported on Windows 10+, macOS 12+, and Ubuntu 20.04+.

## Installation

### From Artifactory (recommended)

```bash
pip install --index-url https://artifactory.citi.internal/api/pypi/pypi-local/simple locatorforge==1.2.0
```

This puts a `locatorforge` command on your PATH. The Java UI JAR is fetched and cached automatically on first run (`~/.locatorforge/cache/`), so no Maven is needed at runtime.

If your Artifactory requires authentication, set `ARTIFACTORY_API_KEY` in your environment before the first launch.

### From source

```bash
git clone <this-repo>
cd LocatorForge
python -m venv .venv
```

Activate the virtual environment:

```bash
.venv\Scripts\Activate.ps1
```

Then install and build:

```bash
pip install -e ".[dev]"
```

```bash
mvn -f java-ui/pom.xml package
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full development workflow.

## Quick start

Launch the tool from the repository that holds your Page Objects:

```bash
locatorforge --repo-root .
```

Running from a source checkout instead? Point at your locally built JAR:

```bash
locatorforge --repo-root . --ui-jar java-ui/target/locatorforge-ui-1.2.0.jar
```

What happens:

1. Chrome launches with remote debugging enabled on port 9222.
2. The backend starts on `http://127.0.0.1:8765`.
3. The Swing UI opens.
4. A `.locatorforge/` directory is created in your repo root.

Navigate Chrome to your application, log in if needed, then click **Refresh** in the UI.

### Command-line options

| Flag | Default | Purpose |
|---|---|---|
| `--repo-root` | *(required)* | Repository containing your Page Objects |
| `--port` | `9222` | Chrome CDP debug port |
| `--api-port` | `8765` | FastAPI bind port |
| `--config` | *(auto)* | Path to a `locatorforge.yaml` |
| `--user-profile-dir` | *(temp)* | Persistent Chrome profile, to keep logins |
| `--ui-jar` | *(fetched)* | Use a local UI JAR instead of Artifactory |
| `--no-ui` | off | Run the backend only |
| `--no-launch-chrome` | off | Attach to an already-running Chrome |

## Using the tool

### Inspecting elements

Click **Refresh** to build the tree. Nodes are shown as `role: name` with the default Selenium locator.

| Icon | Meaning |
|---|---|
| 🖼 | An iframe boundary — everything below lives in another document |
| ⚡ | A Shadow DOM boundary |
| *italic* | A manually added verification element |

Single-click highlights the element in Chrome. Select a node to see all candidate locators.

### Reading the locator badges

| Badge | Meaning |
|---|---|
| `✓` green | Unique and stable — use this |
| `#3` amber | Unique, but pinned by **index**. Breaks if the list re-sorts, filters or pages |
| `✗4` red | Matches 4 elements — not unique |
| `⚠0` red | Does not resolve against the live page |

Double-click a row to toggle between Selenium and Playwright syntax for that element.

### Recording a session

Click **Record**, then drive the application normally — click, type, submit. LocatorForge captures each element you interact with, in every frame, and groups them by page. Click **Stop** to write `.locatorforge/recording.json`.

This is usually the fastest way to bootstrap a new Page Object.

### Pushing changes

Select one or more elements and click **Push to POM**. You will be asked for the target Page Object path (or you can let LocatorForge search for it). The result is written to `.locatorforge/output.json` for the agent to apply.

### Working with iframes

Elements inside an iframe appear beneath a 🖼 boundary node. Their locators carry the required frame context automatically:

```java
// requires frame switch:
// driver.switchTo().defaultContent();
// driver.switchTo().frame(driver.findElement(By.cssSelector("iframe[name='Main']")));
@FindBy(xpath = "//mat-card[.//mat-card-title[@aria-label='Acme Holdings']]")
```

```javascript
page.frameLocator("iframe[name='Main']")
    .locator("mat-card:has(mat-card-title[aria-label='Acme Holdings'])")
```

Selenium's `@FindBy` cannot express frame switching, so the required `switchTo()` sequence is emitted as a comment above the annotation, and `output.json` carries a machine-readable `frame_chain`.

## Configuration

LocatorForge runs with sensible defaults. To customise, create `locatorforge.yaml` in your repository root.

Resolution order: `--config` → `<repo-root>/locatorforge.yaml` → `~/.locatorforge/config.yaml` → built-in defaults.

```yaml
cdp:
  debug_port: 9222
  user_profile_dir: null      # set a path to keep logins between runs

api:
  fastapi_port: 8765
  bind_host: 127.0.0.1        # never change this

search:
  source_dirs:
    - src/test/java/pages
    - src/test/java/pageobjects
    - tests/pages
  file_patterns: ["*Page.java", "*Page.ts", "*_page.py"]
  exclude_dirs: [node_modules, build, target, .git]
  pom_framework: selenium-java   # selenium-java | playwright-ts | pytest-selenium

locators:
  priority:                   # tie-break order among locators that WORK
    - data-testid
    - aria-label
    - id
    - name
    - descendant-attr
    - descendant-text
    - role
    - css
    - xpath
  default_format: selenium    # selenium | playwright

shadow_dom:
  max_depth: 5
  traverse_closed: false

agent_output:
  enable_code_block: false    # see below
  code_style:
    indent: "    "
    access_modifier: private

agent_ipc:
  poll_dir: .locatorforge
  status_poll_interval_sec: 2
```

**A note on `locators.priority`:** this only decides between locators that actually resolve. A candidate measured at zero matches is always ranked last regardless of its configured priority.

**`enable_code_block`** (default `false`): when `true`, `output.json` additionally contains literal `code_block` lines and find/replace patterns, letting an agent apply changes mechanically without generating any code itself.

## Output files

All written to `<repo-root>/.locatorforge/`. Writes are atomic (temp file + rename).

| File | Direction | Purpose |
|---|---|---|
| `output.json` | tool → agent | Locator changes to apply |
| `status.json` | tool → agent | `idle` / `pending` / `output_ready` / `error` / `terminated` |
| `recording.json` | tool → you | Elements captured during a recording session |
| `command.json` | agent → tool | `refresh` / `terminate` / `navigate <url>` |
| `ack.json` | agent → tool | Confirmation that changes were applied |

`.locatorforge/` is added to `.gitignore` automatically. **Do not commit it** — recordings contain application data from whatever environment you were driving.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `locatorforge: command not found` | Virtual environment not active. Re-run the activate script |
| `Could not locate a Chrome / Chromium executable` | Chrome is not on PATH. Add its directory, or install Chrome |
| `Chrome launched but did not start CDP on 127.0.0.1:9222` | A Chrome instance is already using that profile. Close all Chrome windows, or pass `--port 9333` |
| Tree is empty after Refresh | The page had not finished loading. Wait for it to settle and press Refresh again |
| Tree shows only top-level navigation | The app's content is in an iframe that had not loaded when you refreshed. Press Refresh again |
| Elements inside an iframe are not recorded | Restart the recording after the frame has loaded |
| `Only DevTools/internal pages are open` | Navigate a tab to your application; DevTools windows are ignored deliberately |
| Port `8765` already in use | Pass `--api-port 8766` |
| `Activate.ps1 cannot be loaded` (Windows) | Run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that shell |
| `mvn` reports `JAVA_HOME is not defined` | Point `JAVA_HOME` at your JDK 17 installation, not a JRE |
| POM search returns nothing | `ripgrep` is not installed, or `search.source_dirs` does not match your layout |

Set `LOCATORFORGE_LOG=DEBUG` for verbose backend logging.

## Security and scope

- CDP and the API bind to `127.0.0.1` only, and are never exposed on the network.
- Only element **metadata** is captured — role, name, attributes. No page text, screenshots, or credentials.
- Values from fields that look secret (password, CVV, SSN, token, OTP, …) are never recorded.
- No telemetry. The tool operates entirely offline apart from the one-time UI JAR fetch.
- LocatorForge **does not modify your source code**. It only writes JSON to `.locatorforge/`.

**Out of scope by design:** generating Page Object classes from scratch, generating tests, non-Chromium browsers, test execution, and authentication handling for the application under test.

## Further reading

- [CONTRIBUTING.md](CONTRIBUTING.md) — development setup, testing, and how to extend the tool
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — design decisions, data schemas, and known limitations
- [CHANGELOG.md](CHANGELOG.md) — release history

---

**Version 1.2.0** · Internal tool · Maintained by the QA Engineering team
