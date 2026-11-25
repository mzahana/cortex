"""`CORE_TARGETS` parity with the frontend mapping Select.

`core_targets.json` is the shared fixture: this test pins it to the backend's
`CORE_TARGETS`; `frontend/src/api/types.test.ts` asserts `IMPORT_CORE_TARGETS`
equals the same file. A new core target must be added in both places.
"""

import json
from pathlib import Path

from apps.imports.exports import CORE_EXPORT_COLUMNS
from apps.imports.services import CORE_TARGETS


def test_core_targets_match_shared_fixture():
    expected = json.loads((Path(__file__).parent / "core_targets.json").read_text())
    assert sorted(CORE_TARGETS) == expected
    assert sorted(CORE_EXPORT_COLUMNS) == expected
