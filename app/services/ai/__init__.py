"""AI agent services — generate → machine-validate → retry → human review.

Package layout:

    config      provider settings (Setting table + env fallback)
    provider    minimal OpenAI-compatible chat client (stdlib urllib)
    base        the shared generate-validate-retry loop
    c_index     deterministic C source indexer (variable/function inventory)
    prompts     per-scenario prompt builders
    validators  machine validators (schema + name-existence checks)
    scenarios   the five scenario orchestrators (pure, DB-free)
    apply       apply an approved draft via the existing service layer

Nothing in this package writes project data directly; every write goes
through :func:`apply.apply_draft` after a human approves the draft.
"""

from __future__ import annotations

from . import apply, base, c_index, config, prompts, provider, scenarios, validators  # noqa: F401

__all__ = [
    "apply", "base", "c_index", "config", "prompts", "provider",
    "scenarios", "validators",
    "ProviderError", "GenerationError", "ApplyError",
]

from .apply import ApplyError  # noqa: E402
from .base import GenerationError  # noqa: E402
from .provider import ProviderError  # noqa: E402
