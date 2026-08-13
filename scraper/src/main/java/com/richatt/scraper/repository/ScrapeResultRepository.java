package com.richatt.scraper.repository;

import com.richatt.scraper.model.Platform;
import com.richatt.scraper.model.ScrapeResult;
import org.springframework.data.domain.Pageable;
import org.springframework.data.mongodb.repository.MongoRepository;

import java.util.Collection;
import java.util.List;
import java.util.Optional;

public interface ScrapeResultRepository extends MongoRepository<ScrapeResult, String> {
    List<ScrapeResult> findByScrapeId(String scrapeId);

    // Cache de re-scraping: le document unique d'une video (upsert par
    // platform+postId) et le lookup batch utilise par l'endpoint interne.
    Optional<ScrapeResult> findFirstByPlatformAndPostId(Platform platform, String postId);

    List<ScrapeResult> findByPlatformAndPostIdIn(Platform platform, Collection<String> postIds);

    List<ScrapeResult> findByPlatformAndAuthorIgnoreCaseOrderByScrapedAtDesc(
            Platform platform,
            String author,
            Pageable pageable
    );

    List<ScrapeResult> findByScrapeIdOrderByIdAsc(String scrapeId, Pageable pageable);

    List<ScrapeResult> findByScrapeIdAndIdGreaterThanOrderByIdAsc(String scrapeId, String id, Pageable pageable);

    Optional<ScrapeResult> findFirstByScrapeIdAndPostId(String scrapeId, String postId);

    List<ScrapeResult> findByScrapeIdOrderByScrapedAtDesc(String scrapeId, Pageable pageable);

    List<ScrapeResult> findByPlatformOrderByScrapedAtDesc(Platform platform, Pageable pageable);

    List<ScrapeResult> findByPlatformAndScrapeIdOrderByScrapedAtDesc(
            Platform platform,
            String scrapeId,
            Pageable pageable
    );

    List<ScrapeResult> findAllByOrderByScrapedAtDesc(Pageable pageable);
}
