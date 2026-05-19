package com.richatt.scraper.messaging.dto;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class ScrapeTaskMessage {
    private String scrapeId;
    private String url;
    private String platform;
    private String requestedAt;
}
