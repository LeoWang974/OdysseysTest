# Local Reproduction Guide

This fork keeps the upstream Odysseys dataset and official rubric judge, then
adds `odysseys_eval` as a local orchestration layer for AgentV4 browser-agent
runs. OSWorld remains available only as a legacy comparison backend.

The intended workflow is:

1. Prepare a fixed task subset.
2. Run AgentV4 trajectories.
3. Generate a runner report.
4. Score trajectories with the Odysseys rubric judge.
5. Summarize or merge runner and judge metrics.

## Repository Layout

- `data/odysseys.json` - 200 Odysseys tasks and rubrics.
- `scripts/python/convert_odysseys_to_osworld.py` - upstream conversion script.
- `scripts/python/run_full_trajectory_per_rubric.py` - rubric judge script, with a local curl fallback for OpenAI-compatible endpoints.
- `odysseys_eval/` - local orchestration CLI.
- `configs/eval.local.example.json` - non-secret local config example.
- `outputs/` - ignored run artifacts and reports.

## Environment

Install local Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

Create `.env` locally. Do not commit it.

```text
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://tokenhub.sensetime.com/v1
OPENAI_API_BASE=https://tokenhub.sensetime.com/v1
```

Optional legacy OSWorld runs can still set `OSWORLD_PATH` and
`OSWORLD_VM_PATH`, but these are not required for the current AgentV4 pipeline.

## Doctor

```powershell
python -m odysseys_eval doctor
```

Expected dataset shape:

- 200 tasks
- 45 easy, 46 medium, 109 hard
- 1,225 total rubric items

## Stable Smoke Subset

Use `about:blank` plus Chrome stability flags to avoid spending steps on Chrome
certificate interstitials.

```powershell
python -m odysseys_eval prepare `
  --level easy `
  --limit 1 `
  --output-dir outputs\smoke_stable `
  --start-url about:blank `
  --local-chrome-stability
```

Run a smoke trajectory with AgentV4:

```powershell
python -m odysseys_eval run-agentv4 `
  --prepared-dir outputs\smoke_stable `
  --result-dir outputs\agentv4_smoke_stable_gpt56luna `
  --model gpt-5.6-luna `
  --max-steps 10
```

The runner writes:

- `run_manifest.json`
- `run_console.log`
- `pyautogui/screenshot/<model>/<domain>/<task_id>/traj.jsonl`
- screenshots
- runner report JSON/CSV when `--write-report` is enabled

## Fixed dev_10 Subset

The shared development subset is 10 tasks:

- 4 easy
- 3 medium
- 3 hard

Generate it with:

```powershell
python -m odysseys_eval prepare-dev-subset `
  --output-dir outputs\dev_10 `
  --easy 4 `
  --medium 3 `
  --hard 3 `
  --start-url about:blank `
  --local-chrome-stability
```

Current fixed task IDs:

```text
easy   440ed7f388a2a4528a8d9fb75f83e11f934b5b5d
easy   2cb0ed2a5df6053c6c982a5c5d436d25e006370f
easy   082aa17f3e88c3ce10796244e3677c5643dd19c9
easy   1211fbaa646424ab75869c0379431d5d049d2c9b
medium 63d68bb25e279fc22e6e3592d8ca59add33b6eb1
medium 53419597c0c8897d49f1af65f5255bf265edcfbf
medium 8fcdeed84a0deb05342b07c26116792a5b6a6a3f
hard   753ce2163f6e018ea33423ad4400ba3f759e9df8
hard   3add0c2ffff8e0b3cacedf2e895d213735702f62
hard   fc98d55986ef93480fb659db44e070c04f93301a
```

Run the current AgentV4 baseline through the suite wrapper:

```powershell
python -m odysseys_eval run-suite `
  --config configs\dev_10_models.example.json `
  --model gpt-5.6-luna `
  --env-file .env `
  --agent-backend agentv4
```

Follow-up experiments are run on this local Windows machine; use a dedicated
PowerShell or Windows Terminal tab for long runs.

If the outer shell or Codex tool times out but the runner lands artifacts,
finalize the run from disk:

```powershell
python -m odysseys_eval finalize-run `
  --prepared-dir outputs\dev_10 `
  --result-dir outputs\runs_dev_10_gpt56luna `
  --report-output outputs\reports\dev_10_gpt56luna_runner_report.json
```

## Runner Report

If needed, regenerate a runner report from an existing run directory:

```powershell
python -m odysseys_eval smoke-report `
  --runs-dir outputs\runs_dev_10_gpt56luna `
  --console-log outputs\runs_dev_10_gpt56luna\run_console.log `
  --output outputs\reports\dev_10_gpt56luna_runner_report.json `
  --csv-output outputs\reports\dev_10_gpt56luna_runner_report.csv
```

Runner report fields include:

- `runner_backend`
- `execution_success_rate`
- AgentV4 `exit_code`, `steps`, and screenshot counts
- raw step events and `max_step_num`
- screenshots
- prompt/completion/total tokens
- model calls with usage
- duration
- trajectory errors
- runtime and console error counters

For AgentV4 runs, OSWorld Task SR is intentionally `null`. Task quality is
measured by the Odysseys rubric judge.

## AgentV4 Runner

AgentV4 runs use the same `outputs\dev_10\selected_tasks.json` task ids and
the same downstream rubric judge. The local runner prepares the ignored
`agentv4-agent-browser-skill-framework/` directory for TokenHub, runs the
`browser-gui` CLI, and adapts AgentV4 transcripts into the OSWorld-like scorer
layout:

```text
<result_dir>\pyautogui\screenshot\<model>\mind2web_chrome\<task_id>\
```

