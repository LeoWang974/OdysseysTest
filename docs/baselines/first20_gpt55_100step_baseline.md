# Legacy OSWorld first20 gpt-5.5 100-Step Baseline

This is a legacy OSWorld-backend first20 reproduction baseline retained for
comparison. Current and future local experiments use the AgentV4 `browser-gui`
backend; AgentV4 quality is judged by rubric score, perfect task rate, and
trajectory efficiency, not OSWorld Task SR.

## Setup

- Subset: `outputs/first20/selected_tasks.json`
- Runner output: `outputs/runs_first20_gpt55_100step_v3`
- Runner report: `outputs/reports/first20_gpt55_100step_v3_runner_report.json`
- Runner CSV: `outputs/reports/first20_gpt55_100step_v3_runner_report.csv`
- Rubric score: `outputs/scores/first20_gpt55_100step_v3_eval_max45.json`
- Rubric CSV: `outputs/scores/first20_gpt55_100step_v3_eval_max45.csv`
- Merged report: `outputs/leaderboards/first20_gpt55_100step_v3_baseline_max45.json`
- Merged CSV: `outputs/leaderboards/first20_gpt55_100step_v3_baseline_max45.csv`
- Agent model: `gpt-5.5`
- Judge model: `gpt-5.5`
- Judge backend: `openai_curl`
- OSWorld path: `D:\gitWorkSpace\OSWorld`
- Max agent steps: 100
- Judge max steps: 100
- Run date: 2026-08-04 19:06:51 to 2026-08-05 08:11:18, Asia/Shanghai
- Score date: 2026-08-05 09:17:10 to 2026-08-05 10:02:53, Asia/Shanghai

## Context And Input Size

- Runner step budget: `max_steps=100`
- Runner trajectory context: OSWorld default agent setting `max_trajectory_length=3`
- Scorer trajectory budget: `--max-steps 100`
- Scorer image cap: `--max-images 45`
- Actual runner prompt tokens: 12,233,786
- Actual runner completion tokens: 810,231
- Actual runner total tokens: 13,044,017
- Average runner total tokens per task: 652,200.85

## Summary

| Metric | Value |
|---|---:|
| Tasks | 20 |
| Completed trajectories | 20 |
| Scored tasks | 20 |
| Rubrics | 94 |
| Rubric average | 0.1170 |
| Perfect tasks | 0 |
| Perfect task rate | 0.0000 |
| Trajectory efficiency | 0.001167 |
| OSWorld task success rate | 0.0000 |
| Runner total steps | 1,988 |
| Judge parsed steps | 1,974 |
| Average merged steps | 98.7 |
| Average runner steps | 99.4 |
| Screenshots | 1,988 |
| Screenshot coverage | 100% |
| Prompt tokens | 12,233,786 |
| Completion tokens | 810,231 |
| Total tokens | 13,044,017 |
| Average tokens per task | 652,200.85 |
| Calls with usage | 1,993 |
| Runner duration | 47,061.138 seconds |
| Scorer duration | 2,219.702 seconds |
| Trajectory errors | 0 |
| Runtime error mentions | 0 |
| Console error mentions | 47 |
| Judge errored tasks | 0 |
| Judge errored rubrics | 0 |

## By Level

| Level | Tasks | Rubric avg | Perfect rate | Avg merged steps | Total tokens |
|---|---:|---:|---:|---:|---:|
| easy | 20 | 0.1170 | 0.0000 | 98.7 | 13,044,017 |

## Commands

```powershell
python -m odysseys_eval prepare `
  --task-source-json data\odysseys.json `
  --output-dir outputs\first20 `
  --limit 20 `
  --domain mind2web_chrome `
  --local-chrome-stability
```

```powershell
python -m odysseys_eval run-osworld `
  --prepared-dir outputs\first20 `
  --result-dir outputs\runs_first20_gpt55_100step_v3 `
  --model gpt-5.5 `
  --max-steps 100 `
  --path-to-vm "<local Ubuntu.qcow2 path>" `
  --write-report `
  --report-output outputs\reports\first20_gpt55_100step_v3_runner_report.json
```

```powershell
python -m odysseys_eval score `
  --runs-dir outputs\runs_first20_gpt55_100step_v3 `
  --task-source-json outputs\first20\selected_tasks.json `
  --output outputs\scores\first20_gpt55_100step_v3_eval_max45.json `
  --csv-output outputs\scores\first20_gpt55_100step_v3_eval_max45.csv `
  --model gpt-5.5 `
  --num-workers 1 `
  --max-images 45 `
  --max-steps 100 `
  --include-incomplete `
  --api-base https://tokenhub.sensetime.com/v1 `
  --use-curl-openai `
  --manifest-output outputs\scores\first20_gpt55_100step_v3_eval_max45_manifest.json
```

```powershell
python -m odysseys_eval merge-report `
  --runner-report outputs\reports\first20_gpt55_100step_v3_runner_report.json `
  --score-results outputs\scores\first20_gpt55_100step_v3_eval_max45.json `
  --task-source-json outputs\first20\selected_tasks.json `
  --output outputs\leaderboards\first20_gpt55_100step_v3_baseline_max45.json `
  --csv-output outputs\leaderboards\first20_gpt55_100step_v3_baseline_max45.csv `
  --model gpt-5.5
```

## Notes

The runner and judge pipeline completed cleanly: all 20 trajectories were
generated, all 20 tasks were scored, screenshots were continuously available,
and there were no judge API errors.

The corrected `max45` score is valid: the judge completed without rubric-level
API errors. An earlier score run with `--max-images 0` sent too many screenshots
to the judge API and returned 0 for every rubric due to `Too many images in
request`; that run should not be used as a model-quality result.

The corrected score is still low. Most tasks consumed almost the full 100-step
budget, which shows that the current OSWorld pyautogui agent can keep acting but
is not yet efficient enough for these real web tasks. Google CAPTCHA/search
blocking and repeated page-navigation loops remain important bottlenecks to
address before larger local experiments.
