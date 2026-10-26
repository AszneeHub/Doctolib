#!/usr/bin/env python3
"""
Doctolib-Termin-Checker – Gastroenterologie am Stachus (München)
=================================================================

Prüft regelmäßig, ob bei der Praxis ein Termin frei ist für:
  * Versicherung:   Gesetzlich versichert
  * Leistung:       Magenspiegelung / Gastroskopie
  * Arzt/Ärztin:    Ich habe keine Präferenz (alle Kalender der Praxis)

…und schickt dir sofort eine Push-Nachricht über ntfy aufs Handy.

EINRICHTUNG (einmalig, ca. 3 Minuten)
-------------------------------------
1. Python 3 installieren (https://www.python.org/downloads/) – bei Windows
   im Installer "Add Python to PATH" anhaken.
2. Im Terminal / in der Eingabeaufforderung:
       pip install curl_cffi
3. ntfy-App aufs Handy (App Store / Google Play: "ntfy"), dort auf "+"
   tippen und den Kanal (Topic) abonnieren, der unten bei NTFY_TOPIC steht.
4. Testnachricht schicken:
       python doctolib_checker.py --test
5. Einmal prüfen, ob die richtige Leistung gefunden wird:
       python doctolib_checker.py --list
6. Dauerhaft laufen lassen (Fenster offen lassen, PC nicht in Standby):
       python doctolib_checker.py

Optionen:  --once  (nur einmal prüfen)   --list  (Leistungen/Kalender anzeigen)
           --test  (Test-Push senden)       --debug (Fehlersuche: Antworten speichern)
"""

import argparse
import datetime as dt
import json
import os
import random
import sys
import time

# ============================ EINSTELLUNGEN ============================

PROFILE_SLUG = "gastroenterologie-am-stachus"
PRACTICE_ID = 77659                       # placeId=practice-77659
INSURANCE_SECTOR = "public"               # "Gesetzlich versichert"

# Leistung wird über den Namen gesucht ("Magenspiegelung / Gastroskopie")
MOTIVE_KEYWORDS = ["gastroskopie", "magenspiegelung"]
MOTIVE_EXCLUDE = ["privat", "koloskopie", "darmspiegelung", "video", "telefon"]
# Falls die automatische Suche danebenliegt: IDs aus "--list" hier eintragen,
# z. B. VISIT_MOTIVE_IDS = [1234567]. Leer lassen = automatisch.
VISIT_MOTIVE_IDS: list[int] = []

# Nur Termine bis zu diesem Datum melden (z. B. "2027-01-31"), None = alle
LATEST_DATE: str | None = None

CHECK_INTERVAL_MINUTES = 10               # nicht kleiner als 5 wählen
NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or "gastro-stachus-38f9e2fb54"  # geheimer Kanalname (bei GitHub als Secret)
NTFY_SERVER = "https://ntfy.sh"

BOOKING_URL = (
    "https://www.doctolib.de/gemeinschaftspraxis/muenchen/gastroenterologie-am-stachus"
    "/booking/patient-insurance-sector?specialityId=1097&telehealth=false"
    "&placeId=practice-77659&source=external_referral"
)

# ======================================================================

BASE = "https://www.doctolib.de"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".doctolib_seen.json")

try:
    from curl_cffi import requests as http  # imitiert einen echten Chrome-Browser
    _KW = {"impersonate": "chrome"}
except ImportError:  # Fallback, wird von Doctolib aber öfter blockiert
    import requests as http  # type: ignore
    _KW = {}
    print("Hinweis: 'pip install curl_cffi' wird empfohlen (sonst blockt Doctolib eher).")

HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "de-DE,de;q=0.9",
    "Referer": BOOKING_URL,
}

session = http.Session()


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%d.%m.%Y %H:%M:%S}] {msg}", flush=True)


def get_json(path: str, params: dict | None = None) -> dict:
    r = session.get(BASE + path, params=params, headers=HEADERS, timeout=30, **_KW)
    if r.status_code in (403, 429):
        raise PermissionError(f"Doctolib blockt gerade (HTTP {r.status_code})")
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- Praxisdaten

