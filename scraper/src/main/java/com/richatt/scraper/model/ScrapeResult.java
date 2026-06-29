package com.richatt.scraper.model;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;
import org.springframework.data.annotation.Id;
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
