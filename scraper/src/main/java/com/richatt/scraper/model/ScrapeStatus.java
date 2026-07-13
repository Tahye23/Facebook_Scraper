package com.richatt.scraper.model;

public enum ScrapeStatus {
    QUEUED,
    RUNNING,
    SUCCESS,
    // Le scraping s'est termine avec au moins un resultat exploitable, mais
    // une partie des donnees demandees n'a pas pu etre recuperee (challenge
    // TikTok, page en echec, etc.). Considere comme un etat terminal, au
    // meme titre que SUCCESS/FAILED.
    PARTIAL_SUCCESS,
    FAILED
}
