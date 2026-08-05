from __future__ import annotations

import argparse
import base64
import csv
import importlib.util
import json
import os
import re
import shlex
import shutil
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
DEFAULT_AGENTV4_PATH = REPO_ROOT / "agentv4-agent-browser-skill-framework"
DEFAULT_JUDGE_MAX_IMAGES = 45
OSWORLD_BROWSER_PROMPT_SUFFIX = (
    "Local Odysseys browser stability notes: when Chrome is at about:blank or you need to navigate/search, "
    "first use pyautogui.hotkey('ctrl', 'l'), then type the complete URL or search query with pyautogui.write(...), "
    "then pyautogui.press('enter'). Avoid relying on the clipboard or pyperclip; this VM may not have xclip. "
    "If a click or navigation visibly does nothing, do not repeat the same coordinates; use ctrl+l direct navigation "
    "or a clearly different visible target."
)


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


def command_available(command: str) -> bool:
    try:
        completed = subprocess.run(
            ["where.exe", command] if os.name == "nt" else ["which", command],
            text=True,
            capture_output=True,
            check=False,
        )
        return completed.returncode == 0
    except Exception:
        return False


def terminate_process_tree(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            text=True,
            capture_output=True,
            check=False,
        )
    else:
        proc.kill()


def run_capture_with_timeout(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    timeout_seconds: float | None,
) -> tuple[int, str, str, str | None]:
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        return proc.returncode or 0, stdout or "", stderr or "", None
    except subprocess.TimeoutExpired:
        terminate_process_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return 124, stdout or "", stderr or "", f"timeout after {timeout_seconds} seconds"


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


def agentv4_health(agentv4_path: Path) -> dict[str, Any]:
    vendor_bin = agentv4_path / "vendor" / "agent-browser" / "bin"
    win_agent_browser = vendor_bin / "agent-browser-win32-x64.exe"
    health = {
        "path": str(agentv4_path),
        "exists": agentv4_path.is_dir(),
        "package_json": (agentv4_path / "package.json").is_file(),
        "pnpm_workspace": (agentv4_path / "pnpm-workspace.yaml").is_file(),
        "node_modules": (agentv4_path / "node_modules").is_dir(),
        "harness_agent": (agentv4_path / ".harness" / "agents" / "browser-gui.md").is_file(),
        "harness_skill": (agentv4_path / ".harness" / "skills" / "agent-browser" / "SKILL.md").is_file(),
        "harness_batch_script": (agentv4_path / ".harness" / "scripts" / "run-online-mind2web-harness-batch.mjs").is_file(),
        "harness_bin": (agentv4_path / ".harness" / "bin" / "agent-browser").is_file(),
        "vendor_agent_browser_js": (vendor_bin / "agent-browser.js").is_file(),
        "vendor_agent_browser_win32": win_agent_browser.is_file(),
        "sdk_sessions_source": (agentv4_path / "packages" / "sdk" / "src" / "sessions" / "index.ts").is_file(),
        "node": command_available("node"),
        "pnpm": command_available("pnpm"),
        "bun": command_available("bun"),
    }
    required = [
        "exists",
        "package_json",
        "harness_agent",
        "harness_skill",
        "harness_batch_script",
        "vendor_agent_browser_js",
        "vendor_agent_browser_win32",
        "sdk_sessions_source",
    ]
    health["ready"] = all(bool(health[key]) for key in required)
    health["blocking"] = [key for key in required if not health[key]]
    return health


