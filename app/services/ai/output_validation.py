"""Shared, database-free validation for generated, edited and approved outputs."""

from __future__ import annotations

import math
import re
import hashlib
import json

from . import c_index, registry, validators


def procedure_entries(output, refs=None):
    entries = output.get("procedures")
    if entries is None and "steps_doc" in output:
        return ([output], []) if refs is None else ([], ["旧版单条输出不支持 refs"])
    if not isinstance(entries, list) or not entries:
        return [], ["procedures 必须是非空数组"]
    if refs is not None:
        if (not isinstance(refs, list) or not refs
                or any(not isinstance(ref, str) or not ref.strip() for ref in refs)
                or len(set(refs)) != len(refs)):
            return [], ["refs 必须是非空且不重复的字符串"]
        selected = [entry for entry in entries if isinstance(entry, dict) and entry.get("ref") in refs]
        found = [entry["ref"] for entry in selected]
        if any(found.count(ref) != 1 for ref in refs):
            return [], ["每个选中的 ref 必须恰好对应一条已有手顺"]
    else:
        selected = entries
    problems = []
    seen = set()
    for entry in selected:
        ref = entry.get("ref") if isinstance(entry, dict) else None
        if not isinstance(ref, str) or not ref.strip():
            problems.append("每条手顺都需要非空 ref")
        elif ref in seen:
            problems.append(f"手顺 ref 重复：{ref}")
        else:
            seen.add(ref)
    return selected, problems


def _positive_integer(value):
    return type(value) is int and value > 0


def context_digest(value):
    encoded = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def item_snapshots(scenario, payload, output, refs=None):
    context = payload.get("_context")
    if (not isinstance(context, dict) or context.get("schema_version") != 1
            or not _positive_integer(context.get("project_id"))
            or not isinstance(context.get("items"), list)
            or not isinstance(context.get("provenance"), list)):
        return {}, ["缺少服务器生成上下文，请重新生成该草稿"]
    if scenario in ("procedure", "lib") and "dependencies" in context:
        dependencies = context["dependencies"]
        if (not isinstance(dependencies, list)
                or any(not isinstance(entry, dict) or not _positive_integer(entry.get("id"))
                       or not _positive_integer(entry.get("version")) for entry in dependencies)
                or len({entry["id"] for entry in dependencies}) != len(dependencies)):
            return {}, ["生成来源依赖无效，请重新生成该草稿"]
    source = []
    if scenario == "procedure":
        entries, problems = procedure_entries(output, refs)
        if problems:
            return {}, problems
        if "procedures" not in output:
            source = [{"item_id": payload.get("item_id")}]
        else:
            viewpoints = payload.get("viewpoints")
            if not isinstance(viewpoints, list):
                return {}, ["缺少生成观点，请重新生成该草稿"]
            for entry in entries:
                matches = [vp for vp in viewpoints if isinstance(vp, dict) and vp.get("ref") == entry["ref"]]
                if len(matches) != 1:
                    return {}, [f"生成 ref 缺失或不唯一：{entry['ref']}"]
                source.append(matches[0])
    elif scenario == "lib":
        source = payload.get("procedures")
        if not isinstance(source, list) or not source:
            return {}, ["缺少生成手顺，请重新生成该草稿"]
    elif scenario == "failure":
        source = [{"item_id": payload.get("item_id")}]
    snapshots = {}
    for entry in source:
        identity = entry.get("item_id") if isinstance(entry, dict) else None
        if not _positive_integer(identity) or identity in snapshots:
            return {}, ["生成项必须使用互不相同的正整数 item ID"]
        matches = [item for item in context["items"] if isinstance(item, dict) and item.get("id") == identity]
        if len(matches) != 1 or not _positive_integer(matches[0].get("version")):
            return {}, [f"缺少生成项 {identity} 的版本，请重新生成该草稿"]
        version = matches[0]["version"]
        if scenario == "lib" or scenario == "procedure" and "procedures" in output:
            if not _positive_integer(entry.get("version")) or entry["version"] != version:
                return {}, [f"生成项 {identity} 的版本缺失或不一致，请重新生成该草稿"]
        snapshots[identity] = version
    return snapshots, []


def _known_paths(payload):
    index = c_index.index_source(payload.get("source_files") or {}, compile_args=payload.get("compile_args"))
    seeds = []
    for viewpoint in payload.get("viewpoints") or []:
        for variable in viewpoint.get("variables") or []:
            if isinstance(variable, str):
                seeds.append(variable)
            elif isinstance(variable, list) and len(variable) == 2:
                seeds.append([variable[1], variable[0]])
    history = list(payload.get("historical_pairs") or [])
    for procedure in payload.get("procedures") or []:
        doc = procedure.get("steps_doc") or {}
        for key in ("input_signals", "expected_signals"):
            history.extend(doc.get(key) or [])
    return set(registry.build(index=index, sbs_text=payload.get("sbs_text") or "",
                             sbs_variables=payload.get("sbs_variables"), historical_pairs=history,
                             viewpoint_seeds=seeds, signal_dict=payload.get("signal_dict")).paths())


