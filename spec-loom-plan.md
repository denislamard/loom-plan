# Spec — loom-plan (serveur MCP agenda : Google Calendar + Google Tasks)

Version : brouillon v1.2 — 13/09/2026
Nom du serveur : `loom-plan` (dépôt, serveur MCP et package `loom_plan`)

## 1. Objectif

Donner à Claude Desktop / Cowork une vue unifiée et actionnable de l'agenda et des tâches de Denis, avec Google comme unique référentiel. Le serveur n'a **aucun état propre** en dehors du token OAuth et d'un cache court en lecture.

Hors périmètre v1 : déclenchement d'actions automatiques (jobs), synchronisation avec un store local, agendas partagés tiers, interface HTML rendue par le serveur (MCP Apps — envisagé en v2 si le rendu markdown se révèle insuffisant).

## 2. Principes

1. **Google est la source de vérité.** Aucune donnée métier n'est stockée côté serveur.
2. **Vocabulaire pensé pour Claude.** Claude ne voit jamais un objet Google brut : un seul format de sortie, unifié, pour événements et tâches.
3. **Lecture libre, écriture explicite.** Toute écriture n'est faite que sur demande explicite de Denis. Cette règle figure dans la description de chaque tool d'écriture.
4. **Rien n'est deviné.** Une ambiguïté (occurrence vs série, liste cible absente…) est refusée avec un message clair, pas résolue par défaut.
5. **Rien ne se perd.** Ce qui est passé et non traité remonte tant qu'il n'a pas été traité.
6. **Le serveur fournit les données, Claude fait la présentation.** Le format attendu (tableaux markdown) est prescrit dans la description des tools, pas produit par le serveur. La sortie du serveur est néanmoins déjà ordonnée comme le rendu voulu, pour réduire la latitude de réarrangement.

## 3. Modèle unifié

Deux types d'objets, un seul schéma de sortie.

| Champ | Type | Notes |
|---|---|---|
| `id` | string | id Google, préfixé par le type (`evt_…`, `task_…`) |
| `type` | `event` \| `task` | |
| `title` | string | sans les préfixes de statut (cf. §4) |
| `status` | `open` \| `done` \| `cancelled` | statut unifié |
| `start` | datetime local | événements uniquement |
| `end` | datetime local | événements uniquement |
| `due` | date | tâches uniquement, optionnel |
| `all_day` | bool | événements uniquement |
| `recurring` | bool | événement issu d'une série |
| `series_id` | string | id de la série si `recurring` |
| `container` | string | nom de l'agenda ou de la liste Tasks |
| `notes` | string | description / notes, tronquée si longue |
| `estimate_min` | int | optionnel, extrait des notes (convention `~30min`) |
| `location` | string | événements, optionnel |
| `updated` | datetime local | |

Toutes les dates sont exprimées en heure locale `Europe/Paris`, format lisible (`2026-09-14 09:30`). Jamais d'UTC côté Claude.

## 4. Statuts

| Statut | Tâche (Google Tasks) | Événement (Google Calendar) |
|---|---|---|
| `open` | statut `needsAction` | événement `confirmed`, sans marque |
| `done` | statut `completed` (natif) | titre préfixé `[fait]` (convention) |
| `cancelled` | statut `completed` + titre préfixé `[annulé]` (convention, on garde la trace) | statut `cancelled` (natif) |

Règles :
- Un événement passé sans marque reste `open`. Le passage du temps ne vaut pas traitement.
- Le serveur retire les préfixes `[fait]` / `[annulé]` du `title` et les traduit en `status` ; il les réécrit à l'écriture.
- Par défaut les lectures ne renvoient que `open` ; les autres statuts sur demande via un paramètre `status`.

## 5. Tools — lecture

### `next(hours=8)`
Tool principal : l'état global de la journée en **un seul appel**. Réponse en quatre sections, dans cet ordre :
1. `overdue` — tout ce qui est passé et toujours `open` (même règles que le tool `overdue` ci-dessous), trié du plus ancien au plus récent ;
2. `events` — événements de la fenêtre ;
3. `free_slots` — créneaux libres entre eux (cf. `free_slots`) ;
4. `tasks` — tâches `open` triées par `due` puis par `estimate_min`, y compris sans date.

Réponse précédée d'un en-tête `now: <datetime local>` pour ancrer Claude dans le temps. Les champs sont déjà formatés pour l'affichage (heures locales, durées en « 45 min », sections séparées) afin que Claude n'ait pas à réorganiser.

Description destinée à Claude : *à appeler pour toute question du type « qu'est-ce que j'ai aujourd'hui / à faire ». Présenter le résultat en markdown, en trois blocs — **En retard**, **Aujourd'hui**, **À faire** — chacun sous forme de tableau (heure ou échéance, titre, durée estimée, agenda/liste). Les créneaux libres s'intercalent dans le tableau Aujourd'hui. Ne pas omettre le bloc En retard s'il est vide : l'indiquer explicitement.*

### `overdue(include_recurring=false)`
Tout ce qui est passé et toujours `open` : tâches dont `due` < aujourd'hui, événements dont `end` < maintenant. Sans borne de date, trié du plus ancien au plus récent. Les occurrences d'événements récurrents sont **exclues** par défaut.

Sert seul pour la question « qu'est-ce que je n'ai pas fait » ; sinon son contenu est déjà inclus dans `next`.

### `agenda(from, to, containers?, status?="open")`
Événements et tâches datées fusionnés et triés, récurrences expansées en instances. Fenêtre bornée à **31 jours** ; au-delà, erreur explicite.

### `tasks(list?, status?="open")`
Tâches, avec ou sans date. C'est le seul tool qui remonte les tâches non datées hors de `next`.

