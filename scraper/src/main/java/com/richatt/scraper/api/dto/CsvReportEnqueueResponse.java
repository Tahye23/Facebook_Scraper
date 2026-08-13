package com.richatt.scraper.api.dto;

public record CsvReportEnqueueResponse(
        String scrape_id,
        String status,
        int urls_count,
        int time_window_hours,
        String mode
) {
}
