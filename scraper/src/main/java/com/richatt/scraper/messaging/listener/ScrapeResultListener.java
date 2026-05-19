package com.richatt.scraper.messaging.listener;

import com.richatt.scraper.config.rabbit.RabbitConfig;
import com.richatt.scraper.messaging.dto.ScrapeResultMessage;
import com.richatt.scraper.model.Platform;
import com.richatt.scraper.model.ScrapeJob;
import com.richatt.scraper.model.ScrapeResult;
import com.richatt.scraper.model.ScrapeStatus;
import com.richatt.scraper.repository.ScrapeJobRepository;
import com.richatt.scraper.repository.ScrapeResultRepository;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.amqp.rabbit.annotation.RabbitListener;
import org.springframework.stereotype.Component;

import java.time.Instant;

@Slf4j
@Component
@RequiredArgsConstructor
public class ScrapeResultListener {

    private final ScrapeJobRepository jobRepository;
    private final ScrapeResultRepository resultRepository;

    @RabbitListener(queues = RabbitConfig.QUEUE_RESULT)
    public void onResult(ScrapeResultMessage message) {
        log.info("Résultat reçu pour scrapeId={} success={}", message.getScrapeId(), message.isSuccess());

        ScrapeJob job = jobRepository.findByScrapeId(message.getScrapeId())
                .orElse(null);

        if (job == null) {
            log.warn("Aucun job trouvé pour scrapeId={}", message.getScrapeId());
            return;
        }

        if (!message.isSuccess()) {
            updateJobStatus(job, ScrapeStatus.FAILED, message.getErrorMessage());
            return;
        }

        // Sauvegarder le résultat dans scrape_results
        ScrapeResult result = ScrapeResult.builder()
                .scrapeId(message.getScrapeId())
                .platform(Platform.from(message.getPlatform()))
                .postId(message.getPostId())
                .author(message.getAuthor())
                .textContent(message.getTextContent())
                .hashtags(message.getHashtags())
                .metrics(message.getMetrics())
                .sourceUrl(message.getSourceUrl())
                .sourceMediaUrl(message.getSourceMediaUrl())
                .mediaPath(null)
                .publishedAt(message.getPublishedAt())
                .scrapedAt(message.getScrapedAt() != null ? message.getScrapedAt() : Instant.now())
                .build();

        resultRepository.save(result);
        log.info("ScrapeResult sauvegardé : postId={}", result.getPostId());

        // Mettre le job en SUCCESS
        updateJobStatus(job, ScrapeStatus.SUCCESS, null);
    }

    private void updateJobStatus(ScrapeJob job, ScrapeStatus status, String errorMessage) {
        job.setStatus(status);
        job.setErrorMessage(errorMessage);
        job.setUpdatedAt(Instant.now());
        jobRepository.save(job);
        log.info("Job {} → {}", job.getScrapeId(), status);
    }
}
