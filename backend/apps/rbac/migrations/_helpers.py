"""Helpers shared by the rbac data migrations.

Module name starts with `_` on purpose: Django's `MigrationLoader` skips
modules whose name begins with an underscore, so this file is never mistaken
for a migration.

`unscoped(model, using)` lives in `apps.rbac.seed` (the seed helpers need the
exact same escape hatch on their *forward* path, so keeping two copies would
be an invitation to drift) and is re-exported here for the migrations that
already import it from this module.
"""

from __future__ import annotations

from apps.rbac.seed import unscoped

__all__ = ["unscoped"]
