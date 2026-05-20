package com.richatt.scraper.api;

import com.richatt.scraper.api.dto.ScrapeRequest;
import com.richatt.scraper.api.dto.ScrapeResponse;
import com.richatt.scraper.model.ScrapeJob;
import com.richatt.scraper.model.ScrapeResult;
import com.richatt.scraper.model.ScrapeStatus;
import com.richatt.scraper.service.ScrapeOrchestrationService;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/scrape")
@RequiredArgsConstructor
public class ScrapeController {

    private final ScrapeOrchestrationService orchestrationService;

    @PostMapping
    public ResponseEntity<ScrapeResponse> enqueue(@Valid @RequestBody ScrapeRequest request) {
        ScrapeJob job = orchestrationService.enqueue(request);
        return ResponseEntity.accepted().body(
                new ScrapeResponse(job.getScrapeId(), job.getStatus().name())
        );
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
}
