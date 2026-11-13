# Garmin MCP

Serveur MCP distant qui permet à Claude (claude.ai, app mobile, Claude Code…) de piloter Garmin Connect :

| Outil | Rôle |
|---|---|
| `get_athlete_profile` | FTP, zones de puissance, zones FC, poids |
| `set_ftp` | Change la FTP et recalcule les zones (`keep_percentages`, `coggan` ou `unchanged`) |
| `set_power_zones` | Zones de puissance personnalisées (7 planchers en W) |
| `set_heart_rate_zones` | FC max / repos / seuil, méthode de calcul, planchers des 5 zones |
| `create_cycling_workout` | Crée une séance vélo structurée (cibles en W, % FTP, zone, bpm, cadence), planification optionnelle |
| `update_cycling_workout` | Remplace le contenu d'une séance (garde son id et ses dates) |
| `list_workouts` / `get_workout` / `delete_workout` | Bibliothèque de séances |
| `schedule_workout` / `reschedule_workout` / `unschedule_workout` / `list_scheduled_workouts` | Calendrier Garmin (synchro montre/compteur) |
| `garmin_status` | Vérifie la connexion à Garmin |

> ⚠️ Garmin n'a pas d'API publique pour ces fonctions : le serveur utilise l'API interne de Garmin Connect via
> [`garminconnect`](https://github.com/cyberjunky/python-garminconnect) (0.3.20, sans `garth`, devenu obsolète).
> Ça peut casser si Garmin change son API : mettre à jour la version dans `pyproject.toml` règle en général le problème.

## Sécurité

- **Ton mot de passe Garmin n'est jamais stocké.** Il sert une seule fois (`garmin-mcp-login`) à obtenir des tokens,
  qui sont enregistrés dans le volume `/data` (fichier `0600`), puis rafraîchis automatiquement.
- **Accès au endpoint MCP protégé par OAuth 2.1** (PKCE, enregistrement dynamique, requis par claude.ai). Lors de
  la connexion, une page te demande `OWNER_PASSWORD`. Seules les URL de redirection Claude sont acceptées.
  Après 5 mauvais mots de passe, la page est bloquée pendant 15 min.
- Tokens d'accès valables 24 h, refresh tokens 90 jours, à usage unique (rotation). Ils sont stockés hachés.

## Déploiement avec Dokploy

1. **Pousse ce dépôt** sur GitHub/GitLab (privé), ou utilise le dépôt Git de Dokploy.
2. Dans Dokploy : **Create Service → Compose**, source = ce dépôt, fichier `docker-compose.yml`.
3. Onglet **Environment** :
   ```
   PUBLIC_URL=https://garmin-mcp.ton-domaine.fr
   OWNER_PASSWORD=<openssl rand -base64 24>
   ```
4. Onglet **Domains** : ajoute `garmin-mcp.ton-domaine.fr`, service `garmin-mcp`, port `8000`, HTTPS activé
   (Let's Encrypt). Le DNS (enregistrement A) doit pointer vers le VPS.
5. **Deploy**, puis vérifie que `https://garmin-mcp.ton-domaine.fr/health` répond `{"status":"ok"}`.

### Connexion à Garmin (une seule fois)

Dans Dokploy → service → **Terminal** (conteneur `garmin-mcp`), ou en SSH sur le VPS :

```bash
docker exec -it $(docker ps -qf name=garmin-mcp) garmin-mcp-login
```

Entre ton email, ton mot de passe et le code MFA si tu l'as activé. Le serveur prend les tokens en compte
immédiatement, sans redémarrage. Pour te reconnecter plus tard : `garmin-mcp-login --force`.

Si Garmin répond « Too many requests », attends 15 à 30 min avant de réessayer : chaque tentative ratée prolonge le blocage.

### Déployer une mise à jour

L'application Dokploy est déjà créée (projet « Garmin MCP », identifiants dans `deploy/dokploy.json`).

```bash
git push origin main
DOKPLOY_API_KEY_FILE=~/Downloads/dokployapikey.txt python3 scripts/dokploy.py deploy
DOKPLOY_API_KEY_FILE=~/Downloads/dokployapikey.txt python3 scripts/dokploy.py status
```

`OWNER_PASSWORD` est rangé hors du dépôt, dans `~/Downloads/garmin-mcp-owner-password.txt`.

## Ajouter le connecteur dans Claude

claude.ai → **Paramètres → Connecteurs → Ajouter un connecteur personnalisé** :

- Nom : `Garmin`
- URL : `https://garmin-mcp.ton-domaine.fr/mcp`

Clique sur *Se connecter* : la page du serveur s'ouvre, entre `OWNER_PASSWORD` puis clique sur *Autoriser*.
Le connecteur est ensuite disponible aussi sur l'app mobile.

Exemples de demandes :
- « Ma FTP est maintenant de 265 W, mets à jour Garmin et garde les mêmes pourcentages de zones. »
- « Crée-moi une séance 4×8 min à 90 % FTP avec 4 min de récup, et mets-la mardi prochain. »
- « Planifie-moi 3 séances par semaine pour les 4 prochaines semaines : 1 VO2, 1 sweet spot, 1 endurance. »

## Développement local

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest            # tests unitaires + test bout-en-bout (OAuth + MCP, Garmin simulé)
DATA_DIR=./data .venv/bin/garmin-mcp-login
DATA_DIR=./data PUBLIC_URL=http://localhost:8000 OWNER_PASSWORD=un-mot-de-passe-local .venv/bin/garmin-mcp
```

## Structure

```
src/garmin_mcp/
  server.py         outils MCP + lancement HTTP (Streamable HTTP, /mcp)
  oauth.py          serveur OAuth mono-utilisateur + page de consentement
  workouts.py       conversion d'une séance simple → JSON Garmin
  zones.py          recalcul FTP / zones puissance / zones FC
  garmin_client.py  session Garmin (tokens, reconnexion)
  login.py          connexion Garmin interactive (une fois)
```
