"""Named UT host toolchain registry and execution-time binary verification."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
from typing import Any

from flask import current_app, has_app_context

from ..lanmatrix.errors import ServiceError

PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

TOOLCHAIN_KEYS = (
    "host_clang",
    "host_llvm_cov",
    "host_llvm_profdata",
    "host_profile_runtime",
)
REQUIRED_TOOLCHAIN_KEYS = (
    "host_clang",
    "host_llvm_cov",
    "host_llvm_profdata",
)

DEFAULT_PROFILE: dict[str, str] = {
    "id": "default",
    "label": "System Default (clang / llvm-cov / llvm-profdata)",
    "host_clang": "clang",
    "host_llvm_cov": "llvm-cov",
    "host_llvm_profdata": "llvm-profdata",
    "host_profile_runtime": "",
}

BUILTIN_DEFAULTS: dict[str, frozenset[str]] = {
    "host_clang": frozenset({"clang", "clang.exe"}),
    "host_llvm_cov": frozenset({"llvm-cov", "llvm-cov.exe"}),
    "host_llvm_profdata": frozenset({"llvm-profdata", "llvm-profdata.exe"}),
    "host_profile_runtime": frozenset({""}),
}


def _active_config(config: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    if config is not None:
        return config
    if has_app_context():
        return current_app.config
    return {}


def _clean_toolchain_field(key: str, raw: Any) -> str:
    if not isinstance(raw, str):
        raise ServiceError(f"{key} must be a string", code="VALIDATION_ERROR")
    if any(ch in raw for ch in ("\x00", "\r", "\n")):
        raise ServiceError(
            f"{key} contains invalid control characters", code="VALIDATION_ERROR"
        )
    value = raw.strip()
    if len(value) > 1024:
        raise ServiceError(f"{key} exceeds 1024 characters", code="VALIDATION_ERROR")
    if key in REQUIRED_TOOLCHAIN_KEYS and not value:
        raise ServiceError(f"{key} must not be empty", code="VALIDATION_ERROR")
    return value


def _load_profiles(config: Mapping[str, Any] | None = None) -> dict[str, dict[str, str]]:
    cfg = _active_config(config)
    profiles: dict[str, dict[str, str]] = {"default": dict(DEFAULT_PROFILE)}
    raw = cfg.get("UT_TOOLCHAIN_PROFILES", "")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return profiles

    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ServiceError(
                "UT_TOOLCHAIN_PROFILES must be valid JSON", code="VALIDATION_ERROR"
            ) from exc
    elif isinstance(raw, Mapping):
        parsed = dict(raw)
    else:
        raise ServiceError(
            "UT_TOOLCHAIN_PROFILES must be a JSON object", code="VALIDATION_ERROR"
        )

    if not isinstance(parsed, dict):
        raise ServiceError(
            "UT_TOOLCHAIN_PROFILES must be a JSON object", code="VALIDATION_ERROR"
        )

    for profile_id, spec in parsed.items():
        if not isinstance(profile_id, str) or not PROFILE_ID_RE.fullmatch(profile_id):
            raise ServiceError(
                f"Invalid toolchain profile ID: {profile_id!r}",
                code="VALIDATION_ERROR",
            )
        if profile_id == "default":
            continue
        if not isinstance(spec, Mapping):
            raise ServiceError(
                f"Toolchain profile {profile_id!r} must be an object",
                code="VALIDATION_ERROR",
            )
        raw_label = spec.get("label", profile_id)
        if not isinstance(raw_label, str) or any(
            ch in raw_label for ch in ("\x00", "\r", "\n")
        ):
            raise ServiceError(
                f"Toolchain profile {profile_id!r} has an invalid label",
                code="VALIDATION_ERROR",
            )
        label = raw_label.strip() or profile_id
        entry: dict[str, str] = {"id": profile_id, "label": label}
        for key in TOOLCHAIN_KEYS:
            entry[key] = _clean_toolchain_field(
                key, spec.get(key, DEFAULT_PROFILE[key])
            )
        profiles[profile_id] = entry

    return profiles


def list_profiles(config: Mapping[str, Any] | None = None) -> list[dict[str, str]]:
    """Return ordered public metadata for all registered toolchain profiles."""
    profiles = _load_profiles(config)
    return [
        {
            "id": item["id"],
            "label": item["label"],
            "host_clang": item["host_clang"],
            "host_llvm_cov": item["host_llvm_cov"],
            "host_llvm_profdata": item["host_llvm_profdata"],
            "host_profile_runtime": item["host_profile_runtime"],
        }
        for item in profiles.values()
    ]


def resolve_profile(
    profile_id: str, config: Mapping[str, Any] | None = None
) -> dict[str, str]:
    """Resolve a registered toolchain profile ID or raise VALIDATION_ERROR."""
    if not isinstance(profile_id, str):
        raise ServiceError("toolchain_profile must be a string", code="VALIDATION_ERROR")
    pid = profile_id.strip() or "default"
    if not PROFILE_ID_RE.fullmatch(pid):
        raise ServiceError(
            f"Invalid toolchain_profile: {profile_id!r}", code="VALIDATION_ERROR"
        )
    profiles = _load_profiles(config)
    if pid not in profiles:
        raise ServiceError(
            f"Unknown toolchain_profile: {pid!r}", code="VALIDATION_ERROR"
        )
    return dict(profiles[pid])


def _is_bare_command(value: str) -> bool:
    if not value or value in {".", ".."}:
        return False
    if "/" in value or "\\" in value:
        return False
    drive, _ = os.path.splitdrive(value)
    return not drive


def _norm_resolved_path(value: str) -> str | None:
    path = Path(value)
    if not path.is_absolute():
        return None
    try:
        return os.path.normcase(str(path.resolve(strict=False)))
    except OSError:
        return None


def _allowed_toolchain_entries(config: Mapping[str, Any]) -> list[str]:
    raw = config.get("UT_ALLOWED_TOOLCHAINS") or ()
    if isinstance(raw, str):
        items = raw.replace("\n", ";").split(";")
    else:
        items = list(raw)
    return [str(item).strip() for item in items if str(item).strip()]


def _matches_entry(value: str, entry: str) -> bool:
    if not entry:
        return False
    if _is_bare_command(value):
        if not _is_bare_command(entry):
            return False
        return value.lower() == entry.lower() if os.name == "nt" else value == entry
    if _is_bare_command(entry):
        return False
    norm_val = _norm_resolved_path(value)
    norm_entry = _norm_resolved_path(entry)
    return norm_val is not None and norm_val == norm_entry


def _is_trusted_binary(
    key: str,
    value: str,
    *,
    profiles: Mapping[str, Mapping[str, str]],
    allowed_entries: list[str],
) -> bool:
    if value in BUILTIN_DEFAULTS[key]:
        return True
    if os.name == "nt" and value.lower() in {d.lower() for d in BUILTIN_DEFAULTS[key]}:
        return True

    for prof in profiles.values():
        for field_key in TOOLCHAIN_KEYS:
            candidate = prof.get(field_key, "")
            if candidate and _matches_entry(value, candidate):
                return True

    for entry in allowed_entries:
        if _matches_entry(value, entry):
            return True

    return False


def _extract_field(cfg_or_dict: Any, key: str, default: Any) -> Any:
    if isinstance(cfg_or_dict, Mapping):
        val = cfg_or_dict.get(key, default)
    else:
        val = getattr(cfg_or_dict, key, default)
    return default if val is None else val


def _is_admin_audited_toolchain(project_id: int | None, cfg_or_dict: Any) -> bool:
    """Return True if a system_admin previously persisted this exact toolchain via save_config."""
    if not project_id:
        return False
    from ...extensions import db
    from ...models import AuditLog, LMUser

    target = {
        key: _clean_toolchain_field(
            key, _extract_field(cfg_or_dict, key, DEFAULT_PROFILE[key])
        )
        for key in TOOLCHAIN_KEYS
    }
    entries = (
        db.session.query(AuditLog.new_value)
        .join(LMUser, LMUser.id == AuditLog.actor_id)
        .filter(
            AuditLog.project_id == project_id,
            AuditLog.action == "ut.config.update",
            LMUser.is_system_admin.is_(True),
        )
        .order_by(AuditLog.id.desc())
        .limit(50)
        .all()
    )
    for (new_val,) in entries:
        if isinstance(new_val, Mapping) and all(
            str(new_val.get(key) or "") == target[key] for key in TOOLCHAIN_KEYS
        ):
            return True
    return False


def verify_toolchain_config(
    cfg_or_dict: Any,
    *,
    allow_custom_admin: bool = False,
    admin_override_authorized: bool = False,
    project_id: int | None = None,
    config: Mapping[str, Any] | None = None,
) -> None:
    """Validate toolchain_profile and host_* binaries on save or execution."""
    cfg = _active_config(config)
    profiles = _load_profiles(cfg)

    raw_profile = _extract_field(cfg_or_dict, "toolchain_profile", "default")
    if not isinstance(raw_profile, str):
        raise ServiceError("toolchain_profile must be a string", code="VALIDATION_ERROR")
    profile_id = raw_profile.strip() or "default"
    if not PROFILE_ID_RE.fullmatch(profile_id) or profile_id not in profiles:
        raise ServiceError(
            f"Unknown toolchain_profile: {profile_id!r}", code="VALIDATION_ERROR"
        )
    selected = profiles[profile_id]
    allowed_entries = _allowed_toolchain_entries(cfg)
    audited_cache: bool | None = None

    for key in TOOLCHAIN_KEYS:
        raw_val = _extract_field(cfg_or_dict, key, DEFAULT_PROFILE[key])
        val = _clean_toolchain_field(key, raw_val)
        differs = val != selected[key]
        if differs and not allow_custom_admin:
            raise ServiceError(
                "仅系统管理员可覆盖所选工具链配置中的宿主机编译器与覆盖率工具路径",
                code="PERMISSION_DENIED",
            )
        if not _is_trusted_binary(
            key, val, profiles=profiles, allowed_entries=allowed_entries
        ):
            if allow_custom_admin and not allowed_entries:
                if admin_override_authorized:
                    continue
                if audited_cache is None:
                    audited_cache = _is_admin_audited_toolchain(project_id, cfg_or_dict)
                if audited_cache:
                    continue
            raise ServiceError(
                f"{key} 不在受信任的工具链配置（UT_TOOLCHAIN_PROFILES / UT_ALLOWED_TOOLCHAINS）内",
                code="PERMISSION_DENIED",
            )


def verify_execution_toolchain(
    cfg_or_dict: Any,
    *,
    project_id: int | None = None,
    config: Mapping[str, Any] | None = None,
) -> None:
    """Worker execution-time gate blocking tampered or unauthorized binaries."""
    verify_toolchain_config(
        cfg_or_dict,
        allow_custom_admin=True,
        admin_override_authorized=False,
        project_id=project_id,
        config=config,
    )
