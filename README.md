# Activate Travel Distance Analyzer

Script Python qui calcule la distance et le temps de trajet en voiture entre les codes postaux clients et le site Activate Val d'Europe.

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

1. Copier `.env.example` en `.env` :
   ```bash
   cp .env.example .env
   ```
2. Ajouter votre clé API Google Maps dans `.env` :
   ```
   GOOGLE_MAPS_API_KEY=votre_cle_ici
   ```

### API Google requise

Activer cette API dans la Google Cloud Console :
- **Routes API** (`routes.googleapis.com`)

Le geocoding est fait offline via `pgeocode` (pas besoin de Geocoding API).

## Utilisation

```bash
# Lancer le calcul
python main.py

# Estimer les appels API sans executer
python main.py --dry-run
```

1. Placer le fichier Excel `.xlsx` dans `input/`
2. Lancer `python main.py`
3. Le fichier enrichi est généré dans `output/`

## Architecture

| Composant | Détail |
|-----------|--------|
| Geocoding | `pgeocode` (offline, pas d'appel API) |
| Routes | `computeRouteMatrix` (batch jusqu'à 100 origines) |
| Routing | `TRAFFIC_UNAWARE` (tier Essentials, moins cher) |
| Cache | `cache/distance_cache.json` (expiration 30 jours) |
| Retry | 3 tentatives avec backoff exponentiel |

## Colonnes ajoutées

| Colonne | Description |
|---------|-------------|
| `code_postal_utilise` | Code postal utilisé pour le calcul |
| `distance_km` | Distance en km jusqu'au site |
| `temps_trajet_voiture_min` | Temps de trajet en minutes |
| `statut_calcul` | OK, CODE_POSTAL_INEXPLOITABLE, GEOCODING_NON_TROUVE, ROUTE_NON_CALCULEE |
| `message_erreur` | Détail en cas d'erreur |

## Coûts API

- Plan gratuit : 10 000 éléments/mois
- Au-delà : ~$5/1000 éléments
- Le dédoublonnage + cache réduisent fortement les appels

## Paramétrage

En haut de `main.py` :
- `SITE_POSTAL_CODE` : code postal du site destination
- `POSTAL_CODE_COLUMN` : nom de la colonne code postal dans l'Excel
- `INPUT_FILENAME` : nom du fichier (vide = premier .xlsx trouvé)
