package com.richatt.scraper.api.dto;

import com.fasterxml.jackson.annotation.JsonAlias;
import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;

public record ScrapeRequest(
        @NotBlank(message = "url is required")
        String url,
        @NotBlank(message = "platform is required")
        String platform,
        @JsonProperty("max_posts")
        @JsonAlias({"maxPosts"})
        @Min(value = 1, message = "maxPosts must be >= 1")
        @Max(value = 200, message = "maxPosts must be <= 200")
        Integer maxPosts
) {
}
