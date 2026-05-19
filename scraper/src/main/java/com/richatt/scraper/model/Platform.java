package com.richatt.scraper.model;

public enum Platform {
    FACEBOOK,
    TIKTOK;

    public static Platform from(String raw) {
        return Platform.valueOf(raw.trim().toUpperCase());
    }
}
