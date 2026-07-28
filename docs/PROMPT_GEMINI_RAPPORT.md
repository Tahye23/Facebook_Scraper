# Prompt Gemini — générer le rapport développeurs

Copie-colle **tout ce bloc** dans Gemini (et attache / colle aussi le contenu de `docs/HANDOFF_DEVELOPERS.md`).

---

```text
Tu es un tech lead senior et un excellent rédacteur technique.

À partir du document source HANDOFF_DEVELOPERS.md (fourni ci-dessous / en pièce jointe), génère un RAPPORT EXPLICATIF COMPLET destiné aux développeurs qui reprendront le projet après moi.

## Objectif
Un document d’onboarding + maintenance + évolution :
- clair, professionnel, actionnable
- riche en diagrammes (Mermaid + ASCII)
- qui explique non seulement COMMENT ça marche, mais POURQUOI on a fait ces choix
- qui documente les incidents passés et les correctifs
- qui prévient les pièges

## Contraintes
- Français
- Ne pas inventer de features absentes du document source
- Si un détail manque : écrire “à vérifier dans le code” + indiquer le fichier probable
- Garder les noms techniques exacts (queues, services Docker, variables d’env, fichiers)
- Style documentation d’équipe (pas marketing)

## Structure obligatoire du rapport

1. Résumé exécutif (1/2 page)
2. Architecture globale
   - diagramme Mermaid C4-like / composants
   - tableau responsabilités
3. Décisions d’architecture (ADR légers)
   Pour chaque décision : Contexte / Décision / Pourquoi / Conséquences / Alternatives rejetées
   Couvrir au minimum :
   - Gateway + workers vs monolithe
   - RabbitMQ vs HTTP synchrone
   - Mongo 1 doc/(platform,postId) vs historique
   - Streaming métadonnées puis Gemini
   - Sticky proxy = sticky Chrome profile
   - Sample pool + blacklist vs IP unique
   - Chrome headed + Xvfb
   - TTL metrics 12h + exception metrics null
4. Déploiement Docker
   - schéma services
   - volumes critiques
   - commande de démarrage
5. Flux détaillés
   - séquence Mermaid : scrape profil unique
   - séquence Mermaid : CSV batch lanes
   - états du job (QUEUED→…→SUCCESS/FAILED)
6. Messaging RabbitMQ
   - exchange, queues, routing, DLQ
7. Streaming & enrichissement Gemini
   - ce qui est publié d’abord / ensuite
   - cache video_report
8. MongoDB
   - modèle
   - règles upsert/TTL
   - diagramme décision metrics/report
9. Proxies & sticky sessions
   - modèle mental 1 IP = 1 profil
   - algo sélection IP
   - table blacklist complète
   - quand on retourne 0 posts
10. Pipeline scrape TikTok
    - extraction DOM/JSON/réseau
    - enrichissement metrics pages vidéo
    - fail-fast soft-block
11. Rapports
    - video_report unitaire
    - rapport batch HTML/PDF
12. Observabilité / logs
13. Chronologie incidents & correctifs (tableau)
14. Carte du code (où modifier quoi)
15. Guide debug
16. Pièges à ne pas casser
17. Roadmap d’évolution priorisée
18. Checklist 10 tâches premier jour

## Exigences diagrammes
Inclure AU MINIMUM :
- 1 diagramme composants Docker
- 1 diagramme séquence profil unique
- 1 diagramme flux CSV lanes
- 1 diagramme sélection proxy / blacklist
- 1 diagramme upsert Mongo TTL
- 1 ASCII mental model sticky session

Utilise Mermaid compatible GitHub/GitLab markdown.

## Ton
Pédagogique mais précis. Un nouveau développeur doit pouvoir comprendre le système sans pair-programming.

## Sortie
Markdown propre, titres hiérarchiques, tableaux, diagrammes Mermaid, listes de pièges.

Maintenant, génère le rapport complet.
```

---

## Contenu source à coller juste après le prompt

Ouvre et copie intégralement :

`docs/HANDOFF_DEVELOPERS.md`

Puis envoie à Gemini.

## Astuce

Si Gemini tronque : demande en 2 passes :
1. “Génère sections 1 à 9”
2. “Continue sections 10 à 18 + tous les diagrammes manquants”
