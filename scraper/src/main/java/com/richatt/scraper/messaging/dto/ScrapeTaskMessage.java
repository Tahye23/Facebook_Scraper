package com.richatt.scraper.messaging.dto;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.util.List;

@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class ScrapeTaskMessage {
    private String scrapeId;
    private String url;
    private List<String> urls;
    private String platform;
    private Integer maxPosts;
    private Integer timeWindowHours;
    private Boolean reportMode;
    private String reportType;
    private String requestedAt;
}
