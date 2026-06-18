package com.richatt.scraper.messaging.listener;

import com.richatt.scraper.config.rabbit.RabbitConfig;
import com.richatt.scraper.model.Platform;
import com.richatt.scraper.model.PostMetrics;
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
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

@Slf4j
@Component
@RequiredArgsConstructor
public class ScrapeResultListener {

    private final ScrapeJobRepository jobRepository;
    private final ScrapeResultRepository resultRepository;

    @RabbitListener(queues = RabbitConfig.QUEUE_RESULT)
    public void onResult(Map<String, Object> message) {
        String scrapeId = getString(message, "scrapeId", "scrape_id");
        String eventType = getString(message, "eventType", "event_type");
        boolean success = getBoolean(message, "success");
        log.info("Résultat reçu pour scrapeId={} success={} eventType={}", scrapeId, success, eventType);

        ScrapeJob job = jobRepository.findByScrapeId(scrapeId)
                .orElse(null);

        if (job == null) {
            log.warn("Aucun job trouvé pour scrapeId={}", scrapeId);
            return;
        }

        if (!success) {
            updateJobStatus(job, ScrapeStatus.FAILED, getString(message, "errorMessage", "error_message"));
            return;
        }

        if ("COMPLETED".equalsIgnoreCase(eventType)) {
            updateJobStatus(job, ScrapeStatus.SUCCESS, null);
            return;
        }

        // Sauvegarder le résultat dans scrape_results
        ScrapeResult incoming = ScrapeResult.builder()
                .scrapeId(scrapeId)
                .platform(Platform.from(getString(message, "platform")))
                .postId(getString(message, "postId", "post_id"))
                .author(getString(message, "author"))
                .textContent(getString(message, "textContent", "text_content"))
                .hashtags(getStringList(message.get("hashtags")))
                .metrics(getMetrics(message.get("metrics")))
                .sourceUrl(getString(message, "sourceUrl", "source_url"))
                .sourceMediaUrl(getString(message, "sourceMediaUrl", "source_media_url"))
                .mediaPath(null)
                .publishedAt(parseInstant(getString(message, "publishedAt", "published_at")))
                .scrapedAt(parseInstantOrNow(getString(message, "scrapedAt", "scraped_at")))
                .build();

            ScrapeResult result = upsertResult(incoming);
        log.info("ScrapeResult sauvegardé : postId={}", result.getPostId());

        // Tant que des résultats arrivent, le job est en cours.
        updateJobStatus(job, ScrapeStatus.RUNNING, null);
    }

    private void updateJobStatus(ScrapeJob job, ScrapeStatus status, String errorMessage) {
        job.setStatus(status);
        job.setErrorMessage(errorMessage);
        job.setUpdatedAt(Instant.now());
        jobRepository.save(job);
        log.info("Job {} → {}", job.getScrapeId(), status);
    }

    private Instant parseInstant(String value) {
        if (value == null || value.isBlank()) {
            return null;
        }
        try {
            return Instant.parse(value);
        } catch (DateTimeParseException e) {
            log.warn("Impossible de parser l'instant '{}': {}", value, e.getMessage());
            return null;
        }
    }

    private Instant parseInstantOrNow(String value) {
        Instant parsed = parseInstant(value);
        return parsed != null ? parsed : Instant.now();
    }

    private String getString(Map<String, Object> message, String... keys) {
        for (String key : keys) {
            Object value = message.get(key);
            if (value != null) {
                return String.valueOf(value);
            }
        }
        return null;
    }

    private boolean getBoolean(Map<String, Object> message, String key) {
        Object value = message.get(key);
        if (value instanceof Boolean b) {
            return b;
        }
        return value != null && Boolean.parseBoolean(String.valueOf(value));
    }

    private List<String> getStringList(Object value) {
        if (!(value instanceof List<?> list)) {
            return List.of();
        }
        List<String> result = new ArrayList<>();
        for (Object item : list) {
            if (item != null) {
                result.add(String.valueOf(item));
            }
        }
        return result;
    }

    private PostMetrics getMetrics(Object value) {
        if (!(value instanceof Map<?, ?> map)) {
            return null;
        }
        return PostMetrics.builder()
                .likes(toLong(map.get("likes")))
                .comments(toLong(map.get("comments")))
                .shares(toLong(map.get("shares")))
                .views(toLong(map.get("views")))
                .build();
    }

    private ScrapeResult upsertResult(ScrapeResult incoming) {
        String postId = incoming.getPostId();
        if (postId == null || postId.isBlank()) {
            return resultRepository.save(incoming);
        }

        ScrapeResult existing = resultRepository
                .findFirstByScrapeIdAndPostId(incoming.getScrapeId(), postId)
                .orElse(null);

        if (existing == null) {
            return resultRepository.save(incoming);
        }

        existing.setPlatform(incoming.getPlatform());
        existing.setAuthor(preferNonBlank(incoming.getAuthor(), existing.getAuthor()));
        existing.setTextContent(preferNonBlank(incoming.getTextContent(), existing.getTextContent()));
        existing.setSourceUrl(preferNonBlank(incoming.getSourceUrl(), existing.getSourceUrl()));
        existing.setSourceMediaUrl(preferNonBlank(incoming.getSourceMediaUrl(), existing.getSourceMediaUrl()));
        existing.setMediaPath(preferNonBlank(incoming.getMediaPath(), existing.getMediaPath()));
        existing.setHashtags((incoming.getHashtags() == null || incoming.getHashtags().isEmpty())
                ? existing.getHashtags()
                : incoming.getHashtags());
        existing.setPublishedAt(incoming.getPublishedAt() != null ? incoming.getPublishedAt() : existing.getPublishedAt());
        existing.setScrapedAt(incoming.getScrapedAt() != null ? incoming.getScrapedAt() : existing.getScrapedAt());
        existing.setMetrics(mergeMetrics(existing.getMetrics(), incoming.getMetrics()));

        return resultRepository.save(existing);
    }

    private PostMetrics mergeMetrics(PostMetrics existing, PostMetrics incoming) {
        if (incoming == null) {
            return existing;
        }
        if (existing == null) {
            return incoming;
        }
        return PostMetrics.builder()
                .likes(incoming.getLikes() != null ? incoming.getLikes() : existing.getLikes())
                .comments(incoming.getComments() != null ? incoming.getComments() : existing.getComments())
                .shares(incoming.getShares() != null ? incoming.getShares() : existing.getShares())
                .views(incoming.getViews() != null ? incoming.getViews() : existing.getViews())
                .build();
    }

    private String preferNonBlank(String incoming, String fallback) {
        return incoming != null && !incoming.isBlank() ? incoming : fallback;
    }

    private Long toLong(Object value) {
        if (value == null) {
            return null;
        }
        if (value instanceof Number n) {
            return n.longValue();
        }
        try {
            return Long.parseLong(String.valueOf(value));
        } catch (NumberFormatException e) {
            return null;
        }
    }
}
