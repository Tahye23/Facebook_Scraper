package com.richatt.scraper.messaging.dto;

import com.richatt.scraper.model.PostMetrics;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.time.Instant;
import java.util.List;

/**
 * Message publié par les workers Python (Facebook / TikTok)
 * vers scrape_result_queue après scraping.
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class ScrapeResultMessage {

    /** Identifiant du job créé par la gateway (UUID) */
    private String scrapeId;

    /** facebook | tiktok */
    private String platform;

    /** ID natif du post sur la plateforme */
    private String postId;

    private String author;
    private String textContent;
    private List<String> hashtags;
    private PostMetrics metrics;

    private String sourceUrl;
    private String sourceMediaUrl;

    /** Toujours null dans cette version (pas de stockage vidéo) */
    private String mediaPath;

    private Instant publishedAt;
    private Instant scrapedAt;

    /** true = scraping OK, false = erreur côté worker */
    private boolean success;

    /** Message d'erreur si success = false */
    private String errorMessage;
}
