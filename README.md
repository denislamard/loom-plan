# loom-plan

Serveur MCP qui donne à Claude Desktop une vue unifiée et actionnable de mon Google Agenda et de mes Google Tasks. Les retards d'abord, puis la journée, les créneaux libres et les tâches ouvertes.

Google reste l'unique référentiel. Le serveur ne stocke rien en dehors du token OAuth et d'un cache de lecture de trente secondes. Claude ne voit jamais un objet Google brut : événements et tâches arrivent dans un seul format, en heure locale, déjà triés dans l'ordre où je veux les lire.

## Ce que ça fait

Quand je demande à Claude « qu'est-ce que j'ai aujourd'hui ? », il appelle le tool `next` et reçoit en un seul aller-retour tout ce qui est en retard, les événements de la journée, les trous entre eux et les tâches ouvertes, datées ou non. Il me présente ça en trois tableaux : En retard, Aujourd'hui, À faire.

Quand je dis « c'est fait », il marque l'événement ou la tâche. Quand je dis « ajoute une tâche pour vendredi », il la crée dans Google Tasks. Il ne fait jamais d'écriture de sa propre initiative : la règle figure dans la description de chaque tool d'écriture.

Quelques principes qui structurent tout le reste. Une chose passée et non traitée continue de remonter tant qu'elle n'est pas marquée faite ou annulée ; le passage du temps ne vaut pas traitement. Une ambiguïté est refusée avec un message clair plutôt que résolue par défaut : modifier une occurrence d'une série récurrente sans dire si on vise l'occurrence ou la série, c'est une erreur, pas une supposition. Une tâche ne bloque pas de temps, elle se fait dans les heures libres. Et le serveur fournit des données déjà ordonnées, la mise en forme finale reste à Claude.

## Installation

Il faut Python 3.12 ou plus, `uv`, et un compte Google.

```bash
git clone <ce dépôt> ~/dev/loom-plan
cd ~/dev/loom-plan
uv sync
```

### Côté Google Cloud

Une fois, dans la console Google Cloud. Créer un projet, activer les API Google Calendar et Google Tasks. Dans Google Auth Platform, configurer l'écran de consentement en type Externe, ajouter les trois scopes ci-dessous dans Accès aux données, puis, dans Audience, **publier l'application**. Ce dernier point compte : en statut Test, Google fait expirer le refresh token au bout de sept jours et il faut se réauthentifier chaque semaine. En production sans validation, l'écran « application non vérifiée » s'affiche une fois au consentement et le token devient permanent.

```
https://www.googleapis.com/auth/calendar.events.owned
https://www.googleapis.com/auth/calendar.calendarlist.readonly
https://www.googleapis.com/auth/tasks
```

`calendar.events.owned` ne donne accès qu'aux agendas dont je suis propriétaire. Les abonnements en lecture seule (jours fériés, numéros de semaine) sont ignorés, et le token ne pourrait pas y écrire de toute façon.

Créer ensuite un client OAuth de type Application de bureau, télécharger son JSON et le placer à la racine du dépôt sous le nom `client_secret.json`. Ce fichier et `token.json` sont dans le `.gitignore`.

### Configuration

Le serveur lit `loom-plan.toml`, dont le chemin est donné par la variable `LOOM_PLAN_CONFIG` ou par l'option `--config`, sinon dans le répertoire courant. Les chemins relatifs qu'il contient sont résolus par rapport au fichier lui-même.

```toml
[auth]
client_secret = "client_secret.json"
token = "token.json"

[general]
timezone = "Europe/Paris"
epoch = 2026-09-13
primary_calendar = "lamard.denis@gmail.com"
default_task_list = "Liste de lamard.denis"
notes_max_chars = 500
cache_ttl_seconds = 30

[work]
start = 09:00:00
end = 18:00:00
days = ["mon", "tue", "wed", "thu", "fri"]

[status]
done_prefix = "[fait]"
cancelled_prefix = "[annulé]"
tracked_prefix = "[suivi]"
```

