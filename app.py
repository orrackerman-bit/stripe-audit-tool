import streamlit as st
import requests
import pandas as pd
import json, os, concurrent.futures
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

# ── Cache ────────────────────────────────────────────────────────────────────
CACHE_FILE = "/tmp/stripe_sf_cache.json"

def load_cache():
    try:
        if os.path.exists(CACHE_FILE):
            data = json.load(open(CACHE_FILE))
            age = datetime.now() - datetime.fromisoformat(data.get("cached_at","2000-01-01"))
            if age < timedelta(hours=24):
                return data
    except Exception:
        pass
    return None

def save_cache(results):
    try:
        json.dump({"cached_at": datetime.now().isoformat(), "results": results},
                  open(CACHE_FILE,"w"))
    except Exception:
        pass

def clear_cache():
    try: os.remove(CACHE_FILE)
    except Exception: pass

# ── Config ───────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Stripe × Salesforce", page_icon="💳", layout="wide")
st.markdown("""<style>
  .block-container{padding-top:1.5rem;max-width:1400px}
  .stat-num{font-size:2rem;font-weight:700;margin-bottom:.25rem}
  .stat-label{font-size:.8rem;color:#6b7280;text-transform:uppercase;letter-spacing:.05em}
  .num-active{color:#16a34a}.num-pastdue{color:#d97706}.num-cancels{color:#7c3aed}
  .num-canceled{color:#dc2626}.num-unpaid{color:#b45309}
  div[data-testid="stMetric"]{background:#f9fafb;border-radius:10px;padding:.75rem}
</style>""", unsafe_allow_html=True)

try:
    SFDC_CLIENT_ID     = st.secrets["SFDC_CLIENT_ID"]
    SFDC_CLIENT_SECRET = st.secrets["SFDC_CLIENT_SECRET"]
    SFDC_DOMAIN        = st.secrets["SFDC_DOMAIN"]
    STRIPE_US_KEY      = st.secrets["STRIPE_US_KEY"]
    STRIPE_INTL_KEY    = st.secrets["STRIPE_INTL_KEY"]
except Exception as e:
    st.error(f"Missing secrets: {e}"); st.stop()

REDIRECT_URI = "https://logz-stripe-audit.streamlit.app/"
AUTH_URL     = f"https://{SFDC_DOMAIN}/services/oauth2/authorize"
TOKEN_URL    = f"https://{SFDC_DOMAIN}/services/oauth2/token"
SF_BASE_URL  = "https://logzio.lightning.force.com"

# ── Helpers ──────────────────────────────────────────────────────────────────
def is_on_demand(text):
    t = (text or "").lower()
    return "on-demand" in t or "on demand" in t or "ondemand" in t

def get_product_name(item):
    price = item.get("price") or {}
    prod  = price.get("product")
    if isinstance(prod, dict):
        n = prod.get("name","") or prod.get("description","") or ""
        if n: return n
    nick = price.get("nickname","") or ""
    if nick: return nick
    return price.get("id","")

def get_auth_url():
    return f"{AUTH_URL}?{urlencode({'response_type':'code','client_id':SFDC_CLIENT_ID,'redirect_uri':REDIRECT_URI,'scope':'api refresh_token offline_access'})}"

def exchange_code(code):
    r = requests.post(TOKEN_URL, data={"grant_type":"authorization_code",
        "client_id":SFDC_CLIENT_ID,"client_secret":SFDC_CLIENT_SECRET,
        "redirect_uri":REDIRECT_URI,"code":code}, timeout=15)
    if r.status_code != 200: raise Exception(r.text)
    return r.json()

def refresh_sf_token(rt):
    r = requests.post(TOKEN_URL, data={"grant_type":"refresh_token",
        "client_id":SFDC_CLIENT_ID,"client_secret":SFDC_CLIENT_SECRET,
        "refresh_token":rt}, timeout=15)
    if r.status_code != 200: raise Exception(r.text)
    return r.json()

# ── OAuth callback ────────────────────────────────────────────────────────────
try:    auth_code = st.query_params.get("code")
except: auth_code = None

if auth_code and "sf_token" not in st.session_state:
    try:
        td = exchange_code(auth_code)
        st.session_state.update({"sf_token":td["access_token"],
            "sf_refresh":td.get("refresh_token",""),"sf_instance":td["instance_url"]})
        st.query_params.clear()
        st.rerun()
    except Exception as e:
        st.error(f"Login failed: {e}"); st.stop()

