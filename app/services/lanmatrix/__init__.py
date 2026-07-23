"""LAN Test Matrix service layer.

The online test-matrix editor's business logic — project/field/item CRUD,
validation, batch search-replace, permissions, audit, Excel import/export and
the Test-Matrix bridge — lives here as part of the platform's service layer.

Formerly a self-contained ``app.lanmatrix`` subpackage; now merged into the
Silver Test Platform: models live in :mod:`app.models`, the HTTP blueprints in
:mod:`app.routes`, and this package holds the service/logic modules.

The business logic is split by domain into ``users_service``,
``projects_service``, ``fields_service``, ``items_service``, ``batch_service``
and ``comments_service`` (mirroring the runner's flat ``*_service.py`` layout);
``service`` is a thin compatibility facade that re-exports all of them. Shared
exception types live in ``errors``; query helpers in ``queries``. See
``docs/STRUCTURAL_UNIFICATION.md``.
"""
