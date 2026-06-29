package com.richatt.scraper.api.dto;

import com.fasterxml.jackson.annotation.JsonAlias;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;

public record ScrapeRequest(
        @NotBlank(message = "url is required")
        String url,
        @NotBlank(message = "platform is required")
        String platform,
        @JsonAlias("max_posts")
        @Min(value = 1, message = "maxPosts must be >= 1")
        @Max(value = 200, message = "maxPosts must be <= 200")
        Integer maxPosts
) {
}
