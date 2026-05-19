package com.richatt.scraper.messaging.publisher;

import com.richatt.scraper.config.rabbit.RabbitConfig;
import com.richatt.scraper.messaging.dto.ScrapeTaskMessage;
import lombok.RequiredArgsConstructor;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.stereotype.Component;

@Component
@RequiredArgsConstructor
public class ScrapeTaskPublisher {

    private final RabbitTemplate rabbitTemplate;

    public void publish(ScrapeTaskMessage message) {
        String routingKey = switch (message.getPlatform().toLowerCase()) {
            case "facebook" -> RabbitConfig.ROUTING_FACEBOOK;
            case "tiktok"   -> RabbitConfig.ROUTING_TIKTOK;
            default -> throw new IllegalArgumentException("Unknown platform: " + message.getPlatform());
        };
        rabbitTemplate.convertAndSend(RabbitConfig.SCRAPE_EXCHANGE, routingKey, message);
    }
}
