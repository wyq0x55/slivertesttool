"""LAN Test Matrix service layer (compatibility facade).

The business logic is split by domain into ``users_service``,
``projects_service``, ``fields_service``, ``items_service``,
``batch_service`` and ``comments_service`` — mirroring the flat
``*_service.py`` layout the Silver test runner already uses. This module
re-exports every public name so existing callers that reference
``service.<name>`` keep working unchanged.
"""
from __future__ import annotations

from .errors import ServiceError, VersionConflict  # noqa: F401
from .users_service import *  # noqa: F401,F403
from .projects_service import *  # noqa: F401,F403
from .fields_service import *  # noqa: F401,F403
from .items_service import *  # noqa: F401,F403
from .batch_service import *  # noqa: F401,F403
from .comments_service import *  # noqa: F401,F403
