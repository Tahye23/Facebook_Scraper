package com.richatt.scraper.service;

import org.springframework.web.multipart.MultipartFile;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public final class CsvUrlExtractor {

    private static final Pattern URL_PATTERN = Pattern.compile("https?://[^\\s\\\"'<>]+", Pattern.CASE_INSENSITIVE);

    private CsvUrlExtractor() {
    }

    public static List<String> extractTikTokProfileUrls(MultipartFile file) {
        String name = file.getOriginalFilename();
        if (name == null || !name.toLowerCase().endsWith(".csv")) {
            throw new IllegalArgumentException("Only .csv files are supported");
        }

        Set<String> urls = new LinkedHashSet<>();
        try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(file.getInputStream(), StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) {
                Matcher matcher = URL_PATTERN.matcher(line);
                while (matcher.find()) {
                    String url = cleanupUrl(matcher.group());
                    if (url != null) {
                        urls.add(url);
                    }
                }
            }
        } catch (IOException e) {
            throw new IllegalArgumentException("Failed to read CSV file", e);
        }

        return new ArrayList<>(urls);
    }

    private static String cleanupUrl(String raw) {
        if (raw == null || raw.isBlank()) {
            return null;
        }
        String url = raw.trim();
        while (!url.isEmpty() && "),.;\"'".indexOf(url.charAt(url.length() - 1)) >= 0) {
            url = url.substring(0, url.length() - 1);
        }

        String lower = url.toLowerCase();
        if (!lower.contains("tiktok.com") || !lower.contains("/@")) {
            return null;
        }

        int q = url.indexOf('?');
        if (q >= 0) {
            url = url.substring(0, q);
        }
        while (url.endsWith("/")) {
            url = url.substring(0, url.length() - 1);
        }

        return url.isBlank() ? null : url;
    }
}
