#!/usr/bin/env python3
"""Drive a Kitaru loop: round 0 audits the unchanged agent with n trials per case,
then each round changes one thing and is screened, trialled, gated on pass^k and
no-regression, confirmed on fresh trials, and the loop stops on success, cap or budget.

All commands read kitaru-loop.json (or --config PATH) and keep evidence under its
evidence directory. Commands:

  init        write a config from a JSON file (--from) with defaults filled in
  preflight   read-only readiness checks; records cost per run and run-spec hash
  worker      start | stop | status
  round       start --round N [--prompt-file F | --model M | --params JSON] --change TEXT
  watch       poll one run until it settles
  collect     pair a settled run's replays with evaluations; save per-case rows
  next        apply the criteria to saved evidence and say what comes next
  report      write results.md (round table, per-case matrix, claims and limits)

Creating an experiment and starting a run are paid, non-idempotent remote
writes; `round start` uses idempotency keys so a crashed call can be re-run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from math import comb

os.environ.setdefault("KITARU_ACTIVE_SKILL", "kitaru-loop")
os.environ["KITARU_NON_INTERACTIVE"] = "1"
CONFIG_NAME = "kitaru-loop.json"

DEFAULT_CRITERIA = {
    "trials_per_round": 5, "k": 3, "screen_first": True, "per_case_floor": "allow_one_miss",
    "cohort_target_mean_pass_k": 0.9, "no_regression": True, "confirmation_round": True,
    "max_rounds": 6, "budget_usd": None, "plateau_patience": 2,
}


# ----------------------------------------------------------------------------- statistics

def pass_hat_k(n: int, c: int, k: int) -> float:
    """tau-bench estimator: probability that all k of k i.i.d. trials succeed, from c of n."""
    if n <= 0 or k > n:
        return float("nan")
    return comb(c, k) / comb(n, k)


def wilson_lower(c: int, n: int, z: float = 1.645) -> float:
    """One-sided 95% lower bound on a pass rate (Wilson score)."""
    if n == 0:
        return 0.0
    p = c / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - spread) / denom)


# ----------------------------------------------------------------------------- helpers

def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "loop"


def load_config(path: str | None) -> dict:
    p = pathlib.Path(path or CONFIG_NAME)
    if not p.is_file():
        raise SystemExit(f"No config at {p}. Run `loop.py init --from FILE` first.")
    cfg = json.loads(p.read_text())
    cfg["_path"] = str(p.resolve())
    cfg["_root"] = str(p.resolve().parent)
    for key in ("name", "agent_version", "cohort_version_id", "evaluators", "tool_policy"):
        if key not in cfg:
            raise SystemExit(f"Config is missing '{key}'.")
    cfg.setdefault("kitaru", "kitaru")
    cfg.setdefault("experiment_prefix", slug(cfg["name"]))
    cfg.setdefault("allowed_changes", ["system_prompt", "model"])
    cfg.setdefault("allowed_models", [])
    cfg.setdefault("evidence_dir", "sessions/kitaru-loop")
    cfg.setdefault("case_id_field", "case_id")
    cfg.setdefault("worker_concurrency", 10)
    cfg["criteria"] = {**DEFAULT_CRITERIA, **(cfg.get("criteria") or {})}
    c = cfg["criteria"]
    if c["k"] > c["trials_per_round"]:
        raise SystemExit("criteria.k cannot exceed criteria.trials_per_round")
    if c["per_case_floor"] not in ("strict", "allow_one_miss", "none"):
        raise SystemExit("criteria.per_case_floor must be strict, allow_one_miss or none")
    for e in cfg["evaluators"]:
        e.setdefault("kind", "binary"); e.setdefault("role", "gate")
        if e["kind"] not in ("binary", "metric") or e["role"] not in ("gate", "report"):
            raise SystemExit(f"evaluator {e.get('ref') or e.get('source')}: kind must be binary|metric, role gate|report")
        if e["kind"] == "metric" and e["role"] == "gate" and not e.get("threshold"):
            raise SystemExit(f"metric evaluator {e.get('ref') or e.get('source')} gates but has no threshold")
        if not e.get("ref") and e.get("source") != "session.cost":
            raise SystemExit("each evaluator needs a ref (name@N) or source 'session.cost'")
    return cfg


def kit(cfg: dict, *args: str, check: bool = True) -> dict:
    r = subprocess.run([cfg["kitaru"], *args, "-o", "json", "--no-browser"], capture_output=True, text=True, cwd=cfg["_root"])
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


def experiment_name(cfg: dict, round_no: int) -> str:
    return f"{cfg['experiment_prefix']}-round-{round_no}"


def round_dir(cfg: dict, round_no: int) -> pathlib.Path:
    d = evidence_root(cfg) / experiment_name(cfg, round_no)
    d.mkdir(parents=True, exist_ok=True)
    return d


def runspec_hash(cfg: dict) -> tuple[str, dict]:
    v = kit(cfg, "agent", "version", "get", cfg["agent_version"])["item"]
    return hashlib.sha256(canon({"run_spec": v["run_spec"], "capabilities": v["capabilities"]}).encode()).hexdigest(), v


def evaluator_tokens(cfg: dict) -> list[str]:
    toks = []
    for e in cfg["evaluators"]:
        if not e.get("ref"):
            continue
        toks += ["--evaluator", e["ref"]]
        if e.get("params") is not None:
            toks += ["--evaluator-params", f"{e['ref']}={json.dumps(e['params'])}"]
    return toks


def eval_key(e: dict) -> str:
    return e.get("ref") or e["source"]


def ref_parts(ref: str) -> tuple[str, int]:
    name, _, ver = ref.partition("@")
    return name, int(ver)


def load_json(p: pathlib.Path):
    return json.loads(p.read_text())


# ----------------------------------------------------------------------------- init

def cmd_init(a):
    src = load_json(pathlib.Path(a.frm))
    cfg = {"kitaru": "kitaru", "criteria": {}, **src}
    cfg["criteria"] = {**DEFAULT_CRITERIA, **(src.get("criteria") or {})}
    cfg.setdefault("experiment_prefix", slug(cfg.get("name", "loop")))
    cfg.setdefault("allowed_changes", ["system_prompt", "model"])
    cfg.setdefault("evidence_dir", "sessions/kitaru-loop")
    cfg.setdefault("case_id_field", "case_id")
    out = pathlib.Path(a.config or CONFIG_NAME)
    if out.exists() and not a.force:
        raise SystemExit(f"{out} exists; pass --force to overwrite.")
    out.write_text(json.dumps(cfg, indent=2) + "\n")
    load_config(str(out))  # validate
    print(json.dumps({"wrote": str(out), "experiments": [f"{cfg['experiment_prefix']}-round-0", f"{cfg['experiment_prefix']}-round-N"], "criteria": cfg["criteria"]}))


# ----------------------------------------------------------------------------- preflight

def cmd_preflight(a):
    cfg = load_config(a.config)
    status = kit(cfg, "status")["item"]
    h, v = runspec_hash(cfg)
    caps = (v.get("run_spec") or {}).get("runtime_capabilities") or {}
    cohort = kit(cfg, "cohort", "version", "get", cfg["cohort_version_id"])["item"]
    members = paged(cfg, "session", "list", "--agent", v["agent_id"], "--cohort", f"{cohort.get('cohort_id')}@{cohort.get('version')}") if cohort.get("cohort_id") else []
    est_per_run = sum(float(s.get("cost") or 0) for s in members)
    problems, notes = [], []
    if status.get("authentication") != "authenticated":
        problems.append("CLI is not authenticated to the selected server")
    if caps.get("overrides") is False:
        problems.append("agent version declares overrides=false: rounds cannot use prompt, model or parameter overrides")
    tp = cfg["tool_policy"]
    if caps.get("tool_policies") is False and any(t.get("type") != "passthrough" for t in [tp.get("default", {})] + list((tp.get("tools") or {}).values())):
        problems.append("agent version declares tool_policies=false but the tool policy is not all passthrough")
    evals = []
    for e in cfg["evaluators"]:
        if not e.get("ref"):
            evals.append({"source": e["source"], "kind": e["kind"], "role": e["role"]}); continue
        parent = kit(cfg, "evaluator", "get", ref_parts(e["ref"])[0])["item"]
        scope = parent.get("agent_id")
        if scope and scope != v["agent_id"]:
            problems.append(f"evaluator {e['ref']} is scoped to another agent ({scope})")
        if not scope:
            notes.append(f"evaluator {e['ref']} has no agent scope; confirm it is deliberately global")
        if e.get("judge") and e["role"] == "gate" and not e.get("validated"):
            notes.append(f"evaluator {e['ref']} is an LLM judge gating the loop and is not marked validated against human verdicts")
        evals.append({"ref": e["ref"], "evaluator_id": parent["id"], "agent_scope": scope, "kind": e["kind"], "role": e["role"], "has_params": e.get("params") is not None})
    for t, pol in (tp.get("tools") or {}).items():
        if pol.get("type") in ("history", "static") and pol.get("on_miss") == "passthrough":
            notes.append(f"tool {t}: a {pol['type']} miss falls through to the live tool")
    if (tp.get("default") or {}).get("type") == "passthrough":
        notes.append("default tool policy is passthrough: unnamed tools execute live")
    c = cfg["criteria"]
    cases = len(members) or cohort.get("session_count") or 0
    summary = {
        "checked_at": now(), "server_url": status.get("server_url"), "server_version": (status.get("server") or {}).get("version"),
        "authenticated": status.get("authentication") == "authenticated", "live_worker_count": status.get("live_worker_count"),
        "agent_version": cfg["agent_version"], "agent_id": v["agent_id"], "agent_version_id": v["id"],
        "run_command": (v.get("run_spec") or {}).get("command"), "run_env": (v.get("run_spec") or {}).get("env"),
        "runtime_capabilities": caps, "runspec_public_sha256": h,
        "cohort_version_id": cfg["cohort_version_id"], "cohort_session_count": cases,
        "est_cost_per_run_usd": round(est_per_run, 6), "est_cost_full_round_usd": round(est_per_run * c["trials_per_round"], 6),
        "replays_per_full_round": cases * c["trials_per_round"],
        "criteria": c, "allowed_changes": cfg["allowed_changes"], "allowed_models": cfg["allowed_models"],
        "experiments": [experiment_name(cfg, 0), experiment_name(cfg, 1)],
        "evaluators": evals, "problems": problems, "notes": notes,
    }
    (evidence_root(cfg) / "preflight.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    if problems:
        raise SystemExit(2)


# ----------------------------------------------------------------------------- worker

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0); return True
    except OSError:
        return False


def cmd_worker(a):
    cfg = load_config(a.config)
    root = evidence_root(cfg); pidfile, logfile = root / "worker.pid", root / "worker.log"
    if a.action == "status":
        pid = int(pidfile.read_text()) if pidfile.exists() else None
        print(json.dumps({"live_worker_count": kit(cfg, "status")["item"].get("live_worker_count"), "our_pid": pid, "our_worker_alive": bool(pid and _pid_alive(pid)), "log": str(logfile)})); return
    if a.action == "stop":
        if not pidfile.exists():
            print(json.dumps({"stopped": False, "reason": "no pid file"})); return
        pid = int(pidfile.read_text())
        if _pid_alive(pid):
            os.kill(pid, signal.SIGINT)
            for _ in range(60):
                if not _pid_alive(pid): break
                time.sleep(1)
        pidfile.unlink(missing_ok=True)
        print(json.dumps({"stopped": not _pid_alive(pid), "pid": pid})); return
    if pidfile.exists() and _pid_alive(int(pidfile.read_text())):
        print(json.dumps({"started": False, "reason": "already running", "pid": int(pidfile.read_text())})); return
    _, v = runspec_hash(cfg)
    name = f"kitaru-loop-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    cmd = [cfg["kitaru"], "worker", "start", "--name", name, "--concurrency", str(cfg["worker_concurrency"]), "--claim", f"agent={v['id']}", "--claim", "evaluator", "--log-level", "info"]
    proc = subprocess.Popen(cmd, cwd=cfg["_root"], stdout=open(logfile, "ab"), stderr=subprocess.STDOUT, start_new_session=True)
    pidfile.write_text(str(proc.pid))
    for _ in range(30):
        time.sleep(1)
        if kit(cfg, "status")["item"].get("live_worker_count"): break
    print(json.dumps({"started": True, "pid": proc.pid, "name": name, "live_worker_count": kit(cfg, "status")["item"].get("live_worker_count"), "log": str(logfile)}))


# ----------------------------------------------------------------------------- state

def round_runs(cfg: dict, round_no: int) -> list[dict]:
    d = round_dir(cfg, round_no)
    runs = []
    for p in sorted(d.glob("run*_plan.json"), key=lambda p: int(re.search(r"run(\d+)_", p.name).group(1))):
        plan = load_json(p)
        cmp_path = d / f"run{plan['run_number']}_comparison.json"
        plan["comparison"] = load_json(cmp_path) if cmp_path.exists() else None
        runs.append(plan)
    return runs


def existing_rounds(cfg: dict) -> list[int]:
    root = evidence_root(cfg); pre = cfg["experiment_prefix"] + "-round-"
    return sorted(int(d.name[len(pre):]) for d in root.iterdir() if d.is_dir() and d.name.startswith(pre) and d.name[len(pre):].isdigit())


def per_case_stats(cfg: dict, runs: list[dict]) -> dict:
    """Aggregate collected runs (same phase set) into per-case trial counts, pass^k and metric means."""
    c = cfg["criteria"]; k = c["k"]
    gate_bin = [eval_key(e) for e in cfg["evaluators"] if e["kind"] == "binary" and e["role"] == "gate"]
    rep_bin = [eval_key(e) for e in cfg["evaluators"] if e["kind"] == "binary" and e["role"] == "report"]
    metrics = [e for e in cfg["evaluators"] if e["kind"] == "metric"]
    cases: dict[str, dict] = {}
    cost = 0.0; exec_failures = 0
    for r in runs:
        cmp = r["comparison"]
        if not cmp: continue
        cost += float(cmp.get("cost_usd") or 0)
        exec_failures += cmp.get("execution_failures", 0)
        for row in cmp["rows"]:
            cs = cases.setdefault(row["case_id"], {"n": 0, "c": 0, "report_c": {x: 0 for x in rep_bin}, "metric_sum": {eval_key(e): 0.0 for e in metrics}, "metric_n": 0, "misses": []})
            if row.get("scored") is False:
                continue  # execution failure or missing evaluation: outside the denominator
            cs["n"] += 1
            ok = all(row["binary"].get(x) is True for x in gate_bin)
            cs["c"] += int(ok)
            if not ok:
                cs["misses"].append(r["run_number"])
            for x in rep_bin:
                cs["report_c"][x] += int(row["binary"].get(x) is True)
            for e in metrics:
                val = row["metrics"].get(eval_key(e))
                if val is not None:
                    cs["metric_sum"][eval_key(e)] += float(val)
            cs["metric_n"] += 1
    out = {}
    for cid, cs in cases.items():
        n, cc = cs["n"], cs["c"]
        out[cid] = {"n": n, "c": cc, "pass_rate": (cc / n) if n else None, "pass_k": pass_hat_k(n, cc, k) if n >= k else None,
                    "wilson_lower": wilson_lower(cc, n) if n else None, "misses_in_runs": cs["misses"],
                    "report": {x: (cs["report_c"][x] / n if n else None) for x in rep_bin},
                    "metrics": {x: (cs["metric_sum"][x] / cs["metric_n"] if cs["metric_n"] else None) for x in cs["metric_sum"]}}
    return {"cases": out, "cost_usd": round(cost, 6), "execution_failures": exec_failures}


def gate(cfg: dict, stats: dict, baseline: dict | None) -> dict:
    """Apply floor, cohort target, no-regression and metric thresholds. Returns met + reasons."""
    c = cfg["criteria"]; n = c["trials_per_round"]; k = c["k"]
    reasons, flaky, regressed, below_floor = [], [], [], []
    cases = stats["cases"]
    if not cases:
        return {"met": False, "reasons": ["no scored cases"], "mean_pass_k": None}
    incomplete = [cid for cid, s in cases.items() if s["n"] < n]
    if incomplete:
        return {"met": False, "reasons": [f"{len(incomplete)} case(s) have fewer than {n} scored trials"], "mean_pass_k": None, "incomplete": incomplete}
    for cid, s in cases.items():
        if c["per_case_floor"] == "strict" and s["c"] < s["n"]:
            below_floor.append(cid)
        elif c["per_case_floor"] == "allow_one_miss" and s["c"] < s["n"] - 1:
            below_floor.append(cid)
        if 0 < s["c"] < s["n"]:
            flaky.append(cid)
    if below_floor:
        reasons.append(f"per-case floor ({c['per_case_floor']}) not met by {sorted(below_floor)}")
    mean_pk = sum(s["pass_k"] for s in cases.values()) / len(cases)
    if mean_pk < c["cohort_target_mean_pass_k"]:
        reasons.append(f"mean pass^{k} {mean_pk:.3f} below target {c['cohort_target_mean_pass_k']}")
    if c["no_regression"] and baseline:
        for cid, s in cases.items():
            b = baseline["cases"].get(cid)
            if b and b["pass_rate"] is not None and s["pass_rate"] < b["pass_rate"]:
                regressed.append(cid)
        if regressed:
            reasons.append(f"regressed against round 0: {sorted(regressed)}")
    metric_results = {}
    for e in cfg["evaluators"]:
        if e["kind"] != "metric": continue
        key = eval_key(e)
        vals = [s["metrics"].get(key) for s in cases.values() if s["metrics"].get(key) is not None]
        mean = sum(vals) / len(vals) if vals else None
        ok = None
        if e["role"] == "gate" and mean is not None:
            op, val = e["threshold"]["op"], float(e["threshold"]["value"])
            ok = {"<=": mean <= val, "<": mean < val, ">=": mean >= val, ">": mean > val}[op]
            if not ok:
                reasons.append(f"metric {key} mean {mean:.6f} fails {op} {val}")
        metric_results[key] = {"mean": mean, "gate_ok": ok, "role": e["role"]}
    return {"met": not reasons, "reasons": reasons, "mean_pass_k": round(mean_pk, 4), "below_floor": sorted(below_floor), "flaky": sorted(flaky),
            "regressed": sorted(regressed), "metrics": metric_results,
            "improved_vs_round0": sorted(cid for cid, s in cases.items() if baseline and baseline["cases"].get(cid) and s["pass_rate"] > baseline["cases"][cid]["pass_rate"])}


def round_summary(cfg: dict, round_no: int, baseline: dict | None) -> dict:
    runs = round_runs(cfg, round_no)
    collected = [r for r in runs if r["comparison"]]
    screen = [r for r in collected if r["phase"] == "screen"]
    selection = [r for r in collected if r["phase"] in ("screen", "trial")]
    confirmation = [r for r in collected if r["phase"] == "confirmation"]
    sel_stats = per_case_stats(cfg, selection)
    conf_stats = per_case_stats(cfg, confirmation) if confirmation else None
    return {"round": round_no, "experiment": experiment_name(cfg, round_no), "runs": runs, "collected": len(collected), "uncollected": [r["run_id"] for r in runs if not r["comparison"]],
            "screen_green": (screen[0]["comparison"]["screen_green"] if screen else None),
            "selection_trials": len(selection), "selection": sel_stats, "selection_gate": gate(cfg, sel_stats, baseline) if selection else None,
            "confirmation_trials": len(confirmation), "confirmation": conf_stats, "confirmation_gate": gate(cfg, conf_stats, baseline) if conf_stats else None,
            "change": runs[0].get("change") if runs else None, "override": runs[0].get("override") if runs else None, "model": runs[0].get("model") if runs else None,
            "cost_usd": round(sel_stats["cost_usd"] + (conf_stats["cost_usd"] if conf_stats else 0), 6)}


# ----------------------------------------------------------------------------- round start

def cmd_round(a):
    cfg = load_config(a.config)
    c = cfg["criteria"]; n = c["trials_per_round"]
    r = a.round
    name = experiment_name(cfg, r); d = round_dir(cfg, r)
    override, change_kind = {}, None
    if a.prompt_file:
        override["system_prompt"] = pathlib.Path(a.prompt_file).read_text(); change_kind = "system_prompt"
    if a.model:
        if cfg["allowed_models"] and a.model not in cfg["allowed_models"]:
            raise SystemExit(f"model {a.model} is not in allowed_models {cfg['allowed_models']}")
        override["model"] = a.model; change_kind = "model" if change_kind is None else "MULTIPLE"
    if a.params:
        override["model_params"] = json.loads(a.params); change_kind = "model_params" if change_kind is None else "MULTIPLE"
    if r == 0 and override:
        raise SystemExit("round 0 is the unchanged baseline and takes no override")
    if r > 0 and not override:
        raise SystemExit("a round needs exactly one change: --prompt-file, --model or --params")
    if change_kind == "MULTIPLE":
        raise SystemExit("one change per round: pass only one of --prompt-file, --model, --params")
    if r > 0 and change_kind not in cfg["allowed_changes"]:
        raise SystemExit(f"change kind {change_kind} is not in allowed_changes {cfg['allowed_changes']}")
    if r > 0 and not a.change:
        raise SystemExit("--change TEXT (one sentence: the change and the failure it targets) is required for a round")
    if r > c["max_rounds"]:
        raise SystemExit(f"round {r} exceeds max_rounds {c['max_rounds']}")

    # phase and run number from saved state
    runs = round_runs(cfg, r)
    if any(not x["comparison"] for x in runs):
        raise SystemExit("collect the previous run of this round first")
    if runs and canon(runs[0]["override"]) != canon(override):
        raise SystemExit("this round already has runs with a different override; a changed candidate is a new round")
    run_number = len(runs) + 1
    selection = [x for x in runs if x["phase"] in ("screen", "trial")]
    if r == 0:
        phase = "trial"
        if len(selection) >= n: raise SystemExit("round 0 already has its trials")
    elif not runs:
        phase = "screen" if c["screen_first"] else "trial"
    elif len(selection) < n:
        if selection[0]["phase"] == "screen" and not selection[0]["comparison"]["screen_green"]:
            raise SystemExit("this round failed its screen; start the next round instead")
        phase = "trial"
    else:
        if not c["confirmation_round"]: raise SystemExit("this round already has its trials and confirmation is off")
        conf = [x for x in runs if x["phase"] == "confirmation"]
        if len(conf) >= n: raise SystemExit("this round already has its confirmation trials")
        phase = "confirmation"

    # budget
    pre_path = evidence_root(cfg) / "preflight.json"
    est = load_json(pre_path).get("est_cost_per_run_usd", 0) if pre_path.exists() else 0
    spent = sum(float(x["comparison"].get("cost_usd") or 0) for rr in existing_rounds(cfg) for x in round_runs(cfg, rr) if x["comparison"])
    if c["budget_usd"] is not None and spent + est > float(c["budget_usd"]) and not a.ignore_budget:
        raise SystemExit(f"budget: spent ${spent:.4f} + est ${est:.4f} for this run exceeds ${c['budget_usd']}")

    h1, spec1 = runspec_hash(cfg)
    exp = kit(cfg, "experiment", "get", name, check=False)
    created = False
    if exp.get("ok"):
        exp = exp["item"]
    else:
        args = ["experiment", "create", name, "--agent", spec1["agent_id"], *evaluator_tokens(cfg), "--tool-policy", json.dumps(cfg["tool_policy"]), "--idempotency-key", name]
        if override: args += ["--override", json.dumps(override)]
        desc = (f"Round {r}: {a.change}" if r > 0 else f"Round 0: unchanged baseline, {n} trials per case") + (f" Hypothesis: {cfg.get('hypothesis')}" if cfg.get("hypothesis") else "")
        args += ["--description", desc[:1000]]
        exp = kit(cfg, *args)["item"]; created = True
    exp = kit(cfg, "experiment", "get", exp["id"])["item"]
    (d / "experiment.json").write_text(json.dumps(exp, indent=1))
    problems = []
    if override and canon(exp.get("override") or {}) != canon(override): problems.append("stored override differs from the requested change")
    if not override and strip_nulls(exp.get("override") or {}): problems.append("round 0 experiment has an override stored")
    if canon(exp.get("tool_policy")) != canon(cfg["tool_policy"]): problems.append("stored tool policy differs from the config")
    h2, _ = runspec_hash(cfg)
    if h1 != h2: problems.append("agent run spec changed between read and creation")
    if problems: raise SystemExit("CONFIG PROBLEMS: " + "; ".join(problems))
    run = kit(cfg, "experiment", "run", "start", exp["id"], "--cohort-version", cfg["cohort_version_id"], "--agent", cfg["agent_version"],
              "--baseline-evaluation-mode", "if-missing", "--idempotency-key", f"{name}-run-{run_number}")["item"]
    (d / f"run{run_number}_start_raw.json").write_text(json.dumps(run, indent=1))
    if "id" not in run:
        run = run.get("run") or next((v for v in run.values() if isinstance(v, dict) and "id" in v and "status" in v), run)
    if "id" not in run: raise SystemExit("could not find the run id in the start response")
    plan = {"round": r, "experiment_name": name, "experiment_id": exp["id"], "run_number": run_number, "run_id": run["id"], "phase": phase,
            "started_at": now(), "override": override, "change_kind": change_kind, "change": a.change,
            "model": a.model or (spec1["run_spec"].get("env") or {}).get("OPENAI_MODEL"), "prompt_file": a.prompt_file,
            "cohort_version_id": cfg["cohort_version_id"], "agent_version": cfg["agent_version"], "agent_version_id": spec1["id"],
            "evaluators": cfg["evaluators"], "tool_policy": cfg["tool_policy"], "criteria": c,
            "runspec_public_sha256": h1, "runspec_public": {"run_spec": spec1["run_spec"], "capabilities": spec1["capabilities"]}, "experiment_created_now": created}
    (d / f"run{run_number}_plan.json").write_text(json.dumps(plan, indent=1))
    print(json.dumps({"round": r, "experiment": name, "experiment_id": exp["id"], "run_number": run_number, "phase": phase, "run_id": run["id"], "run_status": run.get("status"), "created_experiment": created}))


# ----------------------------------------------------------------------------- watch / collect

def cmd_watch(a):
    cfg = load_config(a.config)
    d = kit(cfg, "experiment", "run", "watch", a.run_id, "--interval", str(a.interval), "--timeout", str(a.timeout), check=False)
    item = d.get("item") or {}; run = item.get("run") or item
    print(json.dumps({"ok": d.get("ok"), "terminal": item.get("terminal"), "status": run.get("status"), "progress": run.get("progress"), "error": run.get("error") or (d.get("error") or {}).get("message")}))
    if not d.get("ok") or not item.get("terminal"): raise SystemExit(3)


def case_id_of(cfg: dict, session: dict) -> str:
    inputs = session.get("inputs")
    if isinstance(inputs, dict) and inputs.get(cfg["case_id_field"]):
        return str(inputs[cfg["case_id_field"]])
    return session.get("name") or session["id"]


def cmd_collect(a):
    cfg = load_config(a.config)
    plan = None
    for rr in existing_rounds(cfg):
        for x in round_runs(cfg, rr):
            if x["run_id"] == a.run_id: plan = x
    if not plan: raise SystemExit(f"run {a.run_id} has no plan file; was it started by this script?")
    r, k_ = plan["round"], plan["run_number"]; d = round_dir(cfg, r)
    run = kit(cfg, "experiment", "run", "get", a.run_id)["item"]
    jobs = paged(cfg, "experiment", "run", "jobs", a.run_id, "--sort", "created:asc")
    replays = [x for x in paged(cfg, "replay", "list", "--sort", "created:asc") if x.get("experiment_run_id") == a.run_id]
    binaries = {eval_key(e): ref_parts(e["ref"]) for e in cfg["evaluators"] if e["kind"] == "binary"}
    metric_refs = {eval_key(e): ref_parts(e["ref"]) for e in cfg["evaluators"] if e["kind"] == "metric" and e.get("ref")}
    wanted = set(binaries.values()) | set(metric_refs.values())
    by_session: dict[str, dict] = {}
    for e in paged(cfg, "evaluation", "list", "--sort", "created:asc"):
        key = (e.get("evaluator_name"), e.get("evaluator_version"))
        if key in wanted: by_session.setdefault(e["session_id"], {})[key] = e
    gate_bin = [x for x in binaries if any(e["role"] == "gate" and eval_key(e) == x for e in cfg["evaluators"])]
    rows = []; exec_failures = 0
    for x in replays:
        base = kit(cfg, "session", "get", x["baseline_session_id"])["item"]
        res = kit(cfg, "session", "get", x["result_session_id"], check=False) if x.get("result_session_id") else {}
        res = res.get("item", {}) if res.get("ok") else {}
        evs = by_session.get(x.get("result_session_id"), {})
        binary = {name: (evs[key].get("passed") if key in evs else None) for name, key in binaries.items()}
        metrics = {}
        for name, key in metric_refs.items():
            e = evs.get(key); metrics[name] = (e.get("score") if e and e.get("score") is not None else (e.get("value") if e else None))
        for e in cfg["evaluators"]:
            if e.get("source") == "session.cost": metrics[eval_key(e)] = float(res.get("cost") or 0) if res else None
        executed = x["status"] == "completed" and res.get("status") == "completed" and not res.get("error")
        scored = executed and all(binary[g] is not None for g in gate_bin)
        if not executed: exec_failures += 1
        rows.append({"case_id": case_id_of(cfg, base), "replay_id": x["id"], "replay_status": x["status"], "replay_error": x.get("error"),
                     "baseline_session_id": x["baseline_session_id"], "result_session_id": x.get("result_session_id"), "result_status": res.get("status"), "result_error": res.get("error"),
                     "executed": executed, "scored": scored, "binary": binary, "explanations": {name: (evs[key].get("explanation") if key in evs else None) for name, key in binaries.items()},
                     "metrics": metrics, "response": res.get("outputs"), "cost": res.get("cost"), "tool_call_count": res.get("tool_call_count")})
    rows.sort(key=lambda z: z["case_id"])
    gate_pass = sum(1 for z in rows if z["scored"] and all(z["binary"][g] is True for g in gate_bin))
    gate_fail = [z["case_id"] for z in rows if z["scored"] and not all(z["binary"][g] is True for g in gate_bin)]
    prog = run.get("progress") or {}
    screen_green = run.get("status") == "completed" and exec_failures == 0 and not gate_fail and len(rows) > 0 and all(z["scored"] for z in rows)
    summary = {"round": r, "run_number": k_, "phase": plan["phase"], "run_id": a.run_id, "experiment_id": plan["experiment_id"], "collected_at": now(),
               "run_status": run.get("status"), "progress": prog, "replays": len(replays), "jobs": len(jobs), "cases": len(rows),
               "gate_pass": gate_pass, "gate_fail": gate_fail, "unscored": [z["case_id"] for z in rows if not z["scored"]], "execution_failures": exec_failures,
               "screen_green": screen_green, "cost_usd": f"{sum(float(z['cost'] or 0) for z in rows):.6f}", "rows": rows}
    (d / f"run{k_}_comparison.json").write_text(json.dumps(summary, indent=1))
    (d / f"run{k_}_jobs.json").write_text(json.dumps(jobs, indent=1)); (d / f"run{k_}_run.json").write_text(json.dumps(run, indent=1))
    print(json.dumps({key: v for key, v in summary.items() if key != "rows"}))
    for z in rows:
        if not z["scored"] or z["case_id"] in gate_fail:
            print(f"\n--- {z['case_id']}  executed={z['executed']} scored={z['scored']} result_status={z['result_status']}")
            for name in binaries: print(f"    {name}: passed={z['binary'][name]} {z['explanations'][name]}")
            if z.get("response"): print("    reply:", str(z["response"]).replace("\n", " ")[:500])


# ----------------------------------------------------------------------------- next / report

def round_score(s: dict) -> tuple | None:
    """Comparable first look at a round: gate passes in its first run, then mean pass^k when full trials exist."""
    first = next((r for r in s["runs"] if r["comparison"]), None)
    if not first:
        return None
    mean_pk = (s["selection_gate"] or {}).get("mean_pass_k") if s["selection_gate"] and not s["selection_gate"].get("incomplete") else None
    return (first["comparison"]["gate_pass"], mean_pk if mean_pk is not None else -1.0)


def plateau_check(cfg: dict, sums: list[dict]) -> dict:
    """Early-stopping patience over rounds >= 1, plus oscillation and stuck-case notes."""
    c = cfg["criteria"]; patience = c.get("plateau_patience")
    scored = [(s["round"], round_score(s), s) for s in sums if s["round"] >= 1 and round_score(s) is not None]
    out = {"plateau": False, "patience": patience, "best_round": None, "best_score": None, "rounds_without_improvement": 0, "notes": []}
    if not scored:
        return out
    best_round, best = scored[0][0], scored[0][1]; without = 0
    for rnd, sc, _ in scored[1:]:
        if sc > best:
            best, best_round, without = sc, rnd, 0
        else:
            without += 1
    out.update({"best_round": best_round, "best_score": {"first_run_gate_passes": best[0], "mean_pass_k": None if best[1] < 0 else best[1]}, "rounds_without_improvement": without})
    if patience is not None and without >= patience:
        out["plateau"] = True
    fails = []
    for _, _, s in scored:
        first = next((r for r in s["runs"] if r["comparison"]), None)
        fails.append(set(first["comparison"]["gate_fail"]) if first else set())
    if len(fails) >= 3 and fails[-1] == fails[-3] and fails[-1] != fails[-2] and fails[-1] and fails[-2]:
        out["notes"].append(f"oscillation: the failing set alternates between {sorted(fails[-1])} and {sorted(fails[-2])}; fixing one breaks the other (a trade-off, not a dead end)")
    if len(fails) >= 3:
        stuck = fails[-1] & fails[-2] & fails[-3]
        if stuck:
            out["notes"].append(f"stuck: {sorted(stuck)} red in the last three rounds; consider the next change lever")
    return out


def decide(cfg: dict) -> dict:
    c = cfg["criteria"]; n = c["trials_per_round"]
    rounds = existing_rounds(cfg)
    pre_path = evidence_root(cfg) / "preflight.json"
    est = load_json(pre_path).get("est_cost_per_run_usd", 0) if pre_path.exists() else 0
    base = round_summary(cfg, 0, None) if 0 in rounds else None
    base_stats = base["selection"] if base and base["selection_trials"] >= n else None
    sums = {rr: round_summary(cfg, rr, base_stats) for rr in rounds}
    spent = sum(s["cost_usd"] for s in sums.values())
    plateau = plateau_check(cfg, [sums[rr] for rr in rounds])
    def over_budget():
        return c["budget_usd"] is not None and spent + est > float(c["budget_usd"])
    def failures(s):
        out = {}
        for r_ in s["runs"]:
            if r_["comparison"]:
                for cid in r_["comparison"]["gate_fail"]: out.setdefault(cid, []).append(r_["run_number"])
        return out
    def audit(stats):
        if not stats: return None
        cs = stats["cases"]
        return {"consistent_fail": sorted(cid for cid, s_ in cs.items() if s_["c"] == 0), "flaky": sorted(cid for cid, s_ in cs.items() if 0 < s_["c"] < s_["n"]), "consistent_pass": sorted(cid for cid, s_ in cs.items() if s_["c"] == s_["n"])}
    def nxt(last, why, s):
        if plateau["plateau"]:
            return {"action": "stop_plateau", "round": last, "why": f"{why}; no round has beaten round {plateau['best_round']} in {plateau['rounds_without_improvement']} consecutive rounds (patience {plateau['patience']})"}
        if last + 1 > c["max_rounds"]:
            return {"action": "stop_cap", "round": last, "why": f"{why}; max_rounds {c['max_rounds']} reached"}
        return {"action": "start_round", "round": last + 1, "why": why, "last_round_failures": failures(s)}
    def inner():
        if not rounds:
            return {"action": "start_round_0", "phase": "trial", "why": f"no baseline audit yet; round 0 runs {n} trials per case"}
        if base["uncollected"]:
            return {"action": "collect", "round": 0, "run_id": base["uncollected"][0], "why": "a round-0 run has not been collected"}
        if base["selection_trials"] < n:
            return {"action": "start_round_0", "phase": "trial", "why": f"round 0 has {base['selection_trials']}/{n} trials"}
        last = max(rounds)
        if last == 0:
            return {"action": "start_round", "round": 1, "why": "round 0 complete; first change", "round0_audit": audit(base_stats)}
        s = sums[last]
        if s["uncollected"]:
            return {"action": "collect", "round": last, "run_id": s["uncollected"][0], "why": "a run has not been collected"}
        if not s["runs"]:
            return {"action": "continue_round", "round": last, "phase": "screen" if c["screen_first"] else "trial", "why": "round has no runs yet"}
        if s["screen_green"] is False:
            return nxt(last, "screen was red: " + ", ".join(s["runs"][0]["comparison"]["gate_fail"]), s)
        if s["selection_trials"] < n:
            return {"action": "continue_round", "round": last, "phase": "trial", "why": f"{s['selection_trials']}/{n} selection trials"}
        g = s["selection_gate"]
        if not g["met"]:
            return nxt(last, "gate not met: " + "; ".join(g["reasons"]), s)
        if not c["confirmation_round"]:
            return {"action": "stop_success", "round": last, "why": "gate met and confirmation is off", "gate": g}
        if s["confirmation_trials"] < n:
            return {"action": "continue_round", "round": last, "phase": "confirmation", "why": f"gate met on selection trials; {s['confirmation_trials']}/{n} confirmation trials"}
        cg = s["confirmation_gate"]
        if cg["met"]:
            return {"action": "stop_success", "round": last, "why": "gate met on selection trials and again on fresh confirmation trials", "gate": g, "confirmation_gate": cg}
        return nxt(last, "confirmation failed: " + "; ".join(cg["reasons"]), s)
    decision = inner()
    if decision["action"] in ("start_round_0", "start_round", "continue_round") and over_budget():
        decision = {"action": "stop_budget", "why": f"spent ${spent:.4f}; next run est ${est:.4f} exceeds budget ${c['budget_usd']}", "intended": decision}
    decision["spent_usd"] = round(spent, 6)
    decision["plateau"] = plateau
    decision["rounds"] = [{"round": rr, "selection_trials": sums[rr]["selection_trials"], "confirmation_trials": sums[rr]["confirmation_trials"], "screen_green": sums[rr]["screen_green"],
                           "first_run_gate_passes": (round_score(sums[rr]) or (None,))[0], "mean_pass_k": (sums[rr]["selection_gate"] or {}).get("mean_pass_k")} for rr in rounds]
    return decision


def cmd_next(a):
    print(json.dumps(decide(load_config(a.config))))


def cmd_report(a):
    cfg = load_config(a.config); c = cfg["criteria"]; n, k = c["trials_per_round"], c["k"]
    root = evidence_root(cfg); rounds = existing_rounds(cfg)
    pre = load_json(root / "preflight.json") if (root / "preflight.json").exists() else {}
    base = round_summary(cfg, 0, None) if 0 in rounds else None
    base_stats = base["selection"] if base and base["selection_trials"] >= n else None
    sums = [round_summary(cfg, rr, base_stats) for rr in rounds]
    h_now, _ = runspec_hash(cfg)
    approved = {x["runspec_public_sha256"] for s in sums for x in s["runs"]}
    drift = bool(approved) and (len(approved) > 1 or h_now not in approved)
    total = sum(s["cost_usd"] for s in sums)
    winner = next((s for s in reversed(sums) if s["round"] > 0 and s["selection_gate"] and s["selection_gate"]["met"] and (not c["confirmation_round"] or (s["confirmation_gate"] and s["confirmation_gate"]["met"]))), None)
    decision = decide(cfg)
    state_line = {"stop_success": f"Stopped: round {decision.get('round')} met the gate and confirmed.", "stop_cap": "Stopped: round cap reached without a confirmed winner.",
                  "stop_budget": "Stopped: budget reached.", "stop_plateau": f"Stopped: plateau. {decision.get('why')}"}.get(decision["action"], f"In progress: next action is {decision['action']}.")
    plateau_notes = decision.get("plateau", {}).get("notes") or []
    L = [f"# Kitaru loop: {cfg['name']}", "", f"Generated {now()}. Agent version `{cfg['agent_version']}`, cohort version `{cfg['cohort_version_id']}` ({pre.get('cohort_session_count', '?')} cases).",
         f"Hypothesis: {cfg.get('hypothesis') or 'not stated'}", "", f"**{state_line}**" + (" " + " ".join(plateau_notes) if plateau_notes else ""), "",
         f"Criteria: {n} trials per round, k={k}, screen first {'on' if c['screen_first'] else 'off'}, per-case floor `{c['per_case_floor']}`, cohort target mean pass^{k} ≥ {c['cohort_target_mean_pass_k']}, "
         f"no regression {'on' if c['no_regression'] else 'off'}, confirmation round {'on' if c['confirmation_round'] else 'off'}, max {c['max_rounds']} rounds, "
         f"plateau patience {c.get('plateau_patience') if c.get('plateau_patience') is not None else 'off'}, budget {('$' + str(c['budget_usd'])) if c['budget_usd'] is not None else 'none'}.", "",
         "| Round | One change | Model | Screen | Selection trials | Mean pass^k | Floor | Regressions | Confirmation | Outcome |", "| --- | --- | --- | --- | --- | ---: | --- | --- | --- | --- |"]
    for s in sums:
        g, cg = s["selection_gate"], s["confirmation_gate"]
        incomplete = (g and g.get("incomplete")) or (cg and cg.get("incomplete")) or (not g)
        outcome = ("baseline audit" + (" (incomplete)" if s["selection_trials"] < n else "")) if s["round"] == 0 else (
            "screen red" if s["screen_green"] is False else (
            f"incomplete ({s['selection_trials']}/{n} trials)" if incomplete and not (g and g["met"]) else (
            "gate not met" if g and not g["met"] else (
            "confirmation failed" if cg and not cg["met"] else (
            "green, confirmed" if cg and cg["met"] else (
            "green, awaiting confirmation" if c["confirmation_round"] else "green"))))))
        L.append(f"| {s['round']} | {'none (recorded configuration)' if s['round'] == 0 else (s['change'] or '')} | {s['model'] or ''} | {'n/a' if s['round'] == 0 else ('green' if s['screen_green'] else ('red' if s['screen_green'] is False else 'not run'))} | {s['selection_trials']}/{n} | "
                 f"{(g or {}).get('mean_pass_k') if g else ''} | {'ok' if g and not g.get('below_floor') else (', '.join(g['below_floor']) if g else '')} | {', '.join(g['regressed']) if g and g.get('regressed') else ('none' if g else '')} | "
                 f"{(str(s['confirmation_trials']) + '/' + str(n) + (' met' if cg and cg['met'] else (' failed' if cg else ''))) if s['confirmation_trials'] else '—'} | {outcome} |")
    if base_stats:
        L += ["", "## Round 0 consistency audit", "", f"Each case replayed {n} times with the unchanged agent.", "", "| Case | Passes | pass^k | Classification |", "| --- | ---: | ---: | --- |"]
        for cid, s_ in sorted(base_stats["cases"].items()):
            cls = "consistent fail" if s_["c"] == 0 else ("consistent pass" if s_["c"] == s_["n"] else "flaky")
            L.append(f"| {cid} | {s_['c']}/{s_['n']} | {s_['pass_k']:.2f} | {cls} |")
    if winner:
        ws = winner["selection"]; wc = winner["confirmation"]
        L += ["", f"## Winning round: {winner['round']}", "", "| Case | Round 0 | Selection | Confirmation | pass^k (selection) | Change |", "| --- | ---: | ---: | ---: | ---: | --- |"]
        for cid, s_ in sorted(ws["cases"].items()):
            b = base_stats["cases"].get(cid) if base_stats else None
            cf = wc["cases"].get(cid) if wc else None
            delta = "" if not b else ("improved" if s_["pass_rate"] > b["pass_rate"] else ("regressed" if s_["pass_rate"] < b["pass_rate"] else "unchanged"))
            L.append(f"| {cid} | {b['c']}/{b['n'] if b else ''} | {s_['c']}/{s_['n']} | {(str(cf['c']) + '/' + str(cf['n'])) if cf else '—'} | {s_['pass_k']:.2f} | {delta} |")
        trials_total = sum(s_["n"] for s_ in ws["cases"].values()) + (sum(s_["n"] for s_ in wc["cases"].values()) if wc else 0)
        misses = sum(s_["n"] - s_["c"] for s_ in ws["cases"].values()) + (sum(s_["n"] - s_["c"] for s_ in wc["cases"].values()) if wc else 0)
        L += ["", "### What this green can claim", "",
              f"- Across {trials_total} trials on the winning configuration there were {misses} gate misses. " + (f"With zero misses, the rule of three bounds the per-trial failure rate at about {3 / trials_total:.1%} (95% confidence), so the per-trial pass rate is at least about {1 - 3 / trials_total:.1%}." if misses == 0 else f"The observed per-trial pass rate is {1 - misses / trials_total:.1%}; the Wilson 95% lower bound per case is in the comparison files."),
              f"- Cases marked improved went from a lower round-0 pass rate to a higher one under the one change recorded for this round. Cases marked unchanged were already passing and carry no evidence about the change.",
              "- The confirmation trials were not used to choose the round, so the repeat is not selected by luck."]
    L += ["", "## Conditions held fixed", "", f"- Tool policy `{json.dumps(cfg['tool_policy'])}`.", f"- Evaluators {', '.join(eval_key(e) + ' (' + e['kind'] + ', ' + e['role'] + ')' for e in cfg['evaluators'])}.",
          f"- Agent public run spec hash (first 16): {', '.join(sorted(h[:16] for h in approved)) or 'n/a'} at every start; now {h_now[:16]}. Drift observed: {'yes' if drift else 'no'}.",
          "", "## Limits", "",
          "- Results are on this curated cohort; the rounds were chosen adaptively on these cases. They are not a production reliability estimate.",
          "- Binary evaluators grade what they grade; unmeasured qualities stay unmeasured. An LLM judge adds its own variance to pass^k unless validated.",
          "- Replays are fresh model calls from stored inputs under the tool policy; they do not restore the original execution. Hosted model aliases can change.",
          f"- Total estimated model cost across {sum(len(s['runs']) for s in sums)} runs: ${total:.5f}.", "",
          "Per-run files: `<experiment>/runK_plan.json`, `runK_comparison.json` (per-case rows with evaluations, replies, cost), `runK_jobs.json`, `runK_run.json`."]
    (root / "results.md").write_text("\n".join(L) + "\n")
    print(json.dumps({"wrote": str(root / "results.md"), "rounds": len(sums), "runs": sum(len(s["runs"]) for s in sums), "winner_round": winner["round"] if winner else None, "drift": drift, "total_cost_usd": f"{total:.5f}"}))


# ----------------------------------------------------------------------------- cli

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help=f"path to the loop config (default ./{CONFIG_NAME})")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init"); s.add_argument("--from", dest="frm", required=True, help="JSON file with name, agent_version, cohort_version_id, evaluators, tool_policy, criteria, ..."); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_init)
    sub.add_parser("preflight").set_defaults(fn=cmd_preflight)
    w = sub.add_parser("worker"); w.add_argument("action", choices=["start", "stop", "status"]); w.set_defaults(fn=cmd_worker)
    r = sub.add_parser("round"); r.add_argument("action", choices=["start"]); r.add_argument("--round", type=int, required=True)
    r.add_argument("--prompt-file"); r.add_argument("--model"); r.add_argument("--params", help="JSON object of model parameters"); r.add_argument("--change", help="one sentence: the change and the failure it targets")
    r.add_argument("--ignore-budget", action="store_true"); r.set_defaults(fn=cmd_round)
    s = sub.add_parser("watch"); s.add_argument("run_id"); s.add_argument("--interval", type=float, default=5); s.add_argument("--timeout", type=float, default=1800); s.set_defaults(fn=cmd_watch)
    s = sub.add_parser("collect"); s.add_argument("run_id"); s.set_defaults(fn=cmd_collect)
    sub.add_parser("next").set_defaults(fn=cmd_next)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    a = p.parse_args(); a.fn(a)


if __name__ == "__main__":
    main()
