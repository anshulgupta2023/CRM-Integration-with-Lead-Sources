#!/usr/bin/env python3
"""
• Reads a CSV of leads and imports all columns into Odoo CRM.
• Matches the Product column (via Google Gemini) against the live
  product catalogue; if a close match is found, stores the correct product.
  Otherwise the wrong name is kept in x_bad_product_name for
  later manual follow‑up.
"""

# ── Imports ────────────────────────────────────────────────────────────────
import os, sys, re, unicodedata, collections
import pandas as pd
from utils import odoo_rpc as odoo        
import google.generativeai as genai       # Gemini SDK
from dotenv import load_dotenv
import google.api_core.exceptions as gexc   
load_dotenv()

# ── Gemini API key (expects GOOGLE_API_KEY in .env) ────────────────────────
genai.configure(api_key=os.getenv("GOOGLE_API_KEY")) # get it form env

# ── File names ─────────────────────────────────────────────────────────────
CSV_IN             = "leads(2).csv"
CSV_MAPPED         = "leads_mapped.csv"
CSV_ACCEPTED_XLS   = "accepted_leads.xlsx"
CSV_REJECTED_XLS   = "rejected_leads.xlsx"
CSV_IMPORTED       = "imported_new_stage.csv"

# ── Helper: smart CSV reader ───────────────────────────────────────────────

def smart_read(path: str) -> pd.DataFrame:
    for enc in ("utf-8", "latin1"):
        try:
            return pd.read_csv(path, encoding=enc)
        except UnicodeDecodeError:
            pass
    from charset_normalizer import from_path
    enc = from_path(path).best().encoding
    print("Detected encoding:", enc)
    return pd.read_csv(path, encoding=enc)

# ── Helpers for custom fields & IDs ────────────────────────────────────────

def slugify(text: str) -> str:
    txt = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^0-9A-Za-z]+", "_", txt).strip("_").lower()

_field_cache: set[str] = set()
CRM_LEAD_MODEL_ID: int | None = None

def ensure_field(name: str, label: str):
    global CRM_LEAD_MODEL_ID
    if name in _field_cache:
        return
    if CRM_LEAD_MODEL_ID is None:
        CRM_LEAD_MODEL_ID = odoo.call("ir.model", "search", [[("model", "=", "crm.lead")]])[0]
    exists = odoo.call("ir.model.fields", "search", [[("model", "=", "crm.lead"), ("name", "=", name)]])
    if not exists:
        print(f"Creating custom field {name!r}")
        odoo.call("ir.model.fields", "create", [{
            "name": name,
            "field_description": label,
            "ttype": "char",
            "state": "manual",
            "model_id": CRM_LEAD_MODEL_ID,
        }])
    _field_cache.add(name)

# make sure our "bad product" placeholder exists
ensure_field("x_bad_product_name", "Original bad product")

# ── Product helpers ────────────────────────────────────────────────────────

def product_id_by_name(name: str):
    rec = odoo.call("product.product", "search_read", [[("name", "=", name)]], {"fields": ["id"], "limit": 1})
    return rec[0]["id"] if rec else None

# pull catalogue once
all_products: list[str] = [p["name"] for p in odoo.call(
    "product.product", "search_read", [[("sale_ok", "=", True)]], {"fields": ["name"]})]

# Gemini match function ------------------------------------------------

