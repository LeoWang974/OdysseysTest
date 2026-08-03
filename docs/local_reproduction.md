# Local Reproduction Guide

This fork keeps the upstream Odysseys dataset and official rubric judge, then
adds `odysseys_eval` as a local orchestration layer for OSWorld runs.

The intended workflow is:

1. Prepare a fixed task subset.
2. Run OSWorld trajectories.
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
OSWORLD_PATH=D:\gitWorkSpace\OSWorld
OSWORLD_VM_PATH=C:\path\to\Ubuntu.qcow2
```

For OSWorld, keep the VM image outside Git. `run-osworld` reads
`OSWORLD_VM_PATH` from `.env`; use `--path-to-vm` only when overriding it for a
specific run.


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

Run a smoke trajectory:

```powershell
python -m odysseys_eval run-osworld `
  --prepared-dir outputs\smoke_stable `
  --result-dir outputs\runs_smoke_stable_gpt55 `
  --model gpt-5.5 `
  --max-steps 10
```

The runner writes:

- `run_manifest.json`
- `run_console.log`
- `pyautogui/screenshot/<model>/<domain>/<task_id>/traj.jsonl`
- screenshots
- `summary/results.json`
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

Run the baseline:

```powershell
python -m odysseys_eval run-osworld `
  --prepared-dir outputs\dev_10 `
  --result-dir outputs\runs_dev_10_gpt55 `
  --model gpt-5.5 `
  --max-steps 30
```

The local Windows run completed 10/10 trajectories and consumed about 1.87M
tokens over about 2h43m. Server runs should be launched inside `tmux` or
`screen` to avoid terminal/tool timeout.

If the outer shell or Codex tool times out but OSWorld keeps running and lands
artifacts, finalize the run from disk:

```powershell
python -m odysseys_eval finalize-run `
  --prepared-dir outputs\dev_10 `
  --result-dir outputs\runs_dev_10_gpt55 `
  --report-output outputs\reports\dev_10_gpt55_runner_report.json
```

## Runner Report

If needed, regenerate a runner report from an existing run directory:

```powershell
python -m odysseys_eval smoke-report `
  --runs-dir outputs\runs_dev_10_gpt55 `
  --console-log outputs\runs_dev_10_gpt55\run_console.log `
  --output outputs\reports\dev_10_gpt55_runner_report.json `
  --csv-output outputs\reports\dev_10_gpt55_runner_report.csv
```

Runner report fields include:

- OSWorld `task_success_rate`
- raw step events and `max_step_num`
- screenshots
- prompt/completion/total tokens
- model calls with usage
- duration
- trajectory errors
- runtime and console error counters

## Rubric Judge

For OpenAI-compatible judging through tokenhub:

```powershell
python -m odysseys_eval score `
  --runs-dir outputs\runs_dev_10_gpt55\pyautogui\screenshot\gpt-5.5\mind2web_chrome `
  --task-source-json outputs\dev_10\selected_tasks.json `
  --output outputs\scores\dev_10_gpt55_eval.json `
  --csv-output outputs\scores\dev_10_gpt55_eval.csv `
  --model gpt-5.5 `
  --num-workers 1 `
  --api-base https://tokenhub.sensetime.com/v1 `
  --env-file .env
```

`score` writes `outputs\scores\dev_10_gpt55_eval.manifest.json` by default and
enables the local curl fallback automatically. Pass `--no-use-curl-openai` on a
server if the standard Python SDK path is stable.

Summarize an existing score file:

```powershell
python -m odysseys_eval summarize `
  --eval-results outputs\scores\dev_10_gpt55_eval.json `
  --csv-output outputs\scores\dev_10_gpt55_eval.csv
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
  --runner-report outputs\reports\dev_10_gpt55_runner_report.json `
  --score-results outputs\scores\dev_10_gpt55_eval.json `
  --task-source-json outputs\dev_10\selected_tasks.json `
  --output outputs\leaderboards\dev_10_gpt55_baseline.json `
  --csv-output outputs\leaderboards\dev_10_gpt55_baseline.csv `
  --model gpt-5.5
```

The fixed local baseline is summarized in
[`docs/baselines/dev_10_gpt55_baseline.md`](baselines/dev_10_gpt55_baseline.md).

## Server Migration Checklist

1. Push this repository to GitHub.
2. Clone the repo on the server.
3. Clone OSWorld on the server and set `OSWORLD_PATH`.
4. Place the VM image outside the Git repo.
5. Create `.env` on the server with API keys and base URLs.
6. Install Python dependencies.
7. Run `python -m odysseys_eval doctor`.
8. Run a 1-task smoke trajectory.
9. Run `dev_10`.
10. Run rubric judge and collect reports.

## Known Local Issues

- Windows Docker provider has no KVM acceleration and is slow.
- Long `dev_10` runs can exceed local tool/terminal timeouts.
- OSWorld recording stop sometimes emits 400/500 errors; trajectory artifacts
  still land correctly.
- Some websites trigger Google CAPTCHA / unusual traffic pages. Chrome
  certificate interstitials are handled by `--local-chrome-stability`.
- Some local Windows Python SDK imports hit OpenSSL Applink issues. The
  `ODYSSEYS_USE_CURL_OPENAI=1` fallback avoids this for the rubric judge, and
  `OSWORLD_USE_CURL_OPENAI=1` is used for OSWorld agent calls.
