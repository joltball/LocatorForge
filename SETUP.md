# LocatorForge — Windows Local Setup & Test Guide

End-to-end walkthrough for getting LocatorForge running on a clean Windows 10/11 box, against your own Chrome, without touching Artifactory. By the end you'll have:

1. The FastAPI backend serving on `127.0.0.1:8765`.
2. The Java Swing UI talking to it.
3. A live Chrome window whose elements you can inspect, pick locators for, and "Push to POM" — producing a real `.locatorforge/output.json` on disk.

All commands below are written for **PowerShell** (the default shell on modern Windows). If you prefer `cmd.exe`, swap `$env:VAR = "x"` for `set VAR=x`.

---

## 1. Prerequisites

Install these once. Skip any you already have.

| Tool | Recommended install | Verify |
|---|---|---|
| **Python 3.10+** | [python.org installer](https://www.python.org/downloads/windows/) — tick "Add python.exe to PATH" | `python --version` |
| **JDK 17+** | [Adoptium Temurin 17](https://adoptium.net/temurin/releases/?version=17) or `winget install EclipseAdoptium.Temurin.17.JDK` | `java -version` |
| **Maven 3.9+** | `winget install Apache.Maven` or [unzip from maven.apache.org](https://maven.apache.org/download.cgi) and add `bin\` to PATH | `mvn -v` |
| **Google Chrome** | Standard install from [google.com/chrome](https://www.google.com/chrome/) | `Get-Command chrome` (or check `C:\Program Files\Google\Chrome\Application\chrome.exe`) |
| **ripgrep** (optional but recommended) | `winget install BurntSushi.ripgrep.MSVC` or `choco install ripgrep` | `rg --version` |
| **Git** (optional) | `winget install Git.Git` | `git --version` |

> ripgrep is required only for the Phase-3 repo-aware POM search and its tests; everything else runs without it.

---

## 2. Clone / open the repo

If you already have the repo locally (you do — you're reading this from inside it), open PowerShell in the project root:

```powershell
cd "C:\Users\Bhanu Prakash\Documents\GitHub\LocatorForge"
```

Stay in this directory for every command below.

---

## 3. Create a Python virtual environment

Keeps LocatorForge's dependencies isolated from your system Python.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks the activation script with an execution-policy error:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

Your prompt should now start with `(.venv)`.

---

## 4. Install the Python package (editable mode)

```powershell
pip install --upgrade pip
pip install -e ".[dev]"
```

This installs FastAPI, uvicorn, websockets, pyyaml, requests, pydantic, and the test deps (pytest, httpx). The `-e` flag means edits to files under `python/locatorforge/` are picked up immediately — no reinstall needed.

Verify the console script is on PATH:

```powershell
locatorforge --version
# → locatorforge 1.2.0
```

---

## 5. Run the Python unit tests

```powershell
pytest tests\python -q
```

Expected: **14 passed, 1 skipped** (the skipped one is the ripgrep-gated repo scanner test — it runs if you installed `rg` in Step 1).

---

## 6. Build the Java Swing UI

This produces a single fat JAR with FlatLaf + JSON libs bundled in.

```powershell
cd java-ui
mvn package
cd ..
```

Confirm the JAR exists:

```powershell
Test-Path java-ui\target\locatorforge-ui-1.2.0.jar
# → True
```

If `mvn` complains about `JAVA_HOME`, set it for the session:

```powershell
$env:JAVA_HOME = "C:\Program Files\Eclipse Adoptium\jdk-17.0.x-hotspot"   # adjust to your install
$env:PATH = "$env:JAVA_HOME\bin;$env:PATH"
```

---

## 7. Launch LocatorForge end-to-end

Pick (or create) a directory you want to treat as the "consuming repo" — the `.locatorforge/` IPC folder lives there. You can just use this same project for now:

```powershell
locatorforge `
  --repo-root . `
  --port 9222 `
  --api-port 8765 `
  --ui-jar java-ui\target\locatorforge-ui-1.2.0.jar
```

The backtick (`` ` ``) is PowerShell's line-continuation; you can also write it all on one line. What happens:

1. Chrome is launched with `--remote-debugging-port=9222 --user-data-dir=<temp>`. A blank tab opens at `about:blank`.
2. The FastAPI backend starts on `http://127.0.0.1:8765` in a background thread.
3. The Java Swing UI window opens, connecting to `127.0.0.1:8765`.
4. A `.locatorforge\` directory is created in the repo root with an initial `status.json`.

Leave this window running. If you ever need to stop, close the UI window — Chrome and the backend shut down with it.

---

## 8. Smoke-test the loop

With LocatorForge running:

### 8a. Health check
In a **second** PowerShell window:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

Expected JSON: `status: ok`, `version: 1.2.0`, the resolved `repo_root`, the `ipc_dir`.

### 8b. Load a real page
Switch to the Chrome window LocatorForge launched and navigate to a sample page in the URL bar:

```
https://www.example.com
```

In the LocatorForge UI window, click **Refresh** on the toolbar. The tree should populate with the `WebArea` → `heading "Example Domain"` → `link "More information..."`.

### 8c. Click an element
Single-click a tree node — Chrome briefly highlights that element in green and the right pane fills with ranked locator candidates (badges show `✓` for unique).

### 8d. Push to POM
1. Select one or more interactive nodes (Ctrl-click for multi-select).
2. Click **Push to POM** on the toolbar.
3. When prompted for a POM path, accept the default or type something like `src/test/java/pages/ExamplePage.java`.
4. A confirmation popup shows `Wrote 1 modification(s) to .locatorforge\output.json`.

Inspect the file:

```powershell
Get-Content .\.locatorforge\output.json
```

You should see a SPEC §6.1-shaped JSON document with your selected modifications.

### 8e. (Optional) Try element pick mode
- Click **Add Element** in the toolbar.
- Chrome enters inspect mode (blue overlay).
- Click any element (even a non-interactive `<div>`) — the UI's status bar reports `Element picked`, and `getPartialAXTree` data flows back over the WebSocket.

### 8f. (Optional) Try the `code_block` mode
Stop the running tool (close the UI window), then create a `locatorforge.yaml` at the repo root with:

```yaml
agent_output:
  enable_code_block: true
```

Re-launch with the same command from Step 7 and push again. The `output.json` now has populated `code_block` and `replace_pattern` (or `insert_after_pattern`) arrays per ADR-06.

---

## 9. Common Windows gotchas

| Symptom | Fix |
|---|---|
| `locatorforge: command not found` after `pip install -e .` | The venv isn't active. Re-run `.\.venv\Scripts\Activate.ps1`. |
| `Could not locate a Chrome / Chromium executable` | Chrome isn't on PATH and isn't in the standard `Program Files` paths. Set `$env:PATH = "C:\Program Files\Google\Chrome\Application;$env:PATH"`. |
| `Chrome launched but did not start CDP on 127.0.0.1:9222` | Another Chrome instance is already running on port 9222 (often a previous LocatorForge run that didn't clean up). Close all Chrome windows, or pass `--port 9333`. |
| `Activate.ps1 cannot be loaded because running scripts is disabled on this system` | Run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in the current PowerShell session. |
| `mvn` errors with `JAVA_HOME is not defined` | Set it (see Step 6) — pointing at your JDK 17 install, not the JRE. |
| `Invoke-RestMethod` says `Unable to connect` | Backend isn't up. Check the PowerShell window running `locatorforge` for stack traces; common causes are port `8765` already in use (pass `--api-port 8766`) or Chrome failing to launch. |
| Tree shows `(no page target — open a tab in Chrome)` | The launched Chrome window has zero tabs (it sometimes opens with just `about:blank` collapsed). Type a URL in the address bar, hit Enter, then click **Refresh** in the UI. |
| UI shows `Filter:` field but tree is empty after typing | Filtering is a "jump-to-match" tool, not a hide filter. Clear the field and click Refresh to see everything again. |
| `Push to POM` succeeds but no `.locatorforge\output.json` | Check the `--repo-root` you passed — that's where it landed, not the current directory. The success dialog shows the absolute path. |

---

## 10. Cleanup

When you're done testing:

```powershell
# Close the LocatorForge UI window (this stops Chrome + the backend).

# Optionally remove the IPC dir and the venv
Remove-Item -Recurse -Force .\.locatorforge
deactivate    # exits the venv
Remove-Item -Recurse -Force .\.venv
```

The cached UI JAR under `~\.locatorforge\cache\` only exists if you ran without `--ui-jar` and the Artifactory fetch succeeded — on a local-only setup it'll be empty.

---

## 11. What to do next

- Open [PROGRESS.md](PROGRESS.md) to see what's verified and what's deferred (e.g., the FlatLaf icon polish, the `Add to POM` UI flow).
- Read [SPEC.md](SPEC.md) §11 for the performance targets if you want to benchmark against a heavier page.
- The full architecture rationale lives in [PLAN.md](PLAN.md) and the locked ADRs in SPEC §3.