Run a 1-task smoke:

```powershell
python -m odysseys_eval run-agentv4 `
  --prepared-dir outputs\dev_10 `
  --result-dir outputs\agentv4_smoke_adapter_gpt56luna `
  --model gpt-5.6-luna `
  --max-steps 1 `
  --limit 1
```

For TokenHub on this Windows machine, AgentV4 subprocesses set
`NODE_TLS_REJECT_UNAUTHORIZED=0` and use the OpenAI-compatible endpoint. The
manifest records these flags but never records API key values.

## Rubric Judge

For OpenAI-compatible judging through tokenhub:

```powershell
python -m odysseys_eval score `
  --runs-dir outputs\runs_dev_10_gpt56luna\pyautogui\screenshot\gpt-5.6-luna\mind2web_chrome `
  --task-source-json outputs\dev_10\selected_tasks.json `
  --output outputs\scores\dev_10_gpt56luna_eval.json `
  --csv-output outputs\scores\dev_10_gpt56luna_eval.csv `
  --model gpt-5.5 `
  --num-workers 1 `
  --max-images 45 `
  --api-base https://tokenhub.sensetime.com/v1 `
  --env-file .env
```

`score` writes a manifest next to the output JSON by default and
enables the local curl fallback automatically. Pass `--no-use-curl-openai` only
if the standard Python SDK path is stable in the current local environment.
The image cap defaults to 45 and clamps unsafe values to avoid 50-image API
limits.

Summarize an existing score file:

```powershell
python -m odysseys_eval summarize `
  --eval-results outputs\scores\dev_10_gpt56luna_eval.json `
  --csv-output outputs\scores\dev_10_gpt56luna_eval.csv
```

Judge metrics include:

- average rubric score
- perfect task rate
- trajectory efficiency
- per-difficulty breakdown

## Merged Baseline Report

Merge the runner report and rubric score into a single model-comparison table:

```powershell
python -m odysseys_eval merge-report `
  --runner-report outputs\reports\dev_10_gpt56luna_runner_report.json `
  --score-results outputs\scores\dev_10_gpt56luna_eval.json `
  --task-source-json outputs\dev_10\selected_tasks.json `
  --output outputs\leaderboards\dev_10_gpt56luna_baseline.json `
  --csv-output outputs\leaderboards\dev_10_gpt56luna_baseline.csv `
  --model gpt-5.6-luna
```

Legacy OSWorld baselines are kept under `docs/baselines/` only for comparison.

## Multi-model dev_10 Runs

For model comparisons, keep the evaluation surface fixed:

- Same task ids: `outputs\dev_10\selected_tasks.json`
- Same prepared task metadata: `outputs\dev_10\test_all.json`
- Same agent step budget: `max_steps=30`
- Same judge model: `gpt-5.5`
- Same judge step budget: `max_steps=100`
- Same judge concurrency by default: `num_workers=1`

The configured test agent backend is now `agentv4`, using
`agentv4-agent-browser-skill-framework/` and the registered `browser-gui`
agent. Run the integration doctor first:

```powershell
python -m odysseys_eval agentv4-doctor
```

See [`docs/agentv4_integration.md`](agentv4_integration.md) for the directory
analysis, Windows setup notes, and current readiness checks.

The model matrix lives in
[`configs/dev_10_models.example.json`](../configs/dev_10_models.example.json).
It defines the shared subset, judge settings, AgentV4 defaults, output
directories, and model slugs.

Output naming convention:

```text
outputs\runs_dev_10_<model_slug>\
outputs\reports\dev_10_<model_slug>_runner_report.json
outputs\scores\dev_10_<model_slug>_eval.json
outputs\scores\dev_10_<model_slug>_eval.csv
outputs\leaderboards\dev_10_<model_slug>_baseline.json
outputs\leaderboards\dev_10_<model_slug>_baseline.csv
```

Before launching a long run, inspect the resolved plan:

```powershell
python -m odysseys_eval run-suite `
  --config configs\dev_10_models.example.json `
  --model gpt-5.5 `
  --env-file .env `
  --dry-run
```

Run one model end-to-end:

```powershell
python -m odysseys_eval run-suite `
  --config configs\dev_10_models.example.json `
  --model gpt-5.5 `
  --env-file .env
```

`run-suite` executes:

1. `run-agentv4`
2. `finalize-run`
3. `score`
4. `merge-report`

For local long runs, launch this command in a dedicated terminal. `run-suite`
starts a real AgentV4 browser run; use `merge-report` when you only want to recombine
existing runner and judge artifacts.

## Local Experiment Checklist

1. Keep this repository and the AgentV4 framework on the local machine.
2. Keep the AgentV4 framework directory ignored by Git.
3. Keep API keys in local `.env`; never commit it.
4. Run `python -m odysseys_eval doctor`.
5. Run `python -m odysseys_eval agentv4-doctor` before AgentV4 runs.
6. Start with a 1-task smoke trajectory after any agent/backend change.
7. Run `dev_10` only after smoke passes.
8. Run rubric judge, `merge-report`, and inspect the unified table.

## Known Local Issues

- AgentV4 browser commands run through the Windows PowerShell tool and local
  `agent-browser` binary.
- Long `dev_10` runs can exceed local tool/terminal timeouts.
- Some websites trigger Google CAPTCHA / unusual traffic pages. Chrome
  certificate interstitials are handled by `--local-chrome-stability`.
- Some local Windows Python SDK imports hit OpenSSL Applink issues. The
  `ODYSSEYS_USE_CURL_OPENAI=1` fallback avoids this for the rubric judge.
