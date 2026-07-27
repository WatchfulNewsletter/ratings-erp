#!/usr/bin/env python3
"""
Pull UK & Ireland corporate rating DOWNGRADES from ESMA's European Rating
Platform (ERP) and write them as canonical JSON for the Ratings Watch tool.

WHY THIS EXISTS
The Apps Script tool cannot process the whole EU rating universe inside its
6-minute limit, so this runs in a free scheduled GitHub Action, filters to UK &
Ireland downgrades, and writes a small JSON file the Apps Script then pulls.

DATA SOURCE
The ESMA Registers SOLR endpoint for the ERP ("radar") core returns
machine-readable JSON, filterable and paginated. This is the same public SOLR
interface ESMA exposes for other registers (e.g. FIRDS). The captcha only guards
the HTML search page, not the SOLR API.

TWO UNKNOWNS THIS SCRIPT IS DESIGNED AROUND (confirm with a probe run):
  1. The exact SOLR field names in the radar core.
  2. Whether the ERP exposes a rating-action field ("Downgrade") or only ratings
     plus history that must be diffed per entity to detect a downgrade.
Run:  python esma_erp_downgrades.py --probe
It prints numFound, the field names, and one sample document. Send me those, or
set the *_FIELD constants below and the DOWNGRADE detection to match, then run
without --probe.
"""

import json
import sys
import time
import datetime as dt
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# --- endpoint --------------------------------------------------------------
CORE = "esma_registers_radar"            # ERP core (seen in the ERP register URL)
BASE = "https://registers.esma.europa.eu/solr/%s/select" % CORE
ROWS = 200                               # SOLR page size
UA = "Mozilla/5.0 (compatible; RatingsWatchERP/1.0)"

# --- field names: CONFIRM WITH A PROBE RUN, then edit ----------------------
# Placeholders. The probe prints the real field names; set them here afterwards.
# --- ERP data model (confirmed by probes) ----------------------------------
# PARENT records (type_s:parent, entity_type:radar) carry everything we need,
# including the LATEST action, so we query parents directly with no child join.
ENTITY_FIELD    = "issuerName"             # issuer name
COUNTRY_FIELD   = "countryCode"            # ISO domicile: GB, IE, DE, ...
CRA_FIELD       = "craName"                # agency (mapped to MOODYS / SP / FITCH)
RATING_FIELD    = "ratingValueLabel"       # current rating on the parent
ACTION_FIELD    = "lastActionTypeLabel"    # latest action, e.g. "Affirmation"; downgrade label TBC
TYPE_FIELD      = "ratingTypeDescr"        # "Corporate" for corporates
DATE_FIELD      = "racValidityDatetimeStr" # YYYY-MM-DD of the latest action
LEI_FIELD       = "issuerLeiCode"          # dedupe an issuer rated on many instruments
ID_FIELD        = "id"
PRIOR_FIELD     = ""                       # ERP carries no previous-rating field; oldR stays blank
DOWNGRADE_LABEL = ""                       # CONFIRM from round 3, then the extractor runs
UKI_CODES = {"GB", "IE"}                   # add DACH ISO codes (DE, AT, CH) to widen later

# --- filters ---------------------------------------------------------------
UKI = {"UNITED KINGDOM", "UK", "GB", "GREAT BRITAIN", "ENGLAND", "SCOTLAND",
       "WALES", "NORTHERN IRELAND", "JERSEY", "GUERNSEY", "ISLE OF MAN",
       "IRELAND", "IE", "REPUBLIC OF IRELAND", "EIRE"}
# To widen to DACH later, add the country/ISO values ESMA uses, e.g.
# {"GERMANY", "DE", "AUSTRIA", "AT", "SWITZERLAND", "CH"}.

LOOKBACK_DAYS = 3        # small window per daily run; the Apps Script tool dedupes
AGENCY_MAP = {"MOODY": "MOODYS", "STANDARD & POOR": "SP", "S&P": "SP", "FITCH": "FITCH"}

PROBE = "--probe" in sys.argv


def solr(params):
    url = BASE + "?" + urlencode(params, doseq=True)
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urlopen(req, timeout=60) as r:
        return json.load(r)


def fetch(fq_list):
    start, out = 0, []
    while True:
        params = {"q": "*:*", "wt": "json", "rows": ROWS, "start": start}
        if fq_list:
            params["fq"] = fq_list
        data = solr(params)
        resp = data.get("response", {})
        docs = resp.get("docs", [])
        out.extend(docs)
        total = resp.get("numFound", 0)
        start += ROWS
        if start >= total or not docs:
            break
        time.sleep(1)
    return out


def _count(fq):
    d = solr({"q": "*:*", "fq": fq, "wt": "json", "rows": 0})
    return d.get("response", {}).get("numFound")


