"""Scenario orchestration — combines prompts, validators and the retry loop.

Each ``generate_*`` function is pure (no Flask/DB): it takes the scenario
payload, assembles context (including the deterministic C source index) and
returns a validated output dict plus the generation log. The HTTP layer
persists this as an ``AiDraft``; ``apply_draft`` (in :mod:`.apply`) then
routes approved outputs through the existing service layer.
"""

from __future__ import annotations

from typing import Any, Callable

from . import base, c_index, prompts, provider, registry as registry_mod, sparse as sparse_mod, validators  # noqa: F401

# Context ceilings (chars) — prompts stay small and focused on purpose.
_MAX_SOURCE_CHARS = 8000
_MAX_LOG_CHARS = 6000

# Async progress hook: scenarios stay pure (no Flask/DB) — the caller (the
# Huey task) supplies a callback that persists the events it cares about.
EventFn = Callable[[dict[str, Any]], None]


def _emit(on_event: EventFn | None, **event: Any) -> None:
    if on_event is not None:
        try:
            on_event(event)
        except Exception:  # noqa: BLE001 - progress reporting must never fail a run
            pass


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...（截断，原文 {len(text)} 字符）"


# --------------------------------------------------------------------------- #
# viewpoint
# --------------------------------------------------------------------------- #
def generate_viewpoint(payload: dict[str, Any]) -> base.GenerationResult:
    doc_text = payload.get("doc_text") or payload.get("doc") or ""
    if not str(doc_text).strip():
        raise ValueError("doc_text（设计书内容）不能为空")

    def build(feedback: list[str]):
        return prompts.viewpoint_messages(
            _clip(str(doc_text), _MAX_SOURCE_CHARS),
            module_hint=str(payload.get("module_hint") or ""),
            feedback=feedback)

    return base.generate_validated(build_prompt=build,
                                   validate=validators.validate_viewpoints)


# --------------------------------------------------------------------------- #
# procedure：两阶段（规划 → 批量稀疏手顺 → 展开）
# --------------------------------------------------------------------------- #
_PLAN_CHUNK = 40     # plans are tiny; a module fits in one call
_STEP_CHUNK = 8      # procedures are chunked to stay clear of max_tokens


def _normalise_viewpoints(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload.get("viewpoints"), list) and payload["viewpoints"]:
        raw = payload["viewpoints"]
    elif isinstance(payload.get("viewpoint"), dict) and payload["viewpoint"]:
        raw = [payload["viewpoint"]]  # legacy single-viewpoint payload
    else:
        raise ValueError("viewpoints（测试观点列表）不能为空")
    viewpoints = []
    for i, vp in enumerate(raw):
        if not isinstance(vp, dict):
            raise ValueError(f"viewpoints[{i}] 必须是对象")
        item = dict(vp)
        item.setdefault("ref", vp.get("case_id") or str(i + 1))
        viewpoints.append(item)
    return viewpoints


def _viewpoint_seeds(viewpoints: list[dict[str, Any]]) -> list[Any]:
    """Design-doc terms paired to code names, carried by viewpoint rows.

    A viewpoint extracted from the design doc may record its variables as
    ``[[表示名, 变量名], ...]`` or ``["变量名", ...]`` — either shape seeds
    the registry (the semantic bridge for a cold-start project).
    """
    seeds: list[Any] = []
    for vp in viewpoints:
        for var in vp.get("variables") or []:
            if isinstance(var, (list, tuple)) and len(var) == 2:
                seeds.append([str(var[1]), str(var[0])])  # [表示名, path]
            elif isinstance(var, str):
                seeds.append(var)
    return seeds


