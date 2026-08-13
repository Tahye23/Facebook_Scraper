package com.richatt.scraper.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.json.JsonMapper;
import com.richatt.scraper.model.ScrapeJob;
import com.richatt.scraper.model.ScrapeResult;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.io.FileSystemResource;
import org.springframework.core.io.Resource;
import org.springframework.stereotype.Service;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Sert les rapports HTML/PDF generes par worker-tiktok (video_reports/).
 */
@Service
@Slf4j
public class ReportArtifactService {

    private final ObjectMapper objectMapper = JsonMapper.builder().build();

    @Value("${VIDEO_REPORTS_DIR:video_reports}")
    private String videoReportsDir;

    public Path resolveArtifact(String rawPath) {
        if (rawPath == null || rawPath.isBlank()) {
            return null;
        }
        String normalized = rawPath.replace('\\', '/').trim();
        // Les paths worker sont souvent "video_reports/xxx.html" ou absolus conteneur.
        String fileName = normalized;
        int idx = normalized.lastIndexOf('/');
        if (idx >= 0) {
            fileName = normalized.substring(idx + 1);
        }
        if (fileName.isBlank() || fileName.contains("..")) {
            return null;
        }
        Path base = Path.of(videoReportsDir).toAbsolutePath().normalize();
        Path resolved = base.resolve(fileName).normalize();
        if (!resolved.startsWith(base) || !Files.isRegularFile(resolved)) {
            // Essai: path relatif tel quel sous le cwd
            Path alt = Path.of(normalized).toAbsolutePath().normalize();
            if (Files.isRegularFile(alt)) {
                return alt;
            }
            log.warn("Report artifact not found: raw={} tried={}", rawPath, resolved);
            return null;
        }
        return resolved;
    }

    public Resource loadHtmlReport(ScrapeJob job) {
        if (job == null || job.getMetadata() == null) {
            return null;
        }
        Object html = job.getMetadata().get("htmlPath");
        if (html == null) {
            html = job.getMetadata().get("html_path");
        }
        Path path = resolveArtifact(html != null ? String.valueOf(html) : null);
        return path != null ? new FileSystemResource(path) : null;
    }

    /**
     * Mappe le JSON Gemini batch (rapport 24h) vers video_report par post_id / URL,
     * pour remplir la colonne Gemini de l'UI quand le batch n'a pas republie l'enrichissement.
     */
    public void attachBatchGeminiReports(ScrapeJob job, List<ScrapeResult> results) {
        if (job == null || job.getMetadata() == null || results == null || results.isEmpty()) {
            return;
        }
        Object geminiPath = job.getMetadata().get("geminiJsonPath");
        if (geminiPath == null) {
            geminiPath = job.getMetadata().get("gemini_json_path");
        }
        Path path = resolveArtifact(geminiPath != null ? String.valueOf(geminiPath) : null);
        if (path == null) {
            return;
        }
        try {
            JsonNode root = objectMapper.readTree(Files.readString(path));
            JsonNode report = root.path("report");
            JsonNode videos = report.path("videos");
            if (!videos.isArray()) {
                return;
            }
            Map<String, Map<String, Object>> byUrl = new HashMap<>();
            Map<String, Map<String, Object>> byId = new HashMap<>();
            for (JsonNode v : videos) {
                Map<String, Object> mapped = mapBatchVideoToReport(v);
                String url = v.path("post_url").asText("");
                if (!url.isBlank()) {
                    byUrl.put(url, mapped);
                    String id = extractVideoId(url);
                    if (!id.isBlank()) {
                        byId.put(id, mapped);
                    }
                }
            }
            for (ScrapeResult r : results) {
                if (r.getVideoReport() != null && !r.getVideoReport().isEmpty()) {
                    continue;
                }
                Map<String, Object> found = null;
                if (r.getSourceUrl() != null) {
                    found = byUrl.get(r.getSourceUrl());
                }
                if (found == null && r.getPostId() != null) {
                    found = byId.get(r.getPostId());
                }
                if (found != null) {
                    r.setVideoReport(found);
                }
            }
        } catch (Exception ex) {
            log.debug("Could not attach batch Gemini reports: {}", ex.getMessage());
        }
    }

    private static Map<String, Object> mapBatchVideoToReport(JsonNode v) {
        Map<String, Object> report = new HashMap<>();
        String desc = v.path("description_ar").asText("");
        String sentiment = v.path("sentiment_ar").asText("");
        String topic = v.path("topic_ar").asText("");
        report.put("executive_summary", desc.isBlank() ? List.of() : List.of(desc));
        report.put("sentiment", sentiment.isBlank() ? null : sentiment);
        report.put("themes", topic.isBlank() ? List.of() : List.of(topic));
        report.put("confidence_and_limits", Map.of("level", "batch_24h"));
        return report;
    }

    private static String extractVideoId(String url) {
        if (url == null) {
            return "";
        }
        int idx = url.lastIndexOf("/video/");
        if (idx < 0) {
            return "";
        }
        String rest = url.substring(idx + "/video/".length());
        int end = rest.indexOf('?');
        if (end < 0) {
            end = rest.indexOf('/');
        }
        return (end >= 0 ? rest.substring(0, end) : rest).trim();
    }
}