### `find(query, from?, to?)`
Recherche texte (titre, notes) sur les deux types. Sans fenêtre : les 90 derniers jours et les 90 prochains.

### `free_slots(from, to, min_minutes=30, working_hours?)`
Créneaux libres calculés côté serveur, à partir des **événements uniquement** (les tâches ne bloquent pas de temps). `working_hours` par défaut : à fixer par Denis dans la configuration du serveur. Les événements `all_day` ne bloquent pas.

### `containers()`
Liste des agendas Calendar et des listes Tasks disponibles, avec leur id et leur nom. Sert à Claude pour ranger correctement à l'écriture.

## 6. Tools — écriture

Description commune, présente sur chacun : *uniquement sur demande explicite de Denis. Ne jamais appeler de sa propre initiative.*

Chaque écriture renvoie l'objet résultant au format unifié, jamais un simple accusé.

### `add_event(title, start, end, container?, notes?, location?, all_day?=false)`
`container` par défaut : agenda principal.

### `add_task(title, list?, due?, notes?)`
`list` par défaut : à fixer par Denis dans la configuration. Si le paramètre désigne une liste inexistante : erreur, pas de création de liste.

### `update_event(id, scope, fields…)`
`scope` obligatoire pour un événement récurrent : `this` (cette occurrence) ou `series` (toute la série). Absent sur un récurrent → erreur. Ignoré sur un non-récurrent.

### `update_task(id, fields…)`
Titre, notes, échéance, liste (déplacement).

### `set_status(id, status)`
Traduction selon le type (cf. §4). Idempotent : repasser `done` sur un `done` ne fait rien. Pour un événement récurrent : agit sur l'occurrence uniquement.

### `delete(id)`
Suppression définitive. Réservé aux erreurs de saisie ; pour « ne plus faire », préférer `set_status(cancelled)`. La description le dit.

## 7. Règles transverses

- **Fuseau** : `Europe/Paris` en entrée comme en sortie. Les entrées sans fuseau sont interprétées en local.
- **Volume** : fenêtres bornées (`agenda` 31 j, `find` ±90 j), `notes` tronquées à ~500 caractères avec indicateur.
- **Cache** : lecture mise en cache 30 s par (tool, paramètres) ; invalidé par toute écriture.
- **Erreurs** : messages en clair destinés à Claude (« la liste "X" n'existe pas, listes disponibles : … »), jamais une exception Google brute.
- **Double instance** : Claude Desktop lance deux serveurs (chat + Cowork). Aucun état partagé sauf le token ; le rafraîchissement du token est protégé par un verrou fichier, et une lecture échouée sur token expiré est retentée une fois après refresh.

## 8. Authentification

- OAuth 2 flux « application installée » (client de type *Application de bureau*), lancé une fois via la CLI, avec `access_type=offline` et `prompt=consent` pour garantir l'émission d'un refresh token.
- Scopes minimaux :
  - `https://www.googleapis.com/auth/calendar.events.owned` — lecture/écriture des événements des agendas dont Denis est propriétaire (les agendas partagés tiers sont hors périmètre v1 ; passer à `calendar.events` si besoin, avec nouveau consentement) ;
  - `https://www.googleapis.com/auth/calendar.calendarlist.readonly` — liste des agendas, requis par `containers()` ;
  - `https://www.googleapis.com/auth/tasks` — Google Tasks, lecture et écriture.
- **Prérequis console Google Cloud** : écran de consentement de type *Externe* (compte @gmail.com) avec le statut de publication **« In production »** avant de générer le premier token. En statut *Testing*, Google fait expirer les refresh tokens au bout de 7 jours. Aucune vérification de l'application n'est nécessaire (scopes non sensibles) ; l'écran « application non vérifiée » s'affiche une seule fois au consentement.
- `client_secret.json`, token et refresh token dans des fichiers du dépôt, listés dans `.gitignore`, permissions `0600`, écriture atomique (fichier temporaire + `rename`).
- Aucun secret en variable d'environnement autre que le chemin du fichier de configuration, qui pointe vers ces fichiers.

## 9. Configuration

Fichier de configuration du serveur, à renseigner par Denis :
- agenda principal et liste Tasks par défaut,
- horaires de travail par défaut pour `free_slots`,
- `Europe/Paris` (fixé, mais explicite),
- préfixes de statut (`[fait]`, `[annulé]`), modifiables.

## 10. Ordre de construction

1. Client Python (mapping Google → modèle unifié, statuts, récurrences, fuseau).
2. CLI : `overdue`, `next`, `agenda`, `tasks`, `add-task`, `set-status`. Validation à la main sur le vrai compte.
3. Serveur FastMCP exposant les tools ci-dessus, descriptions incluses.
4. Test des deux instances simultanées (chat + Cowork) sur le refresh du token.

Le mapping (étape 1) est la partie qui demandera le plus d'itérations ; elle se stabilise sans Claude dans la boucle.

## 11. Points ouverts

- Convention `~30min` dans les notes : à activer en v1 ou plus tard selon la qualité des estimations de Claude sans elle.
- Événements récurrents dans `overdue` : exclus par défaut ; à revoir si Denis veut suivre certaines récurrences (un point hebdo « à préparer », par exemple).
- Agendas secondaires (perso vs App-Novative) : un seul agenda en v1 ou plusieurs dès le départ.
- Rendu : tableaux markdown en v1. Si le rendu se révèle instable ou insuffisant, v2 avec une page HTML/JS fournie par le serveur (MCP Apps) — écrite une fois par Denis, affichée par Claude Desktop, hors de la boucle du modèle ; support Cowork à vérifier à ce moment-là.