def probe():
    P = "type_s:parent"
    # A) does country filtering work? counts for UK, Ireland, Germany
    for cc in ["GB", "IE", "DE"]:
        try:
            print("parents countryCode=%s -> numFound=%s" % (cc, _count([P, "countryCode:%s" % cc])))
        except Exception as e:
            print("countryCode:%s failed: %s" % (cc, e))
    # B) is lastActionTypeLabel filterable, and what is the downgrade wording?
    #    "Affirmation" is known to exist, so it tests filterability; the rest test wording.
    for lbl in ["Affirmation", "New", "Upgrade", "Downgrade", "Downgraded", "Rating downgrade"]:
        try:
            print("parents lastActionTypeLabel=%r -> numFound=%s" % (lbl, _count([P, 'lastActionTypeLabel:"%s"' % lbl])))
        except Exception as e:
            print("lastActionTypeLabel:%r failed: %s" % (lbl, e))
    # C) does date-range filtering work? GB parents since 2025-01-01
    try:
        print("GB parents since 2025-01-01 -> numFound=%s" %
              _count([P, "countryCode:GB", "racValidityDatetimeStr:[2025-01-01 TO *]"]))
    except Exception as e:
        print("date-range query failed: %s" % e)
    # D) real GB parent samples, newest first if sortable, revealing the label vocabulary
    fl = ("issuerName,issuerLeiCode,countryCode,countryName,craName,ratedObjectValue,"
          "ratingTypeDescr,subType,industryTypeValue,timeHorizonDescr,lastActionTypeLabel,"
          "ratingValueLabel,racValidityDatetimeStr,ratingIssuanceLocationDesc,solicitationStatus,id")
    base = {"q": "*:*", "fq": [P, "countryCode:GB"], "wt": "json", "rows": 15, "fl": fl}
    try:
        d = solr(dict(base, sort="racValidityDatetime desc"))
    except Exception:
        d = solr(base)
    print("\n=== up to 15 GB parent records ===")
    for doc in d.get("response", {}).get("docs", []):
        print(json.dumps(doc, ensure_ascii=False))


def norm(v):
    return str(v if v is not None else "").strip()


def map_agency(cra):
    u = norm(cra).upper()
    for k, v in AGENCY_MAP.items():
        if k in u:
            return v
    return u or "OTHER"


def main():
    if PROBE:
        probe()
        return

    # Not finalised yet: the extractor gets its exact downgrade label and query
    # wired in after round 3 confirms it. Until then, refuse to run so we do not
    # emit junk or a misleading empty file.
    if not (ENTITY_FIELD and COUNTRY_FIELD and CRA_FIELD and DOWNGRADE_LABEL):
        print("Not finalised: run with --probe, send the output, and the extractor "
              "gets its downgrade label and query wired in. Not running yet.", file=sys.stderr)
        sys.exit(1)

    since = (dt.datetime.utcnow() - dt.timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT00:00:00Z")

    # Try a server-side filter first (recent + downgrade). If the field names are
    # wrong the query errors, so fall back to a date-only fetch and filter here.
    try:
        docs = fetch(["%s:[%s TO *]" % (DATE_FIELD, since), "%s:*owngrad*" % ACTION_FIELD])
    except Exception as e:
        print("Filtered query failed (%s); fetching recent and filtering client-side." % e, file=sys.stderr)
        try:
            docs = fetch(["%s:[%s TO *]" % (DATE_FIELD, since)])
        except Exception as e2:
            print("Date-only query also failed (%s); fetching a bounded recent set." % e2, file=sys.stderr)
            docs = fetch([])[:5000]

    out = []
    for d in docs:
        action = norm(d.get(ACTION_FIELD))
        country = norm(d.get(COUNTRY_FIELD)).upper()
        # Keep downgrades only. If the ERP has no action field, replace this with
        # a per-entity history diff once the probe shows the real structure.
        if "OWNGRAD" not in action.upper():
            continue
        if country not in UKI:
            continue
        agency = map_agency(d.get(CRA_FIELD))
        entity = norm(d.get(ENTITY_FIELD))
        new_r = norm(d.get(RATING_FIELD))
        out.append({
            "agency": agency,
            "entity": entity,
            "country": norm(d.get(COUNTRY_FIELD)),
            "oldR": norm(d.get(PRIOR_FIELD)),
            "newR": new_r,
            "action": "downgrade",
            "date": norm(d.get(DATE_FIELD)),
            "headline": "%s downgrades %s to %s" % (agency, entity, new_r) if new_r
                        else "%s downgrades %s" % (agency, entity),
            "url": norm(d.get(URL_FIELD)),
            "id": norm(d.get("id")),
        })

    with open("erp_downgrades.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("Wrote %d UK&I downgrade(s) to erp_downgrades.json" % len(out))


if __name__ == "__main__":
    main()
