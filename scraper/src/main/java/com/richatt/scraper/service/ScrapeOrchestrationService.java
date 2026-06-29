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
