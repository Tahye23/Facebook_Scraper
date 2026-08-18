package com.richatt.scraper.api;

import com.richatt.scraper.api.dto.ScrapeRequest;
import com.richatt.scraper.api.dto.ScrapeResponse;
import com.richatt.scraper.api.dto.CsvReportEnqueueResponse;
import com.richatt.scraper.model.ScrapeJob;
import com.richatt.scraper.model.Platform;
import com.richatt.scraper.model.ScrapeResult;
import com.richatt.scraper.model.ScrapeStatus;
import com.richatt.scraper.service.ApifyUsageService;
import com.richatt.scraper.service.CsvUrlExtractor;
import com.richatt.scraper.service.ReportArtifactService;
import com.richatt.scraper.service.ScrapeOrchestrationService;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.core.io.Resource;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.RequestPart;
import org.springframework.web.multipart.MultipartFile;

import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/scrape")
@RequiredArgsConstructor
public class ScrapeController {

    private static final int MAX_LIMIT = 200;
    private static final int MAX_WAIT_MS = 30000;

    private final ScrapeOrchestrationService orchestrationService;
    private final ApifyUsageService usageService;
    private final ReportArtifactService reportArtifactService;

    @PostMapping
    public ResponseEntity<ScrapeResponse> enqueue(@Valid @RequestBody ScrapeRequest request) {
        ScrapeJob job = orchestrationService.enqueue(request);
        return ResponseEntity.accepted().body(
                new ScrapeResponse(job.getScrapeId(), job.getStatus().name())
        );
    }

