package com.richatt.scraper.api;

import com.richatt.scraper.model.Platform;
import com.richatt.scraper.model.PostMetrics;
import com.richatt.scraper.model.ScrapeResult;
import com.richatt.scraper.repository.ScrapeResultRepository;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.domain.PageRequest;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Endpoints INTERNES (worker -> gateway), non destines au public.
 *
 * - /results/reports : cache Gemini video_report
 * - /results/fresh   : posts avec metrics fraiches (TTL) pour skip Apify
 */
@Slf4j
@RestController
@RequestMapping("/internal")
@RequiredArgsConstructor
public class InternalController {

    private final ScrapeResultRepository resultRepository;

    @Value("${INTERNAL_API_TOKEN:}")
    private String internalApiToken;

    @Value("${SCRAPE_METRICS_TTL_HOURS:12}")
    private long metricsTtlHours;

    @PostMapping("/results/reports")
    public ResponseEntity<?> lookupExistingReports(
            @RequestHeader(name = "X-Internal-Token", required = false) String token,
            @RequestBody Map<String, Object> body
    ) {
        if (!authorize(token)) {
            return ResponseEntity.status(HttpStatus.UNAUTHORIZED)
                    .body(Map.of("error", "invalid internal token"));
        }

        Platform platform;
        try {
            platform = Platform.from(String.valueOf(body.get("platform")));
        } catch (Exception ex) {
            return ResponseEntity.badRequest().body(Map.of("error", "platform must be one of: facebook, tiktok"));
        }

        List<String> postIds = toStringList(body.get("postIds"));
        if (postIds.isEmpty()) {
            return ResponseEntity.ok(Map.of("reports", Map.of()));
        }

        Map<String, Object> reports = new HashMap<>();
        for (ScrapeResult r : resultRepository.findByPlatformAndPostIdIn(platform, postIds)) {
            String pid = r.getPostId();
            Map<String, Object> report = r.getVideoReport();
            if (pid != null && !pid.isBlank() && report != null && !report.isEmpty()
                    && !reports.containsKey(pid)) {
                reports.put(pid, report);
            }
        }

        log.info("Internal report lookup: platform={} requested={} found={}",
                platform, postIds.size(), reports.size());
        return ResponseEntity.ok(Map.of("reports", reports));
    }

    /**
     * Retourne les posts d'un auteur dont les metrics sont encore fraiches (TTL).
     * Utilise par worker-tiktok (moteur Apify) pour ne PAS consommer le quota
     * quand un re-scrape ne mettrait de toute facon pas a jour les metrics.
     *
     * Body: { platform, author, maxPosts?, maxAgeHours? }
     */
    @PostMapping("/results/fresh")
    public ResponseEntity<?> lookupFreshPosts(
            @RequestHeader(name = "X-Internal-Token", required = false) String token,
            @RequestBody Map<String, Object> body
    ) {
        if (!authorize(token)) {
            return ResponseEntity.status(HttpStatus.UNAUTHORIZED)
                    .body(Map.of("error", "invalid internal token"));
        }

        Platform platform;
        try {
            platform = Platform.from(String.valueOf(body.get("platform")));
        } catch (Exception ex) {
            return ResponseEntity.badRequest().body(Map.of("error", "platform must be one of: facebook, tiktok"));
        }

        String author = String.valueOf(body.getOrDefault("author", "")).trim().replace("@", "");
        if (author.isBlank()) {
            return ResponseEntity.badRequest().body(Map.of("error", "author is required"));
        }

        int maxPosts = 20;
        Object rawMax = body.get("maxPosts");
        if (rawMax == null) {
            rawMax = body.get("max_posts");
        }
        if (rawMax != null) {
            try {
                maxPosts = Math.max(1, Math.min(200, Integer.parseInt(String.valueOf(rawMax))));
            } catch (NumberFormatException ignored) {
                maxPosts = 20;
            }
        }

        Integer maxAgeHours = null;
        Object rawAge = body.get("maxAgeHours");
        if (rawAge == null) {
            rawAge = body.get("max_age_hours");
        }
        if (rawAge != null) {
            try {
                int age = Integer.parseInt(String.valueOf(rawAge));
                if (age > 0) {
                    maxAgeHours = age;
                }
            } catch (NumberFormatException ignored) {
                // ignore
            }
        }

        // Over-fetch puis filtre TTL / fenetre publiee.
        int fetch = Math.min(200, Math.max(maxPosts * 3, maxPosts));
        List<ScrapeResult> candidates = resultRepository
                .findByPlatformAndAuthorIgnoreCaseOrderByScrapedAtDesc(
                        platform, author, PageRequest.of(0, fetch));

        Instant now = Instant.now();
        List<Map<String, Object>> posts = new ArrayList<>();
        for (ScrapeResult r : candidates) {
            if (!isFreshMetrics(r, now)) {
                continue;
            }
            if (maxAgeHours != null && r.getPublishedAt() != null) {
                if (Duration.between(r.getPublishedAt(), now).compareTo(Duration.ofHours(maxAgeHours)) > 0) {
                    continue;
                }
            } else if (maxAgeHours != null && r.getPublishedAt() == null) {
                continue;
            }
            posts.add(toWorkerPost(r));
            if (maxAgeHours == null && posts.size() >= maxPosts) {
                break;
            }
        }

        boolean enough = maxAgeHours != null
                ? !posts.isEmpty()
                : posts.size() >= maxPosts;

        log.info("Internal fresh lookup: platform={} author={} found={} enough={} ttlHours={}",
                platform, author, posts.size(), enough, metricsTtlHours);

        return ResponseEntity.ok(Map.of(
                "posts", posts,
                "count", posts.size(),
                "enough", enough,
                "ttl_hours", metricsTtlHours
        ));
    }

