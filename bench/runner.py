#!/usr/bin/env python3
"""Benchmark harness: bare-library agent vs. agent-framework agent on epydemix tasks.

Runs a fixed set of modeling tasks through `claude -p` under two experimental
conditions (git branches), each in an isolated workspace and Python environment,
and records wall-clock time, turn counts, token usage and cost.

Subcommands
-----------
  doctor   check prerequisites and smoke-test the exact claude invocation used
  setup    build the per-condition source checkouts and virtualenvs
  list     show the discovered tasks and conditions
  run      execute the benchmark matrix
  report   aggregate a completed (or partial) run into CSV + markdown
  clean    delete cached checkouts/venvs

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BENCH_DIR = Path(__file__).resolve().parent
REPO_DIR = BENCH_DIR.parent
CACHE_DIR = BENCH_DIR / ".cache"
CHECKOUT_DIR = CACHE_DIR / "checkouts"
VENV_DIR = CACHE_DIR / "venvs"
RESULTS_DIR = BENCH_DIR / "results"

# Agents run OUTSIDE the benchmarked repository. A workspace nested inside the
# real repo lets `git rev-parse --show-toplevel` (and plain `cd ..`) escape into
# it, at which point the agent does its work in the user's tree instead of the
# sandbox. Workspaces are moved into results/ only after the agent has exited.
DEFAULT_WORKSPACE_ROOT = Path.home() / ".cache" / "epydemix-bench-workspaces"
TASKS_DIR = BENCH_DIR / "tasks"
CONDITIONS_FILE = BENCH_DIR / "conditions.json"

TURN_SEPARATOR = "<<<TURN>>>"

# Tools the agent may use. Deliberately excludes planning/orchestration tools
# (Task/Agent, TodoWrite, Workflow), web access, and anything that would let one
# arm reach outside the benchmark sandbox. Bash covers CLI use and running
# custom Python; Write/Edit cover authoring scripts and configs.
DEFAULT_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,BashOutput,KillShell"

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_EFFORT = "medium"
DEFAULT_TIMEOUT_S = 5400


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def log(msg: str) -> None:
    print(f"[bench {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def run_cmd(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """Run a command, raising with captured output on failure."""
    proc = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc


# --------------------------------------------------------------------------- #
# config / task loading
# --------------------------------------------------------------------------- #


def load_conditions() -> tuple[dict[str, dict], str]:
    if not CONDITIONS_FILE.exists():
        die(f"missing {CONDITIONS_FILE}")
    cfg = json.loads(CONDITIONS_FILE.read_text())
    return cfg["conditions"], cfg.get("common_suffix", "")


def parse_task_file(path: Path) -> dict:
    """Parse a task markdown file with a simple `key: value` frontmatter block."""
    text = path.read_text()
    meta: dict[str, Any] = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 3)
        if end == -1:
            die(f"unterminated frontmatter in {path}")
        for line in text[4:end].splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
        body = text[end + 5 :]

    turns = [t.strip() for t in body.split(TURN_SEPARATOR)]
    turns = [t for t in turns if t]
    if not turns:
        die(f"no prompt turns found in {path}")

    return {
        "id": meta.get("id", path.stem),
        "title": meta.get("title", path.stem),
        "timeout_s": int(meta.get("timeout_s", DEFAULT_TIMEOUT_S)),
        "tags": [t.strip() for t in meta.get("tags", "").split(",") if t.strip()],
        "source": meta.get("source", ""),
        "path": str(path),
        "turns": turns,
    }


def load_tasks(selector: str) -> list[dict]:
    files = sorted(TASKS_DIR.glob("*.md"))
    if not files:
        die(f"no task files in {TASKS_DIR}")
    tasks = [parse_task_file(p) for p in files]
    if selector in ("", "all"):
        return tasks
    wanted = [s.strip() for s in selector.split(",") if s.strip()]
    by_id = {t["id"]: t for t in tasks}
    picked = []
    for w in wanted:
        matches = [t for t in tasks if t["id"] == w or t["id"].startswith(w)]
        if not matches:
            die(f"unknown task '{w}'. Available: {', '.join(by_id)}")
        picked.extend(m for m in matches if m not in picked)
    return picked


# --------------------------------------------------------------------------- #
# setup: per-condition source checkout + venv
# --------------------------------------------------------------------------- #


def git_rev(branch: str) -> str:
    return run_cmd(["git", "-C", str(REPO_DIR), "rev-parse", branch]).stdout.strip()


def export_branch(branch: str, dest: Path) -> None:
    """Extract a pristine copy of `branch` into `dest` (tracked files only).

    `git archive` is used rather than `git worktree` so that the benchmark never
    registers state in the user's repository, and so that untracked files (old
    transcripts, scratch outputs, local .claude config) can never leak into an
    agent's workspace.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    tar_path = dest.parent / f".{dest.name}.tar"
    with open(tar_path, "wb") as fh:
        proc = subprocess.run(
            ["git", "-C", str(REPO_DIR), "archive", "--format=tar", branch],
            stdout=fh,
            stderr=subprocess.PIPE,
            text=False,
        )
    if proc.returncode != 0:
        tar_path.unlink(missing_ok=True)
        die(f"git archive {branch} failed: {proc.stderr.decode(errors='replace')}")
    with tarfile.open(tar_path) as tf:
        try:
            tf.extractall(dest, filter="data")
        except TypeError:  # python < 3.12
            tf.extractall(dest)
    tar_path.unlink()


def init_workspace_repo(workspace: Path) -> None:
    """Make the workspace a self-contained git repository.

    Without this, `git rev-parse --show-toplevel` — a very common first move for
    an agent asked to work "at the repository root" — walks up past the workspace
    and resolves to whatever repo encloses it.
    """
    ident = [
        "-c",
        "user.email=bench@localhost",
        "-c",
        "user.name=epydemix bench",
        "-c",
        "commit.gpgsign=false",
    ]
    try:
        run_cmd(["git", "init", "-q", "-b", "main", str(workspace)])
    except RuntimeError:  # git < 2.28 has no -b
        run_cmd(["git", "init", "-q", str(workspace)])
    run_cmd(["git", "-C", str(workspace), *ident, "add", "-A"])
    run_cmd(
        ["git", "-C", str(workspace), *ident, "commit", "-q", "-m", "benchmark start"]
    )