def agentv4_api_key_env_for_model(model: str, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    lower = model.lower()
    if lower.startswith("claude"):
        return "ANTHROPIC_API_KEY"
    if lower.startswith("gemini"):
        return "GEMINI_API_KEY"
    if lower.startswith("deepseek"):
        return "DEEPSEEK_API_KEY"
    if lower.startswith("kimi"):
        return "KIMI_API_KEY"
    return "OPENAI_API_KEY"


def build_agentv4_env(
    base_env: dict[str, str],
    *,
    model: str,
    api_base: str,
    api_key_env: str | None,
    allow_insecure_tls: bool,
) -> dict[str, str]:
    env = base_env.copy()
    selected_key_name = agentv4_api_key_env_for_model(model, api_key_env)
    selected_key = env.get(selected_key_name) or env.get("OPENAI_API_KEY") or env.get("TOKENHUB_API_KEY")
    if selected_key:
        env["OPENAI_API_KEY"] = selected_key
        env["TOKENHUB_API_KEY"] = selected_key
    env["OPENAI_BASE_URL"] = api_base
    env["OPENAI_API_BASE"] = api_base
    env.setdefault("npm_config_strict_ssl", "false")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("AGENT_BROWSER_HEADED", "1")
    env.setdefault("BROWSER_HEADLESS", "0")
    env.setdefault("PLAYWRIGHT_HEADLESS", "0")
    env.setdefault("AGENT_BROWSER_HEADLESS", "0")
    if allow_insecure_tls:
        env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
    return env


def write_agentv4_settings(agentv4_path: Path, *, model: str, api_base: str) -> None:
    settings_path = agentv4_path / ".harness" / "settings.local.json"
    settings = load_json(settings_path) if settings_path.is_file() else {}
    llm = settings.setdefault("llm", {})
    llm["provider"] = "openai-compatible"
    llm["baseURL"] = api_base.rstrip("/")
    llm["model"] = model
    write_json(settings_path, settings)


def update_agentv4_agent_model(agentv4_path: Path, agent_id: str, model: str, *, disable_thinking: bool) -> bool:
    agent_path = agentv4_path / ".harness" / "agents" / f"{agent_id}.md"
    if not agent_path.is_file():
        return False
    text = agent_path.read_text(encoding="utf-8")
    updated = re.sub(r"(?m)^model:\s*.+$", f"model: {model}", text, count=1)
    if disable_thinking:
        updated = re.sub(r"(?m)^thinking(?:BudgetTokens|Display|Effort)?:\s*.+\r?\n", "", updated)
    if updated != text:
        agent_path.write_text(updated, encoding="utf-8")
    return updated != text


def agentv4_browser_command_prefix(agentv4_path: Path) -> str:
    win_bin = agentv4_path / "vendor" / "agent-browser" / "bin" / "agent-browser-win32-x64.exe"
    if os.name == "nt" and win_bin.is_file():
        return "vendor/agent-browser/bin/agent-browser-win32-x64.exe"
    vendor = agentv4_path / "vendor" / "agent-browser" / "bin" / "agent-browser.js"
    if os.name == "nt" and vendor.is_file():
        return "node vendor/agent-browser/bin/agent-browser.js"
    return ".harness/bin/agent-browser"


def agentv4_browser_command_options() -> str:
    return " --headed --ignore-https-errors" if os.name == "nt" else ""


def ensure_agentv4_powershell_auto_screenshot(agentv4_path: Path) -> bool:
    runner_path = agentv4_path / "packages" / "core" / "src" / "application" / "runtime" / "AgentRunner.ts"
    if not runner_path.is_file():
        return False
    text = runner_path.read_text(encoding="utf-8")
    updated = text
    changed = False
    needle = "if (execution === undefined || execution.toolName !== 'Bash') return content"
    replacement = (
        "if (\n"
        "      execution === undefined ||\n"
        "      (execution.toolName !== 'Bash' && execution.toolName !== 'PowerShell')\n"
        "    ) return content"
    )
    if "execution.toolName !== 'PowerShell'" not in updated and needle in updated:
        updated = updated.replace(needle, replacement, 1)
        changed = True

    bash_spawn = """    const child = childProcess.spawn('bash', ['-lc', command], {
      cwd,
      env: process.env,
      timeout: timeoutMs,
    })"""
    cross_shell_spawn = """    const child = process.platform === 'win32'
      ? childProcess.spawn(`${process.env.SystemRoot ?? 'C:\\\\Windows'}\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe`, ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command], {
        cwd,
        env: process.env,
        timeout: timeoutMs,
      })
      : childProcess.spawn('bash', ['-lc', command], {
        cwd,
        env: process.env,
        timeout: timeoutMs,
      })"""
    if "System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe" not in updated and bash_spawn in updated:
        updated = updated.replace(bash_spawn, cross_shell_spawn, 1)
        changed = True
    old_powershell_spawn = "childProcess.spawn('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command]"
    new_powershell_spawn = "childProcess.spawn(`${process.env.SystemRoot ?? 'C:\\\\Windows'}\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe`, ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command]"
    if "System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe" not in updated and old_powershell_spawn in updated:
        updated = updated.replace(old_powershell_spawn, new_powershell_spawn, 1)
        changed = True

    wrapper_cmd = """  const viewportCmd =
    `.harness/bin/agent-browser set viewport ${AUTO_SCREENSHOT_VIEWPORT_WIDTH} ${AUTO_SCREENSHOT_VIEWPORT_HEIGHT} ${AUTO_SCREENSHOT_DEVICE_SCALE_FACTOR}`
  const screenshotCmd = `.harness/bin/agent-browser screenshot ${shellQuote(requestedPath)}`"""
    vendor_cmd = """  const agentBrowserCmd = process.platform === 'win32'
    ? `& ${shellQuote(path.join(opts.cwd, 'vendor', 'agent-browser', 'bin', 'agent-browser-win32-x64.exe'))}`
    : '.harness/bin/agent-browser'
  const agentBrowserOptions = process.platform === 'win32'
    ? ' --headed --ignore-https-errors'
    : ''
  const viewportCmd =
    `${agentBrowserCmd} set viewport ${AUTO_SCREENSHOT_VIEWPORT_WIDTH} ${AUTO_SCREENSHOT_VIEWPORT_HEIGHT} ${AUTO_SCREENSHOT_DEVICE_SCALE_FACTOR}${agentBrowserOptions}`
  const screenshotCmd = `${agentBrowserCmd} screenshot ${shellQuote(requestedPath)}${agentBrowserOptions}`"""
    if "const agentBrowserCmd = process.platform === 'win32'" not in updated and wrapper_cmd in updated:
        updated = updated.replace(wrapper_cmd, vendor_cmd, 1)
        changed = True
    old_agent_browser_cmd = "? `node ${shellQuote(path.join(opts.cwd, 'vendor', 'agent-browser', 'bin', 'agent-browser.js'))}`"
    new_agent_browser_cmd = "? `& ${shellQuote(path.join(opts.cwd, 'vendor', 'agent-browser', 'bin', 'agent-browser-win32-x64.exe'))}`"
    if old_agent_browser_cmd in updated:
        updated = updated.replace(old_agent_browser_cmd, new_agent_browser_cmd, 1)
        changed = True
    old_wait_cmd = "'.harness/bin/agent-browser wait 250'"
    new_wait_cmd = "`${agentBrowserCmd} wait 250${agentBrowserOptions}`"
    if old_wait_cmd in updated:
        updated = updated.replace(old_wait_cmd, new_wait_cmd)
        changed = True
    if "const agentBrowserOptions = process.platform === 'win32'" not in updated and "const agentBrowserCmd = process.platform === 'win32'" in updated:
        updated = updated.replace(
            "  const viewportCmd =\n    `${agentBrowserCmd} set viewport ${AUTO_SCREENSHOT_VIEWPORT_WIDTH} ${AUTO_SCREENSHOT_VIEWPORT_HEIGHT} ${AUTO_SCREENSHOT_DEVICE_SCALE_FACTOR}`\n  const screenshotCmd = `${agentBrowserCmd} screenshot ${shellQuote(requestedPath)}`",
            "  const agentBrowserOptions = process.platform === 'win32'\n    ? ' --headed --ignore-https-errors'\n    : ''\n  const viewportCmd =\n    `${agentBrowserCmd} set viewport ${AUTO_SCREENSHOT_VIEWPORT_WIDTH} ${AUTO_SCREENSHOT_VIEWPORT_HEIGHT} ${AUTO_SCREENSHOT_DEVICE_SCALE_FACTOR}${agentBrowserOptions}`\n  const screenshotCmd = `${agentBrowserCmd} screenshot ${shellQuote(requestedPath)}${agentBrowserOptions}`",
            1,
        )
        changed = True
    if "`${agentBrowserCmd} wait 250`" in updated:
        updated = updated.replace("`${agentBrowserCmd} wait 250`", "`${agentBrowserCmd} wait 250${agentBrowserOptions}`")
        changed = True
    old_executable_token = """function isAgentBrowserExecutableToken(token: string | undefined): boolean {
  if (token === undefined) return false
  if (token === 'agent-browser') return true
  if (token.endsWith('/agent-browser')) return true
  if (token.endsWith('/agent-browser-darwin-arm64')) return true
  return false
}"""
    new_executable_token = """function isAgentBrowserExecutableToken(token: string | undefined): boolean {
  if (token === undefined) return false
  const normalized = token.replace(/\\\\/g, '/')
  if (normalized === 'agent-browser') return true
  if (normalized.endsWith('/agent-browser')) return true
  if (normalized.endsWith('/agent-browser.js')) return true
  if (normalized.endsWith('/agent-browser-win32-x64.exe')) return true
  if (normalized.endsWith('/agent-browser-darwin-arm64')) return true
  return false
}"""
    if "agent-browser-win32-x64.exe')) return true" not in updated and old_executable_token in updated:
        updated = updated.replace(old_executable_token, new_executable_token, 1)
        changed = True

    old_run_shell_body = """  return new Promise((resolve, reject) => {
    const child = process.platform === 'win32'
      ? childProcess.spawn(`${process.env.SystemRoot ?? 'C:\\\\Windows'}\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe`, ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command], {
        cwd,
        env: process.env,
        timeout: timeoutMs,
      })
      : childProcess.spawn('bash', ['-lc', command], {
        cwd,
        env: process.env,
        timeout: timeoutMs,
      })
    let stdout = ''
    let stderr = ''
    child.stdout?.on('data', (d) => { stdout += String(d) })
    child.stderr?.on('data', (d) => { stderr += String(d) })
    signal.addEventListener('abort', () => child.kill(), { once: true })
    child.on('close', (code) => resolve({ stdout, stderr, exitCode: code ?? 1 }))
    child.on('error', reject)
  })"""
    new_run_shell_body = """  return new Promise((resolve, reject) => {
    const child = process.platform === 'win32'
      ? childProcess.spawn(`${process.env.SystemRoot ?? 'C:\\\\Windows'}\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe`, ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command], {
        cwd,
        env: process.env,
      })
      : childProcess.spawn('bash', ['-lc', command], {
        cwd,
        env: process.env,
      })
    let stdout = ''
    let stderr = ''
    let settled = false
    const finish = (payload: { stdout: string; stderr: string; exitCode: number }) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      signal.removeEventListener('abort', onAbort)
      resolve(payload)
    }
    const fail = (err: Error) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      signal.removeEventListener('abort', onAbort)
      reject(err)
    }
    const killChild = () => {
      try { child.kill() } catch {}
    }
    const onAbort = () => {
      stderr += '\\n[odysseys_eval] auto screenshot command aborted'
      killChild()
      finish({ stdout, stderr, exitCode: 130 })
    }
    const timer = setTimeout(() => {
      stderr += `\\n[odysseys_eval] auto screenshot command timed out after ${timeoutMs}ms`
      killChild()
      finish({ stdout, stderr, exitCode: 124 })
    }, timeoutMs)
    child.stdout?.on('data', (d) => { stdout += String(d) })
    child.stderr?.on('data', (d) => { stderr += String(d) })
    signal.addEventListener('abort', onAbort, { once: true })
    child.on('close', (code) => finish({ stdout, stderr, exitCode: code ?? 1 }))
    child.on('error', fail)
  })"""
    if "[odysseys_eval] auto screenshot command timed out" not in updated and old_run_shell_body in updated:
        updated = updated.replace(old_run_shell_body, new_run_shell_body, 1)
        changed = True
    fallback_anchor = "  return undefined\n}\n\nfunction readPngDimensions"
    fallback_block = """  if (process.platform === 'win32') {
    return await captureWindowsDesktopScreenshot({
      childProcess,
      cwd: opts.cwd,
      fs,
      path,
      requestedPath,
      signal: opts.signal,
    })
  }

  return undefined
}

async function captureWindowsDesktopScreenshot(opts: {
  childProcess: typeof import('node:child_process')
  cwd: string
  fs: typeof import('node:fs/promises')
  path: typeof import('node:path')
  requestedPath: string
  signal: AbortSignal
}): Promise<{ path: string; mediaType: string; base64: string } | undefined> {
  const psScript = [
    'Add-Type -AssemblyName System.Windows.Forms',
    'Add-Type -AssemblyName System.Drawing',
    `$out=${JSON.stringify(opts.requestedPath)}`,
    '$bounds=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds',
    '$source=New-Object System.Drawing.Bitmap $bounds.Width,$bounds.Height',
    '$sourceGraphics=[System.Drawing.Graphics]::FromImage($source)',
    '$sourceGraphics.CopyFromScreen($bounds.Location,[System.Drawing.Point]::Empty,$bounds.Size)',
    `$target=New-Object System.Drawing.Bitmap ${AUTO_SCREENSHOT_VIEWPORT_WIDTH},${AUTO_SCREENSHOT_VIEWPORT_HEIGHT}`,
    '$targetGraphics=[System.Drawing.Graphics]::FromImage($target)',
    '$targetGraphics.DrawImage($source,0,0,$target.Width,$target.Height)',
    '$target.Save($out,[System.Drawing.Imaging.ImageFormat]::Png)',
    '$targetGraphics.Dispose()',
    '$target.Dispose()',
    '$sourceGraphics.Dispose()',
    '$source.Dispose()',
  ].join('; ')
  const output = await runShellCommand(
    opts.childProcess,
    psScript,
    opts.cwd,
    opts.signal,
    10_000,
  )
  if (output.exitCode !== 0) return undefined
  try {
    const data = await opts.fs.readFile(opts.requestedPath)
    const dimensions = readPngDimensions(data)
    if (
      dimensions === undefined
      || dimensions.width !== AUTO_SCREENSHOT_VIEWPORT_WIDTH
      || dimensions.height !== AUTO_SCREENSHOT_VIEWPORT_HEIGHT
    ) {
      return undefined
    }
    const modelImage = await encodeScreenshotForModel({
      childProcess: opts.childProcess,
      cwd: opts.cwd,
      fs: opts.fs,
      path: opts.path,
      signal: opts.signal,
      sourcePath: opts.requestedPath,
      data,
    })
    return { path: opts.requestedPath, ...modelImage }
  } catch {
    return undefined
  }
}

function readPngDimensions"""
    if "function captureWindowsDesktopScreenshot" not in updated and fallback_anchor in updated:
        updated = updated.replace(fallback_anchor, fallback_block, 1)
        changed = True
    screenshot_path_anchor = "  if (saved?.[1]) return saved[1].trim()\n  const absolute = stdout.match(/(\\/[^\\n\\r]+?\\.png)/)"
    screenshot_path_block = "  if (saved?.[1]) return saved[1].trim()\n  const windowsAbsolute = stdout.match(/[A-Za-z]:\\\\[^\\n\\r]+?\\.png/)\n  if (windowsAbsolute?.[0]) return windowsAbsolute[0].trim()\n  const absolute = stdout.match(/(\\/[^\\n\\r]+?\\.png)/)"
    if "const windowsAbsolute = stdout.match" not in updated and screenshot_path_anchor in updated:
        updated = updated.replace(screenshot_path_anchor, screenshot_path_block, 1)
        changed = True

    if changed:
        runner_path.write_text(updated, encoding="utf-8")
    return changed


def ensure_agentv4_powershell_tool_windows_runner(agentv4_path: Path) -> bool:
    tool_path = agentv4_path / "packages" / "core" / "src" / "application" / "builtins" / "tools" / "PowerShellTool.ts"
    if not tool_path.is_file():
        return False
    text = tool_path.read_text(encoding="utf-8")
    updated = text
    changed = False
    if "const fs = await import('node:fs')" not in updated:
        updated = updated.replace(
            "  const cp = await import('node:child_process')\n",
            "  const cp = await import('node:child_process')\n  const fs = await import('node:fs')\n",
            1,
        )
        changed = True
    old_spawn = "const child = cp.spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', input.command], {"
    new_spawn = "const child = cp.spawn(`${process.env.SystemRoot ?? 'C:\\\\Windows'}\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe`, ['-NoProfile', '-NonInteractive', '-Command', input.command], {"
    if old_spawn in updated:
        updated = updated.replace(old_spawn, new_spawn, 1)
        changed = True
    old_cwd = "      cwd: input.cwd ?? process.cwd?.(),"
    new_cwd = "      cwd: input.cwd && fs.existsSync(input.cwd) ? input.cwd : process.cwd?.(),"
    if old_cwd in updated:
        updated = updated.replace(old_cwd, new_cwd, 1)
        changed = True
    old_runner_body = """    const child = cp.spawn(`${process.env.SystemRoot ?? 'C:\\\\Windows'}\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe`, ['-NoProfile', '-NonInteractive', '-Command', input.command], {
      cwd: input.cwd && fs.existsSync(input.cwd) ? input.cwd : process.cwd?.(),
      timeout: input.timeout ?? DEFAULT_TIMEOUT_MS,
      env: process.env,
    })
    let stdout = ''
    let stderr = ''
    child.stdout?.on('data', (d) => { stdout += String(d) })
    child.stderr?.on('data', (d) => { stderr += String(d) })
    input.signal?.addEventListener('abort', () => child.kill(), { once: true })
    child.on('close', (code) => {
      resolve({ stdout, stderr, exitCode: code ?? 1 })
    })
    child.on('error', reject)"""
    new_runner_body = """    const requestedTimeoutMs = input.timeout ?? DEFAULT_TIMEOUT_MS
    const timeoutMs = /agent-browser(?:\\.js|-win32-x64\\.exe|\\b)/.test(input.command)
      ? Math.min(requestedTimeoutMs, 15_000)
      : requestedTimeoutMs
    const child = cp.spawn(`${process.env.SystemRoot ?? 'C:\\\\Windows'}\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe`, ['-NoProfile', '-NonInteractive', '-Command', input.command], {
      cwd: input.cwd && fs.existsSync(input.cwd) ? input.cwd : process.cwd?.(),
      env: process.env,
    })
    let stdout = ''
    let stderr = ''
    let settled = false
    const finish = (payload: ShellOutput) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      input.signal?.removeEventListener('abort', onAbort)
      resolve(payload)
    }
    const fail = (err: Error) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      input.signal?.removeEventListener('abort', onAbort)
      reject(err)
    }
    const killChild = () => {
      try { child.kill() } catch {}
    }
    const onAbort = () => {
      stderr += '\\n[odysseys_eval] PowerShell tool aborted'
      killChild()
      finish({ stdout, stderr, exitCode: 130 })
    }
    const timer = setTimeout(() => {
      stderr += `\\n[odysseys_eval] PowerShell tool timed out after ${timeoutMs}ms`
      killChild()
      finish({ stdout, stderr, exitCode: 124 })
    }, timeoutMs)
    child.stdout?.on('data', (d) => { stdout += String(d) })
    child.stderr?.on('data', (d) => { stderr += String(d) })
    input.signal?.addEventListener('abort', onAbort, { once: true })
    child.on('close', (code) => {
      finish({ stdout, stderr, exitCode: code ?? 1 })
    })
    child.on('error', fail)"""
    if "[odysseys_eval] PowerShell tool timed out" not in updated and old_runner_body in updated:
        updated = updated.replace(old_runner_body, new_runner_body, 1)
        changed = True
    old_timeout_line = "    const timeoutMs = input.timeout ?? DEFAULT_TIMEOUT_MS\n    const child = cp.spawn"
    new_timeout_line = (
        "    const requestedTimeoutMs = input.timeout ?? DEFAULT_TIMEOUT_MS\n"
        "    const timeoutMs = /agent-browser(?:\\.js|-win32-x64\\.exe|\\b)/.test(input.command)\n"
        "      ? Math.min(requestedTimeoutMs, 15_000)\n"
        "      : requestedTimeoutMs\n"
        "    const child = cp.spawn"
    )
    if "const requestedTimeoutMs = input.timeout ?? DEFAULT_TIMEOUT_MS" not in updated and old_timeout_line in updated:
        updated = updated.replace(old_timeout_line, new_timeout_line, 1)
        changed = True
    soft_timeout_anchor = """    if (result.stderr.length > this.maxOutputBytes) {
      result.stderr = result.stderr.slice(0, this.maxOutputBytes) + '\\n...[truncated]'
    }
    return { output: result }"""
    soft_timeout_block = """    if (result.stderr.length > this.maxOutputBytes) {
      result.stderr = result.stderr.slice(0, this.maxOutputBytes) + '\\n...[truncated]'
    }
    if (/agent-browser(?:\\.js|-win32-x64\\.exe|\\b)/.test(input.command) && result.exitCode === 124) {
      result.stderr += '\\n[odysseys_eval] agent-browser timed out after dispatch; treating as observable because the browser action may have taken effect. Inspect the attached screenshot before deciding the next action.'
      result.exitCode = 0
    }
    return { output: result }"""
    if "agent-browser timed out after dispatch" not in updated and soft_timeout_anchor in updated:
        updated = updated.replace(soft_timeout_anchor, soft_timeout_block, 1)
        changed = True
    if changed:
        tool_path.write_text(updated, encoding="utf-8")
    return changed


def ensure_agentv4_windows_browser_instructions(agentv4_path: Path, agent_id: str) -> bool:
    if os.name != "nt":
        return False
    marker = "<!-- odysseys_eval_windows_agent_browser_override -->"
    browser_command = agentv4_browser_command_prefix(agentv4_path)
    note = (
        f"\n\n{marker}\n"
        "Windows local runner override:\n\n"
        f"- Use PowerShell tool calls, not Bash tool calls, for browser actions in this Windows environment.\n"
        f"- Use `{browser_command}` for all browser actions in this Windows environment.\n"
        "- Do not call `.harness/bin/agent-browser`; it is a Unix bash wrapper without a Windows executable extension and can trigger the Windows \"open with\" dialog.\n"
        "- Issue exactly one browser command per PowerShell tool call. Do not chain commands with `&&`, `;`, or pipes.\n"
    )
    changed = False
    targets = [
        agentv4_path / ".harness" / "agents" / f"{agent_id}.md",
        agentv4_path / ".harness" / "skills" / "agent-browser" / "SKILL.md",
    ]
    for path in targets:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if marker in text:
            updated = text.split(marker, 1)[0].rstrip() + note + "\n"
            if updated != text:
                path.write_text(updated, encoding="utf-8")
                changed = True
            continue
        path.write_text(text.rstrip() + note + "\n", encoding="utf-8")
        changed = True
    return changed


def prepare_agentv4_runtime(
    agentv4_path: Path,
    *,
    agent_id: str,
    model: str,
    api_base: str,
    auto_patch: bool,
) -> dict[str, Any]:
    health = agentv4_health(agentv4_path)
    if not health["ready"]:
        raise SystemExit(
            "AgentV4 browser-gui runner is not ready. "
            f"Blocking checks: {health.get('blocking')}"
        )
    changes = {
        "settings_local_json": str(agentv4_path / ".harness" / "settings.local.json"),
        "provider": "openai-compatible",
        "api_base": api_base.rstrip("/"),
        "model": model,
        "agent_model_updated": False,
        "agent_thinking_disabled": not model.lower().startswith("claude"),
        "powershell_auto_screenshot_patch_applied": False,
        "powershell_tool_patch_applied": False,
        "windows_browser_instruction_patch_applied": False,
    }
    if auto_patch:
        write_agentv4_settings(agentv4_path, model=model, api_base=api_base)
        changes["agent_model_updated"] = update_agentv4_agent_model(
            agentv4_path,
            agent_id,
            model,
            disable_thinking=bool(changes["agent_thinking_disabled"]),
        )
        changes["powershell_auto_screenshot_patch_applied"] = ensure_agentv4_powershell_auto_screenshot(agentv4_path)
        changes["powershell_tool_patch_applied"] = ensure_agentv4_powershell_tool_windows_runner(agentv4_path)
        changes["windows_browser_instruction_patch_applied"] = ensure_agentv4_windows_browser_instructions(agentv4_path, agent_id)
    return changes


def agentv4_runs_dir(result_dir: Path, model: str, domain: str) -> Path:
    return result_dir / "pyautogui" / "screenshot" / str(model) / domain


def safe_file_part(value: str, limit: int = 120) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-")
    return (cleaned or "item")[:limit]


def build_agentv4_task_prompt(task: dict[str, Any], *, max_steps: int, preopen_website: bool, browser_command: str) -> str:
    task_id = task.get("task_id") or task.get("id") or "unknown"
    website = task.get("website") or task.get("url") or task.get("web") or "about:blank"
    confirmed_task = task.get("confirmed_task") or task.get("task") or task.get("annotation") or ""
    start_instruction = (
        f"Start by opening the website URL with {browser_command} open {website}{agentv4_browser_command_options()}."
        if preopen_website
        else "Start from the website URL in the task."
    )
    return " ".join([
        "Use the agent-browser GUI skill.",
        "Run this Odysseys browser task using screenshot-only visual policy.",
        f"Dataset item: task_id={task_id}; website={website}; confirmed_task={confirmed_task}",
        f"Use at most {max_steps} browser action steps before giving your final answer.",
        "The runtime fixes automatic screenshots to 1280x720 at device scale factor 1; use absolute pixel bounding boxes x1,y1,x2,y2 in the newest screenshot frame.",
        "Do not output point-only x,y coordinates for GUI target actions.",
        start_instruction,
        "Do not use read/snapshot/DOM/text extraction; rely on runtime screenshots after each browser action.",
        f"Execute GUI actions with bbox coordinates through {browser_command}; append `{agentv4_browser_command_options().strip()}` to every agent-browser command in this Windows run.",
        "If Google or an initial page shows a blank white/black screenshot, do not immediately stop; use the next browser action to open a task-relevant direct site URL or search URL.",
        "Do not add or override --session; the runner sets AGENT_BROWSER_SESSION separately for this task.",
        "Use the PowerShell tool for browser commands in this Windows run, not the Bash tool.",
        "If the loaded skill mentions .harness/bin/agent-browser, override that path for this local Windows run and use the command prefix above instead.",
        "On Windows PowerShell, issue exactly one browser command per tool call; do not chain commands with &&, ;, or pipes, and do not pass a cwd for browser commands.",
        "When completed, answer concisely with the completed result, or report why completion was blocked.",
    ])


def run_agent_browser_close(agentv4_path: Path, env: dict[str, str]) -> None:
    vendor = agentv4_path / "vendor" / "agent-browser" / "bin" / "agent-browser.js"
    command = ["node", str(vendor), "close", "--all"] if vendor.is_file() else ["bash", "-lc", ".harness/bin/agent-browser close --all"]
    run_capture_with_timeout(command, cwd=agentv4_path, env=env, timeout_seconds=15)
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/IM", "agent-browser-win32-x64.exe"],
            text=True,
            capture_output=True,
            check=False,
        )


