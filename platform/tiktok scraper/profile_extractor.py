"""Extraction profile-first depuis hydration SSR TikTok (__DEFAULT_SCOPE__)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from logging_setup import get_logger

LOGGER = get_logger(__name__, platform="tiktok", service="profile_extractor")


def _parse_count(raw: Any) -> int | None:
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    text = str(raw).strip().upper().replace(",", "").replace(" ", "")
    if not text or text in {"-", "N/A", "NULL"}:
        return None
    mult = 1.0
    if text.endswith("K"):
        mult = 1_000.0
        text = text[:-1]
    elif text.endswith("M"):
        mult = 1_000_000.0
        text = text[:-1]
    elif text.endswith("B"):
        mult = 1_000_000_000.0
        text = text[:-1]
    try:
        return int(float(text) * mult)
    except ValueError:
        digits = re.sub(r"[^\d]", "", str(raw))
        return int(digits) if digits else None


def _to_iso(raw: Any) -> str | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        ts = int(raw)
        if ts > 10_000_000_000:
            ts //= 1000
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (ValueError, OSError):
            return None
    text = str(raw).strip()
    if text.isdigit():
        return _to_iso(int(text))
    return None


def _is_video_id(raw: Any) -> bool:
    text = str(raw or "").strip()
    return text.isdigit() and len(text) >= 5


def _stats(item: dict) -> dict[str, int | None]:
    stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
    stats_v2 = item.get("statsV2") if isinstance(item.get("statsV2"), dict) else {}

    def pick(*keys: str) -> int | None:
        for key in keys:
            for source in (stats_v2, stats):
                if key in source and source.get(key) not in (None, ""):
                    parsed = _parse_count(source.get(key))
                    if parsed is not None:
                        return parsed
        return None

    return {
        "likes": pick("diggCount", "diggCountValue", "likeCount", "likes"),
        "views": pick("playCount", "playCountValue", "viewCount", "views"),
        "comments": pick("commentCount", "commentCountValue", "comments"),
        "shares": pick("shareCount", "shareCountValue", "shares"),
    }


def _author_handle(item: dict) -> str:
    node = item.get("author") or item.get("authorInfo") or {}
    if isinstance(node, dict):
        handle = (
            node.get("uniqueId")
            or node.get("unique_id")
            or node.get("nickname")
            or ""
        )
    else:
        handle = str(node or "")
    if not handle:
        handle = str(item.get("authorUniqueId") or "")
    return str(handle).lstrip("@").strip()


def item_to_post(item: dict, fallback_author: str = "") -> dict | None:
    """Mappe un itemStruct profil -> dict post scraper (metrics inclus)."""
    if not isinstance(item, dict):
        return None
    post_id = ""
    for key in ("id", "idCode", "aweme_id", "awemeId", "video_id", "videoId"):
        if _is_video_id(item.get(key)):
            post_id = str(item.get(key)).strip()
            break
    if not post_id:
        return None

    author = _author_handle(item) or fallback_author
    author_url = author.lower()
    desc = item.get("desc") or item.get("description") or item.get("title") or ""
    if not isinstance(desc, str):
        desc = str(desc or "")
    desc = desc.strip()
    metrics = _stats(item)
    published_at = _to_iso(item.get("createTime") or item.get("create_time"))
    post_url = (
        f"https://www.tiktok.com/@{author_url}/video/{post_id}"
        if author_url
        else f"https://www.tiktok.com/video/{post_id}"
    )
    return {
        "post_id": post_id,
        "post_url": post_url,
        "message": desc,
        "text_content": desc,
        "text": desc,
        "author": author or author_url,
        "published_at": published_at,
        "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
        "likes": metrics["likes"],
        "comments_count": metrics["comments"],
        "shares": metrics["shares"],
        "views": metrics["views"],
        "metrics": {
            "likes": metrics["likes"],
            "views": metrics["views"],
            "comments": metrics["comments"],
            "shares": metrics["shares"],
        },
    }


def post_has_usable_metrics(post: dict) -> bool:
    """True si likes/views (ou metrics.*) deja presents — skip /video/<id>."""
    metrics = post.get("metrics") if isinstance(post.get("metrics"), dict) else {}
    has_likes = post.get("likes") not in (None, "") or metrics.get("likes") not in (None, "")
    has_views = post.get("views") not in (None, "") or metrics.get("views") not in (None, "")
    has_text = bool(str(post.get("text_content") or post.get("message") or "").strip())
    return (has_likes and has_views) or (has_likes and has_text)


def _collect_raw_items_from_scope(scope: dict) -> list[dict]:
    items: list[dict] = []

    def _pull_lists_and_modules(node: dict) -> None:
        if not isinstance(node, dict):
            return
        for list_key in ("itemList", "item_list", "items"):
            val = node.get(list_key)
            if not isinstance(val, list) or not val:
                continue
            for x in val:
                if isinstance(x, dict):
                    # itemStruct wrappe parfois
                    struct = x.get("itemStruct")
                    items.append(struct if isinstance(struct, dict) else x)
                elif _is_video_id(x):
                    # itemList peut etre une liste d'IDs string
                    items.append({"id": str(x).strip()})
        for mod_key in ("itemModule", "ItemModule"):
            mod = node.get(mod_key)
            if isinstance(mod, dict) and mod:
                items.extend([x for x in mod.values() if isinstance(x, dict)])
        # Structures imbriquees frequentes sous user-detail
        for nested_key in ("userDetail", "userInfo", "videoData", "postList"):
            nested = node.get(nested_key)
            if isinstance(nested, dict):
                _pull_lists_and_modules(nested)
            elif isinstance(nested, list):
                for x in nested:
                    if isinstance(x, dict):
                        items.append(x)
                    elif _is_video_id(x):
                        items.append({"id": str(x).strip()})

    # 1) webapp.user-post-list / user-detail / user-post -> itemList / itemModule
    # Note: user-detail ne contient souvent que userInfo/shareMeta ; les videos
    # sont dans webapp.user-post-list.
    for key in (
        "webapp.user-post-list",
        "webapp.user-detail",
        "webapp.user-post",
        "webapp.user-detail.page",
    ):
        node = scope.get(key)
        if isinstance(node, dict):
            _pull_lists_and_modules(node)

    # 2) webapp.app-context -> itemModule
    app_ctx = scope.get("webapp.app-context")
    if isinstance(app_ctx, dict):
        for mod_key in ("itemModule", "ItemModule"):
            mod = app_ctx.get(mod_key)
            if isinstance(mod, dict) and mod:
                items.extend([x for x in mod.values() if isinstance(x, dict)])

    # 3) Fallback: toute cle contenant itemList / itemModule (toujours, pas seulement si vide)
    for key, val in scope.items():
        if not isinstance(val, dict):
            continue
        kl = str(key).lower()
        if "itemlist" in kl or "itemmodule" in kl or "user-detail" in kl or "user-post" in kl:
            _pull_lists_and_modules(val)
        else:
            for list_key in ("itemList", "item_list", "items"):
                lst = val.get(list_key)
                if isinstance(lst, list):
                    for x in lst:
                        if isinstance(x, dict):
                            items.append(x)
                        elif _is_video_id(x):
                            items.append({"id": str(x).strip()})
            for mod_key in ("itemModule", "ItemModule"):
                mod = val.get(mod_key)
                if isinstance(mod, dict):
                    items.extend([x for x in mod.values() if isinstance(x, dict)])

    # 4) Parcours profond itemStruct / objets video
    if not items:

        def walk(node: Any, depth: int = 0) -> None:
            if depth > 14 or node is None:
                return
            if isinstance(node, dict):
                raw_id = node.get("id") or node.get("idCode") or node.get("aweme_id")
                if _is_video_id(raw_id) and (
                    "stats" in node
                    or "statsV2" in node
                    or "desc" in node
                    or "video" in node
                    or "author" in node
                ):
                    items.append(node)
                    return
                struct = node.get("itemStruct")
                if isinstance(struct, dict):
                    items.append(struct)
                    return
                for v in node.values():
                    if isinstance(v, (dict, list)):
                        walk(v, depth + 1)
            elif isinstance(node, list):
                for el in node:
                    walk(el, depth + 1)

        walk(scope)

    return items


def extract_posts_from_hydration_payload(
    payload: object,
    *,
    profile_url: str = "",
) -> list[dict]:
    """Parse un JSON rehydration / SIGI et retourne des posts normalises."""
    if not isinstance(payload, dict):
        return []

    fallback_author = ""
    try:
        path = urlparse(profile_url).path
        for seg in path.split("/"):
            if seg.startswith("@"):
                fallback_author = seg[1:].lower()
                break
    except Exception:
        fallback_author = ""

    raw_items: list[dict] = []
    scope = payload.get("__DEFAULT_SCOPE__")
    if isinstance(scope, dict):
        raw_items.extend(_collect_raw_items_from_scope(scope))
    else:
        # Payload deja egal au scope, ou SIGI ItemModule top-level
        if any(str(k).startswith("webapp.") for k in payload.keys()):
            raw_items.extend(_collect_raw_items_from_scope(payload))
        for mod_key in ("ItemModule", "itemModule"):
            mod = payload.get(mod_key)
            if isinstance(mod, dict):
                raw_items.extend([x for x in mod.values() if isinstance(x, dict)])
        for list_key in ("itemList", "item_list"):
            lst = payload.get(list_key)
            if isinstance(lst, list):
                raw_items.extend([x for x in lst if isinstance(x, dict)])

    posts: list[dict] = []
    seen: set[str] = set()
    for item in raw_items:
        post = item_to_post(item, fallback_author=fallback_author)
        if not post:
            continue
        pid = post["post_id"]
        if pid in seen:
            continue
        seen.add(pid)
        posts.append(post)
    return posts


def _normalize_browser_ssr_post(raw: dict, fallback_author: str = "") -> dict | None:
    """Mappe un post extrait in-browser vers le format scraper."""
    if not isinstance(raw, dict):
        return None
    # Deja au format scraper ?
    if raw.get("post_id") and (raw.get("post_url") or raw.get("author")):
        return item_to_post(
            {
                "id": raw.get("post_id") or raw.get("id"),
                "desc": raw.get("desc") or raw.get("message") or raw.get("text_content") or "",
                "createTime": raw.get("createTime") or raw.get("published_at"),
                "author": {"uniqueId": raw.get("author") or fallback_author},
                "stats": raw.get("stats")
                or {
                    "diggCount": raw.get("likes"),
                    "playCount": raw.get("views"),
                    "commentCount": raw.get("comments") or raw.get("comments_count"),
                    "shareCount": raw.get("shares"),
                },
            },
            fallback_author=fallback_author,
        )
    return item_to_post(raw, fallback_author=fallback_author)


def extract_posts_from_hydration(page: Any, profile_url: str = "") -> tuple[int, list[dict]]:
    """Extrait les videos depuis __UNIVERSAL_DATA_FOR_REHYDRATION__ IN-BROWSER.

    Parse JSON dans le contexte page (evite de transferer 255KB via CDP, source
    fragile). Retourne (universalLen, posts_normalises).
    """
    fallback_author = ""
    try:
        path = urlparse(profile_url).path
        for seg in path.split("/"):
            if seg.startswith("@"):
                fallback_author = seg[1:].lower()
                break
    except Exception:
        fallback_author = ""

    try:
        result = page.evaluate(
            r"""
            (fallbackAuthor) => {
              const el = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__')
                || document.getElementById('SIGI_STATE')
                || document.getElementById('sigi-persisted-data');
              if (!el || !el.textContent) {
                return {
                  uniLen: 0, posts: [], error: 'missing',
                  scopeKeys: [], udKeys: [], source: null,
                };
              }
              const text = el.textContent;
              const uniLen = text.length;
              let data;
              try { data = JSON.parse(text); }
              catch (e) {
                return {
                  uniLen, posts: [], error: 'json:' + String(e),
                  scopeKeys: [], udKeys: [], source: null,
                };
              }

              const scope = data.__DEFAULT_SCOPE__ || {};
              const scopeKeys = Object.keys(scope).slice(0, 40);
              const userPostList = scope['webapp.user-post-list'] || {};
              const userDetail = scope['webapp.user-detail'] || {};
              const udKeys = Object.keys(userDetail || {});
              const uplKeys = Object.keys(userPostList || {});

              let rawItems = [];
              let itemModule = {};
              let source = null;

              // 1) webapp.user-post-list (prioritaire) — user-detail n'a souvent
              //    que userInfo/shareMeta/statusCode, PAS les videos.
              if (Array.isArray(userPostList.itemList) && userPostList.itemList.length > 0) {
                rawItems = userPostList.itemList;
                source = 'webapp.user-post-list.itemList';
              } else if (Array.isArray(userDetail.itemList) && userDetail.itemList.length > 0) {
                rawItems = userDetail.itemList;
                source = 'webapp.user-detail.itemList';
              }

              // 2) itemModule (map video_id -> item)
              if (userPostList.itemModule && typeof userPostList.itemModule === 'object'
                  && !Array.isArray(userPostList.itemModule)
                  && Object.keys(userPostList.itemModule).length > 0) {
                itemModule = userPostList.itemModule;
                source = source || 'webapp.user-post-list.itemModule';
              } else if (userDetail.itemModule && typeof userDetail.itemModule === 'object'
                  && !Array.isArray(userDetail.itemModule)
                  && Object.keys(userDetail.itemModule).length > 0) {
                itemModule = userDetail.itemModule;
                source = source || 'webapp.user-detail.itemModule';
              } else if (scope.itemModule && typeof scope.itemModule === 'object'
                  && !Array.isArray(scope.itemModule)
                  && Object.keys(scope.itemModule).length > 0) {
                itemModule = scope.itemModule;
                source = source || 'scope.itemModule';
              } else if (scope.ItemModule && typeof scope.ItemModule === 'object'
                  && !Array.isArray(scope.ItemModule)
                  && Object.keys(scope.ItemModule).length > 0) {
                itemModule = scope.ItemModule;
                source = source || 'scope.ItemModule';
              }

              // 3) Fallback: premier module du scope avec itemList / itemModule
              if (rawItems.length === 0 && Object.keys(itemModule).length === 0) {
                for (const key of Object.keys(scope)) {
                  const module = scope[key];
                  if (!module || typeof module !== 'object' || Array.isArray(module)) continue;
                  if (Array.isArray(module.itemList) && module.itemList.length > 0) {
                    rawItems = module.itemList;
                    source = key + '.itemList';
                    if (module.itemModule && typeof module.itemModule === 'object'
                        && !Array.isArray(module.itemModule)) {
                      itemModule = module.itemModule;
                    }
                    break;
                  }
                  if (module.itemModule && typeof module.itemModule === 'object'
                      && !Array.isArray(module.itemModule)
                      && Object.keys(module.itemModule).length > 0) {
                    itemModule = module.itemModule;
                    source = key + '.itemModule';
                    break;
                  }
                }
              }

              const isVid = (v) => /^\d{5,}$/.test(String(v || ''));
              const posts = [];
              const seen = new Set();

              const pushItem = (item, vidHint) => {
                if (!item || typeof item !== 'object') return;
                const id = String(
                  item.id || item.idCode || item.aweme_id || item.awemeId || vidHint || ''
                );
                if (!isVid(id) || seen.has(id)) return;
                seen.add(id);
                const authorNode = item.author || item.authorInfo || {};
                const author = (
                  typeof authorNode === 'object'
                    ? (authorNode.uniqueId || authorNode.unique_id || '')
                    : String(authorNode || '')
                ) || fallbackAuthor || 'user';
                const handle = String(author).replace(/^@/, '');
                const stats = item.stats || item.statsV2 || {};
                posts.push({
                  id: id,
                  post_id: id,
                  desc: item.desc || item.description || item.title || '',
                  createTime: item.createTime || item.create_time || null,
                  author: handle,
                  video_url: 'https://www.tiktok.com/@' + handle + '/video/' + id,
                  stats: stats,
                  likes: stats.diggCount ?? stats.diggCountValue ?? null,
                  views: stats.playCount ?? stats.playCountValue ?? null,
                  comments: stats.commentCount ?? stats.commentCountValue ?? null,
                  shares: stats.shareCount ?? stats.shareCountValue ?? null,
                });
              };

              // Priorite itemModule (objets complets), puis itemList
              if (Object.keys(itemModule).length > 0) {
                for (const [id, item] of Object.entries(itemModule)) {
                  if (item && typeof item === 'object') {
                    pushItem(item.itemStruct || item, id);
                  } else if (isVid(id)) {
                    pushItem({ id: id, author: { uniqueId: fallbackAuthor } }, id);
                  }
                }
              }

              if (rawItems.length > 0) {
                for (const item of rawItems) {
                  if (typeof item === 'string' || typeof item === 'number') {
                    // itemList d'IDs → enrichir via itemModule si possible
                    const id = String(item);
                    if (isVid(id)) {
                      const full = itemModule[id];
                      pushItem(
                        (full && typeof full === 'object')
                          ? (full.itemStruct || full)
                          : { id: id, author: { uniqueId: fallbackAuthor } },
                        id
                      );
                    }
                  } else if (item && typeof item === 'object') {
                    pushItem(item.itemStruct || item, item.id);
                  }
                }
              }

              // 4) Dernier recours: walk objets video + regex /video/ID
              if (posts.length === 0) {
                const walk = (node, depth) => {
                  if (depth > 14 || !node) return;
                  if (Array.isArray(node)) {
                    for (const n of node) walk(n, depth + 1);
                    return;
                  }
                  if (typeof node !== 'object') return;
                  const id = node.id || node.idCode || node.aweme_id;
                  if (isVid(id) && (node.stats || node.statsV2 || node.desc || node.video || node.author)) {
                    pushItem(node, id);
                    return;
                  }
                  if (node.itemStruct && typeof node.itemStruct === 'object') {
                    pushItem(node.itemStruct);
                    return;
                  }
                  for (const v of Object.values(node)) {
                    if (v && typeof v === 'object') walk(v, depth + 1);
                  }
                };
                walk(scope, 0);
                source = source || 'deep-walk';
              }

              if (posts.length === 0) {
                const re = /\/video\/(\d{10,})/g;
                let m;
                while ((m = re.exec(text)) !== null) {
                  pushItem({ id: m[1], author: { uniqueId: fallbackAuthor } }, m[1]);
                }
                if (posts.length > 0) source = 'regex-/video/';
              }

              return {
                uniLen,
                posts,
                error: null,
                scopeKeys,
                udKeys,
                uplKeys,
                source,
                itemListLen: rawItems.length,
                itemModuleSize: Object.keys(itemModule).length,
              };
            }
            """,
            fallback_author,
        )
    except Exception:
        LOGGER.warning("In-browser SSR hydration extract failed", exc_info=True)
        return 0, []

    if not isinstance(result, dict):
        return 0, []

    uni_len = int(result.get("uniLen") or 0)
    raw_posts = result.get("posts") or []
    if result.get("error"):
        LOGGER.warning(
            "SSR hydration probe error=%s uniLen=%s",
            result.get("error"),
            uni_len,
            extra={"url": profile_url},
        )
    if uni_len > 50000 and not raw_posts:
        LOGGER.warning(
            "SSR uniLen=%s but 0 posts — scopeKeys=%s udKeys=%s uplKeys=%s "
            "itemListLen=%s itemModuleSize=%s source=%s",
            uni_len,
            result.get("scopeKeys"),
            result.get("udKeys"),
            result.get("uplKeys"),
            result.get("itemListLen"),
            result.get("itemModuleSize"),
            result.get("source"),
            extra={"url": profile_url},
        )
    elif raw_posts:
        LOGGER.info(
            "SSR extract source=%s posts=%s itemListLen=%s itemModuleSize=%s",
            result.get("source"),
            len(raw_posts),
            result.get("itemListLen"),
            result.get("itemModuleSize"),
            extra={"url": profile_url},
        )

    posts: list[dict] = []
    seen: set[str] = set()
    for raw in raw_posts:
        post = _normalize_browser_ssr_post(raw, fallback_author=fallback_author)
        if not post:
            continue
        pid = post["post_id"]
        if pid in seen:
            continue
        seen.add(pid)
        posts.append(post)

    if posts:
        LOGGER.info(
            "Extrait %d videos depuis JSON SSR (universalLen=%s) — pas de scroll/API requis",
            len(posts),
            uni_len,
            extra={"url": profile_url},
        )
    return uni_len, posts


def probe_hydration_signals(page: Any) -> dict:
    """Signaux SSR legers pour Response Classifier (pas d'extraction posts)."""
    try:
        result = page.evaluate(
            r"""
            () => {
              const el = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__')
                || document.getElementById('SIGI_STATE')
                || document.getElementById('sigi-persisted-data');
              if (!el || !el.textContent) {
                return {
                  ssr_universal_len: 0,
                  ssr_scope_keys: [],
                  ssr_has_user_info: false,
                  ssr_has_post_list: false,
                  item_list_len: 0,
                };
              }
              let data = null;
              try { data = JSON.parse(el.textContent); } catch (e) {
                return {
                  ssr_universal_len: el.textContent.length,
                  ssr_scope_keys: [],
                  ssr_has_user_info: false,
                  ssr_has_post_list: false,
                  item_list_len: 0,
                  parse_error: String(e),
                };
              }
              const scope = (data && data.__DEFAULT_SCOPE__) || data || {};
              const keys = Object.keys(scope).slice(0, 40);
              const ud = scope['webapp.user-detail'] || {};
              const upl = scope['webapp.user-post-list'] || {};
              const items = upl.itemList || upl.item_list || [];
              const hasUser = !!(ud.userInfo || ud.user || ud.statusCode !== undefined);
              return {
                ssr_universal_len: el.textContent.length,
                ssr_scope_keys: keys,
                ssr_has_user_info: hasUser || keys.includes('webapp.user-detail'),
                ssr_has_post_list: keys.includes('webapp.user-post-list')
                  && Array.isArray(items) && items.length > 0,
                item_list_len: Array.isArray(items) ? items.length : 0,
              };
            }
            """
        )
    except Exception:
        LOGGER.debug("probe_hydration_signals failed", exc_info=True)
        return {
            "ssr_universal_len": 0,
            "ssr_scope_keys": [],
            "ssr_has_user_info": False,
            "ssr_has_post_list": False,
            "item_list_len": 0,
        }
    return result if isinstance(result, dict) else {
        "ssr_universal_len": 0,
        "ssr_scope_keys": [],
        "ssr_has_user_info": False,
        "ssr_has_post_list": False,
        "item_list_len": 0,
    }


def extract_posts_from_profile_page(page: Any, profile_url: str = "") -> list[dict]:
    """Lit #__UNIVERSAL_DATA_FOR_REHYDRATION__ / SIGI_STATE depuis la page profil."""
    # Preferer parse in-browser (fiable pour payloads ~255KB).
    try:
        _uni_len, posts = extract_posts_from_hydration(page, profile_url=profile_url)
        if posts:
            return posts
    except Exception:
        LOGGER.warning("extract_posts_from_hydration failed, trying Python parse", exc_info=True)

    try:
        script_content = page.evaluate(
            r"""
            () => {
              const byId = (id) => {
                const el = document.getElementById(id);
                return el && el.textContent ? el.textContent : null;
              };
              return byId('__UNIVERSAL_DATA_FOR_REHYDRATION__')
                || byId('SIGI_STATE')
                || byId('sigi-persisted-data')
                || null;
            }
            """
        )
    except Exception:
        LOGGER.warning("Failed to read hydration script from profile page", exc_info=True)
        return []

    if not script_content or not str(script_content).strip():
        return []

    try:
        data = json.loads(script_content)
    except json.JSONDecodeError as exc:
        LOGGER.warning("Hydration JSONDecodeError: %s", exc)
        return []
    except Exception as exc:
        LOGGER.warning("Failed to parse hydration JSON: %s", exc)
        return []

    posts = extract_posts_from_hydration_payload(data, profile_url=profile_url)
    if posts:
        LOGGER.info(
            "Profile-first hydration extracted %d posts with metrics/text",
            len(posts),
            extra={"url": profile_url},
        )
    return posts
