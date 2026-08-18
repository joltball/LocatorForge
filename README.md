# LocatorForge

AI-agent driven locator management and POM maintenance utility. See [SPEC.md](SPEC.md) for the full PRD and [PLAN.md](PLAN.md) / [PROGRESS.md](PROGRESS.md) for the phased implementation.

LocatorForge bridges a live Chrome instance (via the DevTools Protocol) and a Java Swing locator editor, then writes structured JSON instruction files that an external AI coding agent applies to your Page Object Model source files. **This tool never edits source code itself** — it only produces instructions.

## Install

```bash
pip install --index-url https://artifactory.citi.internal/api/pypi/pypi-local/simple locatorforge==1.2.0
```

On first run, the matching UI JAR is fetched once from Artifactory and cached under `~/.locatorforge/cache/`.

## Run

```bash
locatorforge --repo-root . --port 9222
```

This launches Chrome with CDP enabled, starts the FastAPI backend on `127.0.0.1:8765`, and spawns the Swing UI. The `.locatorforge/` directory is created in your repo root (and auto-added to `.gitignore` if `.git` is present).

Flags:
- `--repo-root` — path to the consuming repo root (required).
- `--port` — Chrome CDP debug port (default 9222, configurable via `locatorforge.yaml`).
- `--api-port` — FastAPI bind port (default 8765, localhost only).
- `--config` — explicit path to a `locatorforge.yaml`.
- `--user-profile-dir` — persistent Chrome profile (default: ephemeral temp dir).
- `--ui-jar` — path to a locally built UI jar (skips Artifactory fetch — useful for development).
- `--no-launch-chrome` — assume Chrome is already running on `--port`.
- `--no-ui` — backend-only mode.

## Configure

Copy [`locatorforge.yaml.example`](locatorforge.yaml.example) to `locatorforge.yaml` at your repo root. All values shown there are the defaults. To enable the ADR-06 `code_block` output (so the agent does pure mechanical find-and-insert with zero code generation), flip:

```yaml
agent_output:
  enable_code_block: true
```

## Build from source

```bash
# Python backend
pip install -e .[dev]
pytest

# Java UI (produces target/locatorforge-ui-1.2.0.jar)
cd java-ui && mvn package
```

To run against a locally built JAR:

```bash
locatorforge --repo-root . --ui-jar java-ui/target/locatorforge-ui-1.2.0.jar
```

## Agent IPC

After you click **Push to POM**, LocatorForge writes `.locatorforge/output.json` (schema in SPEC §6.1) and flips `.locatorforge/status.json` to `output_ready`. The external agent polls `status.json` every ~2 seconds (configurable), reads `output.json`, applies the changes to your POM source file, and writes `.locatorforge/ack.json` when done. LocatorForge resets status to `idle` on receipt.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Could not locate a Chrome / Chromium executable` | Install Chrome, or ensure `chrome`/`google-chrome` is on PATH. |
| `Chrome launched but did not start CDP on 127.0.0.1:9222` | Another process is using port 9222 — pass `--port` to use a different one. |
| Tree empty / "no page target" | Open at least one tab in the LocatorForge-launched Chrome window before clicking Refresh. |
| UI says `Element picker failed` | The launched Chrome window must be the frontmost active tab — picker is per-target. |

## Security

Per SPEC §12: CDP and FastAPI bind to `127.0.0.1` only and are never exposed on the network. The tool captures only role/name/attribute metadata — never page content, screenshots, or credentials. There is no telemetry.