def venv_python(cond_name: str) -> Path:
    return VENV_DIR / cond_name / "bin" / "python"


def setup_condition(name: str, cond: dict, force: bool = False) -> dict:
    checkout = CHECKOUT_DIR / name
    venv = VENV_DIR / name
    py = venv_python(name)
    rev = git_rev(cond["branch"])

    stamp_file = venv / ".bench-stamp.json"
    stamp = {
        "branch": cond["branch"],
        "rev": rev,
        "install_spec": cond.get("install_spec", "."),
        "install_extras": cond.get("install_extras", []),
    }
    if not force and stamp_file.exists() and py.exists():
        if json.loads(stamp_file.read_text()) == stamp:
            log(f"  {name}: up to date ({cond['branch']} @ {rev[:8]})")
            return stamp

    log(f"  {name}: exporting {cond['branch']} @ {rev[:8]} -> {checkout}")
    CHECKOUT_DIR.mkdir(parents=True, exist_ok=True)
    export_branch(cond["branch"], checkout)

    uv = shutil.which("uv")
    log(f"  {name}: creating venv -> {venv}")
    if venv.exists():
        shutil.rmtree(venv)
    venv.parent.mkdir(parents=True, exist_ok=True)
    if uv:
        run_cmd([uv, "venv", str(venv)])
    else:
        run_cmd([sys.executable, "-m", "venv", str(venv)])

    spec = cond.get("install_spec", ".")
    targets = [spec.replace(".", str(checkout), 1)] + list(
        cond.get("install_extras", [])
    )
    log(f"  {name}: installing {' '.join(targets)}")
    if uv:
        run_cmd([uv, "pip", "install", "--python", str(py), *targets])
    else:
        run_cmd([str(py), "-m", "pip", "install", "--quiet", *targets])

    stamp_file.write_text(json.dumps(stamp, indent=2))
    return stamp


def cmd_setup(args: argparse.Namespace) -> None:
    conditions, _ = load_conditions()
    names = split_selector(args.conditions, conditions)
    log("setting up conditions")
    for name in names:
        setup_condition(name, conditions[name], force=args.force)
    log("setup complete")


# --------------------------------------------------------------------------- #
# stream-json parsing
# --------------------------------------------------------------------------- #


def parse_stream(path: Path) -> dict:
    """Extract metrics from one turn's stream-json transcript."""
    m: dict[str, Any] = {
        "result_found": False,
        "is_error": None,
        "subtype": None,
        "terminal_reason": None,
        "num_turns": None,
        "duration_ms": None,
        "duration_api_ms": None,
        "total_cost_usd": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "n_assistant_messages": 0,
        "n_thinking_blocks": 0,
        "n_tool_calls": 0,
        "n_tool_errors": 0,
        "tool_calls_by_name": {},
        "n_bash_calls": 0,
        "models_used": {},
        "final_text_chars": 0,
        "session_id": None,
        "permission_denials": 0,
        "rate_limited": False,
    }
    if not path.exists():
        return m

    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = ev.get("type")
            if ev.get("session_id") and not m["session_id"]:
                m["session_id"] = ev["session_id"]

            if etype == "rate_limit_event":
                info = ev.get("rate_limit_info", {})
                if info.get("status") not in (None, "allowed"):
                    m["rate_limited"] = True

            elif etype == "assistant":
                m["n_assistant_messages"] += 1
                for block in ev.get("message", {}).get("content", []) or []:
                    btype = block.get("type")
                    if btype == "tool_use":
                        name = block.get("name", "?")
                        m["n_tool_calls"] += 1
                        m["tool_calls_by_name"][name] = (
                            m["tool_calls_by_name"].get(name, 0) + 1
                        )
                        if name == "Bash":
                            m["n_bash_calls"] += 1
                    elif btype == "thinking":
                        m["n_thinking_blocks"] += 1

            elif etype == "user":
                for block in ev.get("message", {}).get("content", []) or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        if block.get("is_error"):
                            m["n_tool_errors"] += 1

            elif etype == "result":
                m["result_found"] = True
                m["is_error"] = bool(ev.get("is_error"))
                m["subtype"] = ev.get("subtype")
                m["terminal_reason"] = ev.get("terminal_reason")
                m["num_turns"] = ev.get("num_turns")
                m["duration_ms"] = ev.get("duration_ms")
                m["duration_api_ms"] = ev.get("duration_api_ms")
                m["total_cost_usd"] = ev.get("total_cost_usd")
                m["permission_denials"] = len(ev.get("permission_denials") or [])
                usage = ev.get("usage") or {}
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                ):
                    m[key] = usage.get(key, 0) or 0
                for model, mu in (ev.get("modelUsage") or {}).items():
                    m["models_used"][model] = {
                        "input": mu.get("inputTokens", 0),
                        "output": mu.get("outputTokens", 0),
                        "cache_read": mu.get("cacheReadInputTokens", 0),
                        "cache_creation": mu.get("cacheCreationInputTokens", 0),
                        "cost_usd": mu.get("costUSD", 0.0),
                    }
                m["final_text_chars"] = len(ev.get("result") or "")

    # modelUsage is the authoritative per-model accounting; the top-level usage
    # block only reports the final request's tokens on some versions.
    if m["models_used"]:
        m["input_tokens_total"] = sum(v["input"] for v in m["models_used"].values())
        m["output_tokens_total"] = sum(v["output"] for v in m["models_used"].values())
        m["cache_read_total"] = sum(v["cache_read"] for v in m["models_used"].values())
        m["cache_creation_total"] = sum(
            v["cache_creation"] for v in m["models_used"].values()
        )
    else:
        m["input_tokens_total"] = m["input_tokens"]
        m["output_tokens_total"] = m["output_tokens"]
        m["cache_read_total"] = m["cache_read_input_tokens"]
        m["cache_creation_total"] = m["cache_creation_input_tokens"]

    m["billable_tokens_total"] = (
        m["input_tokens_total"]
        + m["output_tokens_total"]
        + m["cache_read_total"]
        + m["cache_creation_total"]
    )
    return m


