import os
import re
import sys
import glob
import json
import time
import signal
import argparse
from datetime import datetime, timedelta

import pandas as pd
import pgeocode
import requests
from dotenv import load_dotenv

# ============================================================
# CONFIGURATION
# ============================================================
SITE_POSTAL_CODE = "77700"
POSTAL_CODE_COLUMN = "postal_code"
INPUT_DIR = "input"
OUTPUT_DIR = "output"
CACHE_DIR = "cache"
CACHE_FILE = os.path.join(CACHE_DIR, "distance_cache.json")
CACHE_EXPIRY_DAYS = 30
INPUT_FILENAME = ""  # Empty = first .xlsx found in input/

ROUTES_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
BATCH_SIZE = 100  # Max origins per request
MAX_RETRIES = 3
REQUEST_TIMEOUT = 30

load_dotenv()
API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

# Geocoder offline France
nomi = pgeocode.Nominatim("fr")


# ============================================================
# CACHE
# ============================================================
def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            # Purge expired entries — keep failures only 1 day to allow retry
            now = datetime.now()
            cutoff_ok = (now - timedelta(days=CACHE_EXPIRY_DAYS)).isoformat()
            cutoff_fail = (now - timedelta(days=1)).isoformat()
            cleaned = {}
            for k, v in cache.items():
                cached_at = v.get("cached_at", "")
                if v.get("status") == "OK" and cached_at > cutoff_ok:
                    cleaned[k] = v
                elif v.get("status") != "OK" and cached_at > cutoff_fail:
                    cleaned[k] = v
            return cleaned
        except Exception as e:
            print(f"[WARN] Cache illisible, reset: {e}")
    return {}


def save_cache(cache: dict):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ============================================================
# GEOCODING OFFLINE (pgeocode) — BATCH
# ============================================================
def geocode_postal_codes_batch(postal_codes: list[str]) -> dict[str, dict | None]:
    """Convert a list of French postal codes to lat/lng using pgeocode (offline, batch)."""
    if not postal_codes:
        return {}
    results = nomi.query_postal_code(postal_codes)
    geocoded = {}
    # Single code returns a Series, multiple returns a DataFrame
    if isinstance(results, pd.Series):
        lat, lng = results.latitude, results.longitude
        if pd.notna(lat) and pd.notna(lng):
            geocoded[postal_codes[0]] = {"lat": float(lat), "lng": float(lng)}
        else:
            geocoded[postal_codes[0]] = None
    else:
        for i, cp in enumerate(postal_codes):
            row = results.iloc[i]
            if pd.notna(row.latitude) and pd.notna(row.longitude):
                geocoded[cp] = {"lat": float(row.latitude), "lng": float(row.longitude)}
            else:
                geocoded[cp] = None
    return geocoded


# ============================================================
# ROUTES API (computeRouteMatrix)
# ============================================================
def _sanitize_error(error: Exception) -> str:
    """Remove any potential API key leaks from error messages."""
    msg = str(error)
    if API_KEY and API_KEY in msg:
        msg = msg.replace(API_KEY, "***REDACTED***")
    return msg


def compute_route_matrix(origins: list[dict], destination: dict) -> list[dict]:
    """
    Call Routes API computeRouteMatrix for a batch of origins.
    Returns list of {originIndex, distanceMeters, duration, status, condition}.
    """
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": "originIndex,destinationIndex,status,condition,distanceMeters,duration",
    }
    body = {
        "origins": [
            {"waypoint": {"location": {"latLng": {"latitude": o["lat"], "longitude": o["lng"]}}}}
            for o in origins
        ],
        "destinations": [
            {"waypoint": {"location": {"latLng": {"latitude": destination["lat"], "longitude": destination["lng"]}}}}
        ],
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_UNAWARE",
    }

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(ROUTES_MATRIX_URL, json=body, headers=headers, timeout=REQUEST_TIMEOUT)

            if resp.status_code == 429:
                wait = 2 ** (attempt + 1)
                print(f"  [WARN] Rate limited (429), attente {wait}s...")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp.json()

        except requests.exceptions.RequestException as e:
            safe_msg = _sanitize_error(e)
            if attempt < MAX_RETRIES - 1:
                wait = 2 ** (attempt + 1)
                print(f"  [WARN] Erreur requete, retry dans {wait}s: {safe_msg}")
                time.sleep(wait)
            else:
                print(f"  [ERREUR] Echec apres {MAX_RETRIES} tentatives: {safe_msg}")
                return []

    return []


def validate_api_key(cache: dict) -> bool:
    """
    Test API key only if cache is empty (first run).
    Avoids wasting a billed element on every execution.
    """
    if cache:
        print("[INFO] Cache existant, validation API ignoree")
        return True
    print("[INFO] Premiere execution, validation de la cle API...")
    origins = [{"lat": 48.8566, "lng": 2.3522}]  # Paris centre
    dest = {"lat": 48.8606, "lng": 2.3376}  # Louvre
    results = compute_route_matrix(origins, dest)
    if results:
        print("[OK] Cle API valide")
        return True
    print("[ERREUR] Cle API invalide ou Routes API non activee")
    return False


