"""Tests Identity Pool minimal (Phase 1 + harden cooldown)."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REPO = ROOT.parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def test_lifecycle(tmp_path: Path | None = None):
    td = tmp_path or Path(tempfile.mkdtemp())
    os.environ["TIKTOK_IDENTITY_POOL_FILE"] = str(td / "pool.json")
    os.environ["TIKTOK_IDENTITY_POOL_ENABLED"] = "true"
    os.environ["TIKTOK_QUARANTINE_COOLDOWN_HOURS"] = "2"

    import importlib
    import identity_pool

    importlib.reload(identity_pool)

    virgin = identity_pool.create_virgin(base_user="sdwopfmy", country="fr", session_id="482910")
    assert virgin.identity_id == "fr-482910"
    assert virgin.status == "virgin"

    identity_pool.apply_classification("fr-482910", "success", success=True)
    reused = identity_pool.acquire_for_country("fr", base_user="sdwopfmy")
    assert reused is not None
    assert reused.session_id == "482910"
    assert reused.in_use is True

    # 2e acquire pendant checkout → None (evite double-assign meme job)
    assert identity_pool.acquire_for_country("fr", base_user="sdwopfmy") is None

    # soft_block → release + cooldown: pas de reuse immediat
    identity_pool.apply_classification("fr-482910", "soft_block", success=False)
    assert identity_pool.acquire_for_country("fr", base_user="sdwopfmy") is None

    store = identity_pool._load()  # noqa: SLF001
    store["identities"]["fr-482910"]["quarantine_until"] = 0
    store["identities"]["fr-482910"]["in_use"] = False
    identity_pool._save(store)  # noqa: SLF001

    identity_pool.apply_classification("fr-482910", "structural_change", success=False)
    store = identity_pool._load()  # noqa: SLF001
    assert store["identities"]["fr-482910"]["status"] != "quarantine"
    assert float(store["identities"]["fr-482910"]["quarantine_until"] or 0) > time.time()
    assert identity_pool.acquire_for_country("fr", base_user="sdwopfmy") is None

    store["identities"]["fr-482910"]["quarantine_until"] = 0
    identity_pool._save(store)  # noqa: SLF001

    identity_pool.apply_classification("fr-482910", "hard_block", success=False)
    store = identity_pool._load()  # noqa: SLF001
    assert store["identities"]["fr-482910"]["status"] == "quarantine"


if __name__ == "__main__":
    test_lifecycle()
    print("OK test_lifecycle")
