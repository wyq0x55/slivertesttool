"""Scenario orchestration — combines prompts, validators and the retry loop.

Each ``generate_*`` function is pure (no Flask/DB): it takes the scenario
payload, assembles context (including the deterministic C source index) and
returns a validated output dict plus the generation log. The HTTP layer
persists this as an ``AiDraft``; ``apply_draft`` (in :mod:`.apply`) then
routes approved outputs through the existing service layer.
"""

from __future__ import annotations

from typing import Any

from . import base, c_index, prompts, validators

# Context ceilings (chars) — prompts stay small and focused on purpose.
_MAX_SOURCE_CHARS = 8000
_MAX_LOG_CHARS = 6000


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
# procedure
# --------------------------------------------------------------------------- #
def generate_procedure(payload: dict[str, Any]) -> base.GenerationResult:
    viewpoint = payload.get("viewpoint")
    if not isinstance(viewpoint, dict) or not viewpoint:
        raise ValueError("viewpoint（测试观点）不能为空")
    source_files: dict[str, str] = payload.get("source_files") or {}
    sbs_variables: list[str] = list(payload.get("sbs_variables") or [])

    # Deterministic layer first: the variable inventory the model may use is
    # extracted from the actual source (clang AST, with the project's compile
    # args so #ifdef matches the real build), so inventing names is
    # structurally impossible to sneak past the validator.
    index = c_index.index_source(source_files,
                                 compile_args=payload.get("compile_args"))
    known_paths = set(sbs_variables) | set(index["variables"])
    lib_functions = payload.get("lib_functions") or []
    known_subs = {f["name"] for f in lib_functions if isinstance(f, dict) and f.get("name")}

    def validate(parsed: Any) -> list[str]:
        if not isinstance(parsed, dict):
            return ["输出必须是对象"]
        problems = validators.validate_steps_doc(
            parsed.get("steps_doc"), known_paths=known_paths,
            known_subs=known_subs, allow_missing=True)
        # allow_missing above lets unknown paths through *only* when the model
        # explicitly reports them in missing_variables — hallucinated paths
        # without an accompanying claim are still rejected.
        if not problems:
            doc = parsed["steps_doc"]
            declared = {p for _n, p in doc.get("input_signals", [])
                        + doc.get("expected_signals", [])}
            known_ok = {p for p in known_paths}
            missing_names = set()
            for item in parsed.get("missing_variables") or []:
                if isinstance(item, dict) and item.get("name"):
                    missing_names.add(item["name"])
            undeclared = declared - known_ok
            if undeclared and not undeclared <= missing_names:
                problems.append(
                    "以下信号路径既不在已知清单中，也未申报为 missing_variables："
                    + ", ".join(sorted(undeclared - missing_names)))
        return problems

    def build(feedback: list[str]):
        source_context = payload.get("source_context")
        if not source_context:
            keywords = list(viewpoint.get("variables") or []) + [
                str(viewpoint.get("title") or "")]
            picked = c_index.select_context(index, keywords,
                                            max_chars=_MAX_SOURCE_CHARS)
            source_context = "\n\n".join(picked.values())
        return prompts.procedure_messages(
            viewpoint,
            source_context=_clip(str(source_context), _MAX_SOURCE_CHARS),
            variable_list=sorted(known_paths),
            lib_functions=lib_functions,
            constant_names=payload.get("constant_names"),
            example_steps=payload.get("example_steps"),
            feedback=feedback)

    return base.generate_validated(build_prompt=build, validate=validate)


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


def run_scenario(scenario: str, payload: dict[str, Any]) -> base.GenerationResult:
    try:
        fn = SCENARIOS[scenario]
    except KeyError:
        raise ValueError(f"未知场景：{scenario}（可选：{sorted(SCENARIOS)}）") from None
    return fn(payload)
