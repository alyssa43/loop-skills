#!/usr/bin/env python3
"""Run the Evalve MVP loop against Kitaru. See ../references/handoff.md."""

from __future__ import annotations

import argparse
import difflib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

TERMINAL_STATUSES = {"completed", "failed", "canceled"}
SETTINGS_PREFIX = "Evalve loop: "
RESULT_PREFIX = "Evalve result: "


def compact(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(compact(value).encode()).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    # The old state survives a crash before the replacement.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        temporary = Path(temp.name)
        try:
            json.dump(value, temp, indent=2)
            temp.write("\n")
            temp.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def dollars(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise SystemExit(f"Invalid dollar amount: {value!r}") from None
    if not result.is_finite() or result < 0:
        raise SystemExit("Dollar amounts must be finite and nonnegative.")
    return result


def version_ref(ref: str) -> tuple[str, int]:
    if not isinstance(ref, str) or not re.fullmatch(r"\S(?:[^\r\n]*\S)?@[1-9][0-9]*", ref):
        raise SystemExit(f"Use an exact name@version reference, got {ref!r}.")
    name, version = ref.rsplit("@", 1)
    return name, int(version)


def load_config(path: Path) -> dict:
    cfg = read_json(path)
    for key in ("name", "cohort_version_id", "evaluators", "tool_policy", "allowed_changes",
                "required_consecutive_greens", "max_attempts", "budget_usd"):
        if key not in cfg:
            raise SystemExit(f"Missing {key!r} in {path}. See references/handoff.md.")
    cfg.setdefault("kitaru", "kitaru")
    cfg.setdefault("allowed_models", [])
    cfg.setdefault("evidence_dir", "sessions/evalve-loop")
    if not isinstance(cfg["name"], str) or not (slug := re.sub(r"[^a-z0-9]+", "-", cfg["name"].lower()).strip("-")):
        raise SystemExit("Give the loop a name containing letters or numbers.")
    try:
        cfg["cohort_version_id"] = str(uuid.UUID(cfg["cohort_version_id"]))
    except (ValueError, TypeError, AttributeError):
        raise SystemExit("cohort_version_id must be the exact cohort-version UUID.") from None
    required, cap = cfg["required_consecutive_greens"], cfg["max_attempts"]
    if type(required) is not int or type(cap) is not int or not 1 <= required <= cap:
        raise SystemExit("The streak and attempt cap must be positive integers, with the cap at least the streak.")
    if dollars(cfg["budget_usd"]) <= 0:
        raise SystemExit("budget_usd must be positive.")
    cfg["budget_usd"] = str(dollars(cfg["budget_usd"]))

    changes = cfg["allowed_changes"]
    if not isinstance(changes, list) or not changes or any(x not in ("system_prompt", "model") for x in changes):
        raise SystemExit("allowed_changes must contain system_prompt, model, or both.")
    models = cfg["allowed_models"]
    if not isinstance(models, list) or any(not isinstance(x, str) or not x.strip() for x in models):
        raise SystemExit("allowed_models must contain model IDs.")
    if "model" in changes and not models:
        raise SystemExit("Supply the model IDs selected in the form before allowing model changes.")
    if cfg.get("agent_version") is not None:
        version_ref(cfg["agent_version"])
    if cfg.get("baseline_model") is not None and (
        not isinstance(cfg["baseline_model"], str) or not cfg["baseline_model"].strip()
    ):
        raise SystemExit("baseline_model must be the inherited model ID.")
    evaluators = cfg["evaluators"]
    if not isinstance(evaluators, list) or not evaluators:
        raise SystemExit("Select at least one binary evaluator.")
    seen = set()
    for evaluator in evaluators:
        if not isinstance(evaluator, dict):
            raise SystemExit("Each evaluator must provide a ref and optional params.")
        ref = version_ref(evaluator.get("ref"))
        if ref in seen or not isinstance(evaluator.get("params", {}), dict):
            raise SystemExit("Evaluator references must be unique and params must be objects.")
        seen.add(ref)
    policy = cfg["tool_policy"]
    if not isinstance(policy, dict) or not isinstance(policy.get("default"), dict):
        raise SystemExit("Supply the project's replay tool policy, including its default.")
    if not isinstance(policy.get("tools", {}), dict):
        raise SystemExit("tool_policy.tools must be an object.")
    policy.setdefault("tools", {})
    if any(not isinstance(p, dict) or not p.get("type") for p in [policy["default"], *policy["tools"].values()]):
        raise SystemExit("Every replay tool policy must specify a type.")
    if cfg.get("baseline_estimate_usd") is not None:
        cfg["baseline_estimate_usd"] = str(dollars(cfg["baseline_estimate_usd"]))
    cfg["_hash"] = digest(cfg)
    cfg["_project"] = path.resolve().parent
    cfg["_dir"] = cfg["_project"] / cfg["evidence_dir"] / slug
    cfg["_slug"] = slug
    return cfg


def kitaru(cfg: dict, *args: str, check: bool = True) -> dict:
    env = dict(os.environ, KITARU_NON_INTERACTIVE="1")
    env.setdefault("KITARU_ACTIVE_SKILL", "evalve-loop")
    try:
        proc = subprocess.run(
            [cfg["kitaru"], *args, "-o", "json", "--no-browser"],
            cwd=cfg["_project"], env=env, capture_output=True, text=True,
        )
    except OSError as exc:
        raise SystemExit(f"Could not start Kitaru: {exc}") from None
    data = None
    for stream in (proc.stdout, proc.stderr):
        try:
            envelope = json.loads(stream)
        except json.JSONDecodeError:
            continue
        if isinstance(envelope, dict) and "ok" in envelope:
            data = envelope
            break
    if data is None:
        raise SystemExit(f"Kitaru {' '.join(args[:3])} returned no JSON: {proc.stderr[-500:]}") from None
    if check and not data.get("ok"):
        message = (data.get("error") or {}).get("message", "see the Kitaru CLI output")
        raise SystemExit(f"Kitaru {' '.join(args[:3])} failed: {message}")
    return data


def item(cfg: dict, *args: str) -> dict:
    return kitaru(cfg, *args)["item"]


def paged(cfg: dict, *args: str):
    cursor = None
    while True:
        page_args = ["--size", "500"]
        if cursor:
            page_args += ["--cursor", cursor]
        data = kitaru(cfg, *args, *page_args)
        yield from data["items"]
        cursor = (data.get("page") or {}).get("next_cursor")
        if not cursor:
            return


def load_state(cfg: dict) -> dict:
    path = cfg["_dir"] / "state.json"
    if not path.is_file():
        raise SystemExit("Run preflight before starting the loop.")
    state = read_json(path)
    if state["config_hash"] != cfg["_hash"]:
        raise SystemExit("The config changed after preflight. Restore it or use a new loop name.")
    return state


def save_state(cfg: dict, state: dict) -> None:
    write_json(cfg["_dir"] / "state.json", state)


def resolve_agent_version(cfg: dict, agent_id: str, members: list[dict]) -> tuple[str, dict]:
    ref = cfg.get("agent_version")
    if ref:
        version = item(cfg, "agent", "version", "get", ref)
    else:
        recorded = {session.get("agent_version_id") for session in members}
        if len(recorded) != 1 or None in recorded:
            raise SystemExit("The cohort has mixed or missing recorded versions. Ask which agent_version to use.")
        recorded_id = next(iter(recorded))
        version = next((v for v in paged(cfg, "agent", "version", "list", agent_id) if v["id"] == recorded_id), None)
        if version is None or not version.get("run_spec"):
            raise SystemExit("The cohort's recorded version is unavailable for replay. Ask which agent_version to use.")
    if version["agent_id"] != agent_id or not version.get("run_spec"):
        raise SystemExit("Choose a runnable agent_version that belongs to the cohort's agent.")
    return f"{agent_id}@{version['version']}", version


def evaluator_specs(cfg: dict, agent_id: str) -> list[dict]:
    result = []
    for evaluator in cfg["evaluators"]:
        ref = evaluator["ref"]
        name, version = version_ref(ref)
        parent = item(cfg, "evaluator", "get", name)
        if parent.get("agent_id") not in (None, agent_id):
            raise SystemExit(f"Evaluator {ref} belongs to a different agent.")
        pinned = item(cfg, "evaluator", "version", "get", ref)
        result.append({
            "evaluator": parent["name"], "version": version, "version_id": pinned["id"],
            "params": evaluator.get("params", {}), "connection_id": evaluator.get("connection_id"),
        })
    if len({e["version_id"] for e in result}) != len(result):
        raise SystemExit("Select each evaluator version once.")
    return result


def preflight(cfg: dict, _args) -> dict:
    if (cfg["_dir"] / "state.json").exists():
        state = load_state(cfg)
        return state["preflight"]
    status = item(cfg, "status")
    if status.get("authentication") != "authenticated" or not status.get("server_url"):
        raise SystemExit("Log in to the selected Kitaru server before preflight.")
    cohort_version = item(cfg, "cohort", "version", "get", cfg["cohort_version_id"])
    cohort = item(cfg, "cohort", "get", cohort_version["cohort_id"])
    agent_id = cohort["agent_id"]
    members = list(paged(cfg, "session", "list", "--cohort", cfg["cohort_version_id"]))
    if not members or len({m["id"] for m in members}) != cohort_version["session_count"]:
        raise SystemExit("Could not read the complete nonempty cohort membership.")
    ref, version = resolve_agent_version(cfg, agent_id, members)
    spec = version["run_spec"]
    caps = spec.get("runtime_capabilities") or {}
    if caps.get("overrides") is False:
        raise SystemExit("The selected agent version cannot apply prompt or model overrides.")
    policies = [cfg["tool_policy"]["default"], *cfg["tool_policy"]["tools"].values()]
    if caps.get("tool_policies") is False and any(p.get("type") != "passthrough" for p in policies):
        raise SystemExit("The selected agent version cannot apply the configured tool policy.")
    evaluators = evaluator_specs(cfg, agent_id)
    estimate = cfg.get("baseline_estimate_usd")
    if estimate is None and all(m.get("agent_version_id") == version["id"] and m.get("cost") is not None for m in members):
        estimate = str(sum(dollars(m["cost"]) for m in members))
    baseline_prompt = cfg.get("baseline_prompt_file")
    baseline_model = cfg.get("baseline_model") or (spec.get("env") or {}).get("OPENAI_MODEL")
    notes = []
    if any(p.get("type") == "passthrough" or p.get("on_miss") == "passthrough" for p in policies):
        notes.append("The replay policy can call live tools. Check that this is the intended project policy.")
    if estimate is None:
        notes.append("No baseline cost estimate is available. Resolve one before the first attempt.")
    checked = {
        "server_url": status["server_url"], "live_worker_count": status.get("live_worker_count"),
        "agent_id": agent_id, "agent_version": ref, "agent_version_id": version["id"],
        "cohort_version_id": cfg["cohort_version_id"], "cohort_size": len(members),
        "evaluators": [f"{e['evaluator']}@{e['version']}" for e in evaluators],
        "tool_policy": cfg["tool_policy"], "baseline_estimate_usd": estimate, "baseline_model": baseline_model, "notes": notes,
    }
    state = {
        "config_hash": cfg["_hash"], "loop_id": uuid.uuid4().hex, "preflight": checked,
        "members": {m["id"]: m.get("name") or m["id"] for m in members}, "evaluators": evaluators,
        "run_spec_hash": digest({"run_spec": spec, "capabilities": version.get("capabilities")}),
        "baseline_model": baseline_model,
        "baseline_prompt": (cfg["_project"] / baseline_prompt).read_text(encoding="utf-8") if baseline_prompt else None,
        "laps": [],
    }
    save_state(cfg, state)
    return checked


def green_streak(attempts: list[dict]) -> int:
    count = 0
    for attempt in reversed(attempts):
        if (attempt["result"] or {}).get("outcome") != "green":
            break
        count += 1
    return count


def accounted_spend(state: dict) -> tuple[Decimal, Decimal]:
    recorded = Decimal(0)
    charged = Decimal(0)
    for lap in state["laps"]:
        for attempt in lap["attempts"]:
            if result := attempt["result"]:
                cost = dollars(result["recorded_cost_usd"])
                recorded += cost
                charged += max(cost, dollars(attempt["estimated_cost_usd"])) if result["cost_incomplete"] else cost
    return recorded, charged


def estimate_attempt(state: dict, override: dict) -> Decimal | None:
    # A different model needs its own estimate. The baseline model's historical costs
    # can also inform prompt candidates, with the largest observed attempt as a floor.
    model = override.get("model", state["baseline_model"])
    estimate = state["preflight"]["baseline_estimate_usd"]
    estimates = [dollars(estimate)] if estimate is not None and model == state["baseline_model"] else []
    for lap in state["laps"]:
        if lap["override"].get("model", state["baseline_model"]) == model:
            for attempt in lap["attempts"]:
                estimates.append(dollars(attempt["estimated_cost_usd"]))
                if result := attempt["result"]:
                    estimates.append(dollars(result["recorded_cost_usd"]))
    return max(estimates, default=None)


def decide(cfg: dict, state: dict) -> dict:
    laps = state["laps"]
    lap = laps[-1] if laps else None
    attempts = lap["attempts"] if lap else []
    recorded, charged = accounted_spend(state)
    summary = {
        "attempts": sum(len(l["attempts"]) for l in laps), "recorded_cost_usd": str(recorded),
        "accounted_cost_usd": str(charged), "green_streak": green_streak(attempts),
    }

    def decision(action: str, **details):
        return {**summary, "action": action, **details}

    if attempts and attempts[-1]["result"] is None:
        run_id = attempts[-1]["run_id"]
        return decision("collect" if run_id else "resume_start", lap=lap["number"], run_id=run_id)
    if summary["green_streak"] >= cfg["required_consecutive_greens"]:
        return decision("stop_success", lap=lap["number"])
    if state.get("budget_stop"):
        return decision("stop_budget", reason=state["budget_stop"])
    if charged >= dollars(cfg["budget_usd"]):
        return decision("stop_budget", reason="The replay budget was reached.")
    if summary["attempts"] >= cfg["max_attempts"]:
        return decision("stop_attempt_cap", reason="The total replay attempt cap was reached.")
    if not attempts:
        return decision("start_baseline", lap=0)
    outcomes = [a["result"]["outcome"] for a in attempts]
    baseline_complete = lap["number"] == 0 and sum(x != "incomplete" for x in outcomes) >= cfg["required_consecutive_greens"]
    if (baseline_complete and "red" in outcomes) or (lap["number"] > 0 and outcomes[-1] == "red"):
        return decision("start_candidate", lap=lap["number"] + 1)
    return decision("retry" if outcomes[-1] == "incomplete" else "replay", lap=lap["number"])


def metadata(prefix: str, description: str | None) -> dict:
    line = next((line for line in (description or "").splitlines() if line.startswith(prefix)), None)
    return json.loads(line[len(prefix):]) if line else {}


def description(cfg: dict, state: dict, lap: dict) -> str:
    number = lap["number"]
    change = " ".join(lap["change"].splitlines())
    lines = [f"Round {number}: {change}"]
    settings = {"schema_version": 1, "loop_id": state["loop_id"], "lap": number}
    if number == 0:
        settings.update({key: cfg[key] for key in (
            "name", "cohort_version_id", "required_consecutive_greens", "max_attempts",
            "budget_usd", "allowed_changes", "allowed_models",
        )})
        settings["agent_version_id"] = state["preflight"]["agent_version_id"]
        settings["evaluators"] = state["preflight"]["evaluators"]
    lines.append(SETTINGS_PREFIX + compact(settings))
    return "\n".join(lines)


def without_nulls(value):
    if isinstance(value, dict):
        return {key: without_nulls(v) for key, v in value.items() if v is not None}
    if isinstance(value, list):
        return [without_nulls(v) for v in value]
    return value


def ensure_experiment(cfg: dict, state: dict, lap: dict) -> str:
    name = f"{cfg['_slug']}-round-{lap['number']}"
    response = kitaru(cfg, "experiment", "get", lap["experiment_id"] or name, check=False)
    error = response.get("error") or {}
    if response.get("ok"):
        experiment = response["item"]
    elif not lap["experiment_id"] and error.get("kind") == "not_found":
        args = ["experiment", "create", name, "--agent", state["preflight"]["agent_id"],
                "--description", description(cfg, state, lap), "--tool-policy", compact(cfg["tool_policy"]),
                "--idempotency-key", f"evalve-{state['loop_id']}-lap-{lap['number']}"]
        if lap["override"]:
            args += ["--override", compact(lap["override"])]
        for evaluator in state["evaluators"]:
            ref = f"{evaluator['evaluator']}@{evaluator['version']}"
            args += ["--evaluator", ref]
            if evaluator["params"]:
                args += ["--evaluator-params", f"{ref}={compact(evaluator['params'])}"]
            if evaluator["connection_id"]:
                args += ["--evaluator-connection", f"{ref}={evaluator['connection_id']}"]
        experiment = item(cfg, *args)
    else:
        message = error.get("message", "read failed")
        raise SystemExit(f"Could not read {name}: {message}")

    settings = metadata(SETTINGS_PREFIX, experiment.get("description"))
    if settings.get("loop_id") != state["loop_id"] or settings.get("lap") != lap["number"]:
        raise SystemExit(f"An experiment named {name} already belongs to another run. Choose a new loop name.")
    if (experiment.get("agent_id") != state["preflight"]["agent_id"]
            or without_nulls(experiment.get("override") or {}) != lap["override"]
            or without_nulls(experiment.get("tool_policy")) != without_nulls(cfg["tool_policy"])):
        raise SystemExit(f"Stored experiment configuration changed for {name}.")
    fields = ("evaluator", "version", "params", "connection_id")
    if sorted(compact({k: e.get(k) for k in fields}) for e in experiment["evaluators"]) != sorted(
        compact({k: e.get(k) for k in fields}) for e in state["evaluators"]
    ):
        raise SystemExit(f"Stored evaluator settings changed for {name}.")
    return experiment["id"]


def start(cfg: dict, args) -> dict:
    state = load_state(cfg)
    step = decide(cfg, state)
    action = step["action"]
    requested_change = any(value is not None for value in (args.model, args.prompt_file, args.change, args.baseline_model))
    if action.startswith("stop_") or action == "collect":
        return step
    status = item(cfg, "status")
    if status.get("server_url") != state["preflight"]["server_url"]:
        raise SystemExit("Select the Kitaru server that was used in preflight.")
    ref = state["preflight"]["agent_version"]
    version = item(cfg, "agent", "version", "get", ref)
    if (version["id"] != state["preflight"]["agent_version_id"]
            or digest({"run_spec": version.get("run_spec"), "capabilities": version.get("capabilities")}) != state["run_spec_hash"]):
        raise SystemExit("The pinned agent run spec changed after preflight.")

    if action == "resume_start":
        if requested_change or args.estimate_usd is not None:
            raise SystemExit("Resume the pending submission with plain start. Its configuration is already saved.")
        lap = state["laps"][-1]
        attempt = lap["attempts"][-1]
    else:
        number = step["lap"]
        if action == "start_candidate":
            if not args.change or not args.change.strip() or (args.model is None and args.prompt_file is None):
                raise SystemExit("Start a candidate with one of --model or --prompt-file and a --change summary.")
            if args.model is not None:
                if "model" not in cfg["allowed_changes"] or args.model not in cfg["allowed_models"]:
                    raise SystemExit("The candidate model must be selected in the form.")
                baseline_model = state["baseline_model"] or args.baseline_model
                if not baseline_model or not baseline_model.strip():
                    raise SystemExit("Resolve the inherited model and supply --baseline-model before trying a model candidate.")
                if args.baseline_model and args.baseline_model != baseline_model:
                    raise SystemExit("The inherited model is already pinned for this loop.")
                if args.model == baseline_model:
                    raise SystemExit("Choose a model that differs from the baseline.")
                state["baseline_model"] = baseline_model
                override = {"model": args.model}
            else:
                if args.baseline_model is not None:
                    raise SystemExit("Use --baseline-model with a model candidate.")
                if "system_prompt" not in cfg["allowed_changes"]:
                    raise SystemExit("The form must enable system prompt changes.")
                prompt = (cfg["_project"] / args.prompt_file).read_text(encoding="utf-8")
                if not prompt.strip() or prompt == state["baseline_prompt"]:
                    raise SystemExit("Provide a nonempty system prompt that changes the baseline.")
                override = {"system_prompt": prompt}
            if any(l["override"] == override for l in state["laps"]):
                raise SystemExit("This override has already been tried. Choose a change based on the failed cases.")
            lap = {"number": number, "override": override, "change": args.change.strip(), "experiment_id": None, "attempts": []}
        elif action == "start_baseline":
            lap = {"number": 0, "override": {}, "change": "Unchanged baseline", "experiment_id": None, "attempts": []}
        else:
            lap = state["laps"][-1]

        if action != "start_candidate" and requested_change:
            raise SystemExit("Start or replay this lap with its existing configuration. Omit change flags.")
        estimate = estimate_attempt(state, lap["override"])
        if args.estimate_usd is not None:
            supplied = dollars(args.estimate_usd)
            estimate = max(supplied, estimate) if estimate is not None else supplied
        if estimate is None:
            raise SystemExit("Resolve the cost of one full-cohort replay and supply start --estimate-usd USD.")
        if dollars(step["accounted_cost_usd"]) + estimate > dollars(cfg["budget_usd"]):
            state["budget_stop"] = f"The next replay was estimated at ${estimate} and would exceed the budget."
            save_state(cfg, state)
            return decide(cfg, state)

        if action in ("start_baseline", "start_candidate"):
            state["laps"].append(lap)
        attempt = {
            "number": len(lap["attempts"]) + 1, "run_id": None, "result": None,
            "estimated_cost_usd": str(estimate),
        }
        lap["attempts"].append(attempt)
        save_state(cfg, state)

    lap["experiment_id"] = ensure_experiment(cfg, state, lap)
    save_state(cfg, state)
    response = item(
        cfg, "experiment", "run", "start", lap["experiment_id"], "--cohort-version", cfg["cohort_version_id"],
        "--agent", ref, "--baseline-evaluation-mode", "none",
        "--idempotency-key", f"evalve-{state['loop_id']}-lap-{lap['number']}-run-{attempt['number']}",
    )
    run = response.get("run") or response
    attempt["run_id"] = run["id"]
    save_state(cfg, state)
    return {"lap": lap["number"], "experiment_id": lap["experiment_id"], "run_id": run["id"],
            "estimated_cost_usd": attempt["estimated_cost_usd"], "status": run["status"]}


def watch(cfg: dict, args) -> dict:
    data = kitaru(cfg, "experiment", "run", "watch", args.run_id, "--timeout", str(args.timeout), check=False)
    receipt = data.get("item") or {}
    run = receipt.get("run") or receipt
    return {"run_id": args.run_id, "status": run.get("status"), "terminal": bool(receipt.get("terminal")),
            "error": run.get("error") or (data.get("error") or {}).get("message")}


def collect_result(cfg: dict, state: dict, run: dict) -> dict:
    run_filter = compact({"field": "experiment_run_id", "op": "eq", "value": run["id"]})
    replays = list(paged(cfg, "replay", "list", "--filter", run_filter))
    result_ids = {r["result_session_id"] for r in replays if r.get("result_session_id")}
    expected = {e["version_id"]: e for e in state["evaluators"]}
    evaluations = {}
    if result_ids:
        for row in paged(cfg, "evaluation", "list", "--filter", run_filter, "--sort", "created:asc"):
            session_id = row["session_id"]
            evaluator_id = row.get("evaluator_version_id")
            evaluator = expected.get(evaluator_id)
            if session_id in result_ids and evaluator and (row.get("evaluator_params") or {}) == evaluator["params"]:
                evaluations.setdefault(session_id, {})[evaluator_id] = row

    sessions = {session_id: item(cfg, "session", "get", session_id) for session_id in result_ids}
    rows = []
    for replay in replays:
        baseline_id = replay["baseline_session_id"]
        result_id = replay.get("result_session_id")
        session = sessions.get(result_id, {})
        scored = evaluations.get(result_id, {})
        verdicts = {
            f"{e['evaluator']}@{e['version']}": {
                "id": scored.get(eid, {}).get("id"), "passed": scored.get(eid, {}).get("passed"),
                "explanation": scored.get(eid, {}).get("explanation"),
            }
            for eid, e in expected.items()
        }
        complete = (
            replay["status"] == "completed" and session.get("status") == "completed" and not session.get("error")
            and all(type(verdict["passed"]) is bool for verdict in verdicts.values())
        )
        rows.append({
            "case": state["members"].get(baseline_id, baseline_id), "baseline_session_id": baseline_id,
            "replay_id": replay["id"], "replay_status": replay["status"], "result_session_id": result_id,
            "result_status": session.get("status"), "error": replay.get("error") or session.get("error"),
            "passed": all(v["passed"] for v in verdicts.values()) if complete else None,
            "evaluations": verdicts, "reply": session.get("outputs"), "cost_usd": session.get("cost"),
        })
    seen = {row["baseline_session_id"] for row in rows}
    expected_members = state["members"].keys()
    valid = (
        run["status"] == "completed" and seen == expected_members and len(rows) == len(expected_members)
        and len(result_ids) == len(expected_members) and all(row["passed"] is not None for row in rows)
    )
    failed = sum(row["passed"] is False for row in rows)
    recorded = sum((dollars(s["cost"]) for s in sessions.values() if s.get("cost") is not None), Decimal(0))
    return {
        "run_id": run["id"], "run_status": run["status"], "run_error": run.get("error"),
        "outcome": ("red" if failed else "green") if valid else "incomplete",
        "passed": sum(row["passed"] is True for row in rows), "failed": failed,
        "unscored": len(expected_members - seen) + sum(row["passed"] is None for row in rows),
        "missing_session_ids": sorted(expected_members - seen),
        "recorded_cost_usd": str(recorded),
        "cost_incomplete": len(sessions) != len(expected_members) or any(s.get("cost") is None for s in sessions.values()),
        "rows": rows,
    }


def lap_summaries(state: dict) -> list[dict]:
    result = []
    for lap in state["laps"]:
        attempts = lap["attempts"]
        outcomes = [(a["result"] or {}).get("outcome") for a in attempts]
        runs = []
        for attempt in attempts:
            if attempt["run_id"]:
                scored = attempt["result"] or {}
                runs.append({"run_id": attempt["run_id"], **{
                    key: scored.get(key) for key in ("outcome", "passed", "failed", "unscored")
                }})
        result.append({
            "lap": lap["number"], "experiment_id": lap["experiment_id"], "change": lap["change"],
            "runs": runs, "attempts": len(attempts),
            "green": outcomes.count("green"), "red": outcomes.count("red"), "incomplete": outcomes.count("incomplete"),
            "green_streak": green_streak(attempts),
        })
    return result


def result_metadata(cfg: dict, state: dict) -> dict:
    step = decide(cfg, state)
    return {
        "schema_version": 1, "loop_id": state["loop_id"], "status": step["action"] if step["action"].startswith("stop_") else "running",
        "winner_lap": step.get("lap") if step["action"] == "stop_success" else None,
        "baseline_model": state["baseline_model"],
        "attempts": step["attempts"], "recorded_cost_usd": step["recorded_cost_usd"],
        "accounted_cost_usd": step["accounted_cost_usd"], "reason": step.get("reason"), "laps": lap_summaries(state),
    }


def publish(cfg: dict, state: dict) -> None:
    if not state["laps"] or not state["laps"][0]["experiment_id"]:
        return
    baseline = state["laps"][0]
    experiment = item(cfg, "experiment", "get", baseline["experiment_id"])
    original = experiment.get("description") or ""
    if metadata(SETTINGS_PREFIX, original).get("loop_id") != state["loop_id"]:
        raise SystemExit("The baseline experiment's loop metadata changed. Local evidence is available.")
    lines = [line for line in original.splitlines() if not line.startswith(RESULT_PREFIX)]
    updated = "\n".join([*lines, RESULT_PREFIX + compact(result_metadata(cfg, state))])
    if updated != original:
        item(cfg, "experiment", "update", baseline["experiment_id"], "--description", updated)


def collect(cfg: dict, args) -> dict:
    state = load_state(cfg)
    for lap in state["laps"]:
        for attempt in lap["attempts"]:
            if attempt["run_id"] == args.run_id:
                break
        else:
            continue
        break
    else:
        raise SystemExit("This run ID does not belong to the loop.")

    path = cfg["_dir"] / f"lap-{lap['number']}-run-{attempt['number']}.json"
    if attempt["result"] is None:
        run = item(cfg, "experiment", "run", "get", args.run_id)
        if run["status"] not in TERMINAL_STATUSES:
            raise SystemExit("The run is still in progress. Watch it before collecting.")
        result = collect_result(cfg, state, run)
        write_json(path, result)
        attempt["result"] = {key: value for key, value in result.items() if key != "rows"}
        save_state(cfg, state)
    publish(cfg, state)
    return {**attempt["result"], "evidence": str(path), "next": decide(cfg, state)}


def report(cfg: dict, _args) -> dict:
    state = load_state(cfg)
    result = result_metadata(cfg, state)
    status = {
        "stop_success": f"Lap {result['winner_lap']} reached the green streak.",
        "stop_attempt_cap": "The total replay attempt cap was reached.",
        "stop_budget": result["reason"],
    }.get(result["status"], "Loop in progress.")
    lines = [
        f"# {cfg['name']}", "", status, "",
        f"Agent version `{state['preflight']['agent_version']}` · Cohort `{cfg['cohort_version_id']}`",
        f"Attempts **{result['attempts']}/{cfg['max_attempts']}** · Required streak **{cfg['required_consecutive_greens']}**",
        f"Recorded replay cost **${result['recorded_cost_usd']}** · Budget **${cfg['budget_usd']}**",
        "",
        "| Lap | Change | Green | Red | Incomplete | Experiment ID | Run IDs |",
        "| --- | --- | ---: | ---: | ---: | --- | --- |",
    ]
    for lap in result["laps"]:
        change = " ".join(lap["change"].splitlines()).replace("|", "\\|")
        run_ids = ", ".join(run["run_id"] for run in lap["runs"])
        lines.append(f"| {lap['lap']} | {change} | {lap['green']} | {lap['red']} | {lap['incomplete']} | "
                     f"{lap['experiment_id'] or ''} | {run_ids} |")
    if result["winner_lap"] is not None:
        winner = state["laps"][result["winner_lap"]]
        if winner["override"]:
            lines += ["", "## Winning override", "", "```json", json.dumps(winner["override"], indent=2), "```"]
        if "model" in winner["override"]:
            baseline = state["baseline_model"] or "Unavailable"
            lines += ["", f"Baseline model `{baseline}` → candidate model `{winner['override']['model']}`"]
        if "system_prompt" in winner["override"] and state["baseline_prompt"] is not None:
            changes = difflib.unified_diff(
                state["baseline_prompt"].splitlines(), winner["override"]["system_prompt"].splitlines(),
                fromfile="baseline prompt", tofile="winning prompt", lineterm="",
            )
            lines += ["", "~~~diff", *changes, "~~~"]
    if any(
        (attempt["result"] or {}).get("cost_incomplete")
        for lap in state["laps"] for attempt in lap["attempts"]
    ):
        lines += ["", f"Accounted spend **${result['accounted_cost_usd']}** includes estimates where replay costs were missing."]
    lines += ["", "The cost check runs between replays and uses Kitaru's recorded agent replay costs. "
              "See the JSON evidence for per-session results and evaluator explanations."]
    path = cfg["_dir"] / "results.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_json(cfg["_dir"] / "results.json", result)
    publish(cfg, state)
    return {**result, "report": str(path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("evalve-loop.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight").set_defaults(handler=preflight)
    commands.add_parser("next").set_defaults(handler=lambda cfg, _: decide(cfg, load_state(cfg)))
    runner = commands.add_parser("start", help="Start the next replay or resume a pending submission.")
    change = runner.add_mutually_exclusive_group()
    change.add_argument("--prompt-file", type=Path)
    change.add_argument("--model")
    runner.add_argument("--change", help="Describe the change and the failure it targets.")
    runner.add_argument("--baseline-model", help="Inherited model ID, if not discovered before a model candidate.")
    runner.add_argument("--estimate-usd", help="Estimate for one full-cohort replay of this configuration.")
    runner.set_defaults(handler=start)
    watcher = commands.add_parser("watch")
    watcher.add_argument("run_id")
    watcher.add_argument("--timeout", type=float, default=1800)
    watcher.set_defaults(handler=watch)
    collector = commands.add_parser("collect")
    collector.add_argument("run_id")
    collector.set_defaults(handler=collect)
    commands.add_parser("report").set_defaults(handler=report)
    args = parser.parse_args()
    cfg = load_config(args.config)
    if args.command == "watch":
        result = args.handler(cfg, args)
    else:
        cfg["_dir"].mkdir(parents=True, exist_ok=True)
        with (cfg["_dir"] / ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit("Another command is updating this loop. Try again when it finishes.") from None
            result = args.handler(cfg, args)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
