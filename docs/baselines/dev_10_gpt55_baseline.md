# Legacy OSWorld dev_10 gpt-5.5 Baseline

This is a legacy OSWorld-backend baseline retained for comparison. Current and
future local experiments use the AgentV4 `browser-gui` backend, so OSWorld Task
SR in this document should not be used as the AgentV4 success metric.

## Setup

- Subset: `outputs/dev_10/selected_tasks.json`
- Runner output: `outputs/runs_dev_10_gpt55`
- Runner report: `outputs/reports/dev_10_gpt55_runner_report.json`
- Rubric score: `outputs/scores/dev_10_gpt55_eval.json`
- Merged report: `outputs/leaderboards/dev_10_gpt55_baseline.json`
- Merged CSV: `outputs/leaderboards/dev_10_gpt55_baseline.csv`
- Agent model: `gpt-5.5`
- Judge model: `gpt-5.5`
- Judge backend: `openai_curl`
- Max agent steps: 30
- Judge max steps: 100

## Summary

| Metric | Value |
|---|---:|
| Tasks | 10 |
| Scored tasks | 10 |
| Rubrics | 58 |
| Rubric average | 0.0000 |
| Perfect tasks | 0 |
| Perfect task rate | 0.0000 |
| Trajectory efficiency | 0.000000 |
| OSWorld task success rate | 0.0000 |
| Runner total steps | 298 |
| Judge total steps | 295 |
| Average judge steps | 29.5 |
| Prompt tokens | 1,754,521 |
| Completion tokens | 117,531 |
| Total tokens | 1,872,052 |
| Average tokens per task | 187,205.2 |
| Calls with usage | 298 |
| Runner duration | 9,800.14 seconds |
| Trajectory errors | 0 |
| Runtime error mentions | 0 |
| Judge errored tasks | 0 |

## By Level

| Level | Tasks | Rubric avg | Perfect rate | Avg judge steps | Total tokens |
|---|---:|---:|---:|---:|---:|
| easy | 4 | 0.0000 | 0.0000 | 29.75 | 740,256 |
| medium | 3 | 0.0000 | 0.0000 | 30.00 | 569,285 |
| hard | 3 | 0.0000 | 0.0000 | 28.67 | 562,511 |

## Commands

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

```powershell
python -m odysseys_eval merge-report `
  --runner-report outputs\reports\dev_10_gpt55_runner_report.json `
  --score-results outputs\scores\dev_10_gpt55_eval.json `
  --task-source-json outputs\dev_10\selected_tasks.json `
  --output outputs\leaderboards\dev_10_gpt55_baseline.json `
  --csv-output outputs\leaderboards\dev_10_gpt55_baseline.csv `
  --model gpt-5.5
```

## Notes

The judge pipeline completed cleanly: all 10 tasks were scored, screenshot
counts matched available trajectory steps, and there were no judge API errors.
The all-zero score reflects the current baseline trajectory quality rather than
a scorer failure. Common blockers observed in rubric reasoning included Google
CAPTCHA/unusual-traffic pages, Cloudflare or human-verification pages,
site-level 403 responses, and missing final summaries or CryptPad documents.
