package com.richatt.scraper.api;

import com.richatt.scraper.api.dto.ScrapeRequest;
import com.richatt.scraper.api.dto.ScrapeResponse;
import com.richatt.scraper.api.dto.CsvReportEnqueueResponse;
import com.richatt.scraper.model.ScrapeJob;
import com.richatt.scraper.model.Platform;
import com.richatt.scraper.model.ScrapeResult;
import com.richatt.scraper.model.ScrapeStatus;
import com.richatt.scraper.service.CsvUrlExtractor;
import com.richatt.scraper.service.ScrapeOrchestrationService;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
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

    @PostMapping
    public ResponseEntity<ScrapeResponse> enqueue(@Valid @RequestBody ScrapeRequest request) {
        ScrapeJob job = orchestrationService.enqueue(request);
        return ResponseEntity.accepted().body(
                new ScrapeResponse(job.getScrapeId(), job.getStatus().name())
        );
    }

    @PostMapping(value = "/csv-report", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public ResponseEntity<?> enqueueCsvReport(
            @RequestPart("file") MultipartFile file,
            @RequestParam(name = "maxPostsPerPage", defaultValue = "3") int maxPostsPerPage
    ) {
        if (maxPostsPerPage < 1 || maxPostsPerPage > 20) {
            return ResponseEntity.badRequest().body(Map.of("error", "maxPostsPerPage must be between 1 and 20"));
        }

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
            ScrapeJob job = orchestrationService.enqueueTikTokCsvBatch(urls, maxPostsPerPage);
            return ResponseEntity.accepted().body(new CsvReportEnqueueResponse(
                    job.getScrapeId(),
                    job.getStatus() != null ? job.getStatus().name() : "QUEUED",
                    urls.size(),
                    maxPostsPerPage
            ));
        } catch (IllegalArgumentException ex) {
            return ResponseEntity.badRequest().body(Map.of("error", ex.getMessage()));
        }
    }

    @PostMapping(value = "/csv-report-24h", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public ResponseEntity<?> enqueueCsvReportLast24h(
            @RequestPart("file") MultipartFile file
    ) {
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
            return ResponseEntity.accepted().body(Map.of(
                    "scrape_id", job.getScrapeId(),
                    "status", job.getStatus() != null ? job.getStatus().name() : "QUEUED",
                    "urls_count", urls.size(),
                    "mode", "last_24h",
                    "time_window_hours", 24
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
            return ResponseEntity.ok(Map.of(
                "scrape_id", scrapeId,
                "status", job.getStatus().name(),
                "message", job.getErrorMessage() != null ? job.getErrorMessage() : "Scraping failed",
                "count", 0,
                "results", List.of()
            ));
        }

        return ResponseEntity.ok(Map.of(
                "scrape_id", scrapeId,
            "status", job.getStatus().name(),
                "count", results.size(),
                "results", results
        ));
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
