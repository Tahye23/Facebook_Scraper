package com.richatt.scraper.service;

import com.richatt.scraper.model.ScrapeJob;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.io.FileSystemResource;
import org.springframework.core.io.Resource;
import org.springframework.stereotype.Service;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;

/**
 * Sert le PDF 24h genere par worker-tiktok (seul artefact conserve).
 */
@Service
@Slf4j
public class ReportArtifactService {

    @Value("${VIDEO_REPORTS_DIR:video_reports}")
    private String videoReportsDir;

    public Path resolveArtifact(String rawPath) {
        if (rawPath == null || rawPath.isBlank()) {
            return null;
        }
        String normalized = rawPath.replace('\\', '/').trim();
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
            Path alt = Path.of(normalized).toAbsolutePath().normalize();
            if (Files.isRegularFile(alt)) {
                return alt;
            }
            log.warn("Report artifact not found: raw={} tried={}", rawPath, resolved);
            return null;
        }
        return resolved;
    }

    public String pdfPathFromJob(ScrapeJob job) {
        if (job == null || job.getMetadata() == null) {
            return null;
        }
        Map<String, Object> meta = job.getMetadata();
        Object pdf = meta.get("pdfPath");
        if (pdf == null) {
            pdf = meta.get("pdf_path");
        }
        // Fallback legacy jobs that only had htmlPath — ignore.
        return pdf != null ? String.valueOf(pdf) : null;
    }

    public Resource loadPdfReport(ScrapeJob job) {
        Path path = resolveArtifact(pdfPathFromJob(job));
        return path != null ? new FileSystemResource(path) : null;
    }
}