L'`epoch` est la date de mise en service. Rien d'antérieur n'est considéré comme en retard, sinon tout l'historique de l'agenda remonterait au premier appel. Elle ne filtre que les retards : demander l'agenda de mars reste possible.

### Premier lancement

```bash
uv run loom-plan auth     # ouvre le navigateur pour le consentement Google
uv run loom-plan check    # liste les agendas et les listes Tasks : les trois scopes sont validés d'un coup
uv run loom-plan next
```

### Dans Claude Desktop

Dans `claude_desktop_config.json`, au même niveau que les autres serveurs :

```json
"loom-plan": {
  "command": "/home/denis/dev/loom-plan/.venv/bin/loom-plan-mcp",
  "args": ["--config", "/home/denis/dev/loom-plan/loom-plan.toml"],
  "env": {}
}
```

Claude Desktop lance deux instances du serveur (chat et Cowork). Elles partagent `token.json` ; le rafraîchissement du token est protégé par un verrou fichier, et une instance relit le fichier avant de rafraîchir au cas où l'autre l'aurait déjà fait. Une lecture qui échoue sur un token expiré est retentée une fois.

## Les tools

Lecture libre.

| Tool | Rôle |
|---|---|
| `next(hours?)` | L'état de la journée en un appel : retards, événements, créneaux libres, tâches. Fenêtre par défaut jusqu'à la fin des horaires de travail. |
| `overdue(include_recurring?)` | Tout ce qui est passé et toujours ouvert, du plus ancien au plus récent. |
| `agenda(start, end?, containers?, status?)` | Événements et tâches datées sur une fenêtre de 31 jours maximum. |
| `tasks(list_name?, status?)` | Les tâches, y compris sans date. |
| `find(query, start?, end?)` | Recherche texte sur titres et notes, 90 jours en arrière et en avant par défaut. |
| `free_slots(start, end, min_minutes?, work_start?, work_end?)` | Créneaux libres calculés à partir des événements seuls, sur les jours ouvrés. |
| `containers()` | Agendas et listes disponibles. |

Écriture, uniquement sur demande explicite.

| Tool | Rôle |
|---|---|
| `add_event(title, start, end, container?, notes?, location?, all_day?, recurrence?, tracked?)` | Crée un événement, éventuellement récurrent (règle RRULE). |
| `add_task(title, list_name?, due?, notes?)` | Crée une tâche. Une liste inexistante est refusée, jamais créée. |
| `update_event(id, scope?, ...)` | Modifie un événement. `scope` obligatoire sur un récurrent : `this` ou `series`. |
| `update_task(id, ...)` | Modifie une tâche, y compris pour la déplacer de liste. |
| `set_status(id, status)` | `open`, `done` ou `cancelled`. Idempotent. Sur un récurrent, n'agit que sur l'occurrence. |
| `delete(id, scope?)` | Suppression définitive, réservée aux erreurs de saisie. Pour « ne plus faire », `set_status(cancelled)` garde la trace. |

Chaque item porte un id qui encode son conteneur, `evt_<agenda>/<id>` ou `task_<liste>/<id>`, pour que les écritures sachent où frapper sans état côté serveur.

## Les conventions de titre

Google Tasks distingue « à faire » de « terminé » mais pas « terminé » d'« abandonné », et Google Calendar n'a aucune notion de « fait ». Trois préfixes en tête de titre comblent ça. Ils sont retirés à la lecture et traduits en statut ; le serveur les réécrit lui-même à l'écriture.

| Préfixe | Où | Sens |
|---|---|---|
| `[fait]` | événement | L'événement a été traité. Posé par `set_status(done)`, ou à la main depuis n'importe quelle interface Google. |
| `[annulé]` | tâche | La tâche est terminée côté Google mais c'était un abandon. Posé par `set_status(cancelled)`. |
| `[suivi]` | série récurrente | Chaque occurrence est une chose à faire, pas un rendez-vous qui se produit tout seul. Une occurrence passée sans `[fait]` remonte en retard. C'est le seul préfixe que je pose moi-même, à la création de la série. |