def run_agentv4_cli_task(
    *,
    agentv4_path: Path,
    agent_id: str,
    task: dict[str, Any],
    task_index: int,
    raw_dir: Path,
    env: dict[str, str],
    max_steps: int,
    task_timeout_ms: int,
    preopen_website: bool,
) -> dict[str, Any]:
    task_id = str(task.get("task_id") or task.get("id") or f"task-{task_index}")
    run_dir = raw_dir / f"task{task_index:03d}-{safe_file_part(task_id, 96)}"
    run_dir.mkdir(parents=True, exist_ok=True)
    session_file = run_dir / "session-id.txt"
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    session_file.unlink(missing_ok=True)
    agent_browser_session = safe_file_part(f"odysseys-{task_index:03d}-{task_id[:16]}", 80).lower()
    task_env = env.copy()
    task_env["AGENT_BROWSER_SESSION"] = agent_browser_session
    prompt = build_agentv4_task_prompt(
        task,
        max_steps=max_steps,
        preopen_website=preopen_website,
        browser_command=agentv4_browser_command_prefix(agentv4_path),
    )
    bun_args = [
        "npx",
        "--yes",
        "bun",
        "packages/pilot/cli/src/bin.ts",
        "--workspace",
        str(agentv4_path),
        "--agent",
        agent_id,
        "--bypass",
        "--debug",
        "--no-colors",
        "--session-id-file",
        str(session_file),
        "-p",
        prompt,
    ]
    command = ["cmd.exe", "/c", *bun_args] if os.name == "nt" else bun_args
    started = datetime.now(timezone.utc)
    exit_code, stdout, stderr, error = run_capture_with_timeout(
        command,
        cwd=agentv4_path,
        env=task_env,
        timeout_seconds=task_timeout_ms / 1000 if task_timeout_ms > 0 else None,
    )
    signal = "timeout" if exit_code == 124 else None
    ended = datetime.now(timezone.utc)
    stdout_path.write_text(stdout, encoding="utf-8", errors="replace")
    stderr_path.write_text(stderr, encoding="utf-8", errors="replace")
    session_id = session_file.read_text(encoding="utf-8", errors="replace").strip() if session_file.is_file() else None
    summary = {
        "task_index": task_index,
        "task_id": task_id,
        "status": "finished" if exit_code == 0 else "failed",
        "exit_code": exit_code,
        "signal": signal,
        "error": error,
        "session_id": session_id,
        "agent_browser_session": agent_browser_session,
        "started_at": started.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "ended_at": ended.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "duration_seconds": round((ended - started).total_seconds(), 3),
        "raw_run_dir": str(run_dir),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "transcript": str(agentv4_path / ".harness" / "sessions" / str(session_id) / "transcript.jsonl") if session_id else None,
        "final_text_preview": stdout.strip().replace("\n", " ")[-800:],
    }
    write_json(run_dir / "result.json", summary)
    return summary


