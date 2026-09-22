"""LAN Matrix project-resource blueprint aggregator.

Keep the blueprint name stable: endpoint identities are part of the public route
contract. Resource modules import this single blueprint and register handlers on
it; this module imports each owner exactly once.
"""

from flask import Blueprint

from ._base import register_common

bp = Blueprint("lanmatrix_projects", __name__, url_prefix="/api/v1")
register_common(bp)

# Registration imports: order is explicit and side-effect is limited to adding
# routes to the shared blueprint.
from . import projects as _projects  # noqa: E402,F401
from . import fields as _fields  # noqa: E402,F401
from . import models as _models  # noqa: E402,F401
from . import items as _items  # noqa: E402,F401
from . import imports_exports as _imports_exports  # noqa: E402,F401
from . import audit_trash as _audit_trash  # noqa: E402,F401
from . import members as _members  # noqa: E402,F401
from . import reviews as _reviews  # noqa: E402,F401
from . import dashboard as _dashboard  # noqa: E402,F401

__all__ = ["bp"]