def _missing_problems(entry, for_apply):
    missing = entry.get("missing_variables", [])
    if (not isinstance(missing, list)
            or any(not isinstance(item, dict) or not isinstance(item.get("name"), str)
                   or not item["name"].strip() for item in missing)):
        return ["missing_variables 必须是带名称的变量数组"]
    if for_apply and missing:
        return ["未解析的 missing_variables 必须先登记并重新生成草稿后才能通过"]
    return []


def _parameter_problems(parameters):
    if parameters is None:
        return []
    if not isinstance(parameters, list):
        return ["lib_para 必须是数组"]
    seen = set()
    problems = []
    for parameter in parameters:
        name = parameter.get("name") if isinstance(parameter, dict) else None
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name in seen:
            problems.append("lib_para 需要互不相同的标识符名称")
            continue
        seen.add(name)
        default = parameter.get("default")
        if default is not None and (type(default) not in (str, int, float)
                or isinstance(default, float) and not math.isfinite(default)
                or isinstance(default, str) and (not default.strip() or re.search(r"[,，、\r\n]", default))):
            problems.append(f"lib_para 默认值无法被导出器编码：{name}")
    return problems


def validate_output(scenario, payload, output, *, refs=None, for_apply=False) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(output, dict):
        return ["payload 和 output 必须是对象"]
    if scenario not in ("viewpoint", "procedure", "sbs", "lib", "failure"):
        return ["未知草稿场景"]
    if refs is not None and scenario != "procedure":
        return ["refs 仅支持手顺批次"]
    try:
        problems = []
        if for_apply:
            _snapshots, problems = item_snapshots(scenario, payload, output, refs)
            if problems:
                return problems
        if scenario == "viewpoint":
            problems.extend(validators.validate_viewpoints(output))
            for entry in output.get("viewpoints") or []:
                for key in ("title", "expected", "condition", "precondition"):
                    if entry.get(key) is not None and not isinstance(entry[key], str):
                        problems.append(f"viewpoint.{key} 必须是文本")
        elif scenario == "failure":
            problems.extend(validators.validate_failure(output))
            for key in ("analysis", "likely_cause", "suggested_action"):
                if not isinstance(output.get(key), str):
                    problems.append(f"{key} 必须是文本")
        elif scenario == "sbs":
            index = c_index.index_source(payload.get("source_files") or {}, compile_args=payload.get("compile_args"))
            problems.extend(validators.validate_sbs(output, known_variables=set(index["variables"])))
        elif scenario == "procedure":
            entries, problems = procedure_entries(output, refs)
            if problems:
                return problems
            viewpoints = payload.get("viewpoints") or []
            known_refs = [viewpoint.get("ref") for viewpoint in viewpoints if isinstance(viewpoint, dict)]
            paths = _known_paths(payload)
            subs = {entry["name"] for entry in payload.get("lib_functions") or [] if isinstance(entry, dict) and entry.get("name")}
            for entry in entries:
                if "procedures" in output and known_refs.count(entry["ref"]) != 1:
                    problems.append(f"手顺 ref 不在生成选择中：{entry['ref']}")
                missing = _missing_problems(entry, for_apply)
                problems.extend(missing)
                declared = {item["name"] for item in entry.get("missing_variables") or []
                            if isinstance(item, dict) and isinstance(item.get("name"), str)}
                problems.extend(validators.validate_steps_doc(entry.get("steps_doc"),
                                known_paths=paths | (declared if not for_apply else set()), known_subs=subs))
            if not problems:
                from .runtime_validation import validate_runtime
                problems.extend(validate_runtime(payload, [entry["steps_doc"] for entry in entries]))
        elif scenario == "lib":
            existing = set(payload.get("existing_lib_names") or [])
            item_ids = {entry["item_id"] for entry in payload.get("procedures") or [] if isinstance(entry, dict)}
            problems.extend(validators.validate_lib(output, existing_lib_names=existing, item_ids=item_ids))
            problems.extend(_parameter_problems(output.get("lib_para")))
            paths = _known_paths(payload)
            problems.extend(validators.validate_steps_doc(output.get("lib_stb"), known_paths=paths, known_subs=existing))
            seen = set()
            for entry in output.get("rewritten") or []:
                identity = entry.get("item_id")
                if not _positive_integer(identity) or identity in seen:
                    problems.append("rewritten 的 item ID 必须是互不相同的正整数")
                seen.add(identity)
                problems.extend(_missing_problems(entry, for_apply))
                problems.extend(validators.validate_steps_doc(entry.get("steps_doc"), known_paths=paths,
                                known_subs=existing | {output.get("lib_name")}))
            if not problems:
                from .runtime_validation import validate_runtime
                problems.extend(validate_runtime(payload, [entry["steps_doc"] for entry in output.get("rewritten") or []],
                                                 candidate=output))
        return problems
    except (TypeError, ValueError, KeyError, AttributeError):
        return ["payload 或 output 的字段类型无效"]
