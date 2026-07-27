#!/usr/bin/env python3
"""
Pull UK & Ireland CORPORATE rating DOWNGRADES from ESMA's European Rating
Platform (ERP) and write them as canonical JSON for the Ratings Watch tool.

Runs in a free scheduled GitHub Action. Queries the public ESMA Registers SOLR
endpoint for the ERP ("radar") core, filters to recent UK & Ireland corporate
downgrades across Moody's, S&P and Fitch, dedupes per issuer per agency per day,
and writes erp_downgrades.json, which the Apps Script tool then pulls.

DATA MODEL (confirmed by probing):
  PARENT records (type_s:parent) hold issuerName, countryCode, craName,
  ratingTypeDescr, ratingValueLabel, and the LATEST action in lastActionTypeLabel
  with its date in racValidityDatetime, so we query parents directly.
  - A downgrade is  lastActionTypeLabel == "Downgrade".
  - Date filtering uses racValidityDatetime (the indexed ISO field). The ...Str
    variant is display-only and is NOT filterable.
  - ratingTypeDescr is "Corporate", "Sovereign and public finance" or
    "Structured finance"; we keep only "Corporate".
  - Records are per instrument, so one issuer downgrade spans many rows; we
    dedupe per issuer per agency per day, keeping the most severe rating.

Re-inspect the schema any time with:  python esma_erp_downgrades.py --probe
"""

import json
import sys
import time
import datetime as dt
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

CORE = "esma_registers_radar"
BASE = "https://registers.esma.europa.eu/solr/%s/select" % CORE
ROWS = 500
UA = "Mozilla/5.0 (compatible; RatingsWatchERP/1.0)"

# --- filters (edit here to widen) ------------------------------------------
COUNTRY_CODES = ["GB", "IE"]                 # UK & Ireland; add "DE","AT","CH" for DACH
DOWNGRADE_LABEL = "Downgrade"                # confirmed value of lastActionTypeLabel
KEEP_TYPES = {"Corporate"}                   # drops Sovereign/public finance and Structured finance
EXCLUDE_SUBTYPES = set()                     # add "Financial institution" here to drop banks
LOOKBACK_DAYS = 3                            # small daily window; the Apps Script tool dedupes
AGENCY_MAP = [("MOODY", "MOODYS"), ("STANDARD & POOR", "SP"), ("S&P", "SP"), ("FITCH", "FITCH")]
COUNTRY_NAME = {"GB": "United Kingdom", "IE": "Ireland",
                "DE": "Germany", "AT": "Austria", "CH": "Switzerland"}

PROBE = "--probe" in sys.argv


def solr(params):
    url = BASE + "?" + urlencode(params, doseq=True)
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urlopen(req, timeout=90) as r:
        return json.load(r)


def norm(v):
    return str(v if v is not None else "").strip()


def map_agency(cra):
    u = norm(cra).upper()
    for k, v in AGENCY_MAP:
        if k in u:
            return v
    return "OTHER"


def rating_rank(v):
    ladder = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-",
              "BB+", "BB", "BB-", "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "RD", "SD", "D"]
    v = norm(v).upper()
    return ladder.index(v) if v in ladder else -1


# --- probe (schema re-inspection) ------------------------------------------
def _count(fq):
    return solr({"q": "*:*", "fq": fq, "wt": "json", "rows": 0}).get("response", {}).get("numFound")


def probe():
    P = "type_s:parent"
    for cc in ["GB", "IE", "DE"]:
        print("parents countryCode=%s -> %s" % (cc, _count([P, "countryCode:%s" % cc])))
    for lbl in ["Affirmation", "Upgrade", "Downgrade"]:
        print("lastActionTypeLabel=%r -> %s" % (lbl, _count([P, 'lastActionTypeLabel:"%s"' % lbl])))
    fl = ("issuerName,issuerLeiCode,countryCode,craName,ratedObjectValue,ratingTypeDescr,"
          "subType,lastActionTypeLabel,ratingValueLabel,racValidityDatetime,racValidityDatetimeStr")
    d = solr({"q": "*:*", "fq": [P, "countryCode:GB", 'lastActionTypeLabel:"Downgrade"'],
              "sort": "racValidityDatetime desc", "wt": "json", "rows": 10, "fl": fl})
    print("\n=== recent GB downgrades ===")
    for doc in d.get("response", {}).get("docs", []):
        print(json.dumps(doc, ensure_ascii=False))


# --- extractor -------------------------------------------------------------
def fetch_downgrades(since_iso):
    fq = [
        "type_s:parent",
        "countryCode:(%s)" % " OR ".join(COUNTRY_CODES),
        'lastActionTypeLabel:"%s"' % DOWNGRADE_LABEL,
        "racValidityDatetime:[%s TO *]" % since_iso,
    ]
    fl = ("issuerName,issuerLeiCode,countryCode,countryName,craName,ratingTypeDescr,subType,"
          "ratingValueLabel,racValidityDatetimeStr,racValidityDatetime,id")
    start, out = 0, []
    while True:
        d = solr({"q": "*:*", "fq": fq, "fl": fl, "wt": "json",
                  "rows": ROWS, "start": start, "sort": "racValidityDatetime desc"})
        resp = d.get("response", {})
        docs = resp.get("docs", [])
        out.extend(docs)
        start += ROWS
        if start >= resp.get("numFound", 0) or not docs:
            break
        time.sleep(1)
    return out


def main():
    if PROBE:
        probe()
        return

    since = (dt.datetime.utcnow() - dt.timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT00:00:00Z")
    docs = fetch_downgrades(since)

    # Corporate only; optionally drop chosen subtypes (e.g. banks).
    docs = [d for d in docs
            if norm(d.get("ratingTypeDescr")) in KEEP_TYPES
            and norm(d.get("subType")) not in EXCLUDE_SUBTYPES]

    # Dedupe per issuer per agency per day, keeping the most severe (lowest) rating.
    best = {}
    for d in docs:
        agency = map_agency(d.get("craName"))
        lei = norm(d.get("issuerLeiCode")) or norm(d.get("issuerName"))
        day = norm(d.get("racValidityDatetimeStr"))
        key = (lei, agency, day)
        rank = rating_rank(d.get("ratingValueLabel"))
        if key not in best or rank > best[key][0]:
            best[key] = (rank, agency, d)

    out = []
    for (lei, agency, day), (rank, ag, d) in best.items():
        entity = norm(d.get("issuerName"))
        new_r = norm(d.get("ratingValueLabel"))
        country = norm(d.get("countryName")) or COUNTRY_NAME.get(norm(d.get("countryCode")).upper(),
                                                                 norm(d.get("countryCode")))
        out.append({
            "agency": ag,
            "entity": entity,
            "country": country,
            "oldR": "",                       # ERP carries no previous rating
            "newR": new_r,
            "action": "downgrade",
            "date": day or norm(d.get("racValidityDatetime")),
            "headline": ("%s downgrades %s to %s" % (ag, entity, new_r)) if new_r
                        else ("%s downgrades %s" % (ag, entity)),
            "url": "",
            "id": "ERP-%s-%s-%s" % (ag, lei, day),
        })

    out.sort(key=lambda r: r["date"], reverse=True)
    with open("erp_downgrades.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("Wrote %d UK&I corporate downgrade(s) to erp_downgrades.json" % len(out))


if __name__ == "__main__":
    main()
