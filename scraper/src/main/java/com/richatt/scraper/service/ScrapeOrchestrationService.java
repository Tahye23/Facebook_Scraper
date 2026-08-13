package com.richatt.scraper.service;

import com.richatt.scraper.api.dto.ScrapeRequest;
import com.richatt.scraper.messaging.dto.ScrapeTaskMessage;
import com.richatt.scraper.messaging.publisher.ScrapeTaskPublisher;
import com.richatt.scraper.model.Platform;
import com.richatt.scraper.model.ScrapeJob;
import com.richatt.scraper.model.ScrapeResult;
import com.richatt.scraper.model.ScrapeStatus;
import com.richatt.scraper.repository.ScrapeJobRepository;
import com.richatt.scraper.repository.ScrapeResultRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.domain.PageRequest;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

@Service
@RequiredArgsConstructor
public class ScrapeOrchestrationService {

    private final ScrapeJobRepository jobRepository;
    private final ScrapeResultRepository resultRepository;
    private final ScrapeTaskPublisher taskPublisher;

    /**
     * Fenetre temporelle CSV (heures). Aligne avec APIFY_CSV_REPORT_WINDOW_HOURS cote worker.
     * Plafond de fetch Apify par profil (pas une limite metier) = 200.
     */
    @Value("${scrape.csv-report-window-hours:24}")
    private int csvReportWindowHours;

    @Value("${scrape.csv-report-fetch-limit:100}")
    private int csvReportFetchLimit;

    public int csvReportWindowHours() {
        return Math.max(1, csvReportWindowHours);
    }

    public ScrapeJob enqueue(ScrapeRequest request) {
        Platform platform;
        try {
            platform = Platform.from(request.platform());
        } catch (IllegalArgumentException e) {
            throw new InvalidPlatformException("platform must be one of: facebook, tiktok");
        }

        Instant now = Instant.now();
        String scrapeId = UUID.randomUUID().toString();
        int maxPosts = request.maxPosts() != null ? request.maxPosts() : 20;

        ScrapeJob job = ScrapeJob.builder()
                .scrapeId(scrapeId)
                .url(request.url())
                .platform(platform)
                .status(ScrapeStatus.QUEUED)
                .createdAt(now)
                .updatedAt(now)
                .build();

        jobRepository.save(job);

        try {
            taskPublisher.publish(ScrapeTaskMessage.builder()
                    .scrapeId(scrapeId)
                    .url(request.url())
                    .platform(platform.name().toLowerCase())
                    .maxPosts(maxPosts)
                    .requestedAt(now.toString())
                    .build());
        } catch (RuntimeException ex) {
            job.setStatus(ScrapeStatus.FAILED);
            job.setErrorMessage("Failed to publish task: " + ex.getMessage());
            job.setUpdatedAt(Instant.now());
            jobRepository.save(job);
            throw ex;
        }

        return job;
    }

    /**
     * @deprecated Prefer {@link #enqueueTikTokCsvBatchLast24h(List)} — CSV = fenetre temporelle.
     */
    @Deprecated
    public ScrapeJob enqueueTikTokCsvBatch(List<String> urls, int maxPostsPerPage) {
        // Ignore maxPostsPerPage: meme comportement que le rapport 24h.
        return enqueueTikTokCsvBatchLast24h(urls);
    }

    public ScrapeJob enqueueTikTokCsvBatchLast24h(List<String> urls) {
        if (urls == null || urls.isEmpty()) {
            throw new IllegalArgumentException("No TikTok profile URLs found in uploaded CSV");
        }

        Instant now = Instant.now();
        String scrapeId = UUID.randomUUID().toString();
        int windowHours = csvReportWindowHours();
        int fetchLimit = Math.max(1, Math.min(csvReportFetchLimit, 200));

        ScrapeJob job = ScrapeJob.builder()
                .scrapeId(scrapeId)
                .url("csv-batch-" + windowHours + "h:" + urls.size())
                .platform(Platform.TIKTOK)
                .status(ScrapeStatus.QUEUED)
                .createdAt(now)
                .updatedAt(now)
                .build();

        jobRepository.save(job);

        try {
            // max_posts = plafond de FETCH Apify (pas une limite metier).
            // time_window_hours = contrainte reelle (videos des N dernieres heures).
            taskPublisher.publish(ScrapeTaskMessage.builder()
                    .scrapeId(scrapeId)
                    .url(urls.get(0))
                    .urls(urls)
                    .platform("tiktok")
                    .maxPosts(fetchLimit)
                    .reportMode(true)
                    .reportType("csv_24h")
                    .timeWindowHours(windowHours)
                    .requestedAt(now.toString())
                    .build());
        } catch (RuntimeException ex) {
            job.setStatus(ScrapeStatus.FAILED);
            job.setErrorMessage("Failed to publish task: " + ex.getMessage());
            job.setUpdatedAt(Instant.now());
            jobRepository.save(job);
            throw ex;
        }

        return job;
    }

    public Optional<ScrapeJob> getJob(String scrapeId) {
        return jobRepository.findByScrapeId(scrapeId);
    }

    public List<ScrapeResult> getResultsByScrapeId(String scrapeId) {
        return resultRepository.findByScrapeId(scrapeId);
    }

    public List<ScrapeResult> getResultsAfterCursor(String scrapeId, String cursor, int limitPlusOne) {
        PageRequest page = PageRequest.of(0, limitPlusOne);
        if (cursor == null || cursor.isBlank()) {
            return resultRepository.findByScrapeIdOrderByIdAsc(scrapeId, page);
        }
        return resultRepository.findByScrapeIdAndIdGreaterThanOrderByIdAsc(scrapeId, cursor, page);
    }

    public List<ScrapeJob> listJobs(Platform platform, ScrapeStatus status, int limit) {
        PageRequest page = PageRequest.of(0, limit);

        if (platform != null && status != null) {
            return jobRepository.findByPlatformAndStatusOrderByCreatedAtDesc(platform, status, page);
        }
        if (platform != null) {
            return jobRepository.findByPlatformOrderByCreatedAtDesc(platform, page);
        }
        if (status != null) {
            return jobRepository.findByStatusOrderByCreatedAtDesc(status, page);
        }
        return jobRepository.findAllByOrderByCreatedAtDesc(page);
    }

    public List<ScrapeResult> listResults(String scrapeId, Platform platform, int limit) {
        PageRequest page = PageRequest.of(0, limit);

        if (scrapeId != null && platform != null) {
            return resultRepository.findByPlatformAndScrapeIdOrderByScrapedAtDesc(platform, scrapeId, page);
        }
        if (scrapeId != null) {
            return resultRepository.findByScrapeIdOrderByScrapedAtDesc(scrapeId, page);
        }
        if (platform != null) {
            return resultRepository.findByPlatformOrderByScrapedAtDesc(platform, page);
        }
        return resultRepository.findAllByOrderByScrapedAtDesc(page);
    }
}
