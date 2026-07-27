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
ENTITY_FIELD  = "rated_entity_name"      # CONFIRM
COUNTRY_FIELD = "country"                # CONFIRM (issuer domicile)
RATING_FIELD  = "rating_value"           # CONFIRM (current rating)
PRIOR_FIELD   = "previous_rating_value"  # CONFIRM (may not exist; see note 2)
ACTION_FIELD  = "rating_action"          # CONFIRM (may not exist; see note 2)
DATE_FIELD    = "rating_date"            # CONFIRM
CRA_FIELD     = "cra_name"               # CONFIRM (agency)
URL_FIELD     = "press_release_url"      # CONFIRM (may be absent)

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


def probe():
    try:
        data = solr({"q": "*:*", "wt": "json", "rows": 3})
    except (HTTPError, URLError) as e:
        print("PROBE FAILED to reach the SOLR endpoint:", e, file=sys.stderr)
        print("If this is a 403/captcha, the radar SOLR core is gated; fall back "
              "to the daily ERP open-data XML dump (same filtering, different fetch).",
              file=sys.stderr)
        sys.exit(1)
    resp = data.get("response", {})
    docs = resp.get("docs", [])
    print("numFound:", resp.get("numFound"))
    if docs:
        print("FIELD NAMES:", sorted(docs[0].keys()))
        print("SAMPLE DOC:\n", json.dumps(docs[0], indent=2, ensure_ascii=False)[:2500])
    else:
        print("No docs returned. Re-check the CORE name.")


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
