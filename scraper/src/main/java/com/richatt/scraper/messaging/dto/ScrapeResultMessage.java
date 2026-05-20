package com.richatt.scraper.messaging.dto;

import com.fasterxml.jackson.annotation.JsonAlias;
import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.richatt.scraper.model.PostMetrics;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.util.List;

/**
 * Message publié par les workers Python (Facebook / TikTok)
 * vers scrape_result_queue après scraping.
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
@JsonIgnoreProperties(ignoreUnknown = true)
public class ScrapeResultMessage {

    /** Identifiant du job créé par la gateway (UUID) */
    @JsonAlias({"scrape_id", "scrapeId"})
    private String scrapeId;

    /** facebook | tiktok */
    private String platform;

    /** ID natif du post sur la plateforme */
    @JsonAlias({"post_id", "postId"})
    private String postId;

    private String author;
    @JsonAlias({"text_content", "textContent"})
    private String textContent;
    private List<String> hashtags;
    private PostMetrics metrics;

    @JsonAlias({"source_url", "sourceUrl"})
    private String sourceUrl;
    @JsonAlias({"source_media_url", "sourceMediaUrl"})
    private String sourceMediaUrl;

    /** Toujours null dans cette version (pas de stockage vidéo) */
    @JsonAlias({"media_path", "mediaPath"})
    private String mediaPath;

    @JsonAlias({"published_at", "publishedAt"})
    private String publishedAt;
    @JsonAlias({"scraped_at", "scrapedAt"})
    private String scrapedAt;

    /** true = scraping OK, false = erreur côté worker */
    private boolean success;

    /** Message d'erreur si success = false */
    @JsonAlias({"error_message", "errorMessage"})
    private String errorMessage;
}
