package com.richatt.scraper.model;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;
import org.springframework.data.annotation.Id;
import org.springframework.data.mongodb.core.index.CompoundIndex;
import org.springframework.data.mongodb.core.index.Indexed;
import org.springframework.data.mongodb.core.mapping.Document;

import java.time.Instant;
import java.util.List;
import java.util.Map;

@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
@Document(collection = "scrape_results")
// Index compose (platform, postId): sert le "cache" de re-scraping. On upsert
// desormais par (platform, postId) => un seul document par video (pas
// d'historique), et les lookups par postId (endpoint interne) sont rapides.
// Non unique volontairement: d'anciens doublons issus de jobs precedents ne
// doivent pas faire echouer la creation de l'index; la logique d'upsert
// applicative garantit l'unicite pour les nouveaux ecrits.
@CompoundIndex(name = "platform_postId_idx", def = "{'platform': 1, 'postId': 1}")
public class ScrapeResult {
    @Id
    private String id;

    @Indexed
    private String scrapeId;

    private Platform platform;

    @Indexed
    private String postId;

    private String author;
    private String textContent;
    private List<String> hashtags;
    private PostMetrics metrics;
    private String sourceUrl;
    private String sourceMediaUrl;
    private String mediaPath;
    private Map<String, Object> videoReport;
    private Instant publishedAt;
    private Instant scrapedAt;
}
