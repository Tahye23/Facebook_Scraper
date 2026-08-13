package com.richatt.scraper.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.json.JsonMapper;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneOffset;
import java.util.HashMap;
import java.util.Map;

/**
 * Usage Apify (mensuel) + quota journalier interne (apify_quota.json).
 * Le token n'est jamais expose au frontend — uniquement cote gateway.
 *
 * Note Spring Boot 4: pas d'injection ObjectMapper (bean pas toujours expose) —
 * on instancie un JsonMapper local.
 */
@Service
@Slf4j
public class ApifyUsageService {

    private final ObjectMapper objectMapper = JsonMapper.builder().build();

    private final HttpClient httpClient = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(10))
            .build();

    @Value("${APIFY_TOKEN:}")
    private String apifyToken;

    @Value("${APIFY_DAILY_VIDEO_LIMIT:33}")
    private int dailyVideoLimit;

    @Value("${APIFY_QUOTA_FILE:apify_quota.json}")
    private String quotaFile;

    private volatile Map<String, Object> cachedApifyLimits;
    private volatile Instant cachedAt;

    public Map<String, Object> dashboardUsage() {
        Map<String, Object> out = new HashMap<>();
        out.put("apify", fetchApifyLimitsCached());
        out.put("daily", readDailyQuota());
        out.put("fetched_at", Instant.now().toString());
        return out;
    }

    private Map<String, Object> fetchApifyLimitsCached() {
        Instant now = Instant.now();
        if (cachedApifyLimits != null && cachedAt != null
                && Duration.between(cachedAt, now).compareTo(Duration.ofMinutes(5)) < 0) {
            return cachedApifyLimits;
        }
        Map<String, Object> fresh = fetchApifyLimits();
        cachedApifyLimits = fresh;
        cachedAt = now;
        return fresh;
    }

    private Map<String, Object> fetchApifyLimits() {
        Map<String, Object> result = new HashMap<>();
        String token = apifyToken == null ? "" : apifyToken.trim();
        if (token.isEmpty()) {
            result.put("available", false);
            result.put("error", "APIFY_TOKEN not configured on gateway");
            return result;
        }
        try {
            String url = "https://api.apify.com/v2/users/me/limits?token="
                    + URLEncoder.encode(token, StandardCharsets.UTF_8);
            HttpRequest req = HttpRequest.newBuilder(URI.create(url))
                    .timeout(Duration.ofSeconds(15))
                    .GET()
                    .header("Accept", "application/json")
                    .build();
            HttpResponse<String> resp = httpClient.send(req, HttpResponse.BodyHandlers.ofString());
            if (resp.statusCode() >= 400) {
                result.put("available", false);
                result.put("error", "Apify HTTP " + resp.statusCode());
                return result;
            }
            JsonNode root = objectMapper.readTree(resp.body());
            JsonNode data = root.path("data");
            JsonNode current = data.path("current");
            JsonNode limits = data.path("limits");
            double monthlyUsed = current.path("monthlyUsageUsd").asDouble(0);
            double monthlyMax = limits.path("maxMonthlyUsageUsd").asDouble(5);
            long ramMb = 0;
            if (current.has("monthlyActorMemoryGbytes")) {
                ramMb = Math.round(current.path("monthlyActorMemoryGbytes").asDouble(0) * 1024);
            } else if (current.has("actorMemoryGbytes")) {
                ramMb = Math.round(current.path("actorMemoryGbytes").asDouble(0) * 1024);
            }
            long ramMaxMb = Math.round(limits.path("maxActorMemoryGbytes").asDouble(16) * 1024);

            result.put("available", true);
            result.put("monthly_usage_usd", monthlyUsed);
            result.put("max_monthly_usage_usd", monthlyMax);
            result.put("ram_mb", ramMb);
            result.put("max_ram_mb", ramMaxMb > 0 ? ramMaxMb : 16L * 1024);
            return result;
        } catch (Exception ex) {
            log.warn("Apify limits fetch failed: {}", ex.getMessage());
            result.put("available", false);
            result.put("error", ex.getMessage());
            return result;
        }
    }

    private Map<String, Object> readDailyQuota() {
        Map<String, Object> daily = new HashMap<>();
        int limit = Math.max(1, dailyVideoLimit);
        String today = LocalDate.now(ZoneOffset.UTC).toString();
        int count = 0;
        String date = today;
        Path path = Path.of(quotaFile);
        try {
            if (Files.isRegularFile(path)) {
                JsonNode node = objectMapper.readTree(Files.readString(path));
                date = node.path("date").asText(today);
                count = node.path("count").asInt(0);
                if (!today.equals(date)) {
                    count = 0;
                    date = today;
                }
            }
        } catch (Exception ex) {
            log.debug("Daily quota file read failed: {}", ex.getMessage());
        }
        daily.put("date", date);
        daily.put("count", Math.max(0, count));
        daily.put("limit", limit);
        daily.put("remaining", Math.max(0, limit - count));
        return daily;
    }
}