# ── Login screen ──────────────────────────────────────────────────────────────
if "sf_token" not in st.session_state:
    st.markdown("<br><br>", unsafe_allow_html=True)
    _,c2,_ = st.columns([1.5,1,1.5])
    with c2:
        st.markdown("### 💳 Stripe × Salesforce")
        st.caption("Live customer billing dashboard")
        st.markdown("<br>", unsafe_allow_html=True)
        st.link_button("🔐 Login with Salesforce", get_auth_url(),
                       use_container_width=True, type="primary")
    st.stop()

token    = st.session_state["sf_token"]
refresh  = st.session_state["sf_refresh"]
instance = st.session_state["sf_instance"]

# ── Header — always visible ──────────────────────────────────────────────────
hc1,hc2,hc3 = st.columns([4,2,1])
with hc1:
    st.markdown("## 💳 Stripe Account Statuses")
    cache_note = " · ⚡ cached — hit 🔄 to refresh" if st.session_state.get("from_cache") else ""
    st.caption(f"Last updated: {st.session_state.get('last_loaded','—')}{cache_note}")
with hc2:
    search = st.text_input("🔍 Search", placeholder="Account name...")
with hc3:
    st.markdown("<br>", unsafe_allow_html=True)
    b1,b2,b3 = st.columns(3)
    with b1:
        if st.button("🏠"):
            for k in ["selected_account","filter","saved_filter_state","sel_country",
                      "sel_acct","sel_status","min_sf","min_stripe","arr_match_filter"]:
                st.session_state.pop(k,None)
            st.rerun()
    with b2:
        if st.button("🔄"):
            clear_cache()
            for k in ["results","last_loaded","from_cache"]:
                st.session_state.pop(k,None)
            st.rerun()
    with b3:
        if st.button("↩️"):
            st.session_state.clear(); st.rerun()

# ── Try loading from cache first ──────────────────────────────────────────────
if "results" not in st.session_state:
    cached = load_cache()
    if cached:
        st.session_state["results"]     = cached["results"]
        st.session_state["last_loaded"] = cached["cached_at"][:16].replace("T"," ")
        st.session_state["from_cache"]  = True