def sum_turn_metrics(turns: list[dict]) -> dict:
    """Aggregate per-turn metrics into a per-cell total."""
    agg: dict[str, Any] = {
        "n_prompt_turns": len(turns),
        "num_turns": 0,
        "duration_ms": 0,
        "duration_api_ms": 0,
        "total_cost_usd": 0.0,
        "input_tokens_total": 0,
        "output_tokens_total": 0,
        "cache_read_total": 0,
        "cache_creation_total": 0,
        "billable_tokens_total": 0,
        "n_assistant_messages": 0,
        "n_thinking_blocks": 0,
        "n_tool_calls": 0,
        "n_tool_errors": 0,
        "n_bash_calls": 0,
        "permission_denials": 0,
        "tool_calls_by_name": {},
        "errors": [],
        "rate_limited": False,
        "paths_outside_workspace": [],
        "paths_into_benchmarked_repo": [],
        "paths_into_benchmarked_repo_existing": [],
    }
    for t in turns:
        for key in (
            "num_turns",
            "duration_ms",
            "duration_api_ms",
            "input_tokens_total",
            "output_tokens_total",
            "cache_read_total",
            "cache_creation_total",
            "billable_tokens_total",
            "n_assistant_messages",
            "n_thinking_blocks",
            "n_tool_calls",
            "n_tool_errors",
            "n_bash_calls",
            "permission_denials",
        ):
            agg[key] += t.get(key) or 0
        agg["total_cost_usd"] += t.get("total_cost_usd") or 0.0
        agg["rate_limited"] = agg["rate_limited"] or bool(t.get("rate_limited"))
        for name, n in (t.get("tool_calls_by_name") or {}).items():
            agg["tool_calls_by_name"][name] = agg["tool_calls_by_name"].get(name, 0) + n
        for key in (
            "paths_outside_workspace",
            "paths_into_benchmarked_repo",
            "paths_into_benchmarked_repo_existing",
        ):
            for p in t.get(key) or []:
                if p not in agg[key]:
                    agg[key].append(p)
        if t.get("is_error") or not t.get("result_found"):
            agg["errors"].append(
                {
                    "turn": t.get("turn_index"),
                    "subtype": t.get("subtype"),
                    "terminal_reason": t.get("terminal_reason"),
                    "result_found": t.get("result_found"),
                }
            )
    agg["n_paths_outside_workspace"] = len(agg["paths_outside_workspace"])
    agg["n_paths_into_benchmarked_repo"] = len(agg["paths_into_benchmarked_repo"])
    agg["n_paths_into_benchmarked_repo_existing"] = len(
        agg["paths_into_benchmarked_repo_existing"]
    )
    return agg


# --------------------------------------------------------------------------- #
# workspace artifact inventory
# --------------------------------------------------------------------------- #


SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".ipynb_checkpoints"}


