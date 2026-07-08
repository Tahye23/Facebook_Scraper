package com.richatt.scraper.api.dto;

public record CsvReportEnqueueResponse(
        String scrape_id,
        String status,
        int urls_count,
        int max_posts_per_page
) {
}
