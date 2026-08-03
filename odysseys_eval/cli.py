from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASKS = REPO_ROOT / "data" / "odysseys.json"
DEFAULT_EXAMPLES = REPO_ROOT / "data" / "odysseys_cua_final"
CONVERT_SCRIPT = REPO_ROOT / "scripts" / "python" / "convert_odysseys_to_osworld.py"
JUDGE_SCRIPT = REPO_ROOT / "scripts" / "python" / "run_full_trajectory_per_rubric.py"
LOCAL_CHROME_STABILITY_ARGS = [
    "--ignore-certificate-errors",
    "--ignore-ssl-errors",
    "--allow-running-insecure-content",
    "--test-type",
]


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_text_auto(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(b"\xff\xfe") or data[:200].count(b"\x00") > 20:
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8", errors="replace")


def repair_mojibake(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        repaired = value.encode("gbk").decode("utf-8")
    except UnicodeError:
        return value
    return repaired if Path(repaired).exists() else value


def load_dotenv_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        value = shlex.split(value, posix=False)[0] if value.strip() else ""
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def filter_tasks(tasks: list[dict[str, Any]], level: str | None, limit: int | None) -> list[dict[str, Any]]:
    selected = [task for task in tasks if level is None or task.get("level") == level]
    if limit is not None:
        selected = selected[:limit]
    return selected


def apply_prepare_patches(output_dir: Path, domain: str, start_url: str | None, chrome_args: list[str]) -> None:
    if not start_url and not chrome_args:
        return
    examples_dir = output_dir / "osworld_examples" / "examples" / domain
    for example_path in examples_dir.glob("*.json"):
        example = load_json(example_path)
        example = patch_example_for_local_browser(example, start_url, chrome_args)
        write_json(example_path, example)


def convert_selected_tasks(selected: list[dict[str, Any]], output_dir: Path, domain: str) -> None:
    subset_path = output_dir / "selected_tasks.json"
    write_json(subset_path, selected)
    command = [
        sys.executable,
        str(CONVERT_SCRIPT),
        "--input",
        str(subset_path),
        "--output-dir",
        str(output_dir / "osworld_examples"),
        "--domain",
        domain,
    ]
    subprocess.run(command, cwd=REPO_ROOT, check=True)
    write_json(output_dir / "test_all.json", {domain: [task["task_id"] for task in selected]})


def chrome_args_from_args(args: argparse.Namespace) -> list[str]:
    chrome_args = list(args.chrome_arg)
    if args.local_chrome_stability:
        chrome_args.extend(arg for arg in LOCAL_CHROME_STABILITY_ARGS if arg not in chrome_args)
    return chrome_args


def patch_example_for_local_browser(example: dict[str, Any], start_url: str | None, chrome_args: list[str]) -> dict[str, Any]:
    if start_url:
        example["source"] = start_url
        metadata = example.setdefault("metadata", {})
        metadata["website"] = start_url

    for step in example.get("config", []):
        if step.get("type") == "chrome_open_tabs" and start_url:
            step.setdefault("parameters", {})["urls_to_open"] = [start_url]
        if step.get("type") != "launch":
            continue
        command = step.setdefault("parameters", {}).get("command")
        if not isinstance(command, list) or not command or command[0] != "google-chrome":
            continue
        for arg in chrome_args:
            if arg and arg not in command:
                command.append(arg)
    return example


def has_module(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        return False


def git_info(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {"path": str(path)}
    try:
        head = subprocess.run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"], text=True, capture_output=True, check=True)
        status = subprocess.run(["git", "-C", str(path), "status", "--short"], text=True, capture_output=True, check=True)
        info["commit"] = head.stdout.strip()
        info["dirty"] = bool(status.stdout.strip())
        info["status_short"] = status.stdout.splitlines()
    except Exception as exc:
        info["error"] = str(exc)
    return info


def infer_env_provider(model: str) -> dict[str, str | None]:
    base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE") or os.getenv("NEWAPI_BASE_URL")
    provider = "unknown"
    if base_url and "tokenhub.sensetime.com" in base_url:
        provider = "tokenhub.sensetime.com"
    elif base_url:
        provider = re.sub(r"^https?://", "", base_url).split("/")[0]
    elif model.lower().startswith("gemini"):
        provider = "google"
    elif model.lower().startswith("claude"):
        provider = "anthropic"
    elif model.lower().startswith("gpt"):
        provider = "openai"
    return {"provider": provider, "base_url": base_url}


def resolve_repo_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    return path if path.is_absolute() else REPO_ROOT / path


def expected_tasks_from_prepared(prepared_dir: Path | None) -> dict[str, Any]:
    if not prepared_dir:
        return {"expected_tasks": None, "expected_task_ids": []}
    meta_path = prepared_dir / "test_all.json"
    if not meta_path.is_file():
        return {"expected_tasks": None, "expected_task_ids": []}
    meta = load_json(meta_path)
    task_ids: list[str] = []
    if isinstance(meta, dict):
        for value in meta.values():
            if isinstance(value, list):
                task_ids.extend(str(item) for item in value)
    return {"expected_tasks": len(task_ids), "expected_task_ids": task_ids}


def completion_from_runs(result_dir: Path) -> dict[str, Any]:
    summary_path = result_dir / "summary" / "results.json"
    summary_rows = load_json(summary_path) if summary_path.is_file() else []
    if not isinstance(summary_rows, list):
        summary_rows = []
    summary_task_ids = [str(row.get("task_id")) for row in summary_rows if isinstance(row, dict) and row.get("task_id")]
    traj_task_ids = sorted({path.parent.name for path in result_dir.rglob("traj.jsonl")})
    completed_task_ids = sorted(set(summary_task_ids) | set(traj_task_ids))
    return {
        "completed_tasks": len(completed_task_ids),
        "completed_task_ids": completed_task_ids,
        "summary_results_path": str(summary_path) if summary_path.is_file() else None,
        "summary_results_count": len(summary_rows),
        "trajectory_task_count": len(traj_task_ids),
    }


def finalize_run_manifest(
    manifest_path: Path,
    result_dir: Path,
    prepared_dir: Path | None,
    *,
    exit_code: int | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    status_hint: str | None = None,
) -> dict[str, Any]:
    manifest = load_json(manifest_path) if manifest_path.is_file() else {"schema_version": 1}
    expected = expected_tasks_from_prepared(prepared_dir)
    completion = completion_from_runs(result_dir)
    expected_count = expected["expected_tasks"]
    completed_count = completion["completed_tasks"]

    manifest.update(expected)
    manifest.update(completion)
    if exit_code is not None:
        manifest["exit_code"] = exit_code
    if end_time is not None:
        manifest["end_time"] = end_time.isoformat(timespec="seconds").replace("+00:00", "Z")
    elif not manifest.get("end_time"):
        manifest["end_time"] = utc_now_iso()
    if start_time and end_time:
        manifest["duration_seconds"] = round((end_time - start_time).total_seconds(), 3)

    if status_hint:
        manifest["status"] = status_hint
    elif expected_count and completed_count >= expected_count:
        manifest["status"] = "success" if manifest.get("exit_code") == 0 else "success_inferred"
    elif completed_count:
        manifest["status"] = "partial"
    elif manifest.get("exit_code") not in (None, 0):
        manifest["status"] = "failed"
    else:
        manifest["status"] = manifest.get("status", "unknown")

    write_json(manifest_path, manifest)
    return manifest


def cmd_doctor(_: argparse.Namespace) -> int:
    load_dotenv_file(REPO_ROOT / ".env")
    print(f"repo_root: {REPO_ROOT}")
    print(f"tasks_json: {DEFAULT_TASKS} ({'ok' if DEFAULT_TASKS.exists() else 'missing'})")
    print(f"convert_script: {CONVERT_SCRIPT} ({'ok' if CONVERT_SCRIPT.exists() else 'missing'})")
    print(f"judge_script: {JUDGE_SCRIPT} ({'ok' if JUDGE_SCRIPT.exists() else 'missing'})")

    if DEFAULT_TASKS.exists():
        tasks = load_json(DEFAULT_TASKS)
        levels = Counter(task.get("level", "unknown") for task in tasks)
        rubric_count = sum(len(task.get("rubrics", {})) for task in tasks)
        print(f"dataset: {len(tasks)} tasks, {rubric_count} rubrics, levels={dict(levels)}")

    for module in ("google.genai", "openai", "dotenv"):
        state = "ok" if has_module(module) else "missing"
        print(f"python_module: {module}={state}")

    for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY"):
        print(f"env: {env_name}={'set' if os.getenv(env_name) else 'unset'}")

    osworld_path = os.getenv("OSWORLD_PATH")
    print(f"env: OSWORLD_PATH={osworld_path or 'unset'}")
    vm_path = os.getenv("OSWORLD_VM_PATH")
    print(f"env: OSWORLD_VM_PATH={vm_path or 'unset'}")
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    tasks = load_json(args.task_source_json)
    selected = filter_tasks(tasks, args.level, args.limit)
    if not selected:
        raise SystemExit("No tasks selected. Check --level/--limit.")

    convert_selected_tasks(selected, args.output_dir, args.domain)
    chrome_args = chrome_args_from_args(args)
    apply_prepare_patches(args.output_dir, args.domain, args.start_url, chrome_args)

    levels = Counter(task.get("level", "unknown") for task in selected)
    print(f"Prepared {len(selected)} task(s) at {args.output_dir}")
    print(f"Levels: {dict(levels)}")
    if args.start_url:
        print(f"Start URL override: {args.start_url}")
    if chrome_args:
        print(f"Chrome args: {chrome_args}")
    print(f"Task source for scoring: {args.output_dir / 'selected_tasks.json'}")
    print(f"OSWorld meta: {args.output_dir / 'test_all.json'}")
    return 0


def cmd_prepare_dev_subset(args: argparse.Namespace) -> int:
    tasks = load_json(args.task_source_json)
    selected = []
    for level, count in (("easy", args.easy), ("medium", args.medium), ("hard", args.hard)):
        level_tasks = [task for task in tasks if task.get("level") == level]
        if len(level_tasks) < count:
            raise SystemExit(f"Not enough {level} tasks: requested {count}, found {len(level_tasks)}")
        selected.extend(level_tasks[:count])
    convert_selected_tasks(selected, args.output_dir, args.domain)
    chrome_args = chrome_args_from_args(args)
    apply_prepare_patches(args.output_dir, args.domain, args.start_url, chrome_args)

    levels = Counter(task.get("level", "unknown") for task in selected)
    print(f"Prepared dev subset at {args.output_dir}")
    print(f"Tasks: {len(selected)}, levels={dict(levels)}")
    print("Task IDs:")
    for task in selected:
        print(f"  {task['level']}: {task['task_id']}")
    if args.start_url:
        print(f"Start URL override: {args.start_url}")
    if chrome_args:
        print(f"Chrome args: {chrome_args}")
    print(f"Task source for scoring: {args.output_dir / 'selected_tasks.json'}")
    print(f"OSWorld meta: {args.output_dir / 'test_all.json'}")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    env_file = args.env_file or (REPO_ROOT / ".env")
    load_dotenv_file(env_file)
    api_base = args.api_base or os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE") or os.getenv("NEWAPI_BASE_URL")
    output = resolve_repo_path(args.output).resolve()
    runs_dir = resolve_repo_path(args.runs_dir).resolve()
    task_source_json = resolve_repo_path(args.task_source_json).resolve()
    manifest_output = resolve_repo_path(args.manifest_output)
    if manifest_output is None:
        manifest_output = output.with_suffix(".manifest.json")
    manifest_output = manifest_output.resolve()
    command = [
        sys.executable,
        str(JUDGE_SCRIPT),
        "--model",
        args.model,
        "--runs-dir",
        str(runs_dir),
        "--task-source-json",
        str(task_source_json),
        "--output",
        str(output),
        "--num-workers",
        str(args.num_workers),
        "--max-images",
        str(args.max_images),
        "--max-steps",
        str(args.max_steps),
    ]
    if args.include_incomplete:
        command.append("--include-incomplete")
    if api_base:
        command.extend(["--api-base", api_base])
    if env_file.is_file():
        command.extend(["--env-file", str(env_file)])

    env = os.environ.copy()
    if args.use_curl_openai:
        env["ODYSSEYS_USE_CURL_OPENAI"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")

    start = datetime.now(timezone.utc)
    manifest = {
        "schema_version": 1,
        "status": "running",
        "model": args.model,
        "runs_dir": str(runs_dir),
        "task_source_json": str(task_source_json),
        "output": str(output),
        "num_workers": args.num_workers,
        "max_images": args.max_images,
        "max_steps": args.max_steps,
        "include_incomplete": args.include_incomplete,
        "api_base": api_base,
        "env_provider": infer_env_provider(args.model),
        "env_flags": {
            "ODYSSEYS_USE_CURL_OPENAI": env.get("ODYSSEYS_USE_CURL_OPENAI"),
            "PYTHONIOENCODING": env.get("PYTHONIOENCODING"),
        },
        "command": command,
        "start_time": start.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "end_time": None,
        "duration_seconds": None,
        "exit_code": None,
        "git": {"odysseys": git_info(REPO_ROOT)},
    }
    write_json(manifest_output, manifest)

    print("Running rubric judge...")
    completed = subprocess.run(command, cwd=REPO_ROOT, env=env)
    end = datetime.now(timezone.utc)
    manifest["status"] = "success" if completed.returncode == 0 else "failed"
    manifest["end_time"] = end.isoformat(timespec="seconds").replace("+00:00", "Z")
    manifest["duration_seconds"] = round((end - start).total_seconds(), 3)
    manifest["exit_code"] = completed.returncode
    write_json(manifest_output, manifest)
    print(f"Score manifest: {manifest_output}")

    if completed.returncode == 0 and args.csv_output:
        summarize_args = argparse.Namespace(eval_results=output, csv_output=resolve_repo_path(args.csv_output))
        cmd_summarize(summarize_args)
    return completed.returncode


def cmd_summarize(args: argparse.Namespace) -> int:
    payload = load_json(args.eval_results)
    summary = payload.get("summary", {})
    tasks = payload.get("tasks", [])

    print(json.dumps(summary, indent=2, ensure_ascii=False))

    if args.csv_output:
        args.csv_output.parent.mkdir(parents=True, exist_ok=True)
        try:
            with args.csv_output.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=[
                        "task_id",
                        "num_steps",
                        "average_rubric_score",
                        "perfect",
                        "num_screenshots_sent",
                        "error",
                    ],
                )
                writer.writeheader()
                for item in tasks:
                    writer.writerow({
                        "task_id": item.get("task_id"),
                        "num_steps": item.get("num_steps"),
                        "average_rubric_score": item.get("average_rubric_score"),
                        "perfect": item.get("perfect"),
                        "num_screenshots_sent": item.get("num_screenshots_sent"),
                        "error": item.get("error", ""),
                    })
            print(f"Wrote CSV: {args.csv_output}")
        except PermissionError as exc:
            print(f"WARNING: could not write CSV because the file is locked: {args.csv_output} ({exc})")
    return 0


def cmd_reproduce(args: argparse.Namespace) -> int:
    print("Paper reproduction checklist")
    print("1. Prepare or reuse OSWorld examples from data/odysseys_cua_final.")
    print("2. Run each selected task in OSWorld with Chrome, 100-step budget, and your target model adapter.")
    print("3. Save each trajectory as a run directory containing steps.jsonl or traj.jsonl, screenshots, and result.txt.")
    print("4. Score trajectories with:")
    print(
        "   python -m odysseys_eval score "
        "--runs-dir <runs_dir> "
        "--task-source-json data/odysseys.json "
        "--output outputs/scores/eval_results_full_traj_per_rubric.json "
        "--model gemini-3.1-flash-lite-preview "
        "--num-workers 16"
    )
    print("5. Summarize with:")
    print("   python -m odysseys_eval summarize --eval-results outputs/scores/eval_results_full_traj_per_rubric.json")
    if args.smoke:
        print("\nSmoke subset command:")
        print("   python -m odysseys_eval prepare --level easy --limit 3 --output-dir outputs/smoke")
    return 0


def cmd_osworld_command(args: argparse.Namespace) -> int:
    load_dotenv_file(REPO_ROOT / ".env")
    osworld_path_raw = args.osworld_path or os.getenv("OSWORLD_PATH")
    if not osworld_path_raw:
        raise SystemExit("OSWORLD_PATH is not set. Add it to .env or pass --osworld-path.")
    osworld_path = Path(osworld_path_raw)
    prepared_dir = args.prepared_dir
    if not prepared_dir.is_absolute():
        prepared_dir = REPO_ROOT / prepared_dir
    test_base = (prepared_dir / "osworld_examples").resolve()
    meta_path = (prepared_dir / "test_all.json").resolve()
    result_dir = args.result_dir
    if not result_dir.is_absolute():
        result_dir = REPO_ROOT / result_dir
    result_dir = result_dir.resolve()
    command = [
        sys.executable,
        "run.py",
        "--provider_name",
        args.provider_name,
        "--headless",
        "--observation_type",
        args.observation_type,
        "--model",
        args.model,
        "--sleep_after_execution",
        str(args.sleep_after_execution),
        "--max_steps",
        str(args.max_steps),
        "--result_dir",
        str(result_dir),
        "--test_config_base_dir",
        str(test_base),
        "--test_all_meta_path",
        str(meta_path),
        "--domain",
        args.domain,
    ]
    vm_path = repair_mojibake(args.path_to_vm or os.getenv("OSWORLD_VM_PATH"))
    if vm_path:
        command.extend(["--path_to_vm", vm_path])
    print(f"cd {osworld_path}")
    print(" ".join(shlex.quote(part) for part in command))
    return 0


def build_osworld_run_command(
    osworld_python: str,
    result_dir: Path,
    prepared_dir: Path,
    args: argparse.Namespace,
    vm_path: str | None,
) -> list[str]:
    test_base = (prepared_dir / "osworld_examples").resolve()
    meta_path = (prepared_dir / "test_all.json").resolve()
    command = [
        osworld_python,
        "run.py",
        "--provider_name",
        args.provider_name,
        "--observation_type",
        args.observation_type,
        "--model",
        args.model,
        "--sleep_after_execution",
        str(args.sleep_after_execution),
        "--max_steps",
        str(args.max_steps),
        "--result_dir",
        str(result_dir),
        "--test_config_base_dir",
        str(test_base),
        "--test_all_meta_path",
        str(meta_path),
        "--domain",
        args.domain,
    ]
    if args.headless:
        command.append("--headless")
    if vm_path:
        command.extend(["--path_to_vm", vm_path])
    return command


def cmd_run_osworld(args: argparse.Namespace) -> int:
    env_file = args.env_file or (REPO_ROOT / ".env")
    load_dotenv_file(env_file)
    osworld_path_raw = args.osworld_path or os.getenv("OSWORLD_PATH")
    if not osworld_path_raw:
        raise SystemExit("OSWORLD_PATH is not set. Add it to .env or pass --osworld-path.")
    osworld_path = Path(osworld_path_raw).resolve()
    if not (osworld_path / "run.py").is_file():
        raise SystemExit(f"OSWorld run.py not found under: {osworld_path}")

    prepared_dir = args.prepared_dir if args.prepared_dir.is_absolute() else (REPO_ROOT / args.prepared_dir)
    prepared_dir = prepared_dir.resolve()
    if not (prepared_dir / "test_all.json").is_file():
        raise SystemExit(f"Prepared subset missing test_all.json: {prepared_dir}")

    result_dir = args.result_dir if args.result_dir.is_absolute() else (REPO_ROOT / args.result_dir)
    result_dir = result_dir.resolve()
    result_dir.mkdir(parents=True, exist_ok=True)
    console_log = result_dir / "run_console.log"
    manifest_path = result_dir / "run_manifest.json"
    report_output = resolve_repo_path(args.report_output)
    vm_path = repair_mojibake(args.path_to_vm or os.getenv("OSWORLD_VM_PATH"))

    osworld_python = args.python or sys.executable
    command = build_osworld_run_command(osworld_python, result_dir, prepared_dir, args, vm_path)
    env = os.environ.copy()
    if args.openai_base_url:
        env["OPENAI_BASE_URL"] = args.openai_base_url
        env["OPENAI_API_BASE"] = args.openai_base_url
    env.setdefault("OPENAI_BASE_URL", env.get("OPENAI_API_BASE", "https://tokenhub.sensetime.com/v1"))
    env.setdefault("OPENAI_API_BASE", env["OPENAI_BASE_URL"])
    if args.use_curl_openai:
        env["OSWORLD_USE_CURL_OPENAI"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    docker_bin = r"C:\Program Files\Docker\Docker\resources\bin"
    if docker_bin not in env.get("PATH", ""):
        env["PATH"] = env.get("PATH", "") + os.pathsep + docker_bin

    start_time = utc_now_iso()
    manifest = {
        "schema_version": 1,
        "status": "running",
        "model": args.model,
        "max_steps": args.max_steps,
        "prepared_subset": str(prepared_dir),
        "result_dir": str(result_dir),
        "osworld_path": str(osworld_path),
        "vm_path": vm_path,
        "provider_name": args.provider_name,
        "observation_type": args.observation_type,
        "domain": args.domain,
        "headless": args.headless,
        "sleep_after_execution": args.sleep_after_execution,
        "start_time": start_time,
        "end_time": None,
        "duration_seconds": None,
        "exit_code": None,
        "env_provider": infer_env_provider(args.model),
        "env_flags": {
            "OSWORLD_USE_CURL_OPENAI": env.get("OSWORLD_USE_CURL_OPENAI"),
            "PYTHONIOENCODING": env.get("PYTHONIOENCODING"),
        },
        "command": command,
        "console_log": str(console_log),
        "git": {
            "odysseys": git_info(REPO_ROOT),
            "osworld": git_info(osworld_path),
        },
    }
    manifest.update(expected_tasks_from_prepared(prepared_dir))
    write_json(manifest_path, manifest)

    print(f"Running OSWorld: {result_dir}")
    print(f"Manifest: {manifest_path}")
    started = datetime.now(timezone.utc)
    with console_log.open("w", encoding="utf-8", errors="replace") as log:
        completed = subprocess.run(command, cwd=osworld_path, env=env, stdout=log, stderr=subprocess.STDOUT)
    ended = datetime.now(timezone.utc)

    manifest = finalize_run_manifest(
        manifest_path,
        result_dir,
        prepared_dir,
        exit_code=completed.returncode,
        start_time=started,
        end_time=ended,
    )

    print(f"OSWorld exit_code={completed.returncode}, duration_seconds={manifest['duration_seconds']}")
    print(f"Console log: {console_log}")
    if args.write_report:
        report_output = report_output or (REPO_ROOT / "outputs" / "reports" / f"{result_dir.name}_report.json")
        csv_output = report_output.with_suffix(".csv")
        report_args = argparse.Namespace(
            runs_dir=result_dir,
            console_log=console_log,
            output=report_output,
            csv_output=csv_output,
        )
        cmd_smoke_report(report_args)
    return completed.returncode


def cmd_finalize_run(args: argparse.Namespace) -> int:
    result_dir = resolve_repo_path(args.result_dir).resolve()
    prepared_dir = resolve_repo_path(args.prepared_dir)
    if prepared_dir:
        prepared_dir = prepared_dir.resolve()
    manifest_path = resolve_repo_path(args.manifest) or (result_dir / "run_manifest.json")
    manifest_path = manifest_path.resolve()
    manifest = finalize_run_manifest(
        manifest_path,
        result_dir,
        prepared_dir,
        exit_code=args.exit_code,
        status_hint=args.status,
    )
    print(f"Finalized run manifest: {manifest_path}")
    print(json.dumps({
        "status": manifest.get("status"),
        "expected_tasks": manifest.get("expected_tasks"),
        "completed_tasks": manifest.get("completed_tasks"),
        "summary_results_count": manifest.get("summary_results_count"),
        "trajectory_task_count": manifest.get("trajectory_task_count"),
    }, indent=2, ensure_ascii=False))

    if args.write_report:
        console_log = resolve_repo_path(args.console_log) or (result_dir / "run_console.log")
        report_output = resolve_repo_path(args.report_output) or (REPO_ROOT / "outputs" / "reports" / f"{result_dir.name}_report.json")
        report_args = argparse.Namespace(
            runs_dir=result_dir,
            console_log=console_log,
            output=report_output,
            csv_output=report_output.with_suffix(".csv") if args.write_csv else None,
        )
        cmd_smoke_report(report_args)
    return 0


def parse_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.is_file():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                rows.append({"parse_error": line})
    return rows


def extract_usage(text: str) -> dict[str, int]:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls_with_usage": 0}
    for match in re.finditer(r"LLM_USAGE:\s*(\{.*?\})(?:\r?\n|$)", text):
        try:
            usage = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        totals["calls_with_usage"] += 1
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                totals[key] += value
    return totals


def extract_console_duration_seconds(text: str) -> float | None:
    timestamps = []
    pattern = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})")
    from datetime import datetime

    for match in pattern.finditer(text):
        timestamps.append(datetime.strptime(".".join(match.groups()), "%Y-%m-%d %H:%M:%S.%f"))
    if len(timestamps) < 2:
        return None
    return round((max(timestamps) - min(timestamps)).total_seconds(), 3)


def cmd_smoke_report(args: argparse.Namespace) -> int:
    runs_dir = args.runs_dir.resolve()
    console_text = read_text_auto(args.console_log) if args.console_log and args.console_log.is_file() else ""
    console_usage = extract_usage(console_text)
    console_duration = extract_console_duration_seconds(console_text)
    console_error_mentions = len(re.findall(r"ERROR|Traceback|SyntaxError|Command executed failed|returncode\":\s*[1-9]", console_text))
    summary_path = runs_dir / "summary" / "results.json"
    summary_rows = load_json(summary_path) if summary_path.is_file() else []
    summary_by_task = {row.get("task_id"): row for row in summary_rows if isinstance(row, dict)}

    task_reports = []
    for traj_path in sorted(runs_dir.rglob("traj.jsonl")):
        task_dir = traj_path.parent
        task_id = task_dir.name
        rows = parse_jsonl(traj_path)
        step_rows = [row for row in rows if "step_num" in row]
        error_rows = [row for row in rows if "Error" in row or "parse_error" in row]
        runtime_text = read_text_auto(task_dir / "runtime.log") if (task_dir / "runtime.log").is_file() else ""
        result_text = read_text_auto(task_dir / "result.txt").strip() if (task_dir / "result.txt").is_file() else ""
        screenshots = sorted(task_dir.glob("step_*.png"))
        usage = extract_usage(runtime_text)
        summary = summary_by_task.get(task_id, {})
        score = summary.get("score")
        if score is None and result_text:
            try:
                score = float(result_text)
            except ValueError:
                score = None

        task_reports.append({
            "task_id": task_id,
            "domain": task_dir.parent.name,
            "status": summary.get("status", "unknown"),
            "task_sr": 1 if score == 1 else 0,
            "score": score,
            "steps": len(step_rows),
            "max_step_num": max([row.get("step_num", 0) for row in step_rows], default=0),
            "screenshots": len(screenshots),
            "trajectory_errors": len(error_rows),
            "runtime_error_mentions": len(re.findall(r"ERROR|Traceback|SyntaxError|Command executed failed", runtime_text)),
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "total_tokens": usage["total_tokens"],
            "calls_with_usage": usage["calls_with_usage"],
        })

    aggregate = {
        "runs_dir": str(runs_dir),
        "tasks": len(task_reports),
        "task_success_rate": round(sum(item["task_sr"] for item in task_reports) / len(task_reports), 4) if task_reports else 0,
        "total_steps": sum(item["steps"] for item in task_reports),
        "total_screenshots": sum(item["screenshots"] for item in task_reports),
        "trajectory_errors": sum(item["trajectory_errors"] for item in task_reports),
        "runtime_error_mentions": sum(item["runtime_error_mentions"] for item in task_reports),
        "console_error_mentions": console_error_mentions,
        "prompt_tokens": sum(item["prompt_tokens"] for item in task_reports) or console_usage["prompt_tokens"],
        "completion_tokens": sum(item["completion_tokens"] for item in task_reports) or console_usage["completion_tokens"],
        "total_tokens": sum(item["total_tokens"] for item in task_reports) or console_usage["total_tokens"],
        "calls_with_usage": sum(item["calls_with_usage"] for item in task_reports) or console_usage["calls_with_usage"],
        "duration_seconds": console_duration,
    }

    report = {"summary": aggregate, "tasks": task_reports}
    write_json(args.output, report)
    print(json.dumps(aggregate, indent=2, ensure_ascii=False))

    if args.csv_output:
        args.csv_output.parent.mkdir(parents=True, exist_ok=True)
        try:
            with args.csv_output.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(task_reports[0].keys()) if task_reports else ["task_id"])
                writer.writeheader()
                writer.writerows(task_reports)
            print(f"Wrote CSV: {args.csv_output}")
        except PermissionError as exc:
            print(f"WARNING: could not write CSV because the file is locked: {args.csv_output} ({exc})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local orchestration for Odysseys reproduction runs.")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Check local dataset, scripts, dependencies, and key environment variables.")
    doctor.set_defaults(func=cmd_doctor)

    prepare = sub.add_parser("prepare", help="Create a selected task subset and OSWorld examples for it.")
    prepare.add_argument("--task-source-json", type=Path, default=DEFAULT_TASKS)
    prepare.add_argument("--output-dir", type=Path, default=REPO_ROOT / "outputs" / "prepared")
    prepare.add_argument("--level", choices=["easy", "medium", "hard"], default=None)
    prepare.add_argument("--limit", type=int, default=None)
    prepare.add_argument("--domain", default="mind2web_chrome")
    prepare.add_argument("--start-url", default=None, help="Override chrome_open_tabs URL for local smoke stability.")
    prepare.add_argument(
        "--local-chrome-stability",
        action="store_true",
        help="Append local Chrome args that bypass certificate interstitials in the Docker VM.",
    )
    prepare.add_argument(
        "--chrome-arg",
        action="append",
        default=[],
        help="Append a Chrome launch arg to generated OSWorld examples. Repeat for multiple args.",
    )
    prepare.set_defaults(func=cmd_prepare)

    dev = sub.add_parser("prepare-dev-subset", help="Create a fixed 10-task dev subset with easy/medium/hard mix.")
    dev.add_argument("--task-source-json", type=Path, default=DEFAULT_TASKS)
    dev.add_argument("--output-dir", type=Path, default=REPO_ROOT / "outputs" / "dev_10")
    dev.add_argument("--easy", type=int, default=4)
    dev.add_argument("--medium", type=int, default=3)
    dev.add_argument("--hard", type=int, default=3)
    dev.add_argument("--domain", default="mind2web_chrome")
    dev.add_argument("--start-url", default="about:blank")
    dev.add_argument("--local-chrome-stability", action="store_true", default=True)
    dev.add_argument("--chrome-arg", action="append", default=[])
    dev.set_defaults(func=cmd_prepare_dev_subset)

    score = sub.add_parser("score", help="Run the official full-trajectory per-rubric judge.")
    score.add_argument("--runs-dir", type=Path, required=True)
    score.add_argument("--task-source-json", type=Path, default=DEFAULT_TASKS)
    score.add_argument("--output", type=Path, default=REPO_ROOT / "outputs" / "scores" / "eval_results_full_traj_per_rubric.json")
    score.add_argument("--model", default="gemini-3.1-flash-lite-preview")
    score.add_argument("--num-workers", type=int, default=4)
    score.add_argument("--max-images", type=int, default=0)
    score.add_argument("--max-steps", type=int, default=100)
    score.add_argument("--include-incomplete", action="store_true")
    score.add_argument("--api-base", default=None)
    score.add_argument("--env-file", type=Path, default=None)
    score.add_argument("--use-curl-openai", action=argparse.BooleanOptionalAction, default=True)
    score.add_argument("--manifest-output", type=Path, default=None)
    score.add_argument("--csv-output", type=Path, default=None, help="Optional per-task CSV written after successful scoring.")
    score.set_defaults(func=cmd_score)

    summarize = sub.add_parser("summarize", help="Print score summary and optionally export per-task CSV.")
    summarize.add_argument("--eval-results", type=Path, required=True)
    summarize.add_argument("--csv-output", type=Path, default=None)
    summarize.set_defaults(func=cmd_summarize)

    reproduce = sub.add_parser("reproduce", help="Print the paper reproduction workflow for this local repo.")
    reproduce.add_argument("--smoke", action="store_true")
    reproduce.set_defaults(func=cmd_reproduce)

    osworld_cmd = sub.add_parser("osworld-command", help="Print the OSWorld run.py command for prepared Odysseys tasks.")
    osworld_cmd.add_argument("--prepared-dir", type=Path, default=REPO_ROOT / "outputs" / "smoke")
    osworld_cmd.add_argument("--osworld-path", type=str, default=None)
    osworld_cmd.add_argument("--provider-name", default="vmware")
    osworld_cmd.add_argument("--path-to-vm", default=None)
    osworld_cmd.add_argument("--observation-type", default="screenshot", choices=["screenshot", "a11y_tree", "screenshot_a11y_tree", "som"])
    osworld_cmd.add_argument("--model", default="gpt-4o")
    osworld_cmd.add_argument("--sleep-after-execution", type=float, default=3.0)
    osworld_cmd.add_argument("--max-steps", type=int, default=100)
    osworld_cmd.add_argument("--result-dir", type=Path, default=REPO_ROOT / "outputs" / "runs")
    osworld_cmd.add_argument("--domain", default="mind2web_chrome")
    osworld_cmd.set_defaults(func=cmd_osworld_command)

    run_osworld = sub.add_parser("run-osworld", help="Run OSWorld for a prepared Odysseys subset and write a run manifest.")
    run_osworld.add_argument("--prepared-dir", type=Path, required=True)
    run_osworld.add_argument("--result-dir", type=Path, required=True)
    run_osworld.add_argument("--osworld-path", type=str, default=None)
    run_osworld.add_argument("--path-to-vm", default=None, help="VM image path. Defaults to OSWORLD_VM_PATH from .env.")
    run_osworld.add_argument("--provider-name", default="docker")
    run_osworld.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    run_osworld.add_argument("--observation-type", default="screenshot", choices=["screenshot", "a11y_tree", "screenshot_a11y_tree", "som"])
    run_osworld.add_argument("--model", default="gpt-5.5")
    run_osworld.add_argument("--sleep-after-execution", type=float, default=0.0)
    run_osworld.add_argument("--max-steps", type=int, default=10)
    run_osworld.add_argument("--domain", default="mind2web_chrome")
    run_osworld.add_argument("--env-file", type=Path, default=None)
    run_osworld.add_argument("--openai-base-url", default=None)
    run_osworld.add_argument("--use-curl-openai", action=argparse.BooleanOptionalAction, default=True)
    run_osworld.add_argument("--python", default=None, help="Python executable for OSWorld; defaults to the current interpreter.")
    run_osworld.add_argument("--write-report", action=argparse.BooleanOptionalAction, default=True)
    run_osworld.add_argument("--report-output", type=Path, default=None)
    run_osworld.set_defaults(func=cmd_run_osworld)

    finalize = sub.add_parser("finalize-run", help="Repair/finalize a run manifest and optionally regenerate the runner report.")
    finalize.add_argument("--result-dir", type=Path, required=True)
    finalize.add_argument("--prepared-dir", type=Path, default=None)
    finalize.add_argument("--manifest", type=Path, default=None)
    finalize.add_argument("--exit-code", type=int, default=None)
    finalize.add_argument("--status", choices=["success", "success_inferred", "partial", "failed"], default=None)
    finalize.add_argument("--write-report", action=argparse.BooleanOptionalAction, default=True)
    finalize.add_argument("--write-csv", action=argparse.BooleanOptionalAction, default=True)
    finalize.add_argument("--console-log", type=Path, default=None)
    finalize.add_argument("--report-output", type=Path, default=None)
    finalize.set_defaults(func=cmd_finalize_run)

    smoke_report = sub.add_parser("smoke-report", help="Summarize OSWorld smoke run health and cost signals.")
    smoke_report.add_argument("--runs-dir", type=Path, required=True)
    smoke_report.add_argument("--console-log", type=Path, default=None)
    smoke_report.add_argument("--output", type=Path, required=True)
    smoke_report.add_argument("--csv-output", type=Path, default=None)
    smoke_report.set_defaults(func=cmd_smoke_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)