def iter_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def transcript_message_content(row: dict[str, Any]) -> list[dict[str, Any]]:
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def visible_text_from_content(content: list[dict[str, Any]]) -> str:
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = str(block.get("text") or "").strip()
            if text and not text.startswith("[auto screenshot attached"):
                parts.append(text)
    return "\n".join(parts).strip()


def tool_uses_from_content(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"]


def tool_results_from_content(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [block for block in content if isinstance(block, dict) and block.get("type") == "tool_result"]


def command_from_tool_use(block: dict[str, Any]) -> str | None:
    if block.get("name") not in ("Bash", "PowerShell"):
        return None
    payload = block.get("input")
    if not isinstance(payload, dict):
        return None
    command = payload.get("command")
    return str(command).strip() if command else None


def is_agent_browser_command(command: str | None) -> bool:
    return bool(command and "agent-browser" in command)


def decode_first_image_from_tool_result(result_block: dict[str, Any], output_path: Path) -> str | None:
    content = result_block.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "image":
            continue
        source = item.get("source")
        if not isinstance(source, dict) or source.get("kind") != "base64":
            continue
        data = source.get("data")
        if not isinstance(data, str) or not data:
            continue
        output_path.write_bytes(base64.b64decode(data))
        return output_path.name
    return None


def copy_agentv4_auto_screenshot(agentv4_path: Path, session_id: str | None, tool_use_id: str, output_path: Path) -> str | None:
    if not session_id or not tool_use_id:
        return None
    auto_dir = agentv4_path / ".harness" / "artifacts" / "auto-screenshots"
    if not auto_dir.is_dir():
        return None
    candidates = sorted(auto_dir.glob(f"{safe_file_part(session_id)}-{safe_file_part(tool_use_id)}*.png"))
    if not candidates:
        candidates = sorted(auto_dir.glob(f"*{safe_file_part(tool_use_id)}*.png"))
    if not candidates:
        return None
    shutil.copyfile(candidates[-1], output_path)
    return output_path.name


def parse_agent_browser_screenshot_path(stdout: str) -> Path | None:
    match = re.search(r"saved to\s+(.+?\.png)", stdout, re.IGNORECASE)
    if not match:
        match = re.search(r"([A-Za-z]:\\[^\r\n]+?\.png|/[^\r\n]+?\.png)", stdout)
    if not match:
        return None
    candidate = Path(match.group(1).strip().strip('"').strip("'"))
    return candidate if candidate.is_file() else None


def capture_agentv4_fallback_screenshot(agentv4_path: Path, output_path: Path, env: dict[str, str] | None) -> str | None:
    vendor_js = agentv4_path / "vendor" / "agent-browser" / "bin" / "agent-browser.js"
    vendor_exe = agentv4_path / "vendor" / "agent-browser" / "bin" / "agent-browser-win32-x64.exe"
    if os.name == "nt" and vendor_exe.is_file():
        command = [str(vendor_exe), "screenshot", str(output_path)]
    elif vendor_js.is_file():
        command = ["node", str(vendor_js), "screenshot", str(output_path)]
    else:
        return None
    _, stdout, _, _ = run_capture_with_timeout(
        command,
        cwd=agentv4_path,
        env=env,
        timeout_seconds=15,
    )
    if not output_path.is_file():
        actual_path = parse_agent_browser_screenshot_path(stdout)
        if actual_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(actual_path, output_path)
    if not output_path.is_file() and os.name == "nt":
        output_path.parent.mkdir(parents=True, exist_ok=True)
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "Add-Type -AssemblyName System.Drawing; "
            "$bounds=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
            "$bitmap=New-Object System.Drawing.Bitmap $bounds.Width,$bounds.Height; "
            "$graphics=[System.Drawing.Graphics]::FromImage($bitmap); "
            "$graphics.CopyFromScreen($bounds.Location,[System.Drawing.Point]::Empty,$bounds.Size); "
            f"$bitmap.Save({json.dumps(str(output_path))},[System.Drawing.Imaging.ImageFormat]::Png); "
            "$graphics.Dispose(); $bitmap.Dispose();"
        )
        run_capture_with_timeout(
            [
                os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps_script,
            ],
            cwd=REPO_ROOT,
            env=env,
            timeout_seconds=10,
        )
    return output_path.name if output_path.is_file() else None


def convert_agentv4_session_to_trajectory(
    *,
    agentv4_path: Path,
    task_id: str,
    session_id: str | None,
    raw_summary: dict[str, Any],
    output_dir: Path,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = agentv4_path / ".harness" / "sessions" / str(session_id) / "transcript.jsonl" if session_id else None
    rows = iter_jsonl(transcript_path) if transcript_path else []
    pending: dict[str, dict[str, Any]] = {}
    step_num = 0
    trajectory_rows = []
    final_text = str(raw_summary.get("final_text_preview") or "").strip()

    for row in rows:
        message = row.get("message")
        role = message.get("role") if isinstance(message, dict) else None
        content = transcript_message_content(row)
        if role == "assistant":
            text = visible_text_from_content(content)
            if text:
                final_text = text
            for block in tool_uses_from_content(content):
                command = command_from_tool_use(block)
                if is_agent_browser_command(command):
                    pending[str(block.get("id"))] = {"command": command, "tool": block.get("name")}
            continue
        if role != "user":
            continue
        for result_block in tool_results_from_content(content):
            tool_use_id = str(result_block.get("toolUseId") or result_block.get("tool_use_id") or "")
            tool_use = pending.pop(tool_use_id, None)
            if not tool_use:
                continue
            step_num += 1
            screenshot_path = output_dir / f"step_{step_num}.png"
            screenshot_name = decode_first_image_from_tool_result(result_block, screenshot_path)
            if not screenshot_name:
                screenshot_name = copy_agentv4_auto_screenshot(agentv4_path, session_id, tool_use_id, screenshot_path)
            if not screenshot_name:
                screenshot_name = capture_agentv4_fallback_screenshot(agentv4_path, screenshot_path, env)
            text = visible_text_from_content(result_block.get("content") if isinstance(result_block.get("content"), list) else [])
            trajectory_rows.append({
                "step_num": step_num,
                "response": "",
                "action": {
                    "tool": tool_use.get("tool"),
                    "command": tool_use.get("command"),
                    "tool_use_id": tool_use_id,
                },
                "action_line": str(tool_use.get("command") or ""),
                "screenshot": screenshot_name or "",
                "screenshot_file": screenshot_name or "",
                "tool_result": text,
            })

    if final_text:
        step_num += 1
        trajectory_rows.append({
            "step_num": step_num,
            "response": final_text,
            "action": {"tool": "assistant_final", "command": ""},
            "action_line": "",
            "screenshot": "",
            "screenshot_file": "",
            "final": True,
        })

    traj_path = output_dir / "traj.jsonl"
    with traj_path.open("w", encoding="utf-8") as f:
        for item in trajectory_rows:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    (output_dir / "runtime.log").write_text(
        "\n".join([
            f"task_id={task_id}",
            f"session_id={session_id or ''}",
            f"status={raw_summary.get('status')}",
            f"exit_code={raw_summary.get('exit_code')}",
            f"duration_seconds={raw_summary.get('duration_seconds')}",
            f"transcript={transcript_path or ''}",
        ]) + "\n",
        encoding="utf-8",
    )
    (output_dir / "result.txt").write_text("0\n", encoding="utf-8")
    write_json(output_dir / "agentv4_result.json", raw_summary)
    return {
        "task_id": task_id,
        "session_id": session_id,
        "run_dir": str(output_dir),
        "traj": str(traj_path),
        "steps": len(trajectory_rows),
        "screenshots": len(list(output_dir.glob("step_*.png"))),
        "status": "converted" if trajectory_rows else "empty",
    }


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


def cmd_agentv4_doctor(args: argparse.Namespace) -> int:
    agentv4_path = resolve_repo_path(args.agentv4_path).resolve()
    health = agentv4_health(agentv4_path)
    print(json.dumps(health, indent=2, ensure_ascii=False))
    if not health["ready"]:
        print("AgentV4 browser-gui runner is not ready.")
        if "sdk_sessions_source" in health["blocking"]:
            print("Missing source directory: packages/sdk/src/sessions. The harness CLI imports ./sessions/index.ts.")
        return 1
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
    if args.max_images <= 0 or args.max_images > DEFAULT_JUDGE_MAX_IMAGES:
        print(f"WARNING: clamping --max-images from {args.max_images} to {DEFAULT_JUDGE_MAX_IMAGES} to stay under common 50-image API limits.")
        args.max_images = DEFAULT_JUDGE_MAX_IMAGES
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
                        "judge_errored_rubrics",
                        "judge_error_messages",
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
                        "judge_errored_rubrics": item.get("judge_errored_rubrics", 0),
                        "judge_error_messages": " | ".join(str(msg) for msg in item.get("judge_error_messages", []) or []),
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
    env.setdefault("OSWORLD_DISABLE_RECORDING", "1")
    env.setdefault("OSWORLD_SYSTEM_PROMPT_SUFFIX", OSWORLD_BROWSER_PROMPT_SUFFIX)
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
            "OSWORLD_DISABLE_RECORDING": env.get("OSWORLD_DISABLE_RECORDING"),
            "OSWORLD_SYSTEM_PROMPT_SUFFIX": "set" if env.get("OSWORLD_SYSTEM_PROMPT_SUFFIX") else "unset",
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


def cmd_run_agentv4(args: argparse.Namespace) -> int:
    env_file = args.env_file or (REPO_ROOT / ".env")
    load_dotenv_file(env_file)
    agentv4_path = resolve_repo_path(args.agentv4_path).resolve()
    prepared_dir = args.prepared_dir if args.prepared_dir.is_absolute() else (REPO_ROOT / args.prepared_dir)
    prepared_dir = prepared_dir.resolve()
    selected_tasks_path = args.task_source_json or (prepared_dir / "selected_tasks.json")
    selected_tasks_path = selected_tasks_path if selected_tasks_path.is_absolute() else (REPO_ROOT / selected_tasks_path)
    selected_tasks_path = selected_tasks_path.resolve()
    if not selected_tasks_path.is_file():
        raise SystemExit(f"Prepared task source missing: {selected_tasks_path}")
    tasks = load_json(selected_tasks_path)
    if not isinstance(tasks, list) or not tasks:
        raise SystemExit(f"No tasks found in: {selected_tasks_path}")
    if args.limit is not None:
        tasks = tasks[:args.limit]

    result_dir = args.result_dir if args.result_dir.is_absolute() else (REPO_ROOT / args.result_dir)
    result_dir = result_dir.resolve()
    result_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = result_dir / "agentv4_raw"
    trajectories_dir = agentv4_runs_dir(result_dir, args.model, args.domain)
    raw_dir.mkdir(parents=True, exist_ok=True)
    trajectories_dir.mkdir(parents=True, exist_ok=True)
    console_log = result_dir / "run_console.log"
    manifest_path = result_dir / "run_manifest.json"
    report_output = resolve_repo_path(args.report_output)
    api_base = (args.api_base or os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE") or "https://tokenhub.sensetime.com/v1").rstrip("/")
    api_key_env = agentv4_api_key_env_for_model(args.model, args.api_key_env)
    selected_key = os.getenv(api_key_env) or os.getenv("OPENAI_API_KEY") or os.getenv("TOKENHUB_API_KEY")
    if not selected_key:
        raise SystemExit(f"API key is not set for AgentV4 model={args.model}; expected {api_key_env} or OPENAI_API_KEY.")

    runtime_changes = prepare_agentv4_runtime(
        agentv4_path,
        agent_id=args.agent_id,
        model=args.model,
        api_base=api_base,
        auto_patch=args.auto_patch_agentv4,
    )
    env = build_agentv4_env(
        os.environ,
        model=args.model,
        api_base=api_base,
        api_key_env=api_key_env,
        allow_insecure_tls=args.allow_insecure_tokenhub_tls,
    )

    started = datetime.now(timezone.utc)
    manifest = {
        "schema_version": 1,
        "status": "running",
        "agent_backend": "agentv4",
        "model": args.model,
        "max_steps": args.max_steps,
        "prepared_subset": str(prepared_dir),
        "task_source_json": str(selected_tasks_path),
        "result_dir": str(result_dir),
        "score_runs_dir": str(trajectories_dir),
        "agentv4_path": str(agentv4_path),
        "agentv4_agent_id": args.agent_id,
        "domain": args.domain,
        "api_base": api_base,
        "api_key_env": api_key_env,
        "env_provider": infer_env_provider(args.model),
        "env_flags": {
            "NODE_TLS_REJECT_UNAUTHORIZED": env.get("NODE_TLS_REJECT_UNAUTHORIZED"),
            "npm_config_strict_ssl": env.get("npm_config_strict_ssl"),
            "PYTHONIOENCODING": env.get("PYTHONIOENCODING"),
            "BROWSER_HEADLESS": env.get("BROWSER_HEADLESS"),
            "PLAYWRIGHT_HEADLESS": env.get("PLAYWRIGHT_HEADLESS"),
        },
        "runtime_changes": runtime_changes,
        "start_time": started.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "end_time": None,
        "duration_seconds": None,
        "exit_code": None,
        "console_log": str(console_log),
        "git": {
            "odysseys": git_info(REPO_ROOT),
            "agentv4": git_info(agentv4_path),
        },
    }
    manifest.update(expected_tasks_from_prepared(prepared_dir))
    write_json(manifest_path, manifest)

    print(f"Running AgentV4: {result_dir}")
    print(f"Manifest: {manifest_path}")
    summaries = []
    conversions = []
    with console_log.open("w", encoding="utf-8", errors="replace") as log:
        for index, task in enumerate(tasks):
            task_id = str(task.get("task_id") or task.get("id") or f"task-{index}")
            line = f"\n=== AgentV4 task {index + 1}/{len(tasks)}: {task_id} ===\n"
            print(line.strip())
            log.write(line)
            if args.close_browsers_each_task:
                run_agent_browser_close(agentv4_path, env)
            summary = run_agentv4_cli_task(
                agentv4_path=agentv4_path,
                agent_id=args.agent_id,
                task=task,
                task_index=index,
                raw_dir=raw_dir,
                env=env,
                max_steps=args.max_steps,
                task_timeout_ms=args.task_timeout_ms,
                preopen_website=args.preopen_website,
            )
            summaries.append(summary)
            log.write(json.dumps(summary, ensure_ascii=False) + "\n")
            task_out_dir = trajectories_dir / task_id
            if task_out_dir.exists():
                shutil.rmtree(task_out_dir)
            conversion = convert_agentv4_session_to_trajectory(
                agentv4_path=agentv4_path,
                task_id=task_id,
                session_id=summary.get("session_id"),
                raw_summary=summary,
                output_dir=task_out_dir,
                env=env,
            )
            conversions.append(conversion)
            log.write(json.dumps(conversion, ensure_ascii=False) + "\n")
            print(f"  exit={summary['exit_code']} session={summary.get('session_id')} steps={conversion['steps']} screenshots={conversion['screenshots']}")
        if args.close_browsers_each_task:
            run_agent_browser_close(agentv4_path, env)

    ended = datetime.now(timezone.utc)
    write_json(raw_dir / "summary.json", summaries)
    write_json(result_dir / "agentv4_conversions.json", conversions)
    failed = [item for item in summaries if item.get("exit_code") != 0]
    empty = [item for item in conversions if not item.get("steps")]
    manifest["status"] = "success" if not failed and not empty else ("partial" if conversions else "failed")
    manifest["end_time"] = ended.isoformat(timespec="seconds").replace("+00:00", "Z")
    manifest["duration_seconds"] = round((ended - started).total_seconds(), 3)
    manifest["exit_code"] = 0 if not failed else 1
    manifest["agentv4_failed_tasks"] = len(failed)
    manifest["empty_trajectories"] = len(empty)
    manifest.update(completion_from_runs(result_dir))
    write_json(manifest_path, manifest)

    print(f"AgentV4 duration_seconds={manifest['duration_seconds']}, failed_tasks={len(failed)}, empty_trajectories={len(empty)}")
    print(f"Console log: {console_log}")
    if args.write_report:
        report_output = report_output or (REPO_ROOT / "outputs" / "reports" / f"{result_dir.name}_report.json")
        report_args = argparse.Namespace(
            runs_dir=result_dir,
            console_log=console_log,
            output=report_output,
            csv_output=report_output.with_suffix(".csv"),
        )
        cmd_smoke_report(report_args)
    return int(manifest["exit_code"])


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


def parse_runtime_metadata(text: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            metadata[key] = value.strip()
    return metadata


def infer_runner_backend(runs_dir: Path, manifest: dict[str, Any]) -> str:
    backend = str(manifest.get("agent_backend") or "").strip().lower()
    if backend:
        return backend
    if (runs_dir / "agentv4_raw").is_dir() or (runs_dir / "agentv4_conversions.json").is_file():
        return "agentv4"
    return "osworld"


def cmd_smoke_report(args: argparse.Namespace) -> int:
    runs_dir = args.runs_dir.resolve()
    manifest = load_json(runs_dir / "run_manifest.json") if (runs_dir / "run_manifest.json").is_file() else {}
    runner_backend = infer_runner_backend(runs_dir, manifest if isinstance(manifest, dict) else {})
    console_text = read_text_auto(args.console_log) if args.console_log and args.console_log.is_file() else ""
    console_usage = extract_usage(console_text)
    console_duration = extract_console_duration_seconds(console_text)
    if console_duration is None and (runs_dir / "run_manifest.json").is_file():
        manifest_duration = load_json(runs_dir / "run_manifest.json").get("duration_seconds")
        if isinstance(manifest_duration, (int, float)):
            console_duration = round(float(manifest_duration), 3)
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
        runtime_meta = parse_runtime_metadata(runtime_text)
        summary = summary_by_task.get(task_id, {})
        score = summary.get("score")
        if runner_backend == "osworld" and score is None and result_text:
            try:
                score = float(result_text)
            except ValueError:
                score = None
        exit_code = None
        if runtime_meta.get("exit_code") not in (None, ""):
            try:
                exit_code = int(str(runtime_meta.get("exit_code")))
            except ValueError:
                exit_code = None
        runner_status = runtime_meta.get("status") or summary.get("status", "unknown")
        execution_success = int(
            bool(step_rows)
            and bool(screenshots)
            and not error_rows
            and (exit_code in (None, 0))
        )
        task_sr = 1 if runner_backend == "osworld" and score == 1 else (None if runner_backend != "osworld" else 0)

        task_reports.append({
            "task_id": task_id,
            "domain": task_dir.parent.name,
            "runner_backend": runner_backend,
            "status": runner_status,
            "runner_status": runner_status,
            "execution_success": execution_success,
            "exit_code": exit_code,
            "signal": runtime_meta.get("signal") or "",
            "task_sr": task_sr,
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

    osworld_task_reports = [item for item in task_reports if item.get("runner_backend") == "osworld"]
    osworld_sr_terms = [item["task_sr"] for item in osworld_task_reports if item.get("task_sr") is not None]
    aggregate = {
        "runs_dir": str(runs_dir),
        "runner_backend": runner_backend,
        "tasks": len(task_reports),
        "execution_success_rate": round(sum(item["execution_success"] for item in task_reports) / len(task_reports), 4) if task_reports else 0,
        "task_success_rate": round(sum(osworld_sr_terms) / len(osworld_sr_terms), 4) if osworld_sr_terms else None,
        "osworld_task_success_rate": round(sum(osworld_sr_terms) / len(osworld_sr_terms), 4) if osworld_sr_terms else None,
        "agentv4_execution_success_rate": round(sum(item["execution_success"] for item in task_reports) / len(task_reports), 4) if runner_backend == "agentv4" and task_reports else None,
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


def task_metadata_by_id(task_source_json: Path | None) -> dict[str, dict[str, Any]]:
    if not task_source_json or not task_source_json.is_file():
        return {}
    tasks = load_json(task_source_json)
    if not isinstance(tasks, list):
        return {}
    metadata = {}
    for task in tasks:
        if not isinstance(task, dict) or not task.get("task_id"):
            continue
        rubrics = task.get("rubrics", {})
        metadata[str(task["task_id"])] = {
            "level": task.get("level"),
            "reference_length": task.get("reference_length"),
            "num_rubrics": len(rubrics) if isinstance(rubrics, dict) else None,
            "task": task.get("confirmed_task"),
        }
    return metadata


def mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def model_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "", value).lower()
    return slug or "model"


def select_suite_model(config: dict[str, Any], model_name: str) -> dict[str, Any]:
    for item in config.get("models", []):
        if not isinstance(item, dict):
            continue
        names = {str(item.get("name", "")), str(item.get("agent_model", "")), str(item.get("slug", ""))}
        if model_name in names:
            return item
    available = [item.get("name") for item in config.get("models", []) if isinstance(item, dict)]
    raise SystemExit(f"Model not found in suite config: {model_name}. Available: {available}")


def rubric_judge_error(item: dict[str, Any]) -> str:
    error = str(item.get("judge_error") or "").strip()
    if error:
        return error
    reasoning = str(item.get("final_reasoning") or "").strip()
    if reasoning.startswith("Error judging rubric"):
        return reasoning
    return ""


def score_judge_error_stats(score: dict[str, Any]) -> tuple[int, list[str]]:
    messages: list[str] = []
    if score.get("error"):
        messages.append(str(score.get("error")))
    count = int(score.get("judge_errored_rubrics") or 0)
    for item in score.get("rubric_results", []) or []:
        if not isinstance(item, dict):
            continue
        message = rubric_judge_error(item)
        if message:
            if not score.get("judge_errored_rubrics"):
                count += 1
            if message not in messages:
                messages.append(message)
    for message in score.get("judge_error_messages", []) or []:
        text = str(message).strip()
        if text and text not in messages:
            messages.append(text)
    return count, messages


def cmd_merge_report(args: argparse.Namespace) -> int:
    runner_report_path = resolve_repo_path(args.runner_report).resolve()
    runner_report = load_json(runner_report_path)
    score_results = load_json(resolve_repo_path(args.score_results).resolve())
    runner_summary = runner_report.get("summary", {}) if isinstance(runner_report.get("summary", {}), dict) else {}
    runner_backend = str(runner_summary.get("runner_backend") or "").strip().lower()
    backend_hint = f"{runner_report_path} {runner_summary.get('runs_dir', '')}".lower()
    if not runner_backend:
        runner_backend = "agentv4" if "agentv4" in backend_hint else "osworld"
    task_source_json = resolve_repo_path(args.task_source_json)
    task_meta = task_metadata_by_id(task_source_json.resolve() if task_source_json else None)
    output = resolve_repo_path(args.output).resolve()
    csv_output = resolve_repo_path(args.csv_output)
    if csv_output:
        csv_output = csv_output.resolve()

    runner_by_task = {str(item.get("task_id")): item for item in runner_report.get("tasks", []) if item.get("task_id")}
    score_by_task = {str(item.get("task_id")): item for item in score_results.get("tasks", []) if item.get("task_id")}
    task_ids = sorted(set(runner_by_task) | set(score_by_task))
    if not task_ids:
        task_ids = sorted(set(task_meta))

    rows = []
    for task_id in task_ids:
        runner = runner_by_task.get(task_id, {})
        score = score_by_task.get(task_id, {})
        meta = task_meta.get(task_id, {})
        judge_errored_rubrics, judge_error_messages = score_judge_error_stats(score)
        rubric_scores = score.get("rubric_scores", {}) or {}
        rubric_score_sum = sum(float(value or 0) for value in rubric_scores.values()) if isinstance(rubric_scores, dict) else 0.0
        steps = score.get("num_steps") or runner.get("steps") or 0
        rubric_avg = score.get("average_rubric_score")
        try:
            efficiency = round(float(rubric_avg) / int(steps), 8) if rubric_avg is not None and int(steps) > 0 else None
        except (TypeError, ValueError):
            efficiency = None
        rows.append({
            "task_id": task_id,
            "model": args.model,
            "runner_backend": runner.get("runner_backend") or runner_backend,
            "level": meta.get("level"),
            "reference_length": meta.get("reference_length"),
            "num_rubrics": meta.get("num_rubrics") or len(rubric_scores),
            "rubric_score_sum": rubric_score_sum,
            "rubric_avg": rubric_avg,
            "perfect": score.get("perfect"),
            "trajectory_efficiency": efficiency,
            "judge_num_steps": score.get("num_steps"),
            "runner_steps": runner.get("steps"),
            "max_step_num": runner.get("max_step_num"),
            "screenshots": runner.get("screenshots"),
            "screenshots_sent": score.get("num_screenshots_sent"),
            "runner_status": runner.get("runner_status") or runner.get("status"),
            "execution_success": runner.get("execution_success"),
            "exit_code": runner.get("exit_code"),
            "runner_score": runner.get("score"),
            "runner_task_sr": runner.get("task_sr"),
            "osworld_status": runner.get("status") if (runner.get("runner_backend") or runner_backend) == "osworld" else None,
            "osworld_score": runner.get("score") if (runner.get("runner_backend") or runner_backend) == "osworld" else None,
            "osworld_task_sr": runner.get("task_sr") if (runner.get("runner_backend") or runner_backend) == "osworld" else None,
            "prompt_tokens": runner.get("prompt_tokens"),
            "completion_tokens": runner.get("completion_tokens"),
            "total_tokens": runner.get("total_tokens"),
            "calls_with_usage": runner.get("calls_with_usage"),
            "trajectory_errors": runner.get("trajectory_errors"),
            "runtime_error_mentions": runner.get("runtime_error_mentions"),
            "judge_error": score.get("error", ""),
            "judge_errored_rubrics": judge_errored_rubrics,
            "judge_error_messages": " | ".join(judge_error_messages),
            "run_dir": score.get("run_dir"),
        })

    scored_rows = [row for row in rows if row["rubric_avg"] is not None]
    perfect_count = sum(1 for row in scored_rows if row.get("perfect") is True)
    total_tokens = sum(int(row["total_tokens"] or 0) for row in rows)
    judge_total_steps = sum(int(row["judge_num_steps"] or 0) for row in rows)
    runner_total_steps = sum(int(row["runner_steps"] or 0) for row in rows)
    total_rubrics = sum(int(row["num_rubrics"] or 0) for row in rows)
    rubric_score_sum = sum(float(row["rubric_score_sum"] or 0) for row in rows)
    execution_terms = [int(row["execution_success"]) for row in rows if row.get("execution_success") is not None]
    is_osworld_backend = runner_backend == "osworld"
    is_agentv4_backend = runner_backend == "agentv4"
    summary = {
        "model": args.model,
        "runner_backend": runner_backend,
        "tasks": len(rows),
        "scored_tasks": len(scored_rows),
        "total_rubrics": total_rubrics,
        "average_rubric_score": round(rubric_score_sum / total_rubrics, 6) if total_rubrics else None,
        "perfect_tasks": perfect_count,
        "perfect_task_rate": round(perfect_count / len(scored_rows), 6) if scored_rows else None,
        "trajectory_efficiency": mean([float(row["trajectory_efficiency"]) for row in rows if row["trajectory_efficiency"] is not None]),
        "trajectory_efficiency_x100": mean([float(row["trajectory_efficiency"]) * 100 for row in rows if row["trajectory_efficiency"] is not None]),
        "execution_success_rate": round(sum(execution_terms) / len(execution_terms), 6) if execution_terms else runner_summary.get("execution_success_rate"),
        "agentv4_execution_success_rate": runner_summary.get("agentv4_execution_success_rate") if is_agentv4_backend else None,
        "osworld_task_success_rate": runner_summary.get("osworld_task_success_rate") if is_osworld_backend else None,
        "total_steps": runner_total_steps or judge_total_steps,
        "runner_total_steps": runner_total_steps,
        "judge_total_steps": judge_total_steps,
        "average_steps": mean([float(row["judge_num_steps"] or row["runner_steps"]) for row in rows if row["judge_num_steps"] or row["runner_steps"]]),
        "prompt_tokens": sum(int(row["prompt_tokens"] or 0) for row in rows),
        "completion_tokens": sum(int(row["completion_tokens"] or 0) for row in rows),
        "total_tokens": total_tokens,
        "average_tokens_per_task": round(total_tokens / len(rows), 3) if rows else None,
        "calls_with_usage": sum(int(row["calls_with_usage"] or 0) for row in rows),
        "trajectory_errors": sum(int(row["trajectory_errors"] or 0) for row in rows),
        "runtime_error_mentions": sum(int(row["runtime_error_mentions"] or 0) for row in rows),
        "judge_errored_tasks": sum(1 for row in rows if row.get("judge_error") or int(row.get("judge_errored_rubrics") or 0) > 0),
        "judge_errored_rubrics": sum(int(row["judge_errored_rubrics"] or 0) for row in rows),
        "tasks_with_judge_errors": sum(1 for row in rows if int(row.get("judge_errored_rubrics") or 0) > 0),
        "runner_duration_seconds": runner_report.get("summary", {}).get("duration_seconds"),
        "runner_report": str(runner_report_path),
        "score_results": str(resolve_repo_path(args.score_results).resolve()),
        "task_source_json": str(task_source_json.resolve()) if task_source_json else None,
    }
    by_level: dict[str, dict[str, Any]] = {}
    for level in sorted({row.get("level") or "unknown" for row in rows}):
        level_rows = [row for row in rows if (row.get("level") or "unknown") == level]
        level_scored = [row for row in level_rows if row["rubric_avg"] is not None]
        level_perfect = sum(1 for row in level_scored if row.get("perfect") is True)
        level_rubrics = sum(int(row["num_rubrics"] or 0) for row in level_rows)
        level_score_sum = sum(float(row["rubric_score_sum"] or 0) for row in level_rows)
        by_level[level] = {
            "tasks": len(level_rows),
            "average_rubric_score": round(level_score_sum / level_rubrics, 6) if level_rubrics else None,
            "perfect_task_rate": round(level_perfect / len(level_scored), 6) if level_scored else None,
            "execution_success_rate": round(sum(int(row["execution_success"]) for row in level_rows if row.get("execution_success") is not None) / len([row for row in level_rows if row.get("execution_success") is not None]), 6) if [row for row in level_rows if row.get("execution_success") is not None] else None,
            "average_steps": mean([float(row["judge_num_steps"] or row["runner_steps"]) for row in level_rows if row["judge_num_steps"] or row["runner_steps"]]),
            "total_tokens": sum(int(row["total_tokens"] or 0) for row in level_rows),
            "judge_errored_rubrics": sum(int(row["judge_errored_rubrics"] or 0) for row in level_rows),
            "tasks_with_judge_errors": sum(1 for row in level_rows if int(row.get("judge_errored_rubrics") or 0) > 0),
        }
    summary["by_level"] = by_level

    merged = {"summary": summary, "tasks": rows}
    write_json(output, merged)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"Wrote merged report: {output}")

    if csv_output:
        csv_output.parent.mkdir(parents=True, exist_ok=True)
        try:
            with csv_output.open("w", newline="", encoding="utf-8") as f:
                fieldnames = list(rows[0].keys()) if rows else ["task_id"]
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            print(f"Wrote merged CSV: {csv_output}")
        except PermissionError as exc:
            print(f"WARNING: could not write CSV because the file is locked: {csv_output} ({exc})")
    return 0


def cmd_run_suite(args: argparse.Namespace) -> int:
    config_path = resolve_repo_path(args.config).resolve()
    config = load_json(config_path)
    model_cfg = select_suite_model(config, args.model)

    suite_name = args.suite_name or config.get("suite_name", "dev_10")
    domain = config.get("domain", "mind2web_chrome")
    prepared_dir = resolve_repo_path(Path(config.get("prepared_dir", f"outputs/{suite_name}"))).resolve()
    task_source_json = resolve_repo_path(Path(config.get("task_source_json", str(prepared_dir / "selected_tasks.json")))).resolve()
    reports_dir = resolve_repo_path(Path(config.get("reports_dir", "outputs/reports"))).resolve()
    scores_dir = resolve_repo_path(Path(config.get("scores_dir", "outputs/scores"))).resolve()
    leaderboards_dir = resolve_repo_path(Path(config.get("leaderboards_dir", "outputs/leaderboards"))).resolve()

    agent_model = model_cfg.get("agent_model") or model_cfg.get("name") or args.model
    slug = model_cfg.get("slug") or model_slug(str(agent_model))
    result_dir = resolve_repo_path(Path(model_cfg.get("result_dir", f"outputs/runs_{suite_name}_{slug}"))).resolve()
    runner_report = reports_dir / f"{suite_name}_{slug}_runner_report.json"
    score_output = scores_dir / f"{suite_name}_{slug}_eval.json"
    score_csv = score_output.with_suffix(".csv")
    merged_output = leaderboards_dir / f"{suite_name}_{slug}_baseline.json"
    merged_csv = merged_output.with_suffix(".csv")
    score_runs_dir = result_dir / "pyautogui" / "screenshot" / str(agent_model) / domain

    max_steps = int(args.max_steps or model_cfg.get("max_steps") or config.get("max_steps", 30))
    judge = config.get("judge", {})
    osworld = config.get("osworld", {})
    agent_backend = args.agent_backend or model_cfg.get("agent_backend") or config.get("agent_backend", "osworld")
    agentv4 = config.get("agentv4", {})
    agentv4_path = resolve_repo_path(Path(args.agentv4_path or agentv4.get("path", str(DEFAULT_AGENTV4_PATH)))).resolve()

    plan = {
        "suite": suite_name,
        "agent_backend": agent_backend,
        "model": agent_model,
        "slug": slug,
        "prepared_dir": str(prepared_dir),
        "task_source_json": str(task_source_json),
        "max_steps": max_steps,
        "judge_model": args.judge_model or judge.get("model", config.get("judge_model", "gpt-5.5")),
        "result_dir": str(result_dir),
        "runner_report": str(runner_report),
        "score_runs_dir": str(score_runs_dir),
        "score_output": str(score_output),
        "merged_output": str(merged_output),
    }
    if agent_backend == "agentv4":
        plan["agentv4_path"] = str(agentv4_path)
        plan["agentv4_agent_id"] = agentv4.get("agent_id", "browser-gui")
    print(json.dumps(plan, indent=2, ensure_ascii=False))
    if args.dry_run:
        return 0

    if agent_backend == "agentv4":
        run_args = argparse.Namespace(
            prepared_dir=prepared_dir,
            result_dir=result_dir,
            task_source_json=task_source_json,
            agentv4_path=agentv4_path,
            agent_id=agentv4.get("agent_id", "browser-gui"),
            model=str(agent_model),
            max_steps=max_steps,
            domain=domain,
            env_file=args.env_file,
            api_base=model_cfg.get("api_base") or config.get("api_base", "https://tokenhub.sensetime.com/v1"),
            api_key_env=model_cfg.get("api_key_env"),
            task_timeout_ms=int(model_cfg.get("task_timeout_ms") or agentv4.get("task_timeout_ms", 1_800_000)),
            preopen_website=bool(agentv4.get("preopen_website", True)),
            close_browsers_each_task=bool(agentv4.get("close_browsers_each_task", True)),
            allow_insecure_tokenhub_tls=bool(agentv4.get("allow_insecure_tokenhub_tls", True)),
            auto_patch_agentv4=bool(agentv4.get("auto_patch_agentv4", True)),
            limit=None,
            write_report=True,
            report_output=runner_report,
        )
        run_code = cmd_run_agentv4(run_args)

        finalize_args = argparse.Namespace(
            result_dir=result_dir,
            prepared_dir=prepared_dir,
            manifest=None,
            exit_code=run_code,
            status=None,
            write_report=True,
            write_csv=True,
            console_log=None,
            report_output=runner_report,
        )
        cmd_finalize_run(finalize_args)
        if run_code != 0 and not args.continue_on_runner_error:
            return run_code

        score_args = argparse.Namespace(
            runs_dir=score_runs_dir,
            task_source_json=task_source_json,
            output=score_output,
            model=plan["judge_model"],
            num_workers=int(args.judge_num_workers or judge.get("num_workers", 1)),
            max_images=int(judge.get("max_images", DEFAULT_JUDGE_MAX_IMAGES)),
            max_steps=int(judge.get("max_steps", 100)),
            include_incomplete=bool(judge.get("include_incomplete", False)),
            api_base=args.api_base or judge.get("api_base") or config.get("api_base"),
            env_file=args.env_file,
            use_curl_openai=bool(judge.get("use_curl_openai", True)),
            manifest_output=None,
            csv_output=score_csv,
        )
        score_code = cmd_score(score_args)
        if score_code != 0:
            return score_code

        merge_args = argparse.Namespace(
            runner_report=runner_report,
            score_results=score_output,
            task_source_json=task_source_json,
            output=merged_output,
            csv_output=merged_csv,
            model=str(agent_model),
        )
        return cmd_merge_report(merge_args)

    if agent_backend != "osworld":
        raise SystemExit(f"Unsupported agent backend: {agent_backend}")

    run_args = argparse.Namespace(
        prepared_dir=prepared_dir,
        result_dir=result_dir,
        osworld_path=args.osworld_path or osworld.get("path"),
        path_to_vm=args.path_to_vm or osworld.get("path_to_vm"),
        provider_name=osworld.get("provider_name", "docker"),
        headless=bool(osworld.get("headless", True)),
        observation_type=osworld.get("observation_type", "screenshot"),
        model=str(agent_model),
        sleep_after_execution=float(osworld.get("sleep_after_execution", 0.0)),
        max_steps=max_steps,
        domain=domain,
        env_file=args.env_file,
        openai_base_url=model_cfg.get("openai_base_url") or config.get("openai_base_url"),
        use_curl_openai=bool(model_cfg.get("use_curl_openai", config.get("use_curl_openai", True))),
        python=args.python,
        write_report=True,
        report_output=runner_report,
    )
    run_code = cmd_run_osworld(run_args)

    finalize_args = argparse.Namespace(
        result_dir=result_dir,
        prepared_dir=prepared_dir,
        manifest=None,
        exit_code=run_code,
        status=None,
        write_report=True,
        write_csv=True,
        console_log=None,
        report_output=runner_report,
    )
    cmd_finalize_run(finalize_args)
    if run_code != 0 and not args.continue_on_runner_error:
        return run_code

    score_args = argparse.Namespace(
        runs_dir=score_runs_dir,
        task_source_json=task_source_json,
        output=score_output,
        model=plan["judge_model"],
        num_workers=int(args.judge_num_workers or judge.get("num_workers", 1)),
        max_images=int(judge.get("max_images", DEFAULT_JUDGE_MAX_IMAGES)),
        max_steps=int(judge.get("max_steps", 100)),
        include_incomplete=bool(judge.get("include_incomplete", False)),
        api_base=args.api_base or judge.get("api_base") or config.get("api_base"),
        env_file=args.env_file,
        use_curl_openai=bool(judge.get("use_curl_openai", True)),
        manifest_output=None,
        csv_output=score_csv,
    )
    score_code = cmd_score(score_args)
    if score_code != 0:
        return score_code

    merge_args = argparse.Namespace(
        runner_report=runner_report,
        score_results=score_output,
        task_source_json=task_source_json,
        output=merged_output,
        csv_output=merged_csv,
        model=str(agent_model),
    )
    return cmd_merge_report(merge_args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local orchestration for Odysseys reproduction runs.")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Check local dataset, scripts, dependencies, and key environment variables.")
    doctor.set_defaults(func=cmd_doctor)

    agentv4_doctor = sub.add_parser("agentv4-doctor", help="Check the local AgentV4 browser-gui framework integration.")
    agentv4_doctor.add_argument("--agentv4-path", type=Path, default=DEFAULT_AGENTV4_PATH)
    agentv4_doctor.set_defaults(func=cmd_agentv4_doctor)

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
    score.add_argument("--max-images", type=int, default=DEFAULT_JUDGE_MAX_IMAGES)
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

    run_agentv4 = sub.add_parser("run-agentv4", help="Run AgentV4 browser-gui on a prepared Odysseys subset and adapt transcripts into scorer-compatible trajectories.")
    run_agentv4.add_argument("--prepared-dir", type=Path, required=True)
    run_agentv4.add_argument("--result-dir", type=Path, required=True)
    run_agentv4.add_argument("--task-source-json", type=Path, default=None, help="Defaults to <prepared-dir>/selected_tasks.json.")
    run_agentv4.add_argument("--agentv4-path", type=Path, default=DEFAULT_AGENTV4_PATH)
    run_agentv4.add_argument("--agent-id", default="browser-gui")
    run_agentv4.add_argument("--model", default="claude-opus-4-7-thinking")
    run_agentv4.add_argument("--max-steps", type=int, default=30)
    run_agentv4.add_argument("--domain", default="mind2web_chrome")
    run_agentv4.add_argument("--env-file", type=Path, default=None)
    run_agentv4.add_argument("--api-base", default="https://tokenhub.sensetime.com/v1")
    run_agentv4.add_argument("--api-key-env", default=None, help="Override key env name used for the model; never written to manifests.")
    run_agentv4.add_argument("--task-timeout-ms", type=int, default=1_800_000)
    run_agentv4.add_argument("--preopen-website", action=argparse.BooleanOptionalAction, default=True)
    run_agentv4.add_argument("--close-browsers-each-task", action=argparse.BooleanOptionalAction, default=True)
    run_agentv4.add_argument("--allow-insecure-tokenhub-tls", action=argparse.BooleanOptionalAction, default=True)
    run_agentv4.add_argument("--auto-patch-agentv4", action=argparse.BooleanOptionalAction, default=True)
    run_agentv4.add_argument("--limit", type=int, default=None, help="Run only the first N prepared tasks for smoke testing.")
    run_agentv4.add_argument("--write-report", action=argparse.BooleanOptionalAction, default=True)
    run_agentv4.add_argument("--report-output", type=Path, default=None)
    run_agentv4.set_defaults(func=cmd_run_agentv4)

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

    merge = sub.add_parser("merge-report", help="Merge runner health/cost report with rubric judge scores.")
    merge.add_argument("--runner-report", type=Path, required=True)
    merge.add_argument("--score-results", type=Path, required=True)
    merge.add_argument("--task-source-json", type=Path, default=None)
    merge.add_argument("--output", type=Path, required=True)
    merge.add_argument("--csv-output", type=Path, default=None)
    merge.add_argument("--model", default=None)
    merge.set_defaults(func=cmd_merge_report)

    suite = sub.add_parser("run-suite", help="Run one configured model through run-osworld, finalize-run, score, and merge-report.")
    suite.add_argument("--config", type=Path, default=REPO_ROOT / "configs" / "dev_10_models.example.json")
    suite.add_argument("--model", required=True, help="Model name, agent_model, or slug from the suite config.")
    suite.add_argument("--agent-backend", choices=["osworld", "agentv4"], default=None)
    suite.add_argument("--agentv4-path", default=None)
    suite.add_argument("--suite-name", default=None)
    suite.add_argument("--max-steps", type=int, default=None)
    suite.add_argument("--judge-model", default=None)
    suite.add_argument("--judge-num-workers", type=int, default=None)
    suite.add_argument("--api-base", default=None)
    suite.add_argument("--env-file", type=Path, default=None)
    suite.add_argument("--osworld-path", default=None)
    suite.add_argument("--path-to-vm", default=None)
    suite.add_argument("--python", default=None, help="Python executable for OSWorld; defaults to the current interpreter.")
    suite.add_argument("--continue-on-runner-error", action=argparse.BooleanOptionalAction, default=False)
    suite.add_argument("--dry-run", action="store_true")
    suite.set_defaults(func=cmd_run_suite)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)
