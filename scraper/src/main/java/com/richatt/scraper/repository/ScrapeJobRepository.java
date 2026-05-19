package com.richatt.scraper.repository;

import com.richatt.scraper.model.ScrapeJob;
import org.springframework.data.mongodb.repository.MongoRepository;

import java.util.Optional;

public interface ScrapeJobRepository extends MongoRepository<ScrapeJob, String> {
    Optional<ScrapeJob> findByScrapeId(String scrapeId);
}
