# epydemix agent benchmark

Compares two ways of driving epydemix with a coding agent, on the same set of modeling tasks:

| condition   | branch           | what the agent gets |
|-------------|------------------|---------------------|
| `baseline`  | `main`           | the library, `docs/`, `tutorials/`, `README.md` |
| `framework` | `agent-framework`| `AGENT.md` contract + `epydemix` CLI + bundles/manifests/provenance |

Measured per task: wall-clock time, model turns, tool calls, token usage (input / output / cache),
cost, tool-error count, and an inventory of the files the agent produced.

## Quickstart

```bash
python3 bench/runner.py doctor              # prerequisites + a real 1-token claude smoke test
python3 bench/runner.py setup               # build per-condition checkouts and venvs
python3 bench/runner.py run --tasks t01 --reps 1
python3 bench/runner.py report              # re-aggregate the latest run
```

`doctor` is worth running first: it verifies the exact `claude` invocation the harness uses,
including the model name. `--model sonnet-5` is **not** valid (404); use `claude-sonnet-5` or `sonnet`.

## How a cell is isolated

One *cell* = (task, condition, replicate). For each cell the harness:

1. Extracts a pristine copy of the condition's branch with `git archive` into a workspace under
   `--workspace-root` (default `~/.cache/epydemix-bench-workspaces/`), **outside the benchmarked
   repository**, then `git init`s it and makes one commit.
   `git archive` (not `git worktree`) means **only tracked files** are copied — old transcripts,
   scratch output directories and local `.claude/` config can never leak into an agent's workspace,
   and nothing is registered in your repository.

   The location and the `git init` both matter. Agents asked to work "at the repository root"
   routinely run `git rev-parse --show-toplevel` as their first move; a workspace nested inside the
   real repo with no `.git` of its own resolves to **the real repo**, and the agent then does the
   entire task in your tree. Three layers guard against this: the workspace is a self-contained git
   repo so `--show-toplevel` resolves to itself; the run refuses to start if `--workspace-root` sits
   inside any git repository; and every absolute path in every tool input is checked against the
   workspace, with escapes counted per cell (`paths_into_repo` in the CSV) and flagged loudly in the
   run log and report. The workspace is moved into
   `results/<run_id>/<task>/<condition>/rep<N>/workspace/` only after the agent has exited.
2. Points `PATH`/`VIRTUAL_ENV` at that condition's venv (`bench/.cache/venvs/<condition>`), built
   from that branch. `baseline` gets `epydemix` with no CLI; `framework` gets `epydemix[agent]` with
   the `epydemix` console script. Both get the same third-party toolbelt (`pyarrow`, `click`,
   `pyyaml`) so the environments differ only by branch code.
3. Sets `MPLBACKEND=Agg` so plotting never blocks on a GUI.
4. Runs `claude -p` with `cwd` = the workspace, one invocation per prompt turn.

## The `claude` invocation

```
claude -p "<prompt>" \
  --model claude-sonnet-5 \
  --output-format stream-json --verbose \
  --permission-mode bypassPermissions \
  --tools Bash,Read,Write,Edit,Glob,Grep,BashOutput,KillShell \
  --effort medium \
  --safe-mode --strict-mcp-config --disable-slash-commands \
  --session-id <uuid>          # first turn; --resume <uuid> for later turns
```

- `--safe-mode` disables `CLAUDE.md` discovery, skills, plugins, hooks, custom agents, output styles
  and MCP servers, so neither arm inherits your local setup. Auth and model selection still work.
  Pass `--no-safe-mode` to the harness to turn this off.
- The tool allowlist deliberately excludes subagents (`Task`), `TodoWrite`, workflows, and web
  access — no planning or orchestration features, per the benchmark design. `Bash` covers CLI use and
  running custom Python; `Write`/`Edit` cover authoring scripts and configs.
- Multi-turn tasks reuse one session via `--session-id` / `--resume`, so follow-up turns keep context.

## Prompt construction

Task files hold the **framework-neutral** task text. Each condition prepends its own preamble
(`conditions.json`), and every task's first turn gets a shared suffix telling the agent to work in
`outputs/`. So the only prompt difference between arms is the orientation preamble:

