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
import com.richatt.scraper.util.TikTokUrlUtils;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.domain.PageRequest;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

@Service
@RequiredArgsConstructor
@Slf4j
public class ScrapeOrchestrationService {

    private final ScrapeJobRepository jobRepository;
    private final ScrapeResultRepository resultRepository;
    private final ScrapeTaskPublisher taskPublisher;

    /**
     * Fenetre temporelle CSV (heures). Aligne avec APIFY_CSV_REPORT_WINDOW_HOURS cote worker.
     */
    @Value("${scrape.csv-report-window-hours:24}")
    private int csvReportWindowHours;

    /** TTL cache: servir Mongo sans Apify si scrapedAt plus recent (FIX H). */
    @Value("${SCRAPE_METRICS_TTL_HOURS:24}")
    private int cacheTtlHours;

    public int csvReportWindowHours() {
        return Math.max(1, csvReportWindowHours);
    }

    public int cacheTtlHours() {
        return Math.max(1, cacheTtlHours);
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
        boolean forceRefresh = Boolean.TRUE.equals(request.forceRefresh());

        ScrapeJob job = ScrapeJob.builder()
                .scrapeId(scrapeId)
                .url(request.url())
                .platform(platform)
                .status(ScrapeStatus.QUEUED)
                .createdAt(now)
                .updatedAt(now)
                .build();

        jobRepository.save(job);

        // FIX H (TikTok): cache Mongo avant RabbitMQ.
        if (platform == Platform.TIKTOK && !forceRefresh) {
            String author = TikTokUrlUtils.extractAuthor(request.url());
            if (!author.isBlank()) {
                List<ScrapeResult> authorPosts = resultRepository
                        .findByPlatformAndAuthorIgnoreCaseOrderByScrapedAtDesc(
                                Platform.TIKTOK, author, PageRequest.of(0, Math.max(maxPosts * 3, 60)));

                List<ScrapeResult> fresh = new ArrayList<>();
                int staleCount = 0;
                for (ScrapeResult r : authorPosts) {
                    if (isWithinCacheTtl(r.getScrapedAt(), now)) {
                        fresh.add(r);
                    } else {
                        staleCount++;
                    }
                }

                if (fresh.size() >= maxPosts) {
                    List<ScrapeResult> served = fresh.subList(0, maxPosts);
                    for (ScrapeResult r : served) {
                        r.setScrapeId(scrapeId);
                        resultRepository.save(r);
                    }
                    Map<String, Object> meta = new HashMap<>();
                    meta.put("from_cache", true);
                    meta.put("cache_ttl_hours", cacheTtlHours());
                    meta.put("served_count", served.size());
                    meta.put("author", author);
                    job.setMetadata(meta);
                    job.setStatus(ScrapeStatus.SUCCESS);
                    job.setUpdatedAt(Instant.now());
                    jobRepository.save(job);
                    log.info("Cache hit (<{}h): scrapeId={} author={} served={}",
                            cacheTtlHours(), scrapeId, author, served.size());
                    return job;
                }

                // Des posts existent mais hors TTL → refresh metrics only.
                String refreshMode = (!authorPosts.isEmpty() || staleCount > 0) ? "METRICS_ONLY" : null;
                try {
                    taskPublisher.publish(ScrapeTaskMessage.builder()
                            .scrapeId(scrapeId)
                            .url(request.url())
                            .platform(platform.name().toLowerCase())
                            .maxPosts(maxPosts)
                            .forceRefresh(false)
                            .refreshMode(refreshMode)
                            .requestedAt(now.toString())
                            .build());
                } catch (RuntimeException ex) {
                    failPublish(job, ex);
                    throw ex;
                }
                log.info("Enqueue tiktok scrapeId={} author={} refreshMode={} fresh={} stale={}",
                        scrapeId, author, refreshMode, fresh.size(), staleCount);
                return job;
            }
        }

        try {
            taskPublisher.publish(ScrapeTaskMessage.builder()
                    .scrapeId(scrapeId)
                    .url(request.url())
                    .platform(platform.name().toLowerCase())
                    .maxPosts(maxPosts)
                    .forceRefresh(forceRefresh)
                    .refreshMode(forceRefresh ? "FULL" : null)
                    .requestedAt(now.toString())
                    .build());
        } catch (RuntimeException ex) {
            failPublish(job, ex);
            throw ex;
        }

        return job;
    }

    private void failPublish(ScrapeJob job, RuntimeException ex) {
        job.setStatus(ScrapeStatus.FAILED);
        job.setErrorMessage("Failed to publish task: " + ex.getMessage());
        job.setUpdatedAt(Instant.now());
        jobRepository.save(job);
    }

    private boolean isWithinCacheTtl(Instant scrapedAt, Instant now) {
        if (scrapedAt == null) {
            return false;
        }
        return Duration.between(scrapedAt, now).compareTo(Duration.ofHours(cacheTtlHours())) < 0;
    }

    /**
     * @deprecated Prefer {@link #enqueueTikTokCsvBatchLast24h(List)} — CSV = fenetre temporelle.
     */
    @Deprecated
    public ScrapeJob enqueueTikTokCsvBatch(List<String> urls, int maxPostsPerPage) {
        return enqueueTikTokCsvBatchLast24h(urls);
    }

    public ScrapeJob enqueueTikTokCsvBatchLast24h(List<String> urls) {
        if (urls == null || urls.isEmpty()) {
            throw new IllegalArgumentException("No TikTok profile URLs found in uploaded CSV");
        }

        Instant now = Instant.now();
        String scrapeId = UUID.randomUUID().toString();
        int windowHours = csvReportWindowHours();
        // FIX G: plus de fetchLimit=100 — le filtre date Apify fait le travail.
        // max_posts reste un plafond de securite (= quota journalier typique).
        int safetyCeiling = 33;

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
            taskPublisher.publish(ScrapeTaskMessage.builder()
                    .scrapeId(scrapeId)
                    .url(urls.get(0))
                    .urls(urls)
                    .platform("tiktok")
                    .maxPosts(safetyCeiling)
                    .reportMode(true)
                    .reportType("csv_24h")
                    .timeWindowHours(windowHours)
                    .requestedAt(now.toString())
                    .build());
        } catch (RuntimeException ex) {
            failPublish(job, ex);
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
