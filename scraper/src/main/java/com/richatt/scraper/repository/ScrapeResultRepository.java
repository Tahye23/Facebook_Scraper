package com.richatt.scraper.repository;

import com.richatt.scraper.model.Platform;
import com.richatt.scraper.model.ScrapeResult;
import org.springframework.data.domain.Pageable;
import org.springframework.data.mongodb.repository.MongoRepository;

import java.util.List;
import java.util.Optional;

public interface ScrapeResultRepository extends MongoRepository<ScrapeResult, String> {
    List<ScrapeResult> findByScrapeId(String scrapeId);

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