- `baseline`: "…library source is in `epydemix/`, docs in `docs/`, examples in `tutorials/`…"
- `framework`: "Read AGENT.md at the repo root…"

Task text was derived from the transcripts in `epydemix_agent_test*.txt` with framework-specific
vocabulary removed ("bundle", "manifest", "config inheritance", literal `epydemix inspect` commands),
so neither arm is handed the other's affordances. `epydemix_agent_test3.txt` was **not** turned into a
task: its prompt spells out framework CLI invocations verbatim, which the baseline arm cannot satisfy.
`t03` uses `epydemix_agent_test4.txt`, the framework-neutral version of the same scenario.

## Tasks

| id | turns | what it exercises | derived from |
|----|-------|-------------------|--------------|
| `t01_seirhd_scenarios` | 1 | custom 6-compartment model, 3 scenarios, interventions, 3 figures | test1 |
| `t02_sir_calibration` | 1 | ABC-SMC calibration, posterior quality, 2-parameter sensitivity | test2 |
| `t03_calibration_projection` | 1 | calibrate → project → compare scenarios → 2-panel figure | test4 |
| `t04_school_closure_ny` | 1 | age-structured NY, contact layers, peak tuning, timing sweep | user-specified |
| `t05_measles_vermont` | 1 | age-structured VT, coverage sweep, herd-immunity threshold, targeting | user-specified |

Add a task by dropping a markdown file in `tasks/` with frontmatter (`id`, `title`, `timeout_s`,
`tags`) and splitting conversational turns with a line containing `<<<TURN>>>`.

## Output layout

```
results/<run_id>/
  run.json                  # model, effort, tools, branch revs, claude version
  results.jsonl             # one line per cell, appended live
  results.csv               # flat table, one row per cell
  report.md                 # per-task medians + framework/baseline ratios
  <task>/<condition>/rep<N>/
    metrics.json            # per-turn and aggregated metrics, artifacts, escapes
    git_status.txt          # what the agent changed, vs. the pristine branch
    workspace/              # the agent's working tree, including outputs/
    logs/
      turn1.prompt.txt      # exact prompt sent
      turn1.cmd.txt         # exact command line
      turn1.stream.jsonl    # full stream-json transcript
      turn1.stderr.txt
```

Token accounting prefers the `modelUsage` block of the final `result` event (authoritative per-model
totals) over the top-level `usage` block. `billable_tokens` = input + output + cache-read +
cache-creation.

## Running the full matrix

```bash
python3 bench/runner.py run --reps 3                       # everything, 3 replicates
python3 bench/runner.py run --tasks t01,t03 --conditions framework
python3 bench/runner.py run --dry-run                      # print the plan only
python3 bench/runner.py --model sonnet --effort low run    # sweep model/effort
```

Cells run sequentially and conditions are interleaved within each replicate, so both arms meet
similar API load and rate-limit conditions. Replicates matter: agent runs are high-variance, and a
single cell per arm will not separate the conditions.

## Caveats

- **Wall-clock is not pure agent speed.** It includes API queueing and, in these tasks, real
  simulation time. Compare `wall_s` alongside `api_s` (`duration_api_ms`) and turn counts.
- **Network dependency.** `t04` and `t05` load their populations (New York State, Vermont) from the
  `epydemix-data` GitHub repo at runtime; epydemix has no local cache, so download time and flakiness
  enter both arms.
- **Quota.** A full matrix is many long agent sessions. Check your five-hour limit before starting;
  `report` works on partial runs, so an interrupted matrix is still usable.
- **Correctness is not scored.** The harness measures effort, not whether the science is right.
  `workspace/outputs/` and the final assistant message in each transcript are kept so you can grade
  quality separately.
- **Check `paths_into_repo` before trusting a run.** A non-zero value means that cell worked partly
  outside its sandbox; its artifact counts are meaningless and it should be re-run.

## Housekeeping

```bash
python3 bench/runner.py clean               # drop cached checkouts + venvs
python3 bench/runner.py clean --results     # also drop results/
```

`bench/.cache/` and `bench/results/` are gitignored.