# ── Full data load if no cache ────────────────────────────────────────────────
if "results" not in st.session_state:

    prog_text = st.empty()
    prog_bar  = st.empty()

    # ── Step 1: Salesforce ───────────────────────────────────────────────────
    prog_text.info("Step 1/3 — Loading Salesforce accounts...")
    sf_records = []
    try:
        soql = (
            "SELECT Id,Name,BillingCountry,Billing_Email_Address__c,"
            "All_Time_ARR__c,Logz_Io_Parent_Account_Key__c,Email_Domain__c,"
            "(SELECT Name,ARR__c,Unit__c,Start_Date__c,End_Date__c,"
            "Logging_Retention_Days__c,Active__c FROM Contract_Assets__r WHERE Active__c=TRUE) "
            "FROM Account WHERE Type='Customer' AND All_Time_ARR__c>0 "
            "AND Payment_Method_2__c='Credit Card' "
            "AND (NOT Name LIKE '%test%') AND (NOT Name LIKE '%Test%') "
            "AND (NOT Name LIKE '%runrate%') AND (NOT Name LIKE '%Runrate%') "
            "AND (NOT Name LIKE '%on-demand%') AND (NOT Name LIKE '%On-Demand%') "
            "AND (NOT Name LIKE '%support%') AND (NOT Name LIKE '%Support%') "
            "AND (NOT Name LIKE '%logz.io%') AND (NOT Name LIKE '%Logz.io%') "
            "AND (NOT Name LIKE '%logs.io%') ORDER BY Name ASC"
        )
        hdrs = {"Authorization": f"Bearer {token}"}
        url = f"{instance}/services/data/v59.0/query"
        params = {"q": soql}
        while True:
            r = requests.get(url, headers=hdrs, params=params, timeout=30)
            if r.status_code == 401:
                td = refresh_sf_token(refresh)
                st.session_state["sf_token"] = token = td["access_token"]
                hdrs = {"Authorization": f"Bearer {token}"}
                r = requests.get(url, headers=hdrs, params=params, timeout=30)
            if r.status_code != 200:
                raise Exception(r.text)
            d = r.json()
            sf_records.extend(d.get("records", []))
            if d.get("done"):
                break
            url = instance + d["nextRecordsUrl"]
            params = {}
    except Exception as e:
        st.error(f"Salesforce error: {e}")
        st.stop()

    # ── Step 2: Stripe — fetch customer metadata only (no subscription expand)
    prog_text.info("Step 2/3 — Fetching Stripe customers...")
    all_customers = []
    try:
        for api_key, src in [(STRIPE_US_KEY, "US"), (STRIPE_INTL_KEY, "Intl")]:
            if not api_key or len(api_key) < 10:
                continue
            last_id = None
            while True:
                p2 = {"limit": 100}  # NO expand — much faster
                if last_id:
                    p2["starting_after"] = last_id
                r2 = requests.get("https://api.stripe.com/v1/customers",
                    params=p2,
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=20)
                if not r2.ok:
                    break
                d2 = r2.json()
                batch = d2.get("data", [])
                if not batch:
                    break
                for c in batch:
                    c["_src"] = src
                    c["_api_key"] = api_key
                all_customers.extend(batch)
                if not d2.get("has_more"):
                    break
                last_id = batch[-1]["id"]
    except Exception as e:
        st.error(f"Stripe fetch error: {e}")
        st.stop()

    # Build indexes
    sf_id_idx, parent_idx, domain_idx = {}, {}, {}
    for c in all_customers:
        meta = c.get("metadata") or {}
        sid = (meta.get("salesforce_id") or "").strip()
        if sid:
            sf_id_idx[sid.lower()] = c
            if len(sid) >= 15:
                sf_id_idx[sid[:15].lower()] = c
        for k in ["main_account_id", "our-account-id"]:
            v = str(meta.get(k) or "").strip()
            if v and v not in parent_idx:
                parent_idx[v] = c
        em = (c.get("email") or "").lower().strip()
        dom2 = em.split("@")[-1] if "@" in em else ""
        if dom2 and dom2 not in domain_idx:
            domain_idx[dom2] = c

    # Pre-match to find which ~700 customers we actually need
    matched = {}  # cust_id -> customer obj
    for r in sf_records:
        sf_id_l = (r.get("Id") or "").lower()
        pkey = str(r.get("Logz_Io_Parent_Account_Key__c") or "").strip()
        dom  = str(r.get("Email_Domain__c") or "").strip().lower()
        cust = None
        if sf_id_l and sf_id_l in sf_id_idx:
            cust = sf_id_idx[sf_id_l]
        elif sf_id_l and len(sf_id_l) >= 15 and sf_id_l[:15] in sf_id_idx:
            cust = sf_id_idx[sf_id_l[:15]]
        elif pkey and pkey in parent_idx:
            cust = parent_idx[pkey]
        elif dom and dom in domain_idx:
            cust = domain_idx[dom]
        if cust and cust["id"] not in matched:
            matched[cust["id"]] = cust

    # Fetch subscriptions for matched customers in parallel (fast)
    prog_text.info(f"Step 2/3 — Fetching subscriptions & payments for {len(matched)} matched accounts...")

    def fetch_sub_and_charge(args):
        cid, c = args
        ak = c.get("_api_key", "")
        subs, charge = [], None
        if not ak:
            return cid, subs, charge
        try:
            rs = requests.get("https://api.stripe.com/v1/subscriptions",
                params={"customer": cid, "limit": 100, "status": "all"},
                headers={"Authorization": f"Bearer {ak}"}, timeout=10)
            if rs.ok:
                subs = rs.json().get("data", [])
        except Exception:
            pass
        # Only fetch latest charge if no subscriptions
        if not subs:
            try:
                rc = requests.get("https://api.stripe.com/v1/charges",
                    params={"customer": cid, "limit": 1},
                    headers={"Authorization": f"Bearer {ak}"}, timeout=8)
                if rc.ok:
                    charges = rc.json().get("data", [])
                    if charges:
                        ch = charges[0]
                        charge = {
                            "amount": (ch.get("amount") or 0) / 100,
                            "status": ch.get("status", ""),
                            "date": datetime.fromtimestamp(ch.get("created", 0), tz=timezone.utc).strftime("%b %d, %Y") if ch.get("created") else ""
                        }
            except Exception:
                pass
        return cid, subs, charge

    matched_list = list(matched.items())
    subs_map = {}
    charge_map = {}

    # Use map() — no per-result Streamlit updates which freeze the app
    with concurrent.futures.ThreadPoolExecutor(max_workers=25) as ex:
        results = list(ex.map(fetch_sub_and_charge, matched_list, timeout=60))

    for cid, subs, charge in results:
        subs_map[cid] = subs
        if charge:
            charge_map[cid] = charge

    prog_bar.empty()

    # Attach subs to customer objects
    for cid, c in matched.items():
        c["_subs"] = subs_map.get(cid, [])

    # Fetch product names for all unique product IDs
    prod_id_to_key = {}
    for c in matched.values():
        ak = c.get("_api_key", "")
        for s in c.get("_subs", []):
            for item in (s.get("items") or {}).get("data") or []:
                price = item.get("price") or {}
                prod = price.get("product")
                if isinstance(prod, str) and prod and prod not in prod_id_to_key:
                    prod_id_to_key[prod] = ak

    def fetch_product_name(args):
        prod_id, ak = args
        try:
            rp = requests.get(f"https://api.stripe.com/v1/products/{prod_id}",
                headers={"Authorization": f"Bearer {ak}"}, timeout=8)
            if rp.ok:
                return prod_id, rp.json().get("name", "") or ""
        except Exception:
            pass
        return prod_id, ""

    product_name_map = {}
    if prod_id_to_key:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            for pid, name in ex.map(fetch_product_name, prod_id_to_key.items(), timeout=30):
                if name:
                    product_name_map[pid] = name

    def resolve_product_name(item):
        price = item.get("price") or {}
        prod = price.get("product")
        if isinstance(prod, str) and prod:
            name = product_name_map.get(prod, "")
            if name:
                return name
        if isinstance(prod, dict):
            name = prod.get("name", "") or ""
            if name:
                return name
        nick = price.get("nickname", "") or ""
        if nick:
            return nick
        return price.get("id", "")

    # ── Step 3: Match & build results ───────────────────────────────────────
    prog_text.info(f"Step 3/3 — Matching {len(sf_records)} accounts...")
    pbar3 = prog_bar.progress(0)
    rows = []
    for i, r in enumerate(sf_records):
        sf_id   = r.get("Id", "")
        name    = r.get("Name", "")
        arr     = r.get("All_Time_ARR__c") or 0
        country = r.get("BillingCountry") or ""
        email   = (r.get("Billing_Email_Address__c") or "").lower()
        pkey    = str(r.get("Logz_Io_Parent_Account_Key__c") or "").strip()
        dom     = str(r.get("Email_Domain__c") or "").strip().lower()

        plans = []
        for p in ((r.get("Contract_Assets__r") or {}).get("records") or []):
            if not p.get("Active__c"):
                continue
            plans.append({
                "Plan Name": p.get("Name", ""), "Active": "✓",
                "Units": p.get("Unit__c", "") or "", "ARR": p.get("ARR__c", 0) or 0,
                "Start Date": p.get("Start_Date__c", "") or "",
                "End Date": p.get("End_Date__c", "") or "",
                "Log Retention (days)": p.get("Logging_Retention_Days__c", "") or ""
            })

        cust, via = None, "—"
        sf_id_l = sf_id.lower() if sf_id else ""
        if sf_id_l and sf_id_l in sf_id_idx:
            cust, via = sf_id_idx[sf_id_l], "Salesforce ID"
        elif sf_id_l and len(sf_id_l) >= 15 and sf_id_l[:15] in sf_id_idx:
            cust, via = sf_id_idx[sf_id_l[:15]], "Salesforce ID"
        elif pkey and pkey in parent_idx:
            cust, via = parent_idx[pkey], "Parent Key"
        elif dom and dom in domain_idx:
            cust, via = domain_idx[dom], "Email Domain"

        subs = (cust.get("_subs") or []) if cust else []
        cid = (cust or {}).get("id", "")

        ss, detail = ("no_subscription" if cust else "not_found"), None
        for s in subs:
            if s.get("status") == "active" and s.get("cancel_at_period_end"):
                ts = s.get("cancel_at")
                detail = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d") if ts else "?"
                ss = "cancels_on"; break
        if ss in ("not_found", "no_subscription"):
            for s in subs:
                if s.get("status") == "active":   ss = "active";   break
        if ss in ("not_found", "no_subscription"):
            for s in subs:
                if s.get("status") == "past_due": ss = "past_due"; break
        if ss in ("not_found", "no_subscription"):
            for s in subs:
                if s.get("status") == "unpaid":   ss = "unpaid";   break
        if ss in ("not_found", "no_subscription"):
            for s in subs:
                if s.get("status") == "canceled":
                    ts = s.get("canceled_at")
                    detail = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d, %Y") if ts else "?"
                    ss = "canceled"; break
        # No subscriptions but latest charge succeeded
        if ss == "no_subscription" and cid and cid in charge_map:
            ch = charge_map[cid]
            if ch.get("status") == "succeeded":
                ss = "succeeded"
                detail = "${:,.2f} on {}".format(ch.get("amount", 0), ch.get("date", ""))

        mrr = 0.0
        for s in subs:
            if s.get("status") not in ["active", "past_due", "unpaid"]:
                continue
            for item in (s.get("items") or {}).get("data") or []:
                pname = resolve_product_name(item)
                if is_on_demand(pname):
                    continue
                price = item.get("price") or {}
                amt = (price.get("unit_amount") or 0) / 100
                qty = item.get("quantity") or 1
                iv = (price.get("recurring") or {}).get("interval", "month")
                ic = (price.get("recurring") or {}).get("interval_count", 1) or 1
                mo = amt * qty
                if iv == "year":   mo /= (12 * ic)
                elif iv == "week": mo = mo * 4.33 / ic
                elif iv == "day":  mo = mo * 30 / ic
                else:              mo /= ic
                mrr += mo
        mrr = round(mrr, 2)

        stripe_plans = []
        for s in subs:
            st_s = s.get("status", "")
            ca = s.get("cancel_at")
            cd = datetime.fromtimestamp(ca, tz=timezone.utc).strftime("%b %d, %Y") if ca else None
            ds = f"Cancels {cd}" if (st_s == "active" and cd) else st_s.replace("_", " ").title()
            for item in (s.get("items") or {}).get("data") or []:
                pname = resolve_product_name(item)
                if is_on_demand(pname):
                    continue
                price = item.get("price") or {}
                amt = (price.get("unit_amount") or 0) / 100
                qty = item.get("quantity") or 1
                iv = (price.get("recurring") or {}).get("interval", "month")
                stripe_plans.append({
                    "Product": pname, "Price": f"${amt:,.2f}", "Qty": qty,
                    "Total/mo": f"${round(amt * qty, 2):,.2f}",
                    "Frequency": iv.capitalize(), "Status": ds
                })

        sl_map = {
            "active": "Active", "past_due": "Past due", "unpaid": "Unpaid",
            "cancels_on": f"Cancels {detail}",
            "canceled": "Canceled ({})".format(detail) if detail else "Canceled",
            "not_found": "Not found", "no_subscription": "No subscription",
            "succeeded": "Succeeded ({})".format(detail) if detail else "Succeeded"
        }
        sl = sl_map.get(ss, ss)
        src = (cust or {}).get("_src", "—") if cust else "—"
        stripe_arr = round(mrr * 12, 2)
        rows.append({
            "sf_id": sf_id, "Account Name": name, "Country": country,
            "Billing Email": email, "SF ARR": arr, "Stripe MRR": mrr,
            "Stripe ARR": stripe_arr,
            "ARR Match": abs((arr or 0) - stripe_arr) < 1.0,
            "Stripe Status": ss, "Status Label": sl,
            "Found Via": via, "Stripe Acct": src,
            "sf_plans": plans, "stripe_plans": stripe_plans,
            "sf_url": f"{SF_BASE_URL}/lightning/r/Account/{sf_id}/view" if sf_id else "",
            "stripe_url": f"https://dashboard.stripe.com/customers/{cid}" if cid else ""
        })
        pbar3.progress((i + 1) / len(sf_records))

    prog_text.empty()
    prog_bar.empty()
    st.session_state["results"]     = rows
    st.session_state["last_loaded"] = datetime.now().strftime("%b %d, %Y %H:%M")
    st.session_state["from_cache"]  = False
    save_cache(rows)
    st.rerun()


