#!/usr/bin/env python3
# ────────────────────────────────────────────────────────────────────────────
# automation.py  –  ONE-CLICK version
#
# ❶ Give each un-owned lead a salesperson (mapping table ↓)
# ❷ Send ONE customised e-mail:
#       • Welcome  – if we stock the product            (x_product_id set)
#       • Apology  – if we don’t stock it               (x_bad_product_name set)
# ❸ Tick x_email_sent   only when Odoo reports   state == "sent"
#
# Required beforehand (already done in your instance):
#   • Custom Boolean  x_email_sent           on crm.lead
#   • Custom Char     x_bad_product_name     on crm.lead  (filled by importer)
#   • Two e-mail templates in Settings » Technical » Templates
#         - “Welcome Email”   body must include:  __NAME__, __PRODUCT__, __SALESPERSON__
#         - “Apology Email”   body must include:  __NAME__, __BAD_PRODUCT__
#   • Outgoing SMTP server configured and working
# ────────────────────────────────────────────────────────────────────────────

import re, sys
from utils import odoo_rpc as odoo     # ← your helper wrapper around /jsonrpc

# ═══ 1. CONFIGURATION ─────────────────────────────────────────────────
SOURCE_TO_EMAIL = {         # lead source  → salesperson login
    "instagram": "ankit@example.com",
    "facebook":  "ankit@example.com",
    "linkedin":  "jayesh@example.com",
    "twitter":   "sujal@example.com",
    "website":   "sujal@example.com",
    "conference":"shalini@example.com",
    "event":     "shalini@example.com",
    "cold call": "jayesh@example.com",
    "webinar":   "shalini@example.com",
    "referral":  "jayesh@example.com",
}

WELCOME_TMPL = "Welcome Email"
APOLOGY_TMPL = "Apology Email"

EMAIL_RE  = re.compile(r"^[^@ ]+@[^@ ]+\.[^@ ]+$")   #  syntax check
# ═════════════════════════════════════════════════════════════════════════


# ────────────────────────────────────────────────────────────────────────────
# Helper: fetch one template id + read body & subject once
# ────────────────────────────────────────────────────────────────────────────
def load_template(name: str) -> dict:
    t = odoo.call("mail.template", "search_read",
                  [[["name", "=", name]]],
                  {"fields": ["id", "subject", "body_html"], "limit": 1})
    if not t:
        sys.exit(f" Mail template “{name}” not found – aborting.")
    return t[0]

tmpl_w = load_template(WELCOME_TMPL)
tmpl_a = load_template(APOLOGY_TMPL)


def render(src: str, **ctx) -> str:
    """Replace __PLACEHOLDER__ in template body/subject."""
    for k, v in ctx.items():
        src = src.replace(f"__{k.upper()}__", str(v))
    return src


# ═══ 2.  ASSIGN SALESPERSONS ─────────────────────────────────────────────
print("Assigning un-owned leads …")
unowned = odoo.call("crm.lead", "search_read",
                    [["|", ["user_id", "=", False], ["user_id", "=", None]]],
                    {"fields": ["id", "name", "source_id"]})

for ld in unowned:
    src_name = (ld["source_id"][1] if isinstance(ld["source_id"], list) else "").strip().lower()
    login    = SOURCE_TO_EMAIL.get(src_name)
    if not login:
        print(f"  • Skip {ld['name']}  – no mapping for “{src_name or '∅'}”")
        continue

    uid = odoo.call("res.users", "search", [[("login", "=", login)]], {"limit": 1})
    if not uid:
        print(f"  • Salesperson user “{login}” missing – skip")
        continue

    odoo.call("crm.lead", "write", [[ld["id"]], {"user_id": uid[0]}])
    print(f"   {ld['name']:<20} → {login}")

# ═════════════════════════════════════════════════════════════════════════

# -------------------------------------------------------------------------
#  STEP 1  –  look-up templates once
# -------------------------------------------------------------------------
T_WELCOME = "Welcome Email"
T_APOLOGY = "Apology Email"

tpl_ids = odoo.call(
    "mail.template", "search_read",
    [[("name", "in", [T_WELCOME, T_APOLOGY])]],
    {"fields": ["id", "name", "email_from"]})

tpl_map = {t["name"]: t for t in tpl_ids}
if T_WELCOME not in tpl_map or T_APOLOGY not in tpl_map:
    raise RuntimeError("Both templates must exist! Create them in Email > Templates.")

# default sender if template has none
COMPANY_EMAIL = odoo.call("res.company", "search_read",
                          [[("id", "=", 1)]], {"fields": ["email"]})[0]["email"]

# -------------------------------------------------------------------------
#  STEP 2  –  leads that still need mail
# -------------------------------------------------------------------------
ready_leads = odoo.call(
    "crm.lead", "search_read",
    [[["x_email_sent", "=", False], ["email_from", "!=", False]]],
    {"fields": ["id", "name", "email_from",
                "x_product_id", "x_bad_product_name",
                "user_id"]})

print(f"Leads awaiting mail: {len(ready_leads)}")

# -------------------------------------------------------------------------
#  STEP 3  –  personalised mail per lead
# -------------------------------------------------------------------------
for L in ready_leads:
    lid   = L["id"]
    to    = L["email_from"]
    name  = L["name"]
    prod  = (L["x_product_id"][1]
             if isinstance(L["x_product_id"], list) else "")
    bad   = L["x_bad_product_name"] or ""
    sp    = (L["user_id"][1] if L["user_id"] else "our team")

    if bad:                                     # ── Apology ──
        subject = f"Sorry, we don’t stock “{bad}”"
        body    = (f"Dear {name},<br><br>"
                   f"Unfortunately we don’t sell “{bad}”. "
                   "Please check the name or contact us for alternatives.<br><br>"
                   "Kind regards,<br>Sales team")
        from_addr = tpl_map[T_APOLOGY]["email_from"] or COMPANY_EMAIL
    else:                                       # ── Welcome ──
        subject = f"Thanks for your interest, {name}!"
        body    = (f"Hello {name},<br><br>"
                   f"Thank you for enquiring about <b>{prod}</b>.<br>"
                   f"{sp} will contact you shortly.<br><br>"
                   "Best regards,<br>Sales team")
        from_addr = tpl_map[T_WELCOME]["email_from"] or COMPANY_EMAIL

    try:
        #  create the mail
        mail_id = odoo.call("mail.mail", "create", [{
            "subject": subject,
            "body_html": body,
            "email_to": to,
            "email_from": from_addr,
            "model": "crm.lead",
            "res_id": lid,
            "auto_delete": False,        # keep it so we can read state
        }])

        #    queue / send
        odoo.call("mail.mail", "send", [[mail_id]])

        #   read back state
        state_rec = odoo.call("mail.mail", "read",
                              [[mail_id], ["state"]])
        state = state_rec and state_rec[0]["state"] or "unknown"

        if state in ("sent", "outgoing"):
            odoo.call("crm.lead", "write",
                      [[lid], {"x_email_sent": True}])
            print(f" {name:<18} ({'apology' if bad else 'welcome'}) — OK")
        else:
            print(f" {name:<18} → state={state}; checkbox unticked")

    except Exception as e:
        # show WHY it failed
        print(f" {name:<18} → exception: {e}; checkbox unticked")

