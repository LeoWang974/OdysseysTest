# AgentV4 Browser Agent Integration

The local directory `agentv4-agent-browser-skill-framework/` contains a
separate browser GUI agent stack. It is not an OSWorld `PromptAgent` plugin.

## Directory Structure

- `.harness/agents/browser-gui.md` - registered AgentV4 agent definition.
- `.harness/skills/agent-browser/SKILL.md` - screenshot-only browser GUI policy.
- `.harness/bin/agent-browser` - harness wrapper used by the agent's Bash tool.
- `.harness/scripts/run-online-mind2web-harness-batch.mjs` - batch runner for task JSON files.
- `.harness/settings.json` and `.harness/settings.local.json` - agent/skill sources, permissions, and LLM defaults.
- `packages/core/` - AgentV4 runtime, tools, LLM providers, screenshot attachment logic.
- `packages/sdk/` - SDK/session API surface used by the CLI/daemon.
- `packages/pilot/cli/` - CLI entrypoint for one-shot prompt or REPL runs.
- `packages/pilot/daemon/` - JSONL daemon entrypoint.
- `vendor/agent-browser/` - browser automation CLI source and platform binaries.

The intended control path is:

```text
Odysseys task
  -> AgentV4 browser-gui agent
  -> agent-browser-gui skill
  -> Bash(.harness/bin/agent-browser ...)
  -> agent-browser native CLI
  -> Chrome screenshots/actions
```

## Current Pipeline Switch

`configs/dev_10_models.example.json` now sets:

```json
"agent_backend": "agentv4"
```

`python -m odysseys_eval run-suite ...` will no longer silently use the
OSWorld `PromptAgent` when this backend is selected. It first runs AgentV4
health checks.

Check readiness:

```powershell
python -m odysseys_eval agentv4-doctor
```

Inspect the selected plan:

```powershell
python -m odysseys_eval run-suite `
  --config configs\dev_10_models.example.json `
  --model gpt55 `
  --env-file .env `
  --dry-run
```

## Current Local Status

Working pieces:

- Node.js is available.
- pnpm is available.
- `agent-harness-core` is present at `packages/core/` and is linked into
  workspace consumers such as `packages/pilot/cli/node_modules/agent-harness-core`.
- The Windows `agent-browser-win32-x64.exe` binary has been added under
  `agentv4-agent-browser-skill-framework/vendor/agent-browser/bin/`.
- `agent-browser` itself can launch Chrome and take screenshots.
- A `pnpm-workspace.yaml` was added so pnpm can install workspace dependencies.
- `packages/sdk/src/sessions/` has been restored.
- `browser-gui` can run through the AgentV4 CLI and call `.harness/bin/agent-browser`.
- AgentV4 transcripts can be adapted into scorer-compatible `traj.jsonl`,
  `step_*.png`, and `result.txt` directories.
- A 1-task `gpt-5.6-luna` smoke verified `run-agentv4 -> score -> merge-report`.

Local runtime constraints:

- TokenHub is used through the AgentV4 `openai-compatible` provider at
  `https://tokenhub.sensetime.com/v1`.
- On this Windows machine, Node/fetch reports `unable to verify the first
  certificate` for TokenHub. The local runner therefore sets
  `NODE_TLS_REJECT_UNAUTHORIZED=0` for AgentV4 subprocesses only.
- For non-Claude OpenAI-compatible models, the runner removes AgentV4
  frontmatter `thinking*` fields from the local ignored `browser-gui.md`,
  because TokenHub returns `Unknown parameter: 'thinking'` for models such as
  `gpt-5.6-luna`.
- Windows AgentV4 exposes browser commands through the `PowerShell` tool, so
  the local runner auto-patches AgentV4's auto-screenshot gate to accept both
  `Bash` and `PowerShell`.

Run a one-task AgentV4 smoke:

```powershell
python -m odysseys_eval run-agentv4 `
  --prepared-dir outputs\dev_10 `
  --result-dir outputs\agentv4_smoke_adapter_gpt56luna `
  --model gpt-5.6-luna `
  --max-steps 1 `
  --limit 1 `
  --task-timeout-ms 90000
```

Run one configured model through the full local suite:

```powershell
python -m odysseys_eval run-suite `
  --config configs\dev_10_models.example.json `
  --model gpt-5.6-luna `
  --agent-backend agentv4
```

The adapted scorer input is written under:

```text
outputs/runs_<suite>_<model_slug>/pyautogui/screenshot/<model>/<domain>/<task_id>/
```
