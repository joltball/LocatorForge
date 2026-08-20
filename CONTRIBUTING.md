# Contributing

Development guide for LocatorForge. For what the tool does, see [README.md](README.md); for design rationale, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Development setup

### Prerequisites

| Tool | Version | Install |
|---|---|---|
| Python | 3.10+ | [python.org](https://www.python.org/downloads/) — tick *Add python.exe to PATH* |
| JDK | 17+ | `winget install EclipseAdoptium.Temurin.17.JDK` |
| Maven | 3.9+ | `winget install Apache.Maven` |
| Chrome | 120+ | Standard install |
| ripgrep | any | `winget install BurntSushi.ripgrep.MSVC` |

On macOS or Linux use your usual package manager; only the commands differ.

### Backend

```bash
python -m venv .venv
```

Activate it — PowerShell:

```bash
.venv\Scripts\Activate.ps1
```

Command Prompt:

```bash
.venv\Scripts\activate.bat
```

macOS / Linux:

```bash
source .venv/bin/activate
```

Then install in editable mode:

```bash
pip install -e ".[dev]"
```

Edits under `python/locatorforge/` take effect immediately — no reinstall.

Verify:

```bash
locatorforge --version
```

### UI

```bash
mvn -f java-ui/pom.xml package
```

Produces `java-ui/target/locatorforge-ui-1.2.0.jar`, a shaded JAR with FlatLaf and JSON bundled.

> Maven cannot overwrite the JAR while the UI is running. Close the LocatorForge window before rebuilding.

### Run what you built

```bash
locatorforge --repo-root . --ui-jar java-ui/target/locatorforge-ui-1.2.0.jar
```

`--ui-jar` bypasses the Artifactory fetch, so you always run your local build.

## Testing

```bash
pytest tests/python -q
```

Expected: **26 passed, 1 skipped**. The skip is `test_repo_scanner.py`, which is gated on `ripgrep` being installed.

Coverage by area:

| File | Covers |
|---|---|
| `test_ax_tree_processor.py` | Tree filtering rules |
| `test_locator_resolver.py` | Candidate generation and priority ranking |
| `test_descendant_locators.py` | Descendant anchoring, validation integrity, positional fallback |
| `test_shadow_dom_chains.py` | Shadow-piercing chain generation |
| `test_repo_scanner.py` | ripgrep output parsing |
| `test_agent_ipc.py` | Atomic writes and status transitions |
| `test_code_block.py` | Literal code emission |

The Swing UI has no automated tests and is exercised manually.

### Backend-only runs

Useful when iterating on the API without the UI:

```bash
locatorforge --repo-root . --no-ui --no-launch-chrome --api-port 8765
```

Interactive API docs are then at `http://127.0.0.1:8765/docs`. CDP-dependent endpoints will fail in this mode — drop `--no-launch-chrome` to exercise them.

### Debugging against a live page

Launch Chrome yourself with a **separate profile** — without a distinct `--user-data-dir`, the debug flag is silently ignored when Chrome is already running:

```bash
chrome.exe --remote-debugging-port=9222 --user-data-dir=%TEMP%\lf-debug --no-first-run https://your-app
```

Then attach:

```bash
locatorforge --repo-root . --no-launch-chrome
```

Verbose logging:

```bash
set LOCATORFORGE_LOG=DEBUG
```

## Project layout

```
LocatorForge/
├── README.md               What the tool is and how to use it
├── CONTRIBUTING.md         This file
├── CHANGELOG.md            Release history
├── docs/ARCHITECTURE.md    Design decisions and schemas
├── pyproject.toml          Python package definition
├── python/locatorforge/    Backend
│   ├── cli.py                  Entry point, JAR fetch, lifecycle
│   ├── cdp_engine.py           CDP transport, frame contexts, reconnect
│   ├── ax_tree_processor.py    Tree extraction and assembly
│   ├── frame_traverser.py      Iframe enumeration and chains
│   ├── shadow_dom_traverser.py Shadow DOM
│   ├── locator_resolver.py     Candidate generation and ranking
│   ├── interaction_recorder.py Recording
│   ├── repo_scanner.py         Page Object search
│   ├── agent_ipc.py            JSON handoff
│   ├── code_block.py           Literal code emission
│   ├── api_server.py           FastAPI app
│   ├── config.py               YAML config
│   └── schemas.py              Pydantic models
├── java-ui/                Swing UI (Maven)
│   └── src/main/java/com/citi/qa/locatorforge/ui/
│       ├── Main.java
│       ├── api/                REST and WebSocket clients
│       ├── tree/               Element tree, boundary nodes
│       ├── editor/             Locator editor panel
│       └── toolbar/            Toolbar actions
└── tests/python/           pytest suite
```

## Extending the tool

### Adding a locator strategy

1. Emit the candidate in `_build_candidates` (`locator_resolver.py`).
2. **Set `validation_expr` and `validation_kind` from the same string you put in `selenium`.** This is not optional — it is what prevents a candidate from reporting a match count for an expression it does not display. See ADR-08.
3. Add the strategy name to `LocatorsCfg.priority` in `config.py`.
4. Add a test in `test_descendant_locators.py`.

Never branch validation logic on the strategy *name*. Several strategies emit XPath; branch on `validation_kind` instead.

### Adding an API endpoint

Add the route in `api_server.py`, define request models with Pydantic, and add the client method to `ApiClient` or `LocatorClient` on the Java side. Both Java HTTP clients pin HTTP/1.1 — uvicorn does not speak cleartext HTTP/2, and the JDK client's default upgrade attempt makes it reply 400.

### Working with CDP

- **Never `await` an event handler inside the read loop.** Dispatch it as a task. Blocking the loop means Chrome's responses queue unread and every in-flight request times out.
- Accessibility node ids are **frame-scoped**. Any call about a node in a child frame must carry that frame's `frameId`.
- `backendNodeId` is browser-global and works across frames without qualification.
- Prefer asking the page over inferring. Read the real tag from `DOM.describeNode`; measure an element's index with an identity comparison rather than trusting tree order.

## Coding conventions

**Python** — type hints throughout, `from __future__ import annotations`, Pydantic for anything crossing a process boundary. Comments explain *why*, particularly where a non-obvious constraint drove the design.

**Java** — Java 17, standard Swing idiom, long-running work on a `SwingWorker` and never on the EDT.

**Both** — when you work around a platform quirk, say so in a comment. Several fixes here look arbitrary without the reason attached.

## Release process

| Step | Action |
|---|---|
| 1 | Bump `version` in `pyproject.toml`, `<version>` in `java-ui/pom.xml`, and `__version__` in `python/locatorforge/__init__.py` — all three must match |
| 2 | Update [CHANGELOG.md](CHANGELOG.md) |
| 3 | Confirm `pytest` is green and `mvn package` succeeds |
| 4 | Tag the release |
| 5 | CI publishes: `python -m build` + `twine upload` to Artifactory PyPI; `mvn deploy` to Artifactory Maven |

`cli.py` derives the UI JAR version from `__version__`, so the pip package and JAR always resolve as a matched pair. Older cached JARs are left in place for rollback.

## Reporting problems

Include:

- What you did, expected, and observed
- Backend output with `LOCATORFORGE_LOG=DEBUG`
- Chrome version, OS, Python version
- The **markup of the element** if it concerns locator generation — that is usually the whole diagnosis

Do not paste application data, credentials, or screenshots containing customer information.
