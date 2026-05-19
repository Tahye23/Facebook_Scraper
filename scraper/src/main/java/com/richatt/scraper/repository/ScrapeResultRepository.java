package com.richatt.scraper.repository;

import com.richatt.scraper.model.ScrapeResult;
import org.springframework.data.mongodb.repository.MongoRepository;

import java.util.List;

public interface ScrapeResultRepository extends MongoRepository<ScrapeResult, String> {
    List<ScrapeResult> findByScrapeId(String scrapeId);
}
