"""Tests Identity Pool minimal (Phase 1 + harden cooldown + consecutive soft_block)."""

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

    # 1er soft_block → release + cooldown warm (pas dead)
    identity_pool.apply_classification("fr-482910", "soft_block", success=False)
    assert identity_pool.acquire_for_country("fr", base_user="sdwopfmy") is None
    store = identity_pool._load()  # noqa: SLF001
    row = store["identities"]["fr-482910"]
    assert row["status"] == "warm"
    assert int(row.get("consecutive_soft_blocks") or 0) == 1
    assert float(row.get("quarantine_until") or 0) > time.time()

    # Expire cooldown artificiellement
    store["identities"]["fr-482910"]["quarantine_until"] = 0
    store["identities"]["fr-482910"]["in_use"] = False
    identity_pool._save(store)  # noqa: SLF001

    # Re-checkout puis 2e soft_block consecutif → dead
    reused2 = identity_pool.acquire_for_country("fr", base_user="sdwopfmy")
    assert reused2 is not None
    identity_pool.apply_classification("fr-482910", "soft_block", success=False)
    store = identity_pool._load()  # noqa: SLF001
    row = store["identities"]["fr-482910"]
    assert row["status"] == "dead"
    assert int(row.get("consecutive_soft_blocks") or 0) >= 2
    assert identity_pool.acquire_for_country("fr", base_user="sdwopfmy") is None

    # register_identity ne ressuscite pas un dead
    revived = identity_pool.register_identity(
        base_user="sdwopfmy", country="fr", session_id="482910"
    )
    assert revived.status == "dead"

    # Nouvelle sticky avec autre session_id
    other = identity_pool.create_virgin(
        base_user="sdwopfmy", country="fr", session_id="99999999"
    )
    assert other.status == "virgin"
    identity_pool.apply_classification("fr-99999999", "success", success=True)
    identity_pool.apply_classification("fr-99999999", "structural_change", success=False)
    store = identity_pool._load()  # noqa: SLF001
    assert store["identities"]["fr-99999999"]["status"] != "quarantine"
    assert float(store["identities"]["fr-99999999"]["quarantine_until"] or 0) > time.time()
    assert identity_pool.acquire_for_country("fr", base_user="sdwopfmy") is None

    store["identities"]["fr-99999999"]["quarantine_until"] = 0
    store["identities"]["fr-99999999"]["in_use"] = False
    identity_pool._save(store)  # noqa: SLF001

    identity_pool.apply_classification("fr-99999999", "hard_block", success=False)
    store = identity_pool._load()  # noqa: SLF001
    assert store["identities"]["fr-99999999"]["status"] == "quarantine"

    # Succes reset le compteur soft_block
    identity_pool.create_virgin(base_user="sdwopfmy", country="de", session_id="11111111")
    identity_pool.apply_classification("de-11111111", "soft_block", success=False)
    store = identity_pool._load()  # noqa: SLF001
    store["identities"]["de-11111111"]["quarantine_until"] = 0
    store["identities"]["de-11111111"]["in_use"] = False
    identity_pool._save(store)  # noqa: SLF001
    identity_pool.apply_classification("de-11111111", "success", success=True)
    store = identity_pool._load()  # noqa: SLF001
    assert int(store["identities"]["de-11111111"].get("consecutive_soft_blocks") or 0) == 0
    assert store["identities"]["de-11111111"]["status"] == "active"


if __name__ == "__main__":
    test_lifecycle()
    print("OK test_lifecycle")
