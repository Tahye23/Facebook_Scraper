package com.richatt.scraper.api.dto;

import jakarta.validation.constraints.NotBlank;

public record ScrapeRequest(
        @NotBlank(message = "url is required")
        String url,
        @NotBlank(message = "platform is required")
        String platform
) {
}
