# AgentV4 smoke_3task gpt-5.6-luna Baseline

This is a small AgentV4 backend smoke baseline retained as the current metric
shape example. It is not a full dev_10 result.

## Setup

- Backend: `agentv4`
- Agent: `browser-gui`
- Agent model: `gpt-5.6-luna`
- Judge model: `gpt-5.5`
- Task source: `outputs/dev_10/selected_tasks.json`
- Runner output: `outputs/agentv4_smoke_3task_gpt56luna`
- Runner report: `outputs/reports/agentv4_smoke_3task_gpt56luna_report_agentv4fields.json`
- Rubric score: `outputs/scores/agentv4_smoke_3task_gpt56luna_eval.json`
- Merged report: `outputs/leaderboards/agentv4_smoke_3task_gpt56luna_baseline_agentv4fields.json`
- Agent max steps: 10
- Judge max steps: 100
- Judge max images: 45

## Summary

| Metric | Value |
|---|---:|
| Tasks | 3 |
| Scored tasks | 3 |
| Rubrics | 12 |
| Execution success rate | 1.0000 |
| OSWorld task success rate | null |
| Rubric average | 0.0000 |
| Perfect tasks | 0 |
| Perfect task rate | 0.0000 |
| Trajectory efficiency | 0.000000 |
| Runner total steps | 30 |
| Total screenshots | 27 |
| Average steps | 10.0 |
| Trajectory errors | 0 |
| Runtime error mentions | 0 |
| Judge errored rubrics | 0 |
| Tasks with judge errors | 0 |

## Notes

For AgentV4, `execution_success_rate` means the runner produced usable
trajectories and screenshots. Task quality is measured by the Odysseys rubric
judge: average rubric score, perfect task rate, and trajectory efficiency.
OSWorld Task SR is intentionally null for this backend.
