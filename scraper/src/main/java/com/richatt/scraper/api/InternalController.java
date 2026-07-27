package com.richatt.scraper.api;

import com.richatt.scraper.model.Platform;
import com.richatt.scraper.model.ScrapeResult;
import com.richatt.scraper.repository.ScrapeResultRepository;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Endpoints INTERNES (worker -> gateway), non destines au public.
 *
 * Sert le "cache de re-scraping": avant d'appeler Gemini, le worker demande
 * quelles videos (postId) possedent deja une analyse IA (video_report). Il peut
 * ainsi sauter l'analyse (etape la plus couteuse) et reutiliser l'existante.
 *
 * Protege par un token partage (header X-Internal-Token) quand INTERNAL_API_TOKEN
 * est defini. Si le token n'est pas configure, l'endpoint reste ouvert (dev).
 */
@Slf4j
@RestController
@RequestMapping("/internal")
@RequiredArgsConstructor
public class InternalController {

    private final ScrapeResultRepository resultRepository;

    @Value("${INTERNAL_API_TOKEN:}")
    private String internalApiToken;

    @PostMapping("/results/reports")
    public ResponseEntity<?> lookupExistingReports(
            @RequestHeader(name = "X-Internal-Token", required = false) String token,
            @RequestBody Map<String, Object> body
    ) {
        if (internalApiToken != null && !internalApiToken.isBlank()
                && !internalApiToken.equals(token)) {
            return ResponseEntity.status(HttpStatus.UNAUTHORIZED)
                    .body(Map.of("error", "invalid internal token"));
        }

        Platform platform;
        try {
            platform = Platform.from(String.valueOf(body.get("platform")));
        } catch (Exception ex) {
            return ResponseEntity.badRequest().body(Map.of("error", "platform must be one of: facebook, tiktok"));
        }

        List<String> postIds = toStringList(body.get("postIds"));
        if (postIds.isEmpty()) {
            return ResponseEntity.ok(Map.of("reports", Map.of()));
        }

        // {postId -> video_report} uniquement pour les docs ayant une analyse non vide.
        Map<String, Object> reports = new HashMap<>();
        for (ScrapeResult r : resultRepository.findByPlatformAndPostIdIn(platform, postIds)) {
            String pid = r.getPostId();
            Map<String, Object> report = r.getVideoReport();
            if (pid != null && !pid.isBlank() && report != null && !report.isEmpty()
                    && !reports.containsKey(pid)) {
                reports.put(pid, report);
            }
        }

        log.info("Internal report lookup: platform={} requested={} found={}",
                platform, postIds.size(), reports.size());
        return ResponseEntity.ok(Map.of("reports", reports));
    }

    private List<String> toStringList(Object value) {
        List<String> result = new ArrayList<>();
        if (value instanceof List<?> list) {
            for (Object item : list) {
                if (item != null) {
                    String s = String.valueOf(item).trim();
                    if (!s.isEmpty()) {
                        result.add(s);
                    }
                }
            }
        }
        return result;
    }
}
