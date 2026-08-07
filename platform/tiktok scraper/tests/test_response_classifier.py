"""Tests unitaires Response Classifier (Phase 0 anti-detection-v2)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from response_classifier import (  # noqa: E402
    RecommendedAction,
    ScrapeClass,
    classify_attempt,
    classify_from_result,
)


def test_success_when_posts():
    clf = classify_attempt(posts_count=3)
    assert clf.scrape_class == ScrapeClass.SUCCESS
    assert clf.action == RecommendedAction.NONE


def test_attempt_timeout_not_tls():
    clf = classify_attempt(error="proxy_blocked:attempt_timeout")
    assert clf.scrape_class == ScrapeClass.ATTEMPT_TIMEOUT
    assert clf.action == RecommendedAction.ROTATE_IDENTITY


def test_tls_timeout_is_proxy_infra():
    clf = classify_attempt(error="proxy_blocked:tls_timeout")
    assert clf.scrape_class == ScrapeClass.PROXY_INFRA
    assert "tls" in clf.reason or "timeout" in clf.reason


def test_auth_abort():
    clf = classify_attempt(error="proxy_blocked:auth", http_status=407)
    assert clf.scrape_class == ScrapeClass.AUTH
    assert clf.action == RecommendedAction.ABORT_CREDENTIAL


def test_structural_change_ssr_shell():
    clf = classify_attempt(
        error="no_posts_found",
        http_status=200,
        posts_count=0,
        ssr_universal_len=250_000,
        ssr_has_user_info=True,
        ssr_has_post_list=False,
        ssr_scope_keys=["webapp.user-detail", "webapp.app-context"],
        item_list_xhr_seen=False,
        fetch_failed=True,
    )
    assert clf.scrape_class == ScrapeClass.STRUCTURAL_CHANGE
    assert clf.action == RecommendedAction.ALERT_HUMAN
    assert clf.legacy_error in ("EMPTY_FEED_OR_SOFTBLOCK", "no_posts_found")


def test_platform_change_multi_country():
    clf = classify_attempt(
        error="no_posts_found",
        http_status=200,
        posts_count=0,
        ssr_universal_len=250_000,
        ssr_has_user_info=True,
        ssr_has_post_list=False,
        item_list_xhr_seen=False,
        consecutive_soft_block_countries=3,
    )
    assert clf.scrape_class == ScrapeClass.PLATFORM_CHANGE_SUSPECTED


def test_hard_block_challenge():
    clf = classify_attempt(error="challenge_detected", challenge_detected=True)
    assert clf.scrape_class == ScrapeClass.HARD_BLOCK
    assert clf.mark_waf is True


def test_classify_from_result_reads_signals():
    result = {
        "posts": [],
        "error": "no_posts_found",
        "http_status": 200,
        "classification_signals": {
            "ssr_universal_len": 260000,
            "ssr_has_user_info": True,
            "ssr_has_post_list": False,
            "item_list_xhr_seen": False,
            "ssr_scope_keys": ["webapp.user-detail"],
        },
    }
    clf = classify_from_result(result)
    assert clf.scrape_class == ScrapeClass.STRUCTURAL_CHANGE


def test_rate_limited_403():
    clf = classify_attempt(error="proxy_blocked:http_403", http_status=403)
    assert clf.scrape_class == ScrapeClass.RATE_LIMITED
    assert clf.action == RecommendedAction.BACKOFF


if __name__ == "__main__":
    # Allow `python tests/test_response_classifier.py` without pytest.
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"OK {name}")
    print("all passed")
