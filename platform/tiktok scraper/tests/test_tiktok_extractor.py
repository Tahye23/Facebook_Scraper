"""Tests unitaires du mapping TikTokApi → schema scraper (sans reseau)."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from tiktok_extractor import (
    _map_video_to_post,
    _video_count_from_user_info,
    username_from_profile_url,
)


class _FakeVideo:
    def __init__(self, data: dict):
        self.as_dict = data


def test_username_from_profile_url():
    assert username_from_profile_url("https://www.tiktok.com/@BBCNews") == "bbcnews"
    assert username_from_profile_url("https://www.tiktok.com/@user/video/123") == "user"
    assert username_from_profile_url("https://example.com/") == ""


def test_map_video_to_post_schema():
    raw = {
        "id": "7123456789012345678",
        "desc": "Hello #news",
        "createTime": 1700000000,
        "author": {"uniqueId": "bbcnews"},
        "stats": {
            "diggCount": 10,
            "commentCount": 2,
            "shareCount": 1,
            "playCount": 1000,
        },
    }
    post = _map_video_to_post(_FakeVideo(raw), fallback_author="bbcnews")
    assert post is not None
    assert post["post_id"] == "7123456789012345678"
    assert post["author"] == "bbcnews"
    assert post["message"] == "Hello #news"
    assert post["text_content"] == "Hello #news"
    assert post["likes"] == 10
    assert post["views"] == 1000
    assert post["comments_count"] == 2
    assert post["shares"] == 1
    assert "metrics" in post
    assert post["post_url"].endswith("/video/7123456789012345678")
    assert post["published_at"]


def test_video_count_from_user_info_variants():
    assert _video_count_from_user_info({"stats": {"videoCount": 42}}) == 42
    assert (
        _video_count_from_user_info(
            {"userInfo": {"stats": {"videoCount": 7}, "user": {"uniqueId": "x"}}}
        )
        == 7
    )
    assert _video_count_from_user_info({"uniqueId": "x"}) is None
