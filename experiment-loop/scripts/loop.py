#!/usr/bin/env python3
"""Drive a Kitaru experiment loop: one baseline run, then one change per iteration
until a configuration is all green in N consecutive runs, or a cap is reached.

Every command reads experiment-loop.json (or --config PATH) from the project root
and writes evidence under the configured evidence directory. Commands:

  init        write a config file from explicit values (no server writes)
  preflight   check server, worker, agent version, cohort, evaluators (read-only)
  worker start|stop|status
  start       create (idempotently) and start one run of an experiment
  watch       poll one run until it settles
  collect     pair a settled run's replays with their evaluations; save evidence
  next        say what the loop rule requires next, from saved evidence
  report      write results.md with the version table and a drift check

Every Kitaru call is made with KITARU_ACTIVE_SKILL=experiment-loop unless the
caller already set it. Creating an experiment and starting a run are paid,
non-idempotent remote writes; `start` uses idempotency keys so a crashed
invocation can be re-run safely.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

os.environ.setdefault("KITARU_ACTIVE_SKILL", "experiment-loop")
os.environ["KITARU_NON_INTERACTIVE"] = "1"

CONFIG_NAME = "experiment-loop.json"


# ----------------------------------------------------------------------------- helpers

def load_config(path: str | None) -> dict:
    p = pathlib.Path(path or CONFIG_NAME)
    if not p.is_file():
        raise SystemExit(f"No config at {p}. Run `loop.py init ...` first.")
    cfg = json.loads(p.read_text())
    cfg["_path"] = str(p.resolve())
    cfg["_root"] = str(p.resolve().parent)
    for key in ("agent_version", "cohort_version_id", "evaluators", "tool_policy"):
        if key not in cfg:
            raise SystemExit(f"Config is missing '{key}'.")
    cfg.setdefault("kitaru", "kitaru")
    cfg.setdefault("cap", 6)
    cfg.setdefault("required_consecutive_greens", 2)
    cfg.setdefault("allowed_models", [])
    cfg.setdefault("evidence_dir", "sessions/experiment-loop")
    cfg.setdefault("case_id_field", "case_id")
    cfg.setdefault("worker_concurrency", 10)
    return cfg


def kit(cfg: dict, *args: str, check: bool = True) -> dict:
    cmd = [cfg["kitaru"], *args, "-o", "json", "--no-browser"]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cfg["_root"])
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        d = {"ok": False, "raw": r.stdout[-2000:], "stderr": r.stderr[-2000:]}
    if check and not d.get("ok"):
        raise SystemExit(f"kitaru {' '.join(args[:3])} failed: {json.dumps(d)[:1500]}")
    return d


def paged(cfg: dict, *args: str) -> list:
    items, cursor = [], None
    while True:
        a = list(args) + ["--size", "500"]
        if cursor:
            a += ["--cursor", cursor]
        page = kit(cfg, *a)
        items += page["items"]
        cursor = (page.get("page") or {}).get("next_cursor")
        if not cursor:
            return items


def strip_nulls(x):
    if isinstance(x, dict):
        return {k: strip_nulls(v) for k, v in x.items() if v is not None}
    if isinstance(x, list):
        return [strip_nulls(v) for v in x]
    return x


def canon(x) -> str:
    return json.dumps(strip_nulls(x), sort_keys=True, separators=(",", ":"))


def evidence_root(cfg: dict) -> pathlib.Path:
    p = pathlib.Path(cfg["_root"]) / cfg["evidence_dir"]
    p.mkdir(parents=True, exist_ok=True)
    return p


def agent_name(cfg: dict) -> str:
    return cfg["agent_version"].split("@")[0]


def experiment_name(cfg: dict, which: str) -> str:
    """`baseline` -> <agent>-baseline; `iteration-N` or an integer -> <agent>-iteration-N."""
    if which == "baseline":
        return f"{agent_name(cfg)}-baseline"
    m = re.fullmatch(r"(?:iteration-)?(\d+)", which)
    if not m:
        raise SystemExit("--experiment must be 'baseline', 'iteration-N' or N")
    return f"{agent_name(cfg)}-iteration-{int(m.group(1))}"


def runspec_hash(cfg: dict) -> tuple[str, dict]:
    v = kit(cfg, "agent", "version", "get", cfg["agent_version"])["item"]
    public = {"run_spec": v["run_spec"], "capabilities": v["capabilities"]}
    return hashlib.sha256(canon(public).encode()).hexdigest(), v


def evaluator_tokens(cfg: dict) -> tuple[list[str], list[str]]:
    refs, params = [], []
    for e in cfg["evaluators"]:
        refs += ["--evaluator", e["ref"]]
        if e.get("params") is not None:
            params += ["--evaluator-params", f"{e['ref']}={json.dumps(e['params'])}"]
    return refs, params


def eval_names(cfg: dict) -> set[tuple[str, int]]:
    out = set()
    for e in cfg["evaluators"]:
        name, _, ver = e["ref"].partition("@")
        out.add((name, int(ver)))
    return out


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------------- init

def cmd_init(a):
    cfg = {
        "kitaru": a.kitaru,
        "agent_version": a.agent_version,
        "cohort_version_id": a.cohort_version,
        "evaluators": [],
        "tool_policy": json.loads(pathlib.Path(a.tool_policy_file).read_text()),
        "allowed_models": a.allowed_model or [],
        "cap": a.cap,
        "required_consecutive_greens": a.greens,
        "evidence_dir": a.evidence_dir,
        "case_id_field": a.case_id_field,
        "prompt_dir": a.prompt_dir,
        "worker_concurrency": 10,
    }
    for ref, pfile in zip(a.evaluator, a.evaluator_params_file or []):
        cfg["evaluators"].append({"ref": ref, "params": json.loads(pathlib.Path(pfile).read_text())})
    for ref in a.evaluator[len(a.evaluator_params_file or []):]:
        cfg["evaluators"].append({"ref": ref})
    out = pathlib.Path(a.config or CONFIG_NAME)
    if out.exists() and not a.force:
        raise SystemExit(f"{out} exists; pass --force to overwrite.")
    out.write_text(json.dumps(cfg, indent=2) + "\n")
    print(json.dumps({"wrote": str(out), "experiment_names": [f"{a.agent_version.split('@')[0]}-baseline", f"{a.agent_version.split('@')[0]}-iteration-N"]}))


# ----------------------------------------------------------------------------- preflight

def cmd_preflight(a):
    cfg = load_config(a.config)
    status = kit(cfg, "status")["item"]
    h, v = runspec_hash(cfg)
    caps = (v.get("run_spec") or {}).get("runtime_capabilities") or {}
    cohort = kit(cfg, "cohort", "version", "get", cfg["cohort_version_id"])["item"]
    problems, notes = [], []
    if status.get("authentication") != "authenticated":
        problems.append("CLI is not authenticated to the selected server")
    if caps.get("overrides") is False:
        problems.append("agent version declares overrides=false: candidates cannot use prompt or model overrides")
    if caps.get("tool_policies") is False and any(t.get("type") != "passthrough" for t in [cfg["tool_policy"].get("default", {})] + list((cfg["tool_policy"].get("tools") or {}).values())):
        problems.append("agent version declares tool_policies=false but the config's tool policy is not all passthrough")
    evals = []
    for e in cfg["evaluators"]:
        name = e["ref"].split("@")[0]
        parent = kit(cfg, "evaluator", "get", name)["item"]
        scope = parent.get("agent_id")
        if scope and scope != v["agent_id"]:
            problems.append(f"evaluator {e['ref']} is scoped to another agent ({scope})")
        if not scope:
            notes.append(f"evaluator {e['ref']} has no agent scope; confirm it is deliberately global before trusting it")
        evals.append({"ref": e["ref"], "evaluator_id": parent["id"], "agent_scope": scope, "has_params": e.get("params") is not None})
    for t, pol in (cfg["tool_policy"].get("tools") or {}).items():
        if pol.get("type") in ("history", "static") and pol.get("on_miss") == "passthrough":
            notes.append(f"tool {t}: a {pol['type']} miss falls through to the live tool; fine for isolated local fixtures, not for external systems")
    if (cfg["tool_policy"].get("default") or {}).get("type") == "passthrough":
        notes.append("default tool policy is passthrough: any tool not named in the policy executes live")
    summary = {
        "checked_at": now(),
        "server_url": status.get("server_url"), "server_version": (status.get("server") or {}).get("version"),
        "authenticated": status.get("authentication") == "authenticated", "live_worker_count": status.get("live_worker_count"),
        "agent_version": cfg["agent_version"], "agent_id": v["agent_id"], "agent_version_id": v["id"],
        "run_command": (v.get("run_spec") or {}).get("command"), "run_env": (v.get("run_spec") or {}).get("env"),
        "runtime_capabilities": caps, "runspec_public_sha256": h,
        "cohort_version_id": cfg["cohort_version_id"], "cohort_session_count": cohort.get("session_count"),
        "evaluators": evals, "allowed_models": cfg["allowed_models"], "cap": cfg["cap"],
        "required_consecutive_greens": cfg["required_consecutive_greens"],
        "problems": problems, "notes": notes,
    }
    (evidence_root(cfg) / "preflight.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    if problems:
        raise SystemExit(2)


# ----------------------------------------------------------------------------- worker

def worker_files(cfg):
    root = evidence_root(cfg)
    return root / "worker.pid", root / "worker.log"


def cmd_worker(a):
    cfg = load_config(a.config)
    pidfile, logfile = worker_files(cfg)
    if a.action == "status":
        live = kit(cfg, "status")["item"].get("live_worker_count")
        pid = int(pidfile.read_text()) if pidfile.exists() else None
        alive = pid is not None and _pid_alive(pid)
        print(json.dumps({"live_worker_count": live, "our_pid": pid, "our_worker_alive": alive, "log": str(logfile)}))
        return
    if a.action == "stop":
        if not pidfile.exists():
            print(json.dumps({"stopped": False, "reason": "no pid file"})); return
        pid = int(pidfile.read_text())
        if _pid_alive(pid):
            os.kill(pid, signal.SIGINT)
            for _ in range(60):
                if not _pid_alive(pid):
                    break
                time.sleep(1)
        pidfile.unlink(missing_ok=True)
        print(json.dumps({"stopped": not _pid_alive(pid), "pid": pid}))
        return
    # start
    if pidfile.exists() and _pid_alive(int(pidfile.read_text())):
        print(json.dumps({"started": False, "reason": "already running", "pid": int(pidfile.read_text())})); return
    _, v = runspec_hash(cfg)
    name = f"experiment-loop-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    cmd = [cfg["kitaru"], "worker", "start", "--name", name, "--concurrency", str(cfg["worker_concurrency"]),
           "--claim", f"agent={v['id']}", "--claim", "evaluator", "--log-level", "info"]
    log = open(logfile, "ab")
    proc = subprocess.Popen(cmd, cwd=cfg["_root"], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    pidfile.write_text(str(proc.pid))
    for _ in range(30):
        time.sleep(1)
        if kit(cfg, "status")["item"].get("live_worker_count"):
            break
    print(json.dumps({"started": True, "pid": proc.pid, "name": name, "live_worker_count": kit(cfg, "status")["item"].get("live_worker_count"), "log": str(logfile)}))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ----------------------------------------------------------------------------- start

def cmd_start(a):
    cfg = load_config(a.config)
    name = experiment_name(cfg, a.experiment)
    d = evidence_root(cfg) / name
    d.mkdir(parents=True, exist_ok=True)
    override = {}
    if a.model:
        if cfg["allowed_models"] and a.model not in cfg["allowed_models"]:
            raise SystemExit(f"model {a.model} is not in allowed_models {cfg['allowed_models']}")
        override["model"] = a.model
    if a.prompt_file:
        override["system_prompt"] = pathlib.Path(a.prompt_file).read_text()
    if name.endswith("-baseline") and override:
        raise SystemExit("the baseline experiment takes no override")
    if not name.endswith("-baseline") and not override:
        raise SystemExit("an iteration needs exactly one change: --model or --prompt-file")
    if a.model and a.prompt_file:
        raise SystemExit("one change per iteration: pass --model or --prompt-file, not both")
    existing_runs = sorted(d.glob("run*_plan.json"))
    run_number = len(existing_runs) + 1
    if name.endswith("-baseline") and run_number > 1 and not a.allow_baseline_repeat:
        raise SystemExit("the baseline runs once; pass --allow-baseline-repeat only if the user asked for it")

    h1, spec1 = runspec_hash(cfg)
    exp = kit(cfg, "experiment", "get", name, check=False)
    created = False
    if exp.get("ok"):
        exp = exp["item"]
    else:
        refs, params = evaluator_tokens(cfg)
        args = ["experiment", "create", name, "--agent", spec1["agent_id"], *refs, *params,
                "--tool-policy", json.dumps(cfg["tool_policy"]), "--idempotency-key", name]
        if override:
            args += ["--override", json.dumps(override)]
        if a.description:
            args += ["--description", a.description]
        exp = kit(cfg, *args)["item"]
        created = True
    exp = kit(cfg, "experiment", "get", exp["id"])["item"]
    (d / "experiment.json").write_text(json.dumps(exp, indent=1))
    problems = []
    stored_override = exp.get("override") or {}
    if override and canon(stored_override) != canon(override):
        problems.append("stored override differs from the requested one (an existing experiment with this name has a different change)")
    if not override and strip_nulls(stored_override):
        problems.append("baseline experiment has an override stored")
    if canon(exp.get("tool_policy")) != canon(cfg["tool_policy"]):
        problems.append("stored tool policy differs from the config")
    h2, _ = runspec_hash(cfg)
    if h1 != h2:
        problems.append("agent run spec changed between read and creation")
    if problems:
        raise SystemExit("CONFIG PROBLEMS: " + "; ".join(problems))
    run = kit(cfg, "experiment", "run", "start", exp["id"], "--cohort-version", cfg["cohort_version_id"],
              "--agent", cfg["agent_version"], "--baseline-evaluation-mode", "if-missing",
              "--idempotency-key", f"{name}-run-{run_number}")["item"]
    (d / f"run{run_number}_start_raw.json").write_text(json.dumps(run, indent=1))
    if "id" not in run:
        run = run.get("run") or next((v for v in run.values() if isinstance(v, dict) and "id" in v and "status" in v), run)
    if "id" not in run:
        raise SystemExit("could not find the run id in the start response")
    plan = {"experiment_name": name, "experiment_id": exp["id"], "run_number": run_number, "run_id": run["id"],
            "started_at": now(), "override": override, "change_summary": a.description,
            "model": a.model or (spec1["run_spec"].get("env") or {}).get("OPENAI_MODEL"),
            "prompt_file": a.prompt_file, "cohort_version_id": cfg["cohort_version_id"],
            "agent_version": cfg["agent_version"], "agent_version_id": spec1["id"],
            "evaluators": cfg["evaluators"], "tool_policy": cfg["tool_policy"],
            "runspec_public_sha256": h1, "runspec_public": {"run_spec": spec1["run_spec"], "capabilities": spec1["capabilities"]},
            "experiment_created_now": created}
    (d / f"run{run_number}_plan.json").write_text(json.dumps(plan, indent=1))
    print(json.dumps({"experiment": name, "experiment_id": exp["id"], "run_number": run_number, "run_id": run["id"],
                      "run_status": run.get("status"), "created_experiment": created, "override_keys": list(override)}))


# ----------------------------------------------------------------------------- watch

def cmd_watch(a):
    cfg = load_config(a.config)
    d = kit(cfg, "experiment", "run", "watch", a.run_id, "--interval", str(a.interval), "--timeout", str(a.timeout), check=False)
    item = d.get("item") or {}
    run = item.get("run") or item
    print(json.dumps({"ok": d.get("ok"), "terminal": item.get("terminal"), "status": run.get("status"),
                      "progress": run.get("progress"), "error": run.get("error") or (d.get("error") or {}).get("message")}))
    if not d.get("ok") or not item.get("terminal"):
        raise SystemExit(3)


# ----------------------------------------------------------------------------- collect

def case_id_of(cfg: dict, session: dict) -> str:
    inputs = session.get("inputs")
    if isinstance(inputs, dict) and inputs.get(cfg["case_id_field"]):
        return str(inputs[cfg["case_id_field"]])
    return session.get("name") or session["id"]


def cmd_collect(a):
    cfg = load_config(a.config)
    name = experiment_name(cfg, a.experiment)
    d = evidence_root(cfg) / name
    plans = {json.loads(p.read_text())["run_id"]: json.loads(p.read_text()) for p in d.glob("run*_plan.json")}
    if a.run_id not in plans:
        raise SystemExit(f"run {a.run_id} has no plan file under {d}; was it started by this script?")
    plan = plans[a.run_id]
    k = plan["run_number"]
    run = kit(cfg, "experiment", "run", "get", a.run_id)["item"]
    jobs = paged(cfg, "experiment", "run", "jobs", a.run_id, "--sort", "created:asc")
    replays = [r for r in paged(cfg, "replay", "list", "--sort", "created:asc") if r.get("experiment_run_id") == a.run_id]
    wanted = eval_names(cfg)
    by_session: dict[str, dict] = {}
    for e in paged(cfg, "evaluation", "list", "--sort", "created:asc"):
        key = (e.get("evaluator_name"), e.get("evaluator_version"))
        if key in wanted:
            by_session.setdefault(e["session_id"], {})[key] = e  # latest wins (ascending order)
    rows = []
    for r in replays:
        base = kit(cfg, "session", "get", r["baseline_session_id"])["item"]
        res = kit(cfg, "session", "get", r["result_session_id"], check=False) if r.get("result_session_id") else {}
        res = res.get("item", {}) if res.get("ok") else {}
        b_evals = by_session.get(r["baseline_session_id"], {})
        r_evals = by_session.get(r.get("result_session_id"), {})
        def verdict(evs):
            if len(evs) < len(wanted):
                return None
            return all(e.get("passed") for e in evs.values())
        rows.append({
            "case_id": case_id_of(cfg, base), "replay_id": r["id"], "replay_status": r["status"], "replay_error": r.get("error"),
            "baseline_session_id": r["baseline_session_id"], "result_session_id": r.get("result_session_id"),
            "baseline_passed": verdict(b_evals), "replay_passed": verdict(r_evals),
            "evaluations": {f"{n}@{v}": {"passed": e.get("passed"), "explanation": e.get("explanation"), "id": e["id"]} for (n, v), e in r_evals.items()},
            "baseline_evaluations": {f"{n}@{v}": {"passed": e.get("passed"), "id": e["id"]} for (n, v), e in b_evals.items()},
            "result_status": res.get("status"), "result_error": res.get("error"), "response": res.get("outputs"),
            "cost": res.get("cost"), "tool_call_count": res.get("tool_call_count"),
        })
    rows.sort(key=lambda x: x["case_id"])
    passed = sum(1 for x in rows if x["replay_passed"] is True)
    failed = sum(1 for x in rows if x["replay_passed"] is False)
    unscored = sum(1 for x in rows if x["replay_passed"] is None)
    prog = run.get("progress") or {}
    summary = {
        "experiment_name": name, "experiment_id": plan["experiment_id"], "run_number": k, "run_id": a.run_id,
        "collected_at": now(), "run_status": run.get("status"), "progress": prog, "replays": len(replays), "jobs": len(jobs),
        "cases": len(rows), "passed": passed, "failed": failed, "unscored": unscored,
        "green": run.get("status") == "completed" and failed == 0 and unscored == 0 and len(rows) > 0 and prog.get("failed", 0) == 0 and prog.get("canceled", 0) == 0,
        "improved": [x["case_id"] for x in rows if x["baseline_passed"] is False and x["replay_passed"] is True],
        "regressed": [x["case_id"] for x in rows if x["baseline_passed"] is True and x["replay_passed"] is False],
        "still_failing": [x["case_id"] for x in rows if x["baseline_passed"] is False and x["replay_passed"] is False],
        "failures": [x["case_id"] for x in rows if x["replay_passed"] is False],
        "cost_usd": f"{sum(float(x['cost'] or 0) for x in rows):.6f}",
        "override": plan["override"], "model": plan["model"], "prompt_file": plan["prompt_file"], "rows": rows,
    }
    (d / f"run{k}_comparison.json").write_text(json.dumps(summary, indent=1))
    (d / f"run{k}_jobs.json").write_text(json.dumps(jobs, indent=1))
    (d / f"run{k}_run.json").write_text(json.dumps(run, indent=1))
    brief = {key: v for key, v in summary.items() if key != "rows"}
    brief["override"] = list(summary["override"])  # keys only; the full override is in the plan file
    print(json.dumps(brief))
    for x in rows:
        if x["replay_passed"] is not True:
            print(f"\n--- {x['case_id']}  baseline={x['baseline_passed']} replay={x['replay_passed']} result_status={x['result_status']}")
            for ref, e in x["evaluations"].items():
                print(f"    {ref}: passed={e['passed']} {e['explanation']}")
            if x.get("response"):
                print("    reply:", str(x["response"]).replace("\n", " ")[:500])


# ----------------------------------------------------------------------------- next / report

def loop_state(cfg: dict) -> dict:
    root = evidence_root(cfg)
    exps = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not (d.name.endswith("-baseline") or "-iteration-" in d.name):
            continue
        runs = []
        for p in sorted(d.glob("run*_plan.json")):
            plan = json.loads(p.read_text())
            cmp_path = d / f"run{plan['run_number']}_comparison.json"
            cmp = json.loads(cmp_path.read_text()) if cmp_path.exists() else None
            runs.append({"run_number": plan["run_number"], "run_id": plan["run_id"], "collected": cmp is not None,
                         "green": cmp["green"] if cmp else None, "passed": cmp["passed"] if cmp else None,
                         "cases": cmp["cases"] if cmp else None, "failures": cmp["failures"] if cmp else None,
                         "cost": float(cmp["cost_usd"]) if cmp else 0.0, "override": plan["override"],
                         "model": plan["model"], "prompt_file": plan["prompt_file"], "change_summary": plan.get("change_summary"),
                         "experiment_id": plan["experiment_id"]})
        n = 0 if d.name.endswith("-baseline") else int(d.name.rsplit("-", 1)[1])
        exps.append({"name": d.name, "number": n, "runs": runs})
    exps.sort(key=lambda e: e["number"])
    return {"experiments": exps}


def cmd_next(a):
    cfg = load_config(a.config)
    st = loop_state(cfg)
    need = cfg["required_consecutive_greens"]
    exps = st["experiments"]
    if not exps:
        decision = {"action": "run_baseline", "experiment": "baseline", "why": "no baseline has been run"}
    else:
        last = exps[-1]
        pending = [r for r in last["runs"] if not r["collected"]]
        if pending:
            decision = {"action": "collect", "experiment": last["name"], "run_id": pending[0]["run_id"], "why": "a started run has not been collected"}
        else:
            greens = 0
            for r in reversed(last["runs"]):
                if r["green"]:
                    greens += 1
                else:
                    break
            if greens >= need:
                decision = {"action": "stop_success", "experiment": last["name"], "why": f"{greens} consecutive green runs"}
            elif last["runs"] and last["runs"][-1]["green"]:
                decision = {"action": "repeat", "experiment": last["name"], "why": f"green run; {need - greens} more consecutive green run(s) needed"}
            else:
                nxt = last["number"] + 1
                if nxt > cfg["cap"]:
                    decision = {"action": "stop_cap", "why": f"cap of {cfg['cap']} iterations reached without {need} consecutive greens"}
                else:
                    decision = {"action": "new_iteration", "experiment": f"iteration-{nxt}", "failures": last["runs"][-1]["failures"],
                                "why": "last run had red cases; change exactly one thing aimed at them"}
    decision["state"] = [{"name": e["name"], "runs": [(r["run_number"], r["passed"], r["cases"], r["green"]) for r in e["runs"]]} for e in exps]
    print(json.dumps(decision))


def cmd_report(a):
    cfg = load_config(a.config)
    st = loop_state(cfg)
    root = evidence_root(cfg)
    pre = json.loads((root / "preflight.json").read_text()) if (root / "preflight.json").exists() else {}
    h_now, _ = runspec_hash(cfg)
    approved = {r for e in st["experiments"] for r in [json.loads(p.read_text())["runspec_public_sha256"] for p in (root / e["name"]).glob("run*_plan.json")]}
    drift = bool(approved) and (len(approved) > 1 or h_now not in approved)
    total = sum(r["cost"] for e in st["experiments"] for r in e["runs"])
    lines = [f"# Experiment loop: {agent_name(cfg)}", "", f"Generated {now()}. Agent version `{cfg['agent_version']}`, cohort version `{cfg['cohort_version_id']}`, "
             f"evaluators {', '.join(e['ref'] for e in cfg['evaluators'])}. Rule: baseline once, then one change per iteration until "
             f"{cfg['required_consecutive_greens']} consecutive all-green runs; cap {cfg['cap']} iterations. Allowed models: {', '.join(cfg['allowed_models']) or 'none listed'}.", "",
             "| Experiment | One change from the previous experiment | Model | Runs | Consecutive greens at end |", "| --- | --- | --- | --- | ---: |"]
    for e in st["experiments"]:
        runs = ", ".join(f"run {r['run_number']}: {r['passed']}/{r['cases']}" + (f" (red: {', '.join(r['failures'])})" if r['failures'] else "") if r["collected"] else f"run {r['run_number']}: not collected" for r in e["runs"]) or "none"
        change = "none (recorded configuration)" if e["number"] == 0 else (e["runs"][0]["change_summary"] or (f"prompt file {e['runs'][0]['prompt_file']}" if e["runs"][0]["prompt_file"] else f"model {e['runs'][0]['model']}"))
        greens = 0
        for r in reversed(e["runs"]):
            if r["green"]: greens += 1
            else: break
        lines.append(f"| `{e['name']}` | {change} | {e['runs'][0]['model'] if e['runs'] else ''} | {runs} | {greens} |")
    lines += ["", "## Conditions held fixed", "",
              f"- Cohort version `{cfg['cohort_version_id']}` ({pre.get('cohort_session_count', '?')} sessions).",
              f"- Evaluators and parameters as configured; tool policy: `{json.dumps(cfg['tool_policy'])}`.",
              f"- Agent public run spec hash (first 16): {', '.join(sorted(h[:16] for h in approved)) or 'n/a'} at every start; now {h_now[:16]}. Drift observed: {'yes' if drift else 'no'}.",
              "", "## Per-run evidence", ""]
    for e in st["experiments"]:
        for r in e["runs"]:
            lines.append(f"- `{e['name']}` run {r['run_number']}: run `{r['run_id']}`, experiment `{r['experiment_id']}`; "
                         + (f"{r['passed']}/{r['cases']} passed, red {r['failures'] or 'none'}, est. cost ${r['cost']:.5f}" if r["collected"] else "not collected"))
    lines += ["", "## Limits", "",
              "- Results are on the curated cohort only and the iteration was adaptive on those cases; two green runs satisfy the stopping rule, not a reliability claim.",
              "- The configured evaluators grade what they grade; unmeasured qualities (reply wording, tone, side effects outside the tool policy) remain unmeasured.",
              "- Replays are fresh model calls from stored inputs with the configured tool policy; they do not restore the original execution. Hosted model aliases can change over time.",
              f"- Total estimated model cost across {sum(len(e['runs']) for e in st['experiments'])} runs: ${total:.5f}.",
              "", "Per-run files: `<experiment>/runK_plan.json` (configuration and hash), `runK_comparison.json` (paired per-case results and replies), `runK_jobs.json`, `runK_run.json`."]
    (root / "results.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"wrote": str(root / "results.md"), "experiments": len(st["experiments"]), "runs": sum(len(e["runs"]) for e in st["experiments"]), "drift": drift, "total_cost_usd": f"{total:.5f}"}))


# ----------------------------------------------------------------------------- cli

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help=f"path to the loop config (default ./{CONFIG_NAME})")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="write a config file")
    s.add_argument("--agent-version", required=True, help="exact AGENT@VERSION reference, e.g. support-desk-demo@1")
    s.add_argument("--cohort-version", required=True, help="exact cohort-version UUID")
    s.add_argument("--evaluator", action="append", required=True, help="EVALUATOR@VERSION; repeatable")
    s.add_argument("--evaluator-params-file", action="append", help="JSON params file, one per --evaluator in order")
    s.add_argument("--tool-policy-file", required=True, help="JSON tool policy file")
    s.add_argument("--allowed-model", action="append", help="model the loop may switch to; repeatable")
    s.add_argument("--cap", type=int, default=6); s.add_argument("--greens", type=int, default=2)
    s.add_argument("--evidence-dir", default="sessions/experiment-loop"); s.add_argument("--case-id-field", default="case_id")
    s.add_argument("--prompt-dir", default="prompts"); s.add_argument("--kitaru", default="kitaru"); s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    sub.add_parser("preflight", help="read-only readiness checks").set_defaults(fn=cmd_preflight)

    w = sub.add_parser("worker", help="manage a local worker"); w.add_argument("action", choices=["start", "stop", "status"]); w.set_defaults(fn=cmd_worker)

    s = sub.add_parser("start", help="create (idempotently) and start one run")
    s.add_argument("--experiment", required=True, help="baseline, iteration-N, or N")
    s.add_argument("--model"); s.add_argument("--prompt-file"); s.add_argument("--description", help="one sentence: the single change and the failure it targets")
    s.add_argument("--allow-baseline-repeat", action="store_true"); s.set_defaults(fn=cmd_start)

    s = sub.add_parser("watch"); s.add_argument("run_id"); s.add_argument("--interval", type=float, default=5); s.add_argument("--timeout", type=float, default=1800); s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("collect"); s.add_argument("run_id"); s.add_argument("--experiment", required=True); s.set_defaults(fn=cmd_collect)

    sub.add_parser("next", help="what the loop rule requires next").set_defaults(fn=cmd_next)
    sub.add_parser("report", help="write results.md").set_defaults(fn=cmd_report)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
