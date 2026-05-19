package com.richatt.scraper.config.rabbit;

import org.springframework.amqp.core.Binding;
import org.springframework.amqp.core.BindingBuilder;
import org.springframework.amqp.core.DirectExchange;
import org.springframework.amqp.core.Queue;
import org.springframework.amqp.core.QueueBuilder;
import org.springframework.amqp.rabbit.connection.ConnectionFactory;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.amqp.support.converter.Jackson2JsonMessageConverter;
import org.springframework.amqp.support.converter.MessageConverter;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class RabbitConfig {

    // ─── EXCHANGE ─────────────────────────────────────────────────────────────
    public static final String SCRAPE_EXCHANGE = "scrape.exchange";

    // ─── ROUTING KEYS ─────────────────────────────────────────────────────────
    public static final String ROUTING_FACEBOOK = "scrape.facebook";
    public static final String ROUTING_TIKTOK   = "scrape.tiktok";
    public static final String ROUTING_RESULT   = "scrape.result";

    // ─── QUEUES ───────────────────────────────────────────────────────────────
    public static final String QUEUE_FACEBOOK   = "scraping_queue_facebook";
    public static final String QUEUE_TIKTOK     = "scraping_queue_tiktok";
    public static final String QUEUE_RESULT     = "scrape_result_queue";
    public static final String QUEUE_DLQ        = "scraping_queue_dlq";

    // ─── EXCHANGE BEAN ────────────────────────────────────────────────────────
    @Bean
    public DirectExchange scrapeExchange() {
        return new DirectExchange(SCRAPE_EXCHANGE, true, false);
    }

    // ─── QUEUES BEANS ─────────────────────────────────────────────────────────
    @Bean
    public Queue facebookQueue() {
        return QueueBuilder.durable(QUEUE_FACEBOOK)
                .deadLetterExchange(SCRAPE_EXCHANGE)
                .deadLetterRoutingKey("scrape.dlq")
                .build();
    }

    @Bean
    public Queue tiktokQueue() {
        return QueueBuilder.durable(QUEUE_TIKTOK)
                .deadLetterExchange(SCRAPE_EXCHANGE)
                .deadLetterRoutingKey("scrape.dlq")
                .build();
    }

    @Bean
    public Queue resultQueue() {
        return QueueBuilder.durable(QUEUE_RESULT).build();
    }

    @Bean
    public Queue deadLetterQueue() {
        return QueueBuilder.durable(QUEUE_DLQ).build();
    }

    // ─── BINDINGS ─────────────────────────────────────────────────────────────
    @Bean
    public Binding facebookBinding() {
        return BindingBuilder.bind(facebookQueue())
                .to(scrapeExchange())
                .with(ROUTING_FACEBOOK);
    }

    @Bean
    public Binding tiktokBinding() {
        return BindingBuilder.bind(tiktokQueue())
                .to(scrapeExchange())
                .with(ROUTING_TIKTOK);
    }

    @Bean
    public Binding resultBinding() {
        return BindingBuilder.bind(resultQueue())
                .to(scrapeExchange())
                .with(ROUTING_RESULT);
    }

    @Bean
    public Binding dlqBinding() {
        return BindingBuilder.bind(deadLetterQueue())
                .to(scrapeExchange())
                .with("scrape.dlq");
    }

    // ─── CONVERTER & TEMPLATE ─────────────────────────────────────────────────
    @Bean
    public MessageConverter messageConverter() {
        return new Jackson2JsonMessageConverter();
    }

    @Bean
    public RabbitTemplate rabbitTemplate(ConnectionFactory connectionFactory) {
        RabbitTemplate template = new RabbitTemplate(connectionFactory);
        template.setMessageConverter(messageConverter());
        return template;
    }
}