def best_match_product(raw_name: str, catalogue: list[str]) -> str | None:
    """Return the best-matching product name from `catalogue`, or None."""
    prompt = (
        "You are a product-name corrector.\n"
      
        "Example:\n"
        "  Valid catalogue: Soap, Shampoo, Toothpaste\n"
        "  Input:  Shmpoo\n"
        "  Output: Shampoo\n"
      
      f"Valid catalogue: {', '.join(catalogue)}\n"
      f"Input: {raw_name}\n"
      "Return exactly one catalogue name that is at least 75 % similar.\n"
      "If nothing reaches that similarity, reply with NOTHING."
    )

    model = genai.GenerativeModel("gemini-2.5-flash")

    try:
        rsp = model.generate_content(prompt)
        # Grab the first candidate that actually contains text
        answer_parts = [
            p.text for cand in rsp.candidates for p in cand.content.parts
            if hasattr(p, "text") and p.text.strip()
        ]
        if not answer_parts:
            raise ValueError("Gemini returned no text")
        answer = answer_parts[0].strip()
    except gexc.ResourceExhausted:
        print("  Gemini quota exhausted – skipping AI correction")
        return None
    except ValueError as e:
        # e.g. finish_reason 1 → no usable text
        print(f" Gemini had no answer for “{raw_name}” ({e}); skipping")
        return None

    if answer.upper().startswith("NOTHING"):
        return None
    return answer


# ── Country / state helpers ────────────────────────────────────────────────

def country_id_by_name(name: str):
    rec = odoo.call("res.country", "search_read", [[("name", "=", name)]], {"fields": ["id"], "limit": 1})
    return rec[0]["id"] if rec else None

def state_id_by_name(name: str):
    rec = odoo.call("res.country.state", "search_read", [[("name", "=", name)]], {"fields": ["id"], "limit": 1})
    return rec[0]["id"] if rec else None
    
# ── Helper to fetch-or-create a record (UTM, etc.) ──────────────────────────
def ensure(model: str, name: str):
    """
    Return the ID of `name` in `model` (e.g. utm.source).
    Creates the record if it doesn’t exist.
    """
    rec = odoo.call(model, "search_read", [[("name", "=", name)]],
                    {"fields": ["id"], "limit": 1})
    return rec[0]["id"] if rec else odoo.call(model, "create", [{"name": name}])

# ── 1. Read CSV ────────────────────────────────────────────────────────────

df_orig = smart_read(CSV_IN)

# field‑meta & mapping -------------------------------------------------------
fields_meta = odoo.call("crm.lead", "fields_get", [], {"attributes": ["string"]})
tech_names  = set(fields_meta.keys())
label_map   = {v["string"].strip().lower(): k for k, v in fields_meta.items()}
label_map.update({
    "email": "email_from", "e-mail": "email_from",
    "phone": "phone", "mobile": "mobile",
    "campaign": "campaign_id", "medium": "medium_id",
    "source": "source_id", "lead source": "source_id",
    "referred by": "referred", "country": "country_id", "state": "state_id",
})

NAME_CANDS = {"name", "full name", "customer name", "client name", "opportunity"}
name_header = next((h for h in df_orig.columns if h.lower().strip() in NAME_CANDS), None)
if not name_header:
    sys.exit("No column can feed mandatory field 'name'.")

SYNONYMS = {
    "name": NAME_CANDS,
    "email_from": ["email", "e‑mail", "mail", "email address", "email id"],
    "phone": ["phone", "telephone", "tel", "contact number"],
    "mobile": ["mobile", "mobile number", "cell", "cellphone"],
    "street": ["address", "street", "addr"],
    "city": ["city", "town"],
    "zip": ["zip", "zipcode", "postal code"],
    "country_id": ["country", "nation"],
    "state_id": ["state", "province", "region"],
    "campaign_id": ["campaign"], "medium_id": ["medium"],
    "source_id": ["source", "lead source", "traffic source"],
    "referred": ["referred", "referrer", "referral"],
    "product_id": ["product", "product name", "sku"],
}
SYN_LOOKUP = {}
for tech, words in SYNONYMS.items():
    for w in words:
        SYN_LOOKUP[re.sub(r"[^a-z0-9]", "", w.lower())] = tech
SYN_LOOKUP["leadsource"] = "source_id"

col_map: dict[str, str] = {}
for hdr in df_orig.columns:
    raw_key = hdr.lower().strip()
    clean   = re.sub(r"[^a-z0-9]", "", raw_key)
    if hdr == name_header:
        col_map[hdr] = "name"
    elif raw_key in tech_names:
        col_map[hdr] = raw_key
    elif raw_key in label_map:
        col_map[hdr] = label_map[raw_key]
    elif clean in SYN_LOOKUP:
        col_map[hdr] = SYN_LOOKUP[clean]
    else:
        slug = slugify(hdr)
        odoo_key = slug if slug in tech_names else f"x_{slug}"
        if odoo_key not in tech_names:
            ensure_field(odoo_key, hdr)
            tech_names.add(odoo_key)
        col_map[hdr] = odoo_key