def _collect(obj, key: str, out: list) -> None:
    """Sucht rekursiv nach Listen unter einem bestimmten Schlüssel."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, list):
                out.extend(x for x in v if isinstance(x, dict))
            else:
                _collect(v, key, out)
    elif isinstance(obj, list):
        for x in obj:
            _collect(x, key, out)


def _try(path: str, params: dict | None = None) -> dict | None:
    try:
        return get_json(path, params)
    except PermissionError:
        raise
    except Exception:
        return None


def load_booking_data() -> dict:
    """Lädt Leistungen + Kalender. Probiert neue und alte Doctolib-Schnittstelle."""
    sources = [
        _try("/online_booking/api/slot_selection_funnel/v1/info.json",
             {"profile_slug": PROFILE_SLUG, "locale": "de"}),
        _try(f"/booking/{PROFILE_SLUG}.json"),
    ]
    motives, agendas = [], []
    for src in sources:
        if src:
            _collect(src, "visit_motives", motives)
            _collect(src, "agendas", agendas)

    # Neuere Variante: Leistungen werden separat geladen
    if not motives and agendas:
        agenda_ids = "-".join(str(a["id"]) for a in agendas if "id" in a)
        for params in (
            {"agenda_ids": agenda_ids, "practice_ids": PRACTICE_ID, "speciality_id": 1097,
             "insurance_sector": INSURANCE_SECTOR, "telehealth": "false", "locale": "de"},
            {"profile_slug": PROFILE_SLUG, "insurance_sector": INSURANCE_SECTOR, "locale": "de"},
        ):
            src = _try("/online_booking/api/slot_selection_funnel/v1/visit_motives.json", params)
            if src:
                _collect(src, "visit_motives", motives)
                if not motives and isinstance(src.get("data"), list):
                    motives = [m for m in src["data"] if isinstance(m, dict)]
            if motives:
                break

    if not agendas:
        raise RuntimeError("Konnte die Praxisdaten nicht laden – bitte "
                           "'py doctolib_checker.py --debug' ausführen und Datei schicken.")
    # Duplikate entfernen
    uniq_m = {m["id"]: m for m in motives if "id" in m}
    uniq_a = {a["id"]: a for a in agendas if "id" in a}
    return {"visit_motives": list(uniq_m.values()), "agendas": list(uniq_a.values())}


def debug_dump() -> None:
    """Speichert die Antworten von Doctolib in eine Textdatei zur Fehlersuche."""
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "doctolib_debug.txt")
    urls = [
        ("/online_booking/api/slot_selection_funnel/v1/info.json",
         {"profile_slug": PROFILE_SLUG, "locale": "de"}),
        (f"/booking/{PROFILE_SLUG}.json", None),
    ]
    with open(out_path, "w", encoding="utf-8") as f:
        for path, params in urls:
            try:
                r = session.get(BASE + path, params=params, headers=HEADERS, timeout=30, **_KW)
                f.write(f"=== {path}  ->  HTTP {r.status_code}\n{r.text[:6000]}\n\n")
            except Exception as e:
                f.write(f"=== {path}  ->  FEHLER {e}\n\n")
    log(f"Gespeichert: {out_path}")


def find_motives(data: dict) -> list[dict]:
    motives = data.get("visit_motives", [])
    if VISIT_MOTIVE_IDS:
        return [m for m in motives if m["id"] in VISIT_MOTIVE_IDS] or \
               [{"id": i, "name": f"ID {i}"} for i in VISIT_MOTIVE_IDS]
    hits = []
    for m in motives:
        name = (m.get("name") or "").lower()
        if any(k in name for k in MOTIVE_KEYWORDS) and not any(x in name for x in MOTIVE_EXCLUDE):
            hits.append(m)
    return hits


def find_agendas(data: dict, motive_id: int) -> list[int]:
    """'Ich habe keine Präferenz' = alle buchbaren Kalender der Praxis für diese Leistung."""
    ids = []
    for a in data.get("agendas", []):
        if a.get("booking_disabled"):
            continue
        if a.get("practice_id") not in (None, PRACTICE_ID):
            continue
        if motive_id in (a.get("visit_motive_ids") or []):
            ids.append(a["id"])
    return ids


# ---------------------------------------------------------------- Termine

def fetch_slots(motive_id: int, agenda_ids: list[int]) -> tuple[list[str], str | None]:
    params = {
        "start_date": dt.date.today().isoformat(),
        "visit_motive_ids": motive_id,
        "agenda_ids": "-".join(map(str, agenda_ids)),
        "practice_ids": PRACTICE_ID,
        "insurance_sector": INSURANCE_SECTOR,
        "telehealth": "false",
        "limit": 15,
    }
    res = get_json("/availabilities.json", params)
    slots = []
    for day in res.get("availabilities", []):
        for s in day.get("slots", []):
            slots.append(s if isinstance(s, str) else s.get("start_date"))
    return [s for s in slots if s], res.get("next_slot")


def within_limit(iso: str) -> bool:
    return LATEST_DATE is None or iso[:10] <= LATEST_DATE


def fmt(iso: str) -> str:
    try:
        d = dt.datetime.fromisoformat(iso)
        tage = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
        return f"{tage[d.weekday()]}, {d:%d.%m.%Y}" + (f" um {d:%H:%M}" if "T" in iso else "")
    except ValueError:
        return iso


# ---------------------------------------------------------------- Benachrichtigung

def notify(title: str, message: str, priority: int = 5) -> None:
    payload = {
        "topic": NTFY_TOPIC, "title": title, "message": message,
        "priority": priority, "tags": ["hospital"], "click": BOOKING_URL,
        "actions": [{"action": "view", "label": "Jetzt buchen", "url": BOOKING_URL}],
    }
    r = session.post(NTFY_SERVER, data=json.dumps(payload).encode(),
                     headers={"Content-Type": "application/json"}, timeout=20)
    r.raise_for_status()


def load_seen() -> set[str]:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def save_seen(seen: set[str]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f)


# ---------------------------------------------------------------- Ablauf

def check_once(seen: set[str]) -> None:
    data = load_booking_data()
    motives = find_motives(data)
    if not motives:
        raise RuntimeError("Leistung 'Magenspiegelung / Gastroskopie' nicht gefunden – "
                           "bitte 'python doctolib_checker.py --list' ausführen.")
    found: list[str] = []
    for m in motives:
        agendas = find_agendas(data, m["id"])
        if not agendas:
            continue
        slots, next_slot = fetch_slots(m["id"], agendas)
        if not slots and next_slot:      # nächster freier Termin liegt weiter in der Zukunft
            slots = [next_slot]
        found += [s for s in slots if within_limit(s)]

    found = sorted(set(found))
    new = [s for s in found if s not in seen]
    if not found:
        log("Kein freier Termin.")
        return
    log(f"{len(found)} freie(r) Termin(e), davon neu: {len(new)}")
    if new:
        lines = "\n".join(f"• {fmt(s)}" for s in new[:8])
        more = f"\n… und {len(new) - 8} weitere" if len(new) > 8 else ""
        notify("Gastroskopie-Termin frei!",
               f"Gastroenterologie am Stachus (gesetzlich):\n{lines}{more}\n\nSchnell buchen!")
        log("Push-Nachricht gesendet.")
    seen.clear()
    seen.update(found)        # nur aktuell freie merken -> wird ein Termin wieder frei, kommt erneut eine Meldung
    save_seen(seen)


def list_motives() -> None:
    data = load_booking_data()
    print("\nLeistungen (visit motives) der Praxis:")
    chosen = {m["id"] for m in find_motives(data)}
    for m in data.get("visit_motives", []):
        mark = "  <-- wird geprüft" if m["id"] in chosen else ""
        print(f"  {m['id']:>9}  {m.get('name')}{mark}")
    print("\nKalender (Ärzt:innen):")
    for a in data.get("agendas", []):
        print(f"  {a['id']:>9}  Praxis {a.get('practice_id')}  "
              f"{'(gesperrt)' if a.get('booking_disabled') else ''}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Doctolib-Termin-Checker")
    ap.add_argument("--once", action="store_true", help="nur einmal prüfen")
    ap.add_argument("--list", action="store_true", help="Leistungen und Kalender anzeigen")
    ap.add_argument("--test", action="store_true", help="Test-Push senden")
    ap.add_argument("--debug", action="store_true", help="Doctolib-Antworten in Datei speichern")
    args = ap.parse_args()

    if args.debug:
        debug_dump()
        return
    if args.test:
        notify("Test: Doctolib-Checker", "Push funktioniert! Tippe hier, um die Buchungsseite zu öffnen.", 3)
        log("Test-Nachricht gesendet – ist sie auf dem Handy angekommen?")
        return
    if args.list:
        list_motives()
        return

    seen = load_seen()
    if not args.once:
        log(f"Starte – prüfe alle {CHECK_INTERVAL_MINUTES} Min. (Beenden mit Strg+C)")
    errors = 0
    while True:
        try:
            check_once(seen)
            errors = 0
        except PermissionError as e:
            errors += 1
            log(f"{e} – warte länger.")
        except Exception as e:  # Netzwerkfehler etc. – weiterlaufen
            errors += 1
            log(f"Fehler: {e}")
            if errors == 5:
                try:
                    notify("Doctolib-Checker hat Probleme", f"5 Fehler in Folge: {e}", 3)
                except Exception:
                    pass
        if args.once:
            sys.exit(1 if errors else 0)   # Fehler -> GitHub markiert den Lauf rot
        wait = CHECK_INTERVAL_MINUTES * 60 * min(2 ** errors, 6) + random.randint(-60, 60)
        time.sleep(max(120, wait))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBeendet.")
        sys.exit(0)