def parse_duration(duration_value) -> int:
    """Parse duration from API response. Handles '123s', '1234.5s', int, etc."""
    if isinstance(duration_value, (int, float)):
        return int(duration_value)
    if isinstance(duration_value, str):
        cleaned = duration_value.rstrip("s")
        try:
            return int(float(cleaned))
        except ValueError:
            return 0
    return 0


# ============================================================
# POSTAL CODE VALIDATION
# ============================================================
def is_valid_french_postal_code(code: str) -> bool:
    """A French postal code is exactly 5 digits."""
    return bool(re.match(r"^\d{5}$", str(code).strip()))


# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Activate Travel Distance Analyzer")
    parser.add_argument("--dry-run", action="store_true", help="Estimer les appels API sans executer")
    args = parser.parse_args()

    # --- Validate API key ---
    if not API_KEY:
        print("[ERREUR] GOOGLE_MAPS_API_KEY non trouvee dans .env")
        print("Copiez .env.example en .env et ajoutez votre cle API.")
        return

    # --- Find input file ---
    if INPUT_FILENAME:
        input_path = os.path.join(INPUT_DIR, INPUT_FILENAME)
    else:
        xlsx_files = glob.glob(os.path.join(INPUT_DIR, "*.xlsx"))
        if not xlsx_files:
            print(f"[ERREUR] Aucun fichier .xlsx trouve dans {INPUT_DIR}/")
            return
        input_path = xlsx_files[0]

    print(f"[INFO] Fichier d'entree : {input_path}")

    # --- Read Excel ---
    df = pd.read_excel(input_path)
    print(f"[INFO] {len(df)} lignes lues, {len(df.columns)} colonnes")

    if POSTAL_CODE_COLUMN not in df.columns:
        print(f"[ERREUR] Colonne '{POSTAL_CODE_COLUMN}' non trouvee.")
        print(f"  Colonnes disponibles : {list(df.columns)}")
        return

    # --- Identify unique postal codes ---
    df["_cp_raw"] = df[POSTAL_CODE_COLUMN].astype(str).str.strip()
    unique_codes = df["_cp_raw"].unique()
    print(f"[INFO] {len(unique_codes)} codes postaux uniques")

    exploitable = [cp for cp in unique_codes if is_valid_french_postal_code(cp)]
    non_exploitable = [cp for cp in unique_codes if not is_valid_french_postal_code(cp)]
    print(f"[INFO] {len(exploitable)} exploitables (format 5 chiffres)")
    print(f"[INFO] {len(non_exploitable)} non exploitables")

    # --- Load cache ---
    cache = load_cache()

    # --- Dry-run mode ---
    if args.dry_run:
        cached_count = sum(1 for cp in exploitable if cp in cache)
        to_compute = len(exploitable) - cached_count
        batches = (to_compute + BATCH_SIZE - 1) // BATCH_SIZE if to_compute > 0 else 0
        print(f"\n{'='*50}")
        print("MODE DRY-RUN - Estimation")
        print(f"{'='*50}")
        print(f"Codes postaux exploitables : {len(exploitable)}")
        print(f"Deja en cache              : {cached_count}")
        print(f"A calculer                 : {to_compute}")
        print(f"Requetes API estimees      : {batches}")
        print(f"Elements factures          : {to_compute}")
        print(f"Cout estime (hors gratuit) : ~${to_compute * 0.005:.2f}")
        return

    # --- Validate API key (only on first run) ---
    if not validate_api_key(cache):
        return

    # --- Geocode site destination ---
    print(f"\n[INFO] Geocodage du site : {SITE_POSTAL_CODE}")
    site_geo = geocode_postal_codes_batch([SITE_POSTAL_CODE])
    site_coords = site_geo.get(SITE_POSTAL_CODE)
    if not site_coords:
        print("[ERREUR] Impossible de geocoder le code postal du site. Arret.")
        return
    print(f"[OK] Site : {site_coords}")

    print(f"[INFO] {len(cache)} entrees en cache")

    # --- Geocode all exploitable postal codes (batch) ---
    codes_to_geocode = [cp for cp in exploitable if cp not in cache]
    print(f"\n[INFO] Geocodage offline de {len(codes_to_geocode)} codes postaux...")

    geocoded_all = geocode_postal_codes_batch(codes_to_geocode) if codes_to_geocode else {}
    geocoded = {cp: coords for cp, coords in geocoded_all.items() if coords is not None}
    geocode_failures = [cp for cp, coords in geocoded_all.items() if coords is None]

    print(f"[INFO] {len(geocoded)} nouveaux codes geocodes")
    print(f"[INFO] {len(geocode_failures)} echecs de geocodage")
    print(f"[INFO] {sum(1 for cp in exploitable if cp in cache)} deja en cache")

    # --- Setup graceful shutdown ---
    def handle_interrupt(sig, frame):
        print("\n[WARN] Interruption detectee, sauvegarde du cache...")
        save_cache(cache)
        print("[INFO] Cache sauvegarde. Arret.")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_interrupt)

    # --- Compute routes in batches ---
    codes_to_compute = [cp for cp in exploitable if cp in geocoded]
    total_batches = (len(codes_to_compute) + BATCH_SIZE - 1) // BATCH_SIZE if codes_to_compute else 0
    print(f"\n[INFO] {len(codes_to_compute)} routes a calculer en {total_batches} batch(es)")

    for batch_idx in range(0, len(codes_to_compute), BATCH_SIZE):
        batch_num = batch_idx // BATCH_SIZE + 1
        batch_codes = codes_to_compute[batch_idx:batch_idx + BATCH_SIZE]
        batch_origins = [geocoded[cp] for cp in batch_codes]

        print(f"\n[BATCH {batch_num}/{total_batches}] {len(batch_codes)} origines...")

        results = compute_route_matrix(batch_origins, site_coords)

        if not results:
            print(f"  [ERREUR] Batch {batch_num} echoue")
            # Don't cache failures — they will be retried next run
            continue

        # Build index map from response (handles missing originIndex for 0)
        seen_origins = set()
        for entry in results:
            # originIndex defaults to 0 when omitted by protobuf
            origin_idx = entry.get("originIndex", 0)
            if origin_idx >= len(batch_codes):
                continue
            seen_origins.add(origin_idx)
            cp = batch_codes[origin_idx]

            condition = entry.get("condition", "")
            status_obj = entry.get("status", {})
            status_code = status_obj.get("code", 0) if isinstance(status_obj, dict) else 0

            if condition == "ROUTE_NOT_FOUND" or status_code != 0:
                cache[cp] = {
                    "distance_km": None,
                    "duration_min": None,
                    "status": "ROUTE_NON_CALCULEE",
                    "error": f"condition={condition}",
                    "cached_at": datetime.now().isoformat(),
                }
                continue

            distance_m = entry.get("distanceMeters", 0)
            duration_s = parse_duration(entry.get("duration", "0s"))

            cache[cp] = {
                "distance_km": round(distance_m / 1000, 1),
                "duration_min": round(duration_s / 60, 1),
                "status": "OK",
                "error": "",
                "cached_at": datetime.now().isoformat(),
            }

        # Save cache after each batch
        save_cache(cache)
        ok_count = sum(1 for cp in batch_codes if cache.get(cp, {}).get("status") == "OK")
        print(f"  [OK] {ok_count}/{len(batch_codes)} routes calculees")

    # --- Build results DataFrame for join ---
    all_codes = list(exploitable) + list(non_exploitable)
    rows = []
    for cp in all_codes:
        if cp in cache:
            c = cache[cp]
            rows.append({
                "_cp_raw": cp,
                "code_postal_utilise": cp,
                "distance_km": c.get("distance_km"),
                "temps_trajet_voiture_min": c.get("duration_min"),
                "statut_calcul": c.get("status", "OK"),
                "message_erreur": c.get("error", ""),
            })
        elif cp in geocode_failures:
            rows.append({
                "_cp_raw": cp,
                "code_postal_utilise": cp,
                "distance_km": None,
                "temps_trajet_voiture_min": None,
                "statut_calcul": "GEOCODING_NON_TROUVE",
                "message_erreur": f"pgeocode n'a pas trouve de coordonnees pour {cp}",
            })
        elif not is_valid_french_postal_code(cp):
            rows.append({
                "_cp_raw": cp,
                "code_postal_utilise": cp,
                "distance_km": None,
                "temps_trajet_voiture_min": None,
                "statut_calcul": "CODE_POSTAL_INEXPLOITABLE",
                "message_erreur": f"Format non reconnu : {cp}",
            })
        else:
            rows.append({
                "_cp_raw": cp,
                "code_postal_utilise": cp,
                "distance_km": None,
                "temps_trajet_voiture_min": None,
                "statut_calcul": "ROUTE_NON_CALCULEE",
                "message_erreur": "Non traite",
            })

    results_df = pd.DataFrame(rows)
    enrichment_cols = ["code_postal_utilise", "distance_km", "temps_trajet_voiture_min", "statut_calcul", "message_erreur"]
    df = df.merge(results_df[["_cp_raw"] + enrichment_cols], on="_cp_raw", how="left")
    df.drop(columns=["_cp_raw"], inplace=True)

    # --- Write output ---
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    output_path = os.path.join(OUTPUT_DIR, f"{base_name}_enrichi.xlsx")
    df.to_excel(output_path, index=False)

    # --- Summary ---
    statuts = df["statut_calcul"].value_counts()
    print(f"\n{'='*50}")
    print("RESUME")
    print(f"{'='*50}")
    print(f"Lignes traitees       : {len(df)}")
    print(f"Codes postaux uniques : {len(unique_codes)}")
    for statut, count in statuts.items():
        print(f"  {statut}: {count} lignes")
    print(f"\nFichier genere : {output_path}")
    print("[TERMINE]")


if __name__ == "__main__":
    main()