    private boolean authorize(String token) {
        if (internalApiToken == null || internalApiToken.isBlank()) {
            return true;
        }
        return internalApiToken.equals(token);
    }

    private boolean isFreshMetrics(ScrapeResult r, Instant now) {
        if (r.getScrapedAt() == null || metricsTtlHours <= 0) {
            return false;
        }
        if (Duration.between(r.getScrapedAt(), now).compareTo(Duration.ofHours(metricsTtlHours)) >= 0) {
            return false;
        }
        PostMetrics m = r.getMetrics();
        if (m == null) {
            return false;
        }
        // Au moins une metrique non nulle = exploitable.
        return m.getLikes() != null || m.getViews() != null
                || m.getComments() != null || m.getShares() != null;
    }

    private Map<String, Object> toWorkerPost(ScrapeResult r) {
        Map<String, Object> post = new HashMap<>();
        post.put("post_id", r.getPostId());
        post.put("id", r.getPostId());
        post.put("author", r.getAuthor());
        post.put("message", r.getTextContent());
        post.put("text", r.getTextContent());
        post.put("text_content", r.getTextContent());
        post.put("hashtags", r.getHashtags() != null ? r.getHashtags() : List.of());
        post.put("post_url", r.getSourceUrl());
        post.put("published_at", r.getPublishedAt() != null ? r.getPublishedAt().toString() : null);
        post.put("scraped_at", r.getScrapedAt() != null ? r.getScrapedAt().toString() : null);
        PostMetrics m = r.getMetrics();
        if (m != null) {
            post.put("likes", m.getLikes());
            post.put("comments_count", m.getComments());
            post.put("shares", m.getShares());
            post.put("views", m.getViews());
            post.put("metrics", Map.of(
                    "likes", m.getLikes(),
                    "comments", m.getComments(),
                    "shares", m.getShares(),
                    "views", m.getViews()
            ));
        }
        if (r.getVideoReport() != null && !r.getVideoReport().isEmpty()) {
            post.put("video_report", r.getVideoReport());
        }
        return post;
    }

    private List<String> toStringList(Object value) {
        List<String> result = new ArrayList<>();
        if (value instanceof List<?> list) {
            for (Object item : list) {
                if (item != null) {
                    String s = String.valueOf(item).trim();
                    if (!s.isEmpty()) {
                        result.add(s);
                    }
                }
            }
        }
        return result;
    }
}
