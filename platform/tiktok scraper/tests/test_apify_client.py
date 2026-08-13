"""Tests Apify client: mapping + quota (sans appel réseau)."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REPO = ROOT.parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


SAMPLE_ITEM = {
    "id": "7353646097262202145",
    "text": "Hello #fyp #automation",
    "createTime": 1712154160,
    "createTimeISO": "2024-04-03T14:22:40.000Z",
    "authorMeta": {"name": "apifytech", "nickName": "apifytech"},
    "webVideoUrl": "https://www.tiktok.com/@apifytech/video/7353646097262202145",
    "diggCount": 725,
    "shareCount": 30,
    "playCount": 83900,
    "commentCount": 10,
}


def test_mapping_and_normalize():
    import apify_client

    post = apify_client.apify_item_to_post(SAMPLE_ITEM)
    assert post is not None
    assert post["post_id"] == "7353646097262202145"
    assert post["author"] == "apifytech"
    assert post["likes"] == 725
    assert post["views"] == 83900
    assert post["comments_count"] == 10
    assert post["shares"] == 30
    assert post["published_at"] == "2024-04-03T14:22:40.000Z"

    # Compat worker.normalize_post (meme cles snake_case)
    from worker import normalize_post

    payload = normalize_post("scrape-test", "https://www.tiktok.com/@apifytech", post)
    assert payload["postId"] == "7353646097262202145"
    assert payload["author"] == "apifytech"
    assert payload["textContent"] == "Hello #fyp #automation"
    assert payload["metrics"]["likes"] == 725
    assert payload["metrics"]["views"] == 83900
    assert payload["metrics"]["comments"] == 10
    assert payload["metrics"]["shares"] == 30
    assert "fyp" in " ".join(payload["hashtags"]) or any(
        "fyp" in h for h in payload["hashtags"]
    )


def test_quota_guard():
    td = Path(tempfile.mkdtemp())
    os.environ["APIFY_QUOTA_FILE"] = str(td / "quota.json")
    os.environ["APIFY_DAILY_VIDEO_LIMIT"] = "5"

    import importlib
    import apify_client

    importlib.reload(apify_client)

    apify_client.record_quota_usage(3)
    assert apify_client.get_daily_usage() == 3

    # 3 + 3 > 5 → refuse
    try:
        apify_client.check_quota_or_raise(3)
        raise AssertionError("expected ApifyQuotaExceeded")
    except apify_client.ApifyQuotaExceeded:
        pass

    # 3 + 2 == 5 → OK
    apify_client.check_quota_or_raise(2)
    apify_client.record_quota_usage(2)
    assert apify_client.get_daily_usage() == 5


def test_actor_input_no_downloads():
    import apify_client

    body = apify_client.build_actor_input("bellewarmedia", 30)
    assert body["profiles"] == ["bellewarmedia"]
    assert body["resultsPerPage"] == 30
    assert body["shouldDownloadVideos"] is False
    assert body["shouldDownloadCovers"] is False
    assert body["profileSorting"] == "latest"
    assert "oldestPostDateUnified" not in body

    # max_posts=2 doit rester 2 (pas de default 20 qui ecrase)
    body2 = apify_client.build_actor_input("x", 2)
    assert body2["resultsPerPage"] == 2

    # Fenetre 24h → filtre date natif Apify (YYYY-MM-DD absolu)
    body3 = apify_client.build_actor_input("x", 33, max_age_hours=24)
    assert body3["resultsPerPage"] == 33  # plafond securite, pas un fetch 100
    assert "oldestPostDateUnified" in body3
    assert len(body3["oldestPostDateUnified"]) == 10  # YYYY-MM-DD
    assert body3["profileSorting"] == "latest"

    # Helper date
    d = apify_client.oldest_post_date_unified(24)
    assert len(d) == 10


def test_redact_url():
    import apify_client

    redacted = apify_client._redact_url(  # noqa: SLF001
        "https://api.apify.com/v2/acts/x/runs?token=SECRET123&format=json"
    )
    assert "SECRET123" not in redacted
    assert "token=***" in redacted


if __name__ == "__main__":
    test_mapping_and_normalize()
    test_quota_guard()
    test_actor_input_no_downloads()
    test_redact_url()
    print("OK test_apify_client")
