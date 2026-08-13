package com.richatt.scraper.messaging.dto;

import com.fasterxml.jackson.annotation.JsonAlias;
import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.util.List;

/**
 * Task publiee vers les workers Python.
 * Proprietes JSON en snake_case (contrat worker.py) + alias camelCase.
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class ScrapeTaskMessage {
    @JsonProperty("scrape_id")
    @JsonAlias({"scrapeId"})
    private String scrapeId;

    private String url;

    private List<String> urls;

    private String platform;

    @JsonProperty("max_posts")
    @JsonAlias({"maxPosts"})
    private Integer maxPosts;

    @JsonProperty("time_window_hours")
    @JsonAlias({"timeWindowHours"})
    private Integer timeWindowHours;

    @JsonProperty("report_mode")
    @JsonAlias({"reportMode"})
    private Boolean reportMode;

    @JsonProperty("report_type")
    @JsonAlias({"reportType"})
    private String reportType;

    @JsonProperty("force_refresh")
    @JsonAlias({"forceRefresh"})
    private Boolean forceRefresh;

    /**
     * FULL | METRICS_ONLY — FIX H.
     * METRICS_ONLY: Apify ok, pas de Gemini, upsert metrics seulement pour posts existants.
     */
    @JsonProperty("refresh_mode")
    @JsonAlias({"refreshMode"})
    private String refreshMode;

    @JsonProperty("requested_at")
    @JsonAlias({"requestedAt"})
    private String requestedAt;
}
