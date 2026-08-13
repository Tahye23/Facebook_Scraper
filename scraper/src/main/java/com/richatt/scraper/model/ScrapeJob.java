package com.richatt.scraper.model;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;
import org.springframework.data.annotation.Id;
import org.springframework.data.mongodb.core.index.Indexed;
import org.springframework.data.mongodb.core.mapping.Document;

import java.time.Instant;
import java.util.Map;

@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
@Document(collection = "scrape_jobs")
public class ScrapeJob {
    @Id
    private String id;

    @Indexed(unique = true)
    private String scrapeId;

    private String url;
    private Platform platform;
    private ScrapeStatus status;
    private String errorMessage;
    /** Code machine pour le frontend (ex: QUOTA_EXCEEDED). */
    private String errorReason;
    private Map<String, Object> metadata;
    private Instant createdAt;
    private Instant updatedAt;
}