def _validate_plans(parsed: Any, registry: "registry_mod.Registry",
                    refs: set[str]) -> list[str]:
    if not isinstance(parsed, dict):
        return ["输出必须是对象"]
    plans = parsed.get("plans")
    if not isinstance(plans, list) or not plans:
        return ["plans 必须是非空数组"]
    problems: list[str] = []
    seen: set[str] = set()
    for i, plan in enumerate(plans):
        if not isinstance(plan, dict):
            problems.append(f"plans[{i}] 必须是对象")
            continue
        ref = str(plan.get("ref") or "")
        if not ref:
            problems.append(f"plans[{i}].ref 不能为空")
        elif ref not in refs:
            problems.append(f"plans[{i}].ref 不在观点列表中：{ref}")
        elif ref in seen:
            problems.append(f"plans[{i}].ref 重复：{ref}")
        else:
            seen.add(ref)
        for key in ("precond", "goal", "expected"):
            values = plan.get(key)
            if values is None:
                if key == "expected":
                    problems.append(f"plans[{i}].expected 不能为空")
                continue
            if not isinstance(values, dict):
                problems.append(
                    f"plans[{i}].{key} 必须是 {{\"路径\": \"值\"}} 对象")
                continue
            for name in values:
                try:
                    registry.resolve(str(name))
                except KeyError:
                    problems.append(
                        f"plans[{i}].{key} 引用了注册表外的信号：{name}")
                except ValueError as exc:
                    problems.append(f"plans[{i}].{key} {exc}")
    missing = refs - seen
    if missing:
        problems.append("以下观点缺少 plan：" + ", ".join(sorted(missing)))
    return problems


def _validate_item(item: Any, plan: dict[str, Any],
                   registry: "registry_mod.Registry",
                   known_subs: set[str]) -> tuple[dict | None, list[str]]:
    """Validate one sparse procedure; returns (steps_doc, problems)."""
    if not isinstance(item, dict):
        return None, ["procedure 条目必须是对象"]
    problems: list[str] = []
    missing_names = {str(m.get("name"))
                     for m in item.get("missing_variables") or []
                     if isinstance(m, dict) and m.get("name")}
    steps = item.get("steps")
    if not isinstance(steps, list) or not steps:
        return None, ["steps 必须是非空数组"]
    for i, step in enumerate(steps):
        if isinstance(step, dict) and step.get("subroutine"):
            sub = step["subroutine"]
            if known_subs and sub not in known_subs:
                problems.append(f"steps[{i}].subroutine 不存在：{sub}")
    doc, expand_problems = sparse_mod.expand_procedure(
        {"steps": steps}, registry, allowed_missing=missing_names)
    problems.extend(expand_problems)
    if problems:
        return None, problems
    # Cross-check against the plan: the procedure must actually drive what
    # the plan says it drives — this is what stops "漂亮但没测到点上".
    referenced = sparse_mod.referenced_paths(doc)
    declared_missing = missing_names
    for key, section in (("precond", "inputs"), ("goal", "inputs"),
                         ("expected", "expecteds")):
        for name in (plan.get(key) or {}):
            try:
                path = registry.resolve(str(name))
            except (KeyError, ValueError):
                continue  # already rejected at the plan stage
            if path not in referenced and name not in declared_missing:
                where = "inputs" if section == "inputs" else "expecteds"
                problems.append(
                    f"plan 的 {key} 变量 {name} 未出现在任何步骤的 {where} 中")
    if problems:
        return None, problems
    return doc, []