Sans `[suivi]`, les occurrences récurrentes sont exclues des retards : un point hebdo passé a eu lieu, personne ne le marque fait. Avec, une occurrence oubliée reste visible jusqu'à ce que je la traite, et la marquer faite ne touche que cette occurrence, jamais la série.

Une dernière convention, dans les notes cette fois : `~30min` ou `~1h30` donne une durée estimée, utilisée pour trier les tâches du bloc À faire.

## Ligne de commande

Les mêmes opérations sont disponibles en CLI, ce qui a servi à valider le mapping sur le vrai compte avant d'écrire le serveur.

```bash
uv run loom-plan next
uv run loom-plan overdue
uv run loom-plan agenda 2026-09-14 2026-09-21
uv run loom-plan tasks --status done
uv run loom-plan find notaire
uv run loom-plan free "2026-09-15 09:00" "2026-09-15 18:00" --min 45
uv run loom-plan add-task "Relancer le devis Dupont" --due 2026-09-19 --notes "~15min"
uv run loom-plan add-event "Regarder mon compte Malt" "2026-09-16 09:00" "2026-09-16 09:30" --recurrence "FREQ=WEEKLY;BYDAY=WE" --tracked
uv run loom-plan set-status task_<liste>/<id> done
uv run loom-plan serve     # le serveur MCP en stdio, équivalent de loom-plan-mcp
```

## Quelques détails du mapping

Ce sont les points où Google ne dit pas ce qu'on croit, et qui ont chacun coûté une itération.

Un événement sur la journée entière a chez Google une date de fin exclusive : « du 14 au 17 » signifie du 14 au 16. Le serveur expose le vrai dernier jour et refait la conversion à l'écriture.

L'échéance d'une tâche est renvoyée sous la forme `2026-09-14T00:00:00.000Z`, mais l'heure n'a aucun sens. Elle est lue comme une date pure et jamais convertie en heure locale, sinon on décale d'un jour.

Les descriptions d'événements sont en HTML. Les balises sont retirées, les sauts de ligne conservés, les entités décodées, et le tout est tronqué à 500 caractères avec un indicateur.

Un événement annulé n'est plus renvoyé par Google sans `showDeleted`, et pour un événement simple l'annulation équivaut à une mise à la corbeille. Demander `status="cancelled"` active ce paramètre.

Sur une récurrence, Google renvoie des occurrences dont l'id est celui de la série suffixé par la date. Marquer une occurrence faite crée une exception de la série, ce qui est exactement le comportement voulu.

## Développement

```bash
uv run pytest -q
uv run ruff check src tests
uv run pyright
```

Les tests couvrent le mapping sur des payloads Google figés, le service avec les API Google simulées au niveau HTTP (respx), et le contrat des tools exposés par le serveur. Pyright est en mode strict ; les bibliothèques Google n'étant pas typées, une façade `Protocol` isole la frontière dans `auth.py`.

La spec complète, avec les décisions prises et leur justification, est dans `spec-loom-plan.md`.

## Ce qui n'est pas là

Pas d'interface HTML interactive. Le crochet MCP Apps est en place (le tool `next` déclare une ressource `ui://loom-plan/next.html`), mais Claude Desktop ne rend pas encore les interfaces des serveurs personnalisés, ce qui est un bug connu de son côté. Le jour où ça change, la page minimale s'affichera d'elle-même et il sera temps d'écrire la vraie vue.

Pas de déclencheur d'actions automatiques, pas de synchronisation locale, pas d'agendas partagés tiers. Un seul agenda pour l'instant, mais le format des ids est prêt pour en ajouter un second sans migration.

## Licence

Apache 2.0.