def snapshot_files(root: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                out[str(p.relative_to(root))] = p.stat().st_size
            except OSError:
                continue
    return out


ABS_PATH_RE = re.compile(r"(?:/Users/[^\s\"',:;()\[\]$]+|/home/[^\s\"',:;()\[\]$]+)")

# A token containing shell-glob metacharacters is a search pattern, not a path an
# agent actually touched. Counting them produced false escape alarms from things
# like `grep -rln foo /Users/ciro/src/epydemix/*/tests 2>/dev/null`.
GLOB_META = set("*?[]{}")


def detect_escapes(stream_path: Path, workspace: Path) -> dict:
    """Find absolute paths in tool inputs that point outside the workspace.

    Catches the failure mode where an agent resolves "the repository root" to the
    repo enclosing its sandbox and does all its work there. Paths inside the
    condition's venv are expected (the agent inspects its interpreter) and are
    not counted.
    """
    ws = str(workspace)
    venv = str(VENV_DIR)
    repo = str(REPO_DIR)
    outside: set[str] = set()
    into_repo: set[str] = set()
    if not stream_path.exists():
        return {"n_paths_outside_workspace": 0, "n_paths_into_benchmarked_repo": 0,
                "paths_outside_workspace": [], "paths_into_benchmarked_repo": []}

    with stream_path.open() as fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "assistant":
                continue
            for block in ev.get("message", {}).get("content", []) or []:
                if block.get("type") != "tool_use":
                    continue
                blob = json.dumps(block.get("input", {}))
                for path in ABS_PATH_RE.findall(blob):
                    path = path.rstrip("\\/.,")
                    if path.startswith(ws) or path.startswith(venv):
                        continue
                    if GLOB_META & set(path):
                        continue
                    outside.add(path)
                    # Escapes into the repo under test are the damaging kind:
                    # the agent is now reading and writing the user's tree.
                    if path == repo or path.startswith(repo + "/"):
                        into_repo.add(path)

    # A path that still exists is evidence the agent actually touched something;
    # one that never resolved is a dead reference. Both are reported, but only
    # the former should trigger a re-run.
    existing = sorted(p for p in into_repo if os.path.exists(p))
    return {
        "n_paths_outside_workspace": len(outside),
        "n_paths_into_benchmarked_repo": len(into_repo),
        "n_paths_into_benchmarked_repo_existing": len(existing),
        "paths_outside_workspace": sorted(outside)[:100],
        "paths_into_benchmarked_repo": sorted(into_repo)[:100],
        "paths_into_benchmarked_repo_existing": existing[:100],
    }


FIGURE_SUFFIXES = {".png", ".pdf", ".svg", ".jpg", ".jpeg"}


def distinct_figures(root: Path, paths: list[str]) -> list[str]:
    """Collapse byte-identical images to one entry.

    Frameworks that register a figure inside each result bundle *and* keep a
    copy in an output directory otherwise inflate the count several-fold — a
    3-figure deliverable was being reported as 12. Hashing content (rather than
    basenames) keeps genuinely different images that share a filename apart.
    """
    seen: dict[str, str] = {}
    for rel in sorted(paths):
        try:
            digest = hashlib.sha1((root / rel).read_bytes()).hexdigest()
        except OSError:
            digest = f"unreadable:{rel}"
        seen.setdefault(digest, rel)
    return sorted(seen.values())


def artifact_summary(before: dict[str, int], after: dict[str, int], root: Path) -> dict:
    new = {k: v for k, v in after.items() if k not in before}
    changed = [k for k, v in after.items() if k in before and before[k] != v]
    figure_files = [k for k in new if Path(k).suffix.lower() in FIGURE_SUFFIXES]
    figures = distinct_figures(root, figure_files)
    code = [k for k in new if Path(k).suffix.lower() in {".py", ".sh"}]
    configs = [k for k in new if Path(k).suffix.lower() in {".yaml", ".yml", ".json", ".toml"}]
    data = [k for k in new if Path(k).suffix.lower() in {".csv", ".parquet", ".npz", ".nc"}]
    return {
        "n_new_files": len(new),
        "n_modified_files": len(changed),
        "new_bytes": sum(new.values()),
        "n_figures": len(figures),
        "n_figure_files": len(figure_files),
        "n_code_files": len(code),
        "n_config_files": len(configs),
        "n_data_files": len(data),
        "figures": figures[:50],
        "figure_files": sorted(figure_files)[:100],
        "new_files": sorted(new)[:500],
        "modified_files": sorted(changed)[:100],
    }


# --------------------------------------------------------------------------- #
# running a single cell
# --------------------------------------------------------------------------- #


def build_env(cond_name: str) -> dict[str, str]:
    env = os.environ.copy()
    venv = VENV_DIR / cond_name
    env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = f"{venv / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    # Headless plotting: agents must not block on a GUI backend.
    env["MPLBACKEND"] = "Agg"
    env["PYTHONUNBUFFERED"] = "1"
    # Keep each run's Claude Code project state out of the shared config dir.
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


def claude_command(
    prompt: str,
    args: argparse.Namespace,
    session_id: str,
    first_turn: bool,
) -> list[str]:
    cmd = [
        args.claude_bin,
        "-p",
        prompt,
        "--model",
        args.model,
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
        "--tools",
        args.tools,
        "--effort",
        args.effort,
        "--strict-mcp-config",
        "--disable-slash-commands",
    ]
    if not args.no_safe_mode:
        # Disables CLAUDE.md discovery, skills, plugins, hooks, custom agents and
        # output styles, so neither arm inherits the developer's local setup.
        cmd.append("--safe-mode")
    if first_turn:
        cmd += ["--session-id", session_id]
    else:
        cmd += ["--resume", session_id]
    return cmd


def run_cell(
    task: dict,
    cond_name: str,
    cond: dict,
    rep: int,
    common_suffix: str,
    args: argparse.Namespace,
    run_dir: Path,
    pin_rev: str | None = None,
) -> dict:
    cell_dir = run_dir / task["id"] / cond_name / f"rep{rep:02d}"
    logs = cell_dir / "logs"
    cell_dir.mkdir(parents=True, exist_ok=True)
    logs.mkdir(exist_ok=True)

    # The agent runs outside the benchmarked repo, then the tree is moved into
    # the results directory once it has exited.
    workspace = (
        Path(args.workspace_root)
        / run_dir.name
        / task["id"]
        / cond_name
        / f"rep{rep:02d}"
    )
    workspace.parent.mkdir(parents=True, exist_ok=True)

    source_ref = pin_rev or cond["branch"]
    log(f"  export {source_ref} -> {workspace}")
    export_branch(source_ref, workspace)
    init_workspace_repo(workspace)
    before = snapshot_files(workspace)

    env = build_env(cond_name)
    session_id = str(uuid.uuid4())
    timeout_s = args.timeout or task["timeout_s"]

    turn_metrics: list[dict] = []
    cell_start = time.time()
    aborted = False

    for i, turn_text in enumerate(task["turns"]):
        prompt = turn_text
        if i == 0:
            prompt = cond.get("preamble", "") + turn_text + common_suffix
        stream_path = logs / f"turn{i + 1}.stream.jsonl"
        stderr_path = logs / f"turn{i + 1}.stderr.txt"
        (logs / f"turn{i + 1}.prompt.txt").write_text(prompt)

        cmd = claude_command(prompt, args, session_id, first_turn=(i == 0))
        (logs / f"turn{i + 1}.cmd.txt").write_text(
            "\n".join(cmd[:2] + ["<prompt>"] + cmd[3:])
        )

        log(
            f"  turn {i + 1}/{len(task['turns'])} "
            f"[{task['id']} | {cond_name} | rep{rep}] timeout {timeout_s}s"
        )
        t0 = time.time()
        timed_out = False
        with open(stream_path, "w") as out, open(stderr_path, "w") as err:
            proc = subprocess.Popen(
                cmd,
                cwd=str(workspace),
                env=env,
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                returncode = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(os.getpgid(proc.pid), 15)
                    proc.wait(timeout=30)
                except Exception:
                    try:
                        os.killpg(os.getpgid(proc.pid), 9)
                    except Exception:
                        pass
                returncode = -1
        wall = time.time() - t0

        tm = parse_stream(stream_path)
        tm.update(detect_escapes(stream_path, workspace))
        tm.update(
            {
                "turn_index": i + 1,
                "wall_s": round(wall, 2),
                "returncode": returncode,
                "timed_out": timed_out,
                "prompt_chars": len(prompt),
            }
        )
        turn_metrics.append(tm)

        status = "ok"
        if timed_out:
            status = "TIMEOUT"
        elif returncode != 0 or tm["is_error"]:
            status = f"ERROR (rc={returncode}, {tm['subtype']}/{tm['terminal_reason']})"
        log(
            f"    -> {status} in {wall:.0f}s, "
            f"{tm['num_turns']} model turns, "
            f"{tm['n_tool_calls']} tool calls, "
            f"{tm['billable_tokens_total']:,} tokens, "
            f"${tm['total_cost_usd'] or 0:.3f}"
        )

        if timed_out or returncode != 0:
            aborted = True
            break

    cell_wall = time.time() - cell_start
    after = snapshot_files(workspace)
    try:
        git_status = run_cmd(
            ["git", "-C", str(workspace), "status", "--porcelain"]
        ).stdout
    except RuntimeError:
        git_status = ""
    (cell_dir / "git_status.txt").write_text(git_status)

    metrics = {
        "task_id": task["id"],
        "task_title": task["title"],
        "condition": cond_name,
        "condition_label": cond.get("label", cond_name),
        "branch": cond["branch"],
        "rev": pin_rev or git_rev(cond["branch"]),
        "rep": rep,
        "model": args.model,
        "effort": args.effort,
        "tools": args.tools,
        "session_id": session_id,
        "started_at": datetime.fromtimestamp(cell_start, timezone.utc).isoformat(),
        "wall_s": round(cell_wall, 2),
        "workspace_run_path": str(workspace),
        "aborted": aborted,
        "timed_out": any(t["timed_out"] for t in turn_metrics),
        "timeout_s": timeout_s,
        "turns": turn_metrics,
        "totals": sum_turn_metrics(turn_metrics),
        "artifacts": artifact_summary(before, after, workspace),
    }
    metrics["ok"] = (
        not aborted
        and not metrics["totals"]["errors"]
        and len(turn_metrics) == len(task["turns"])
    )

    escaped = metrics["totals"]["n_paths_into_benchmarked_repo_existing"]
    if escaped:
        log(
            f"  WARNING: {escaped} path(s) referenced inside the benchmarked repo "
            f"({REPO_DIR}) — this cell's artifacts may be outside its workspace"
        )

    # Safe to relocate now: the agent has exited, so no path it resolved is live.
    if args.no_keep_workspace:
        shutil.rmtree(workspace, ignore_errors=True)
        metrics["workspace_kept"] = False
    else:
        final_ws = cell_dir / "workspace"
        if final_ws.exists():
            shutil.rmtree(final_ws, ignore_errors=True)
        shutil.move(str(workspace), str(final_ws))
        metrics["workspace_kept"] = True

    (cell_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    return metrics


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #


def split_selector(selector: str, available: dict) -> list[str]:
    if selector in ("", "all"):
        return list(available)
    names = [s.strip() for s in selector.split(",") if s.strip()]
    for n in names:
        if n not in available:
            die(f"unknown condition '{n}'. Available: {', '.join(available)}")
    return names


def cmd_list(args: argparse.Namespace) -> None:
    conditions, suffix = load_conditions()
    print("Conditions:")
    for name, c in conditions.items():
        print(f"  {name:10s} branch={c['branch']:20s} {c.get('label', '')}")
    print("\nTasks:")
    for t in load_tasks("all"):
        print(
            f"  {t['id']:34s} turns={len(t['turns'])} "
            f"timeout={t['timeout_s']}s  {t['title']}"
        )
    print(f"\nCommon suffix appended to turn 1:\n{suffix.strip()}")


def cmd_doctor(args: argparse.Namespace) -> None:
    ok = True
    print("== prerequisites ==")
    for tool in ("git", "uv", args.claude_bin):
        path = shutil.which(tool)
        marker = "ok  " if path else "MISS"
        if tool == "uv" and not path:
            marker = "warn"  # falls back to venv+pip
        elif not path:
            ok = False
        print(f"  [{marker}] {tool:12s} {path or '(not found)'}")

    print("\n== branches ==")
    conditions, _ = load_conditions()
    for name, c in conditions.items():
        try:
            rev = git_rev(c["branch"])
            print(f"  [ok  ] {name:10s} {c['branch']} @ {rev[:10]}")
        except RuntimeError as exc:
            ok = False
            print(f"  [FAIL] {name:10s} {c['branch']}: {exc.splitlines()[0]}")

    print("\n== environments ==")
    for name in conditions:
        py = venv_python(name)
        if not py.exists():
            print(f"  [warn] {name:10s} venv missing — run `runner.py setup`")
            continue
        try:
            ver = run_cmd(
                [str(py), "-c", "import epydemix; print(epydemix.__version__)"]
            ).stdout.strip()
        except RuntimeError:
            ver = "import failed"
        cli = (VENV_DIR / name / "bin" / "epydemix").exists()
        print(f"  [ok  ] {name:10s} epydemix={ver:10s} cli={'yes' if cli else 'no'}")

    print("\n== tasks ==")
    for t in load_tasks("all"):
        print(f"  [ok  ] {t['id']:34s} {len(t['turns'])} turn(s)")

    if args.skip_smoke:
        print("\n(skipping claude smoke test)")
        raise SystemExit(0 if ok else 1)

    print(f"\n== claude smoke test (model={args.model}) ==")
    tmp = CACHE_DIR / "smoke"
    tmp.mkdir(parents=True, exist_ok=True)
    sid = str(uuid.uuid4())
    ns = argparse.Namespace(
        claude_bin=args.claude_bin,
        model=args.model,
        tools=args.tools,
        effort=args.effort,
        no_safe_mode=args.no_safe_mode,
    )
    cmd = claude_command(
        "Reply with exactly the word OK. Do not use any tools.", ns, sid, True
    )
    proc = subprocess.run(
        cmd, cwd=str(tmp), capture_output=True, text=True, timeout=180
    )
    last = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    result = json.loads(last[-1]) if last else {}
    if result.get("type") == "result" and not result.get("is_error"):
        print(f"  [ok  ] model responded: {result.get('result', '')[:60]!r}")
        print(f"         cost=${result.get('total_cost_usd', 0):.4f} "
              f"models={list((result.get('modelUsage') or {}))}")
    else:
        ok = False
        print(f"  [FAIL] rc={proc.returncode}")
        print(f"         {result.get('result') or proc.stderr[:500]}")
        print("         Check --model (try 'sonnet' or 'claude-sonnet-5') and auth.")
    raise SystemExit(0 if ok else 1)


def assert_workspace_root_is_isolated(root: Path) -> None:
    """Refuse to run agents anywhere a `git` walk could reach an enclosing repo."""
    root.mkdir(parents=True, exist_ok=True)
    for parent in [root, *root.parents]:
        if (parent / ".git").exists():
            die(
                f"workspace root {root} is inside the git repository at {parent}.\n"
                "Agents resolve 'the repository root' with `git rev-parse "
                "--show-toplevel` and would escape into it. Pass "
                "--workspace-root <path outside any repo>."
            )


def cmd_run(args: argparse.Namespace) -> None:
    conditions, common_suffix = load_conditions()
    assert_workspace_root_is_isolated(Path(args.workspace_root))
    cond_names = split_selector(args.conditions, conditions)
    tasks = load_tasks(args.tasks)

    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RESULTS_DIR / run_id

    plan = [
        (t, c, r)
        for t in tasks
        for r in range(1, args.reps + 1)
        for c in cond_names  # conditions interleaved so both see similar API load
    ]

    meta = {
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "effort": args.effort,
        "tools": args.tools,
        "safe_mode": not args.no_safe_mode,
        "reps": args.reps,
        "conditions": {n: conditions[n] for n in cond_names},
        "revs": {n: git_rev(conditions[n]["branch"]) for n in cond_names},
        "tasks": [
            {"id": t["id"], "title": t["title"], "n_turns": len(t["turns"])}
            for t in tasks
        ],
        "common_suffix": common_suffix,
        "claude_version": subprocess.run(
            [args.claude_bin, "--version"], capture_output=True, text=True
        ).stdout.strip(),
        "n_cells": len(plan),
    }

    print(f"\nrun_id: {run_id}")
    print(f"cells:  {len(plan)}  ({len(tasks)} tasks x {len(cond_names)} conditions x {args.reps} reps)")
    for t, c, r in plan:
        print(f"  - {t['id']} | {c} | rep{r}")
    if args.dry_run:
        print("\n(dry run, nothing executed)")
        return

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps(meta, indent=2))

    for name in cond_names:
        setup_condition(name, conditions[name], force=False)

    results = []
    for idx, (task, cond_name, rep) in enumerate(plan, 1):
        log(f"[{idx}/{len(plan)}] {task['id']} | {cond_name} | rep{rep}")
        try:
            m = run_cell(
                task, cond_name, conditions[cond_name], rep, common_suffix, args, run_dir
            )
        except Exception as exc:  # keep the matrix going on infrastructure errors
            log(f"  CELL FAILED: {exc}")
            m = {
                "task_id": task["id"],
                "task_title": task["title"],
                "condition": cond_name,
                "branch": conditions[cond_name]["branch"],
                "rep": rep,
                "ok": False,
                "timed_out": False,
                "wall_s": 0,
                "harness_error": str(exc),
                "totals": sum_turn_metrics([]),
                "artifacts": {},
            }
            cell_dir = run_dir / task["id"] / cond_name / f"rep{rep:02d}"
            cell_dir.mkdir(parents=True, exist_ok=True)
            (cell_dir / "metrics.json").write_text(json.dumps(m, indent=2))
        results.append(m)
        with (run_dir / "results.jsonl").open("a") as fh:
            fh.write(json.dumps(m) + "\n")

    log(f"run complete: {run_dir}")
    write_report(run_dir)


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #

CSV_COLUMNS = [
    "run_id",
    "task_id",
    "condition",
    "branch",
    "rep",
    "ok",
    "timed_out",
    "wall_s",
    "api_s",
    "model_turns",
    "prompt_turns",
    "tool_calls",
    "tool_errors",
    "bash_calls",
    "assistant_msgs",
    "thinking_blocks",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "billable_tokens",
    "cost_usd",
    "new_files",
    "figures",
    "new_bytes",
    "paths_outside_ws",
    "paths_into_repo",
    "escapes",
]


def cell_row(run_id: str, m: dict) -> dict:
    tot = m.get("totals", {})
    art = m.get("artifacts", {})
    return {
        "run_id": run_id,
        "task_id": m.get("task_id"),
        "condition": m.get("condition"),
        "branch": m.get("branch"),
        "rep": m.get("rep"),
        "ok": m.get("ok"),
        "timed_out": m.get("timed_out"),
        "wall_s": round(m.get("wall_s", 0) or 0, 1),
        "api_s": round((tot.get("duration_api_ms") or 0) / 1000, 1),
        "model_turns": tot.get("num_turns"),
        "prompt_turns": tot.get("n_prompt_turns"),
        "tool_calls": tot.get("n_tool_calls"),
        "tool_errors": tot.get("n_tool_errors"),
        "bash_calls": tot.get("n_bash_calls"),
        "assistant_msgs": tot.get("n_assistant_messages"),
        "thinking_blocks": tot.get("n_thinking_blocks"),
        "input_tokens": tot.get("input_tokens_total"),
        "output_tokens": tot.get("output_tokens_total"),
        "cache_read_tokens": tot.get("cache_read_total"),
        "cache_creation_tokens": tot.get("cache_creation_total"),
        "billable_tokens": tot.get("billable_tokens_total"),
        "cost_usd": round(tot.get("total_cost_usd") or 0, 4),
        "new_files": art.get("n_new_files"),
        "figures": art.get("n_figures"),
        "new_bytes": art.get("new_bytes"),
        "paths_outside_ws": tot.get("n_paths_outside_workspace"),
        "paths_into_repo": tot.get("n_paths_into_benchmarked_repo"),
        "escapes": tot.get("n_paths_into_benchmarked_repo_existing"),
    }


def collect_cells(run_dir: Path) -> list[dict]:
    cells = []
    for path in sorted(run_dir.glob("*/*/rep*/metrics.json")):
        cells.append(json.loads(path.read_text()))
    return cells


def _fmt(value: Any, decimals: int = 1) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        if decimals == 1 and value == int(value):
            return f"{int(value):,}"
        return f"{value:,.{decimals}f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _agg(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def write_report(run_dir: Path) -> None:
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_id = run_meta["run_id"]
    cells = collect_cells(run_dir)
    if not cells:
        log("no cells to report")
        return

    rows = [cell_row(run_id, m) for m in cells]
    csv_path = run_dir / "results.csv"
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    conditions = list(run_meta["conditions"])
    tasks = [t["id"] for t in run_meta["tasks"]]

    def pick(task_id: str, cond: str, field: str) -> list[float]:
        return [
            r[field]
            for r in rows
            if r["task_id"] == task_id and r["condition"] == cond and r[field] is not None
        ]

    lines: list[str] = []
    lines.append(f"# epydemix agent benchmark — run `{run_id}`\n")
    lines.append(
        f"- model: `{run_meta['model']}` · effort: `{run_meta['effort']}` · "
        f"safe-mode: `{run_meta['safe_mode']}` · reps: {run_meta['reps']}"
    )
    lines.append(f"- tools: `{run_meta['tools']}`")
    lines.append(f"- claude: `{run_meta['claude_version']}`")
    for name, rev in run_meta["revs"].items():
        lines.append(
            f"- **{name}**: `{run_meta['conditions'][name]['branch']}` @ `{rev[:10]}` "
            f"— {run_meta['conditions'][name].get('label', '')}"
        )
    lines.append("")

    headline = [
        ("wall_s", "wall (s)"),
        ("model_turns", "turns"),
        ("tool_calls", "tool calls"),
        ("billable_tokens", "tokens"),
        ("cost_usd", "cost ($)"),
        ("tool_errors", "tool errs"),
        ("figures", "figs"),
    ]

    lines.append("## Per-task medians\n")
    for task_id in tasks:
        task_rows = [r for r in rows if r["task_id"] == task_id]
        if not task_rows:
            continue
        lines.append(f"### {task_id}\n")
        header = "| metric | " + " | ".join(conditions)
        if len(conditions) == 2:
            header += " | Δ (framework/baseline) |"
        else:
            header += " |"
        lines.append(header)
        lines.append("|---" * (len(conditions) + (2 if len(conditions) == 2 else 1)) + "|")
        for field, label in headline:
            dec = 3 if field == "cost_usd" else 1
            vals = {c: _agg(pick(task_id, c, field)) for c in conditions}
            row = f"| {label} | " + " | ".join(_fmt(vals[c], dec) for c in conditions)
            if len(conditions) == 2:
                a, b = conditions[0], conditions[1]
                if vals[a] and vals[b]:
                    row += f" | {vals[b] / vals[a]:.2f}x |"
                else:
                    row += " | - |"
            else:
                row += " |"
            lines.append(row)
        ok_counts = {
            c: (
                sum(1 for r in task_rows if r["condition"] == c and r["ok"]),
                sum(1 for r in task_rows if r["condition"] == c),
            )
            for c in conditions
        }
        row = "| completed | " + " | ".join(
            f"{ok_counts[c][0]}/{ok_counts[c][1]}" for c in conditions
        )
        lines.append(row + (" | - |" if len(conditions) == 2 else " |"))
        lines.append("")

    lines.append("## Overall (median across all cells)\n")
    lines.append("| metric | " + " | ".join(conditions) + " |")
    lines.append("|---" * (len(conditions) + 1) + "|")
    for field, label in headline:
        dec = 3 if field == "cost_usd" else 1
        vals = [
            _agg([r[field] for r in rows if r["condition"] == c and r[field] is not None])
            for c in conditions
        ]
        lines.append(f"| {label} | " + " | ".join(_fmt(v, dec) for v in vals) + " |")
    totals = [
        sum(r["cost_usd"] or 0 for r in rows if r["condition"] == c) for c in conditions
    ]
    lines.append(
        "| total spend ($) | " + " | ".join(f"{t:,.2f}" for t in totals) + " |"
    )
    lines.append("")
    lines.append(f"Raw per-cell data: `{csv_path.name}`. "
                 f"Full transcripts under `<task>/<condition>/rep*/logs/`.\n")

    leaked = [r for r in rows if (r["escapes"] or 0) > 0]
    if leaked:
        lines.append("## ⚠ Workspace escapes\n")
        lines.append(
            "These cells touched paths inside the benchmarked repository that still "
            "exist on disk, rather than working in their own workspace. Their "
            "artifact counts are unreliable and the cells should be re-run.\n"
        )
        for r in leaked:
            lines.append(
                f"- `{r['task_id']}` / `{r['condition']}` / rep{r['rep']} — "
                f"{r['escapes']} path(s) into the repo under test"
            )
        lines.append("")

    failures = [r for r in rows if not r["ok"]]
    if failures:
        lines.append("## Incomplete cells\n")
        for r in failures:
            lines.append(
                f"- `{r['task_id']}` / `{r['condition']}` / rep{r['rep']} — "
                f"{'timeout' if r['timed_out'] else 'error'} after {r['wall_s']}s"
            )
        lines.append("")

    md_path = run_dir / "report.md"
    md_path.write_text("\n".join(lines))
    log(f"wrote {csv_path}")
    log(f"wrote {md_path}")
    print("\n" + "\n".join(lines))


def cmd_rerun(args: argparse.Namespace) -> None:
    """Re-execute the cells of a finished run that did not complete.

    Replacements are only poolable with the surviving replicates if they are
    produced under the same conditions, so model, effort, tool allowlist and
    branch revision are all taken from the original run.json rather than from
    the current CLI defaults.
    """
    run_dir = RESULTS_DIR / args.run_id if args.run_id else latest_run_dir()
    if not run_dir or not run_dir.exists():
        die("no run found; pass --run-id")
    meta = json.loads((run_dir / "run.json").read_text())

    failed = []
    for path in sorted(run_dir.glob("*/*/rep*/metrics.json")):
        m = json.loads(path.read_text())
        if not m.get("ok"):
            failed.append(m)
    if not failed:
        log("no incomplete cells; nothing to re-run")
        return

    # Pin everything that could otherwise drift between the two runs.
    pinned = argparse.Namespace(
        claude_bin=args.claude_bin,
        model=meta["model"],
        effort=meta["effort"],
        tools=meta["tools"],
        no_safe_mode=not meta.get("safe_mode", True),
        timeout=args.timeout,
        workspace_root=args.workspace_root,
        no_keep_workspace=False,
    )
    assert_workspace_root_is_isolated(Path(pinned.workspace_root))
    conditions = meta["conditions"]
    common_suffix = meta.get("common_suffix", "")
    tasks = {t["id"]: t for t in load_tasks("all")}

    print(f"\nre-running {len(failed)} incomplete cell(s) in {run_dir.name}")
    print(f"pinned: model={pinned.model} effort={pinned.effort} tools={pinned.tools}")
    for m in failed:
        print(f"  - {m['task_id']} | {m['condition']} | rep{m['rep']}")
    if args.dry_run:
        print("\n(dry run, nothing executed)")
        return

    for m in failed:
        task_id, cond_name, rep = m["task_id"], m["condition"], m["rep"]
        task = tasks.get(task_id)
        if task is None:
            log(f"  SKIP {task_id}: task file no longer exists")
            continue

        # The prompt actually sent is on disk; if the task file has been edited
        # since, a replacement would not be comparable.
        stored = run_dir / task_id / cond_name / f"rep{rep:02d}" / "logs" / "turn1.prompt.txt"
        if stored.exists():
            expected = (
                conditions[cond_name].get("preamble", "") + task["turns"][0] + common_suffix
            )
            if stored.read_text() != expected:
                log(
                    f"  WARNING {task_id}/{cond_name}: task text changed since the "
                    "original run — the replacement is not comparable to the others"
                )
                if not args.force:
                    log("           skipping (pass --force to run anyway)")
                    continue

        log(f"re-running {task_id} | {cond_name} | rep{rep}")
        setup_condition(cond_name, conditions[cond_name], force=False)
        try:
            run_cell(
                task,
                cond_name,
                conditions[cond_name],
                rep,
                common_suffix,
                pinned,
                run_dir,
                pin_rev=meta["revs"].get(cond_name),
            )
        except Exception as exc:
            log(f"  CELL FAILED AGAIN: {exc}")

    write_report(run_dir)


def cmd_rescan(args: argparse.Namespace) -> None:
    """Re-derive artifact and escape metrics for a completed run.

    Both are computed from data the harness keeps — the stream transcripts and
    the retained workspaces — so a run can be corrected after a metric bug is
    fixed without paying for the agents again.
    """
    run_dir = RESULTS_DIR / args.run_id if args.run_id else latest_run_dir()
    if not run_dir or not run_dir.exists():
        die("no run found; pass --run-id")

    pristine: dict[str, dict[str, int]] = {}
    tmp_root = CACHE_DIR / "rescan"
    updated = skipped = 0

    for path in sorted(run_dir.glob("*/*/rep*/metrics.json")):
        m = json.loads(path.read_text())
        cell_dir = path.parent
        workspace = cell_dir / "workspace"
        run_path = m.get("workspace_run_path", str(workspace))

        turns = m.get("turns") or []
        for t in turns:
            stream = cell_dir / "logs" / f"turn{t.get('turn_index', 1)}.stream.jsonl"
            t.update(detect_escapes(stream, Path(run_path)))
        m["totals"] = sum_turn_metrics(turns)

        if workspace.exists():
            branch = m.get("branch")
            if branch not in pristine:
                dest = tmp_root / str(branch).replace("/", "_")
                export_branch(branch, dest)
                pristine[branch] = snapshot_files(dest)
            m["artifacts"] = artifact_summary(
                pristine[branch], snapshot_files(workspace), workspace
            )
            updated += 1
        else:
            skipped += 1

        path.write_text(json.dumps(m, indent=2))

    if tmp_root.exists():
        shutil.rmtree(tmp_root, ignore_errors=True)
    log(f"rescanned {updated} cell(s); {skipped} had no retained workspace")
    write_report(run_dir)


def cmd_report(args: argparse.Namespace) -> None:
    run_dir = RESULTS_DIR / args.run_id if args.run_id else latest_run_dir()
    if not run_dir or not run_dir.exists():
        die("no run found; pass --run-id")
    write_report(run_dir)


def latest_run_dir() -> Path | None:
    if not RESULTS_DIR.exists():
        return None
    runs = sorted(p for p in RESULTS_DIR.iterdir() if (p / "run.json").exists())
    return runs[-1] if runs else None


def cmd_clean(args: argparse.Namespace) -> None:
    targets = [CHECKOUT_DIR, VENV_DIR]
    if args.results:
        targets.append(RESULTS_DIR)
    for t in targets:
        if t.exists():
            log(f"removing {t}")
            shutil.rmtree(t)
    log("clean complete")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--claude-bin", default=os.environ.get("CLAUDE_BIN", "claude"))
    p.add_argument("--model", default=DEFAULT_MODEL, help="e.g. claude-sonnet-5, sonnet, opus")
    p.add_argument("--effort", default=DEFAULT_EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--tools", default=DEFAULT_TOOLS, help="comma-separated built-in tool allowlist")
    p.add_argument("--no-safe-mode", action="store_true",
                   help="do NOT pass --safe-mode (lets local CLAUDE.md/skills/plugins leak in)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("doctor", help="check prerequisites and smoke-test the claude invocation")
    s.add_argument("--skip-smoke", action="store_true")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("setup", help="build per-condition checkouts and venvs")
    s.add_argument("--conditions", default="all")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("list", help="list tasks and conditions")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("run", help="execute the benchmark matrix")
    s.add_argument("--conditions", default="all")
    s.add_argument("--tasks", default="all", help="comma-separated task ids or prefixes")
    s.add_argument("--reps", type=int, default=1)
    s.add_argument("--run-id", default=None)
    s.add_argument("--timeout", type=int, default=None, help="override per-task timeout (s)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-keep-workspace", action="store_true",
                   help="delete each workspace after the cell (keeps logs)")
    s.add_argument("--workspace-root", default=str(DEFAULT_WORKSPACE_ROOT),
                   help="where agents run; MUST NOT be inside a git repository "
                        f"(default: {DEFAULT_WORKSPACE_ROOT})")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("report", help="aggregate a run into CSV + markdown")
    s.add_argument("--run-id", default=None)
    s.set_defaults(func=cmd_report)

    s = sub.add_parser(
        "rerun",
        help="re-execute the incomplete cells of a finished run, pinned to that "
             "run's model/effort/tools/revision",
    )
    s.add_argument("--run-id", default=None)
    s.add_argument("--timeout", type=int, default=None)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force", action="store_true",
                   help="re-run even if the task text changed since the original run")
    s.add_argument("--workspace-root", default=str(DEFAULT_WORKSPACE_ROOT))
    s.set_defaults(func=cmd_rerun)

    s = sub.add_parser(
        "rescan",
        help="recompute artifact/escape metrics for a finished run from its "
             "transcripts and retained workspaces, then re-report",
    )
    s.add_argument("--run-id", default=None)
    s.set_defaults(func=cmd_rescan)

    s = sub.add_parser("clean", help="remove cached checkouts and venvs")
    s.add_argument("--results", action="store_true", help="also delete results/")
    s.set_defaults(func=cmd_clean)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