print("DEBUG – column map →", col_map)

# ── 2. Prepare DataFrames ─────────────────────────────────────────────────

df = df_orig.rename(columns=col_map)
df.to_csv(CSV_MAPPED, index=False)
print(CSV_MAPPED, "written (headers mapped).")
mask_full = df.map(lambda v: pd.notna(v) and str(v).strip() != "").all(axis=1)
df_ok  = df[mask_full].copy()
df_bad = df[~mask_full].copy()
print(f"{len(df_ok)} rows accepted, {len(df_bad)} rejected")

df_ok.to_excel(CSV_ACCEPTED_XLS,  index=False)
df_bad.to_excel(CSV_REJECTED_XLS, index=False)

# ── 3. Build payloads ─────────────────────────────────────────────────────

payloads: list[dict] = []

new_stage_id = odoo.call("crm.stage", "search", [[("name", "=", "New"), ("team_id.name", "=", "Sales")]], {"limit": 1})
sales_team_id = odoo.call("crm.team", "search", [[("name", "=", "Sales")]], {"limit": 1})

# group rows by raw product-name so we don’t call Gemini thousands of times
bucket: dict[str, list[dict]] = collections.defaultdict(list)
for row in df_ok.to_dict("records"):
    raw_name = row.get("x_product_id", "").strip()
    bucket[raw_name].append(row)

for raw_name, rec_list in bucket.items():
    fixed_name = None
    if raw_name:
        fixed_name = best_match_product(raw_name, all_products)

    if fixed_name:  #  close match found
        pid = product_id_by_name(fixed_name)
        if pid:
            print(f" '{raw_name}' → '{fixed_name}' → ID {pid}")
            for rec in rec_list:
                rec["x_product_id"] = pid
    else:           #  nothing close enough
        if raw_name:
            print(f" No close match for '{raw_name}'")
        for rec in rec_list:
            rec.pop("x_product_id", None)
            rec["x_bad_product_name"] = raw_name

    # ── per‑record enrichment & append ────────────────────────────────────
    for rec in rec_list:
        if utm := rec.get("campaign_id"):
            rec["campaign_id"] = ensure("utm.campaign", utm)
        if utm := rec.get("medium_id"):
            rec["medium_id"]   = ensure("utm.medium",   utm)
        if utm := rec.get("source_id"):
            rec["source_id"]   = ensure("utm.source",   utm)
        if ctry := rec.get("country_id"):
            cid = country_id_by_name(ctry)
            rec["country_id"] = cid if cid else rec.pop("country_id")
        if st := rec.get("state_id"):
            sid = state_id_by_name(st)
            rec["state_id"] = sid if sid else rec.pop("state_id")
        if new_stage_id:
            rec["stage_id"] = new_stage_id[0]
        if sales_team_id:
            rec["team_id"]  = sales_team_id[0]
        rec["type"]    = "opportunity"
        rec["user_id"] = False
        payloads.append(rec)

# ── 4. Bulk create leads ──────────────────────────────────────────────────
if not payloads:
    print("Nothing to import (no fully‑filled rows).")
    sys.exit()

try:
    new_ids: list[int] = odoo.call("crm.lead", "create", [payloads])
    print(f"Imported {len(new_ids)} leads (default New stage).")
except Exception as e:
    sys.exit("Import failed: " + str(e))

# ── 5. Audit dump of created records ──────────────────────────────────────
imported = odoo.call("crm.lead", "read", [new_ids, list(col_map.values())])
pd.DataFrame(imported).to_csv(CSV_IMPORTED, index=False)
print(CSV_IMPORTED, "saved with", len(imported), "imported rows.")

