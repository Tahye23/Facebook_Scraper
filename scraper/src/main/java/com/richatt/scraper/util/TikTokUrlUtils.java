package com.richatt.scraper.util;

/**
 * Extraction du handle TikTok depuis une URL ou un @user.
 */
public final class TikTokUrlUtils {

    private TikTokUrlUtils() {
    }

    public static String extractAuthor(String urlOrHandle) {
        if (urlOrHandle == null || urlOrHandle.isBlank()) {
            return "";
        }
        String raw = urlOrHandle.trim();
        for (String segment : raw.split("[/?#]")) {
            String s = segment.trim();
            if (s.startsWith("@") && s.length() > 1) {
                return s.substring(1).toLowerCase();
            }
        }
        String cleaned = raw.replace("@", "").trim();
        if (!cleaned.isEmpty() && !cleaned.contains("/") && !cleaned.contains(" ")) {
            return cleaned.toLowerCase();
        }
        return "";
    }
}
