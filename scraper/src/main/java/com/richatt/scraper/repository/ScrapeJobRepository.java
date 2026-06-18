package com.richatt.scraper.repository;

import com.richatt.scraper.model.Platform;
import com.richatt.scraper.model.ScrapeJob;
import com.richatt.scraper.model.ScrapeStatus;
import org.springframework.data.mongodb.repository.MongoRepository;
import org.springframework.data.domain.Pageable;

import java.util.List;
import java.util.Optional;

public interface ScrapeJobRepository extends MongoRepository<ScrapeJob, String> {
    Optional<ScrapeJob> findByScrapeId(String scrapeId);

    List<ScrapeJob> findAllByOrderByCreatedAtDesc(Pageable pageable);

    List<ScrapeJob> findByPlatformOrderByCreatedAtDesc(Platform platform, Pageable pageable);

    List<ScrapeJob> findByStatusOrderByCreatedAtDesc(ScrapeStatus status, Pageable pageable);

    List<ScrapeJob> findByPlatformAndStatusOrderByCreatedAtDesc(
            Platform platform,
            ScrapeStatus status,
            Pageable pageable
    );
}