    /**
     * Batch CSV TikTok: toutes les videos des dernieres N heures (defaut 24h).
     * Plus de maxPostsPerPage — la fenetre temporelle est la seule contrainte.
     * Alias historique: /csv-report-24h (meme comportement).
     */
    @PostMapping(value = "/csv-report", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public ResponseEntity<?> enqueueCsvReport(@RequestPart("file") MultipartFile file) {
        return enqueueCsvReportWindow(file);
    }

    @PostMapping(value = "/csv-report-24h", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public ResponseEntity<?> enqueueCsvReportLast24h(@RequestPart("file") MultipartFile file) {
        return enqueueCsvReportWindow(file);
    }

    private ResponseEntity<?> enqueueCsvReportWindow(MultipartFile file) {
        List<String> urls;
        try {
            urls = CsvUrlExtractor.extractTikTokProfileUrls(file);
        } catch (IllegalArgumentException ex) {
            return ResponseEntity.badRequest().body(Map.of("error", ex.getMessage()));
        }

        if (urls.isEmpty()) {
            return ResponseEntity.badRequest().body(Map.of("error", "No TikTok profile URLs found in CSV"));
        }

        try {
            ScrapeJob job = orchestrationService.enqueueTikTokCsvBatchLast24h(urls);
            int windowHours = orchestrationService.csvReportWindowHours();
            return ResponseEntity.accepted().body(new CsvReportEnqueueResponse(
                    job.getScrapeId(),
                    job.getStatus() != null ? job.getStatus().name() : "QUEUED",
                    urls.size(),
                    windowHours,
                    "last_" + windowHours + "h"
            ));
        } catch (IllegalArgumentException ex) {
            return ResponseEntity.badRequest().body(Map.of("error", ex.getMessage()));
        }
    }

    @GetMapping("/{scrapeId}")
    public ResponseEntity<?> getJob(@PathVariable String scrapeId) {
        return orchestrationService.getJob(scrapeId)
                .<ResponseEntity<?>>map(ResponseEntity::ok)
                .orElseGet(() -> ResponseEntity.notFound().build());
    }

    @GetMapping("/{scrapeId}/results")
    public ResponseEntity<?> getResults(@PathVariable String scrapeId) {
        ScrapeJob job = orchestrationService.getJob(scrapeId).orElse(null);
        if (job == null) {
            return ResponseEntity.status(HttpStatus.NOT_FOUND).body(Map.of(
                "scrape_id", scrapeId,
                "message", "Scrape job not found"
            ));
        }

        List<ScrapeResult> results = orchestrationService.getResultsByScrapeId(scrapeId);

        if ((job.getStatus() == ScrapeStatus.QUEUED || job.getStatus() == ScrapeStatus.RUNNING)
            && results.isEmpty()) {
            return ResponseEntity.status(HttpStatus.ACCEPTED).body(Map.of(
                "scrape_id", scrapeId,
                "status", job.getStatus().name(),
                "message", "Scraping in progress, results not ready yet",
                "count", 0,
                "results", List.of()
            ));
        }

        if (job.getStatus() == ScrapeStatus.FAILED && results.isEmpty()) {
            Map<String, Object> body = new java.util.HashMap<>();
            body.put("scrape_id", scrapeId);
            body.put("status", job.getStatus().name());
            body.put("message", job.getErrorMessage() != null ? job.getErrorMessage() : "Scraping failed");
            body.put("error_reason", job.getErrorReason() != null ? job.getErrorReason() : "");
            body.put("count", 0);
            body.put("results", List.of());
            return ResponseEntity.ok(body);
        }

        Map<String, Object> ok = new java.util.HashMap<>();
        ok.put("scrape_id", scrapeId);
        ok.put("status", job.getStatus().name());
        ok.put("count", results.size());
        ok.put("results", results);
        if (job.getErrorReason() != null) {
            ok.put("error_reason", job.getErrorReason());
        }
        if (job.getErrorMessage() != null) {
            ok.put("message", job.getErrorMessage());
        }
        if (job.getMetadata() != null) {
            ok.put("metadata", job.getMetadata());
            String pdf = reportArtifactService.pdfPathFromJob(job);
            if (pdf != null && !pdf.isBlank()) {
                ok.put("report_url", "/scrape/" + scrapeId + "/report");
                ok.put("pdf_path", pdf);
            }
        }
        return ResponseEntity.ok(ok);
    }

    /**
     * Telecharge le PDF 24h (attachment, pas d'ouverture inline HTML).
     */
    @GetMapping("/{scrapeId}/report")
    public ResponseEntity<Resource> downloadReport(@PathVariable String scrapeId) {
        ScrapeJob job = orchestrationService.getJob(scrapeId).orElse(null);
        if (job == null) {
            return ResponseEntity.notFound().build();
        }
        Resource resource = reportArtifactService.loadPdfReport(job);
        if (resource == null || !resource.exists()) {
            return ResponseEntity.notFound().build();
        }
        String filename = resource.getFilename() != null
                ? resource.getFilename()
                : ("rapport_" + scrapeId + ".pdf");
        return ResponseEntity.ok()
                .header(HttpHeaders.CONTENT_DISPOSITION, "attachment; filename=\"" + filename + "\"")
                .contentType(MediaType.APPLICATION_PDF)
                .body(resource);
    }

    /**
     * Export CSV des resultats du job (mode profil / debug).
     */
    @GetMapping(value = "/{scrapeId}/export.csv", produces = "text/csv")
    public ResponseEntity<String> exportCsv(@PathVariable String scrapeId) {
        ScrapeJob job = orchestrationService.getJob(scrapeId).orElse(null);
        if (job == null) {
            return ResponseEntity.notFound().build();
        }
        List<ScrapeResult> results = orchestrationService.getResultsByScrapeId(scrapeId);
        StringBuilder sb = new StringBuilder();
        sb.append("post_id,author,text,likes,comments,shares,views,published_at,source_url,sentiment,themes\n");
        for (ScrapeResult r : results) {
            String likes = "", comments = "", shares = "", views = "";
            if (r.getMetrics() != null) {
                likes = r.getMetrics().getLikes() != null ? r.getMetrics().getLikes().toString() : "";
                comments = r.getMetrics().getComments() != null ? r.getMetrics().getComments().toString() : "";
                shares = r.getMetrics().getShares() != null ? r.getMetrics().getShares().toString() : "";
                views = r.getMetrics().getViews() != null ? r.getMetrics().getViews().toString() : "";
            }
            String sentiment = "";
            String themes = "";
            if (r.getVideoReport() != null) {
                Object s = r.getVideoReport().get("sentiment");
                if (s != null) sentiment = String.valueOf(s);
                Object t = r.getVideoReport().get("themes");
                if (t instanceof List<?> list) {
                    themes = list.stream().map(String::valueOf).reduce((a, b) -> a + "|" + b).orElse("");
                }
            }
            sb.append(csv(r.getPostId())).append(',')
                    .append(csv(r.getAuthor())).append(',')
                    .append(csv(r.getTextContent())).append(',')
                    .append(csv(likes)).append(',')
                    .append(csv(comments)).append(',')
                    .append(csv(shares)).append(',')
                    .append(csv(views)).append(',')
                    .append(csv(r.getPublishedAt() != null ? r.getPublishedAt().toString() : "")).append(',')
                    .append(csv(r.getSourceUrl())).append(',')
                    .append(csv(sentiment)).append(',')
                    .append(csv(themes)).append('\n');
        }
        return ResponseEntity.ok()
                .header("Content-Disposition", "attachment; filename=\"scrape_" + scrapeId + ".csv\"")
                .body(sb.toString());
    }

    private static String csv(String value) {
        if (value == null) {
            return "\"\"";
        }
        return "\"" + value.replace("\"", "\"\"") + "\"";
    }

    @GetMapping("/{scrapeId}/results/stream")
    public ResponseEntity<?> streamResults(
            @PathVariable String scrapeId,
            @RequestParam(required = false) String cursor,
            @RequestParam(defaultValue = "20") int limit,
            @RequestParam(defaultValue = "0") int waitMs
    ) {
        ScrapeJob job = orchestrationService.getJob(scrapeId).orElse(null);
        if (job == null) {
            return ResponseEntity.status(HttpStatus.NOT_FOUND).body(Map.of(
                    "scrape_id", scrapeId,
                    "message", "Scrape job not found"
            ));
        }

        int safeLimit;
        int safeWaitMs;
        try {
            safeLimit = validateLimit(limit);
            safeWaitMs = validateWaitMs(waitMs);
        } catch (IllegalArgumentException ex) {
            return ResponseEntity.badRequest().body(Map.of("error", ex.getMessage()));
        }

        long deadline = System.currentTimeMillis() + safeWaitMs;
        List<ScrapeResult> fetched = List.of();

        while (true) {
            fetched = orchestrationService.getResultsAfterCursor(scrapeId, cursor, safeLimit + 1);
            if (!fetched.isEmpty()) {
                break;
            }

            ScrapeStatus currentStatus = orchestrationService.getJob(scrapeId)
                    .map(ScrapeJob::getStatus)
                    .orElse(ScrapeStatus.FAILED);
            if (currentStatus == ScrapeStatus.SUCCESS
                    || currentStatus == ScrapeStatus.PARTIAL_SUCCESS
                    || currentStatus == ScrapeStatus.FAILED) {
                break;
            }

            if (safeWaitMs == 0 || System.currentTimeMillis() >= deadline) {
                break;
            }

            try {
                Thread.sleep(1000);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                break;
            }
        }

        boolean hasMore = fetched.size() > safeLimit;
        List<ScrapeResult> items = hasMore ? fetched.subList(0, safeLimit) : fetched;
        String nextCursor = items.isEmpty() ? cursor : items.get(items.size() - 1).getId();

        ScrapeStatus latestStatus = orchestrationService.getJob(scrapeId)
                .map(ScrapeJob::getStatus)
                .orElse(job.getStatus());
        boolean done = latestStatus == ScrapeStatus.SUCCESS
                || latestStatus == ScrapeStatus.PARTIAL_SUCCESS
                || latestStatus == ScrapeStatus.FAILED;

        return ResponseEntity.ok(Map.of(
                "scrape_id", scrapeId,
                "status", latestStatus.name(),
                "cursor", cursor == null ? "" : cursor,
                "next_cursor", nextCursor == null ? "" : nextCursor,
                "has_more", hasMore,
                "done", done,
                "count", items.size(),
                "results", items
        ));
    }

    @GetMapping("/jobs")
    public ResponseEntity<?> listJobs(
            @RequestParam(required = false) String platform,
            @RequestParam(required = false) String status,
            @RequestParam(defaultValue = "50") int limit
    ) {
        Platform parsedPlatform;
        ScrapeStatus parsedStatus;
        int safeLimit;

        try {
            parsedPlatform = parsePlatform(platform);
            parsedStatus = parseStatus(status);
            safeLimit = validateLimit(limit);
        } catch (IllegalArgumentException ex) {
            return ResponseEntity.badRequest().body(Map.of("error", ex.getMessage()));
        }

        List<ScrapeJob> jobs = orchestrationService.listJobs(parsedPlatform, parsedStatus, safeLimit);

        return ResponseEntity.ok(Map.of(
                "count", jobs.size(),
                "limit", safeLimit,
                "filters", Map.of(
                        "platform", parsedPlatform != null ? parsedPlatform.name() : "ALL",
                        "status", parsedStatus != null ? parsedStatus.name() : "ALL"
                ),
                "jobs", jobs
        ));
    }

    /**
     * Dashboard usage (FIX I): Apify monthly $ + quota journalier interne + scrapes recents.
     * Token Apify reste cote serveur (jamais expose au browser).
     */
    @GetMapping("/usage")
    public ResponseEntity<?> usage() {
        return ResponseEntity.ok(usageService.dashboardUsage());
    }

    @GetMapping("/results")
    public ResponseEntity<?> listResults(
            @RequestParam(required = false) String scrapeId,
            @RequestParam(required = false) String platform,
            @RequestParam(defaultValue = "50") int limit
    ) {
        Platform parsedPlatform;
        int safeLimit;

        try {
            parsedPlatform = parsePlatform(platform);
            safeLimit = validateLimit(limit);
        } catch (IllegalArgumentException ex) {
            return ResponseEntity.badRequest().body(Map.of("error", ex.getMessage()));
        }

        List<ScrapeResult> results = orchestrationService.listResults(scrapeId, parsedPlatform, safeLimit);

        return ResponseEntity.ok(Map.of(
                "count", results.size(),
                "limit", safeLimit,
                "filters", Map.of(
                        "scrapeId", scrapeId != null ? scrapeId : "ALL",
                        "platform", parsedPlatform != null ? parsedPlatform.name() : "ALL"
                ),
                "results", results
        ));
    }

    private Platform parsePlatform(String platform) {
        if (platform == null || platform.isBlank()) {
            return null;
        }
        try {
            return Platform.from(platform);
        } catch (IllegalArgumentException ex) {
            throw new IllegalArgumentException("platform must be one of: facebook, tiktok");
        }
    }

    private ScrapeStatus parseStatus(String status) {
        if (status == null || status.isBlank()) {
            return null;
        }
        try {
            return ScrapeStatus.valueOf(status.trim().toUpperCase());
        } catch (IllegalArgumentException ex) {
            throw new IllegalArgumentException("status must be one of: QUEUED, RUNNING, SUCCESS, PARTIAL_SUCCESS, FAILED");
        }
    }

    private int validateLimit(int limit) {
        if (limit <= 0 || limit > MAX_LIMIT) {
            throw new IllegalArgumentException("limit must be between 1 and " + MAX_LIMIT);
        }
        return limit;
    }

    private int validateWaitMs(int waitMs) {
        if (waitMs < 0 || waitMs > MAX_WAIT_MS) {
            throw new IllegalArgumentException("waitMs must be between 0 and " + MAX_WAIT_MS);
        }
        return waitMs;
    }
}