def generate_procedure(payload: dict[str, Any],
                       on_event: EventFn | None = None) -> base.GenerationResult:
    viewpoints = _normalise_viewpoints(payload)
    source_files: dict[str, str] = payload.get("source_files") or {}

    # Deterministic layer: clang index + semantic registry (comments, SBS
    # mining, historical pairs, viewpoint seeds, the project signal dict —
    # no human upkeep beyond the optional dictionary).
    index = c_index.index_source(source_files,
                                 compile_args=payload.get("compile_args"))
    reg = registry_mod.build(
        index=index,
        sbs_text=payload.get("sbs_text") or "",
        sbs_variables=payload.get("sbs_variables"),
        historical_pairs=payload.get("historical_pairs"),
        viewpoint_seeds=_viewpoint_seeds(viewpoints),
        signal_dict=payload.get("signal_dict"),
    )
    if len(reg) == 0:
        raise ValueError(
            "信号注册表为空：请提供 source_files（源码）或 sbs_variables"
            "（已登记变量），否则手顺没有可用的信号名")
    registry_lines = reg.prompt_lines()
    lib_functions = payload.get("lib_functions") or []
    known_subs = {f["name"] for f in lib_functions
                  if isinstance(f, dict) and f.get("name")}
    # Fixed, byte-stable module context — NOT per-viewpoint keyword selection,
    # so every chunk and retry round shares the same long prompt prefix
    # (prompt caching) and plans/procedures see the same code.
    source_context = payload.get("source_context")
    if not source_context and source_files:
        per_file = max(1, _MAX_SOURCE_CHARS // len(source_files))
        source_context = "\n\n".join(
            "// " + name + "\n" + _clip(content, per_file)
            for name, content in source_files.items())
    source_context = _clip(str(source_context or ""), _MAX_SOURCE_CHARS)

    log: list[dict[str, Any]] = []
    usage_total: dict[str, int] = {}

    # ---- Phase A: plans (viewpoint → variable/value mapping) ------------- #
    refs = {vp["ref"] for vp in viewpoints}
    _emit(on_event, phase="plan", message="规划中（观点 → 变量/值映射）")

    def build_plan(feedback: list[str]):
        return prompts.plan_messages(
            viewpoints, source_context=source_context,
            registry_lines=registry_lines, lib_functions=lib_functions,
            feedback=feedback)

    plan_result = base.generate_validated(
        build_prompt=build_plan,
        validate=lambda parsed: _validate_plans(parsed, reg, refs),
        max_tokens=4096)
    log.extend({"phase": "plan", **entry} for entry in plan_result.log)
    plans_by_ref = {str(p["ref"]): p for p in plan_result.output["plans"]}
    base.merge_usage(usage_total, plan_result.usage)

    # ---- Phase B: sparse procedures, chunked, per-item targeted retry ---- #
    procedures: list[dict[str, Any]] = []
    failed: list[str] = []
    ordered_refs = [vp["ref"] for vp in viewpoints]
    total_chunks = (len(ordered_refs) + _STEP_CHUNK - 1) // _STEP_CHUNK
    for chunk_no, start in enumerate(range(0, len(ordered_refs), _STEP_CHUNK), 1):
        chunk_refs = ordered_refs[start:start + _STEP_CHUNK]
        collected: dict[str, dict[str, Any]] = {}
        remaining = [plans_by_ref[r] for r in chunk_refs]
        feedback: list[str] = []
        for round_no in range(1, base.DEFAULT_MAX_ROUNDS + 1):
            _emit(on_event, phase="procedures", chunk=chunk_no,
                  total_chunks=total_chunks, round=round_no,
                  message=f"手顺批次 {chunk_no}/{total_chunks} 第 {round_no} 轮")
            messages = prompts.procedure_batch_messages(
                remaining, source_context=source_context,
                registry_lines=registry_lines, lib_functions=lib_functions,
                constant_names=payload.get("constant_names"),
                example_sparse=payload.get("example_sparse"),
                feedback=feedback)
            usage: dict[str, int] = {}
            try:
                text = provider.chat(messages, temperature=0.2, max_tokens=8192,
                                     usage=usage)
                parsed = provider.extract_json(text)
                items = (parsed or {}).get("procedures") if isinstance(parsed, dict) else None
                if not isinstance(items, list):
                    items = []
                    feedback = [f"第 {round_no} 轮输出缺少 procedures 数组"]
                else:
                    feedback = []
            except (provider.ProviderError, ValueError) as exc:
                items = []
                feedback = [f"第 {round_no} 轮解析失败：{exc}"]
            base.merge_usage(usage_total, usage)
            still_bad: list[dict[str, Any]] = []
            for item in items:
                ref = str((item or {}).get("ref") or "")
                plan = plans_by_ref.get(ref)
                if plan is None:
                    feedback.append(f"ref {ref!r} 不在本批观点中，请勿生成")
                    continue
                if ref in collected:
                    continue
                doc, item_problems = _validate_item(item, plan, reg, known_subs)
                if item_problems:
                    feedback.append(
                        f"ref {ref} 的问题：{'；'.join(item_problems)}。请只重新输出该条。")
                    still_bad.append(plan)
                    continue
                entry_out = {"ref": ref, "steps_doc": doc}
                missing = item.get("missing_variables") or []
                if missing:
                    entry_out["missing_variables"] = missing
                collected[ref] = entry_out
            # Plans that produced no item at all this round stay in the retry set.
            for plan in remaining:
                if str(plan["ref"]) not in collected and plan not in still_bad:
                    still_bad.append(plan)
            log.append({"phase": "procedures",
                        "chunk": chunk_refs[0],
                        "round": round_no,
                        "accepted": len(collected),
                        "remaining": len(still_bad),
                        "problems": feedback})
            remaining = still_bad
            if not remaining:
                break
        for r in chunk_refs:
            if r in collected:
                procedures.append(collected[r])
            else:
                failed.append(r)

    output = {"plans": plan_result.output["plans"],
              "procedures": procedures,
              "failed_refs": failed}
    _emit(on_event, phase="done",
          message=f"完成：{len(procedures)} 条手顺，{len(failed)} 条失败")
    from . import config as _config
    cfg = _config.get_ai_config(include_secret=False)
    return base.GenerationResult(
        output=output, rounds=plan_result.rounds + max(1, len(log)), log=log,
        model=cfg.get(_config.KEY_MODEL, ""), usage=usage_total)


# --------------------------------------------------------------------------- #
# sbs
# --------------------------------------------------------------------------- #
def generate_sbs(payload: dict[str, Any]) -> base.GenerationResult:
    source_files: dict[str, str] = payload.get("source_files") or {}
    if not source_files:
        raise ValueError("source_files（源码文件）不能为空")
    index = c_index.index_source(source_files,
                                 compile_args=payload.get("compile_args"))
    known_variables = set(index["variables"])
    per_file_budget = max(1, _MAX_SOURCE_CHARS // len(source_files))
    source_context = "\n\n".join(
        "// " + name + "\n" + _clip(content, per_file_budget)
        for name, content in source_files.items())
    needed = payload.get("needed_variables")

    def build(feedback: list[str]):
        return prompts.sbs_messages(
            _clip(source_context, _MAX_SOURCE_CHARS),
            current_sbs=_clip(str(payload.get("current_sbs") or ""), _MAX_SOURCE_CHARS),
            needed_variables=needed,
            feedback=feedback)

    return base.generate_validated(
        build_prompt=build,
        validate=lambda parsed: validators.validate_sbs(
            parsed, known_variables=known_variables))


# --------------------------------------------------------------------------- #
# lib
# --------------------------------------------------------------------------- #
def generate_lib(payload: dict[str, Any]) -> base.GenerationResult:
    proposal = payload.get("proposal") or payload.get("note") or ""
    if not str(proposal).strip():
        raise ValueError("proposal（人工提议说明）不能为空")
    procedures = payload.get("procedures") or []
    if not procedures:
        raise ValueError("procedures（被标注的手顺）不能为空")
    existing = set(payload.get("existing_lib_names") or [])
    item_ids = {p.get("item_id") for p in procedures if isinstance(p, dict)}

    def build(feedback: list[str]):
        return prompts.lib_messages(
            str(proposal), procedures=procedures,
            existing_lib_names=sorted(existing), feedback=feedback)

    return base.generate_validated(
        build_prompt=build,
        validate=lambda parsed: validators.validate_lib(
            parsed, existing_lib_names=existing, item_ids=item_ids))


# --------------------------------------------------------------------------- #
# failure
# --------------------------------------------------------------------------- #
def generate_failure(payload: dict[str, Any]) -> base.GenerationResult:
    log_text = payload.get("log_text") or ""
    if not str(log_text).strip():
        raise ValueError("log_text（失败用例日志段落）不能为空")
    viewpoint = payload.get("viewpoint") or {}

    def build(feedback: list[str]):
        return prompts.failure_messages(
            viewpoint, steps_doc=payload.get("steps_doc"),
            log_text=_clip(str(log_text), _MAX_LOG_CHARS),
            feedback=feedback)

    return base.generate_validated(build_prompt=build,
                                   validate=validators.validate_failure)


SCENARIOS = {
    "viewpoint": generate_viewpoint,
    "procedure": generate_procedure,
    "sbs": generate_sbs,
    "lib": generate_lib,
    "failure": generate_failure,
}


def run_scenario(scenario: str, payload: dict[str, Any],
                 on_event: EventFn | None = None) -> base.GenerationResult:
    try:
        fn = SCENARIOS[scenario]
    except KeyError:
        raise ValueError(f"未知场景：{scenario}（可选：{sorted(SCENARIOS)}）") from None
    if on_event is None or not _supports_events(fn):
        return fn(payload)
    return fn(payload, on_event=on_event)


def _supports_events(fn) -> bool:
    """Only the long-running scenario takes a progress hook for now."""
    return fn is generate_procedure
