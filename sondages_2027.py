#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agrégateur de sondages — élection présidentielle française 2027
===============================================================

Télécharge le CSV consolidant tous les sondages publiés (Sondax, d'après la
page Wikipédia « Liste de sondages sur l'élection présidentielle française de
2027 »), en extrait les intentions de vote du premier tour (hypothèse
principale de chaque sondage) et publie le résultat dans `data.json`,
consommé par la page `index.html`.

Utilisation
-----------
    python3 sondages_2027.py              # exécution unique (à planifier via cron)
    python3 sondages_2027.py --loop       # boucle interne : mise à jour tous les 7 jours
    python3 sondages_2027.py --interval-days 7
    python3 sondages_2027.py --inject    # réinjecte aussi les données dans index.html
                                         # (la page reste alors consultable en file://)

Planification quotidienne avec cron (06:00) :
    0 6 * * *  /usr/bin/python3 /chemin/vers/sondages_2027.py

Ou avec un timer systemd (`sondages-2027.timer`, OnCalendar=Mon *-*-* 06:00:00)
lançant un service `ExecStart=/usr/bin/python3 /chemin/vers/sondages_2027.py`.

Sources
-------
    CSV (mise à jour quotidienne) :
        https://sondax.fr/donnees/sondages-presidentielle-2027.csv
    Miroir data.gouv.fr :
        https://www.data.gouv.fr/datasets/sondages-dintentions-de-vote-election-presidentielle-2027
    Licence des données : CC BY-SA 4.0 — mention « Sondax, d'après Wikipédia ».

Aucune dépendance externe : bibliothèque standard uniquement.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- constantes

SOURCES = [
    # Ordre de priorité : source primaire, puis miroir.
    "https://sondax.fr/donnees/sondages-presidentielle-2027.csv",
    "https://www.data.gouv.fr/fr/datasets/r/6b97e100-6031-42e0-9c93-aeb9dd49ef0d",
]

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_FILE = SCRIPT_DIR / "data.json"
HTML_FILE = SCRIPT_DIR / "index.html"

# Marqueurs du bloc de données embarquées dans index.html (voir ce fichier) :
# « /* __DATA__ */ ... /* __END_DATA__ */ » (utilisés par inject_into_html()).

HTTP_HEADERS = {"User-Agent": "sondages-2027-aggregator/1.0 (usage personnel)"}
HTTP_TIMEOUT = 60.0


# ------------------------------------------------------------------ utilitaires

def _to_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_float(value):
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def log(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}")


# ------------------------------------------------------------------ téléchargement

def download_csv() -> str:
    """Télécharge le CSV depuis la première source qui répond."""
    last_error: Exception | None = None
    for url in SOURCES:
        try:
            request = urllib.request.Request(url, headers=HTTP_HEADERS)
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return response.read().decode("utf-8")
        except Exception as exc:  # noqa: BLE001 — on veut essayer le miroir ensuite
            last_error = exc
            log(f"! source indisponible : {url} ({exc})")
    raise RuntimeError(f"aucune source accessible (dernière erreur : {last_error})")


# ------------------------------------------------------------------ analyse du CSV

def parse_polls(csv_text: str) -> list[dict]:
    """Extrait les scores du premier tour, hypothèse principale de chaque sondage.

    Le CSV contient une ligne par (sondage, configuration, candidat) ; on ne
    garde que la configuration principale (`configuration_principale == 1`)
    du premier tour, retenue par Sondax comme l'hypothèse la plus complète.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    polls: dict[str, dict] = {}

    for row in reader:
        if row.get("tour") != "1" or row.get("configuration_principale") != "1":
            continue

        poll_id = row["sondage_id"]
        poll = polls.setdefault(
            poll_id,
            {
                "id": poll_id,
                "institut": row["institut"],
                "date": row["terrain_fin"],          # fin de terrain (AAAA-MM-JJ)
                "n": _to_int(row.get("echantillon")),
                "scores": [],                        # [candidat, parti, score]
            },
        )

        score = _to_float(row.get("score"))
        if score is not None:
            poll["scores"].append([row["candidat"], row.get("parti") or "", score])

    result = sorted(polls.values(), key=lambda p: p["date"], reverse=True)
    for poll in result:
        poll["scores"].sort(key=lambda s: -s[2])
    return result


def build_dataset(polls: list[dict]) -> dict:
    return {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "nb_polls": len(polls),
        "license": "CC BY-SA 4.0 — Sondax, d'après Wikipédia",
        "polls": polls,
    }


# ------------------------------------------------------------------ publication

def write_data_json(dataset: dict) -> None:
    payload = json.dumps(dataset, ensure_ascii=False, separators=(",", ":"))
    tmp = DATA_FILE.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(DATA_FILE)          # écriture atomique : pas de fichier à moitié écrit


def inject_into_html(dataset: dict) -> bool:
    """Réécrit le bloc de données embarquées de index.html.

    La page HTML cherche d'abord `data.json` (servi par HTTP) ; sinon elle
    retombe sur ces données embarquées, ce qui la rend consultable directement
    en file:// même sans serveur.
    """
    if not HTML_FILE.exists():
        log("! index.html introuvable : réinjection ignorée")
        return False

    html = HTML_FILE.read_text(encoding="utf-8")
    start, end = "/* __DATA__ */", "/* __END_DATA__ */"
    i, j = html.find(start), html.find(end)
    if i < 0 or j < i:
        log("! marqueurs de données introuvables dans index.html")
        return False

    payload = "const FALLBACK_DATA = " + json.dumps(dataset, ensure_ascii=False) + ";"
    updated = html[: i + len(start)] + "\n" + payload + "\n" + html[j:]
    tmp = HTML_FILE.with_suffix(".html.tmp")
    tmp.write_text(updated, encoding="utf-8")
    tmp.replace(HTML_FILE)
    return True


# ------------------------------------------------------------------ orchestration

def refresh(inject: bool) -> bool:
    log("Téléchargement des sondages…")
    try:
        csv_text = download_csv()
        polls = parse_polls(csv_text)
        if not polls:
            raise ValueError("CSV reçu mais aucune ligne du premier tour extraite")
        dataset = build_dataset(polls)
    except Exception as exc:  # noqa: BLE001 — une seule stratégie : retenter plus tard
        log(f"! échec de la mise à jour : {exc}")
        log("  data.json existant conservé en l'état.")
        return False

    write_data_json(dataset)
    last = polls[0]
    log(f"OK — {len(polls)} sondages enregistrés dans {DATA_FILE.name} "
        f"(plus récent : {last['institut']}, terrain jusqu'au {last['date']})")

    if inject and inject_into_html(dataset):
        log(f"données réinjectées dans {HTML_FILE.name}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Télécharge les sondages de la présidentielle 2027 et publie data.json."
    )
    parser.add_argument("--loop", action="store_true",
                        help="tourner en continu au lieu d'une seule exécution")
    parser.add_argument("--interval-days", type=float, default=7.0,
                        help="intervalle entre deux mises à jour, en jours (défaut : 7)")
    parser.add_argument("--inject", action="store_true",
                        help="réinjecter aussi les données dans index.html (mode file://)")
    args = parser.parse_args()

    if not args.loop:
        sys.exit(0 if refresh(inject=args.inject) else 1)

    interval_s = max(1.0, args.interval_days) * 86400.0
    while True:
        refresh(inject=args.inject)   # les erreurs sont journalisées, on retentera
        log(f"prochaine mise à jour dans {args.interval_days:g} jour(s)")
        time.sleep(interval_s)


if __name__ == "__main__":
    main()
