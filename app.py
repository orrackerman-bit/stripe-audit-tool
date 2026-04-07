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

    # ── Step 1: Salesforce ───────────────────────────────────────────────────
    with st.spinner("Step 1/3 — Loading Salesforce accounts..."):
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

    # ── Step 2: Stripe customers (metadata only — fast) ──────────────────────
    with st.spinner("Step 2/3 — Fetching Stripe customers..."):
        all_customers = []
        try:
            for api_key, src in [(STRIPE_US_KEY, "US"), (STRIPE_INTL_KEY, "Intl")]:
                if not api_key or len(api_key) < 10:
                    continue
                last_id = None
                while True:
                    p2 = {"limit": 100}
                    if last_id:
                        p2["starting_after"] = last_id
                    r2 = requests.get("https://api.stripe.com/v1/customers",
                        params=p2,
                        headers={"Authorization": f"Bearer {api_key}"},
                        timeout=15)
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

        # Pre-match to find which customers we need subscriptions for
        needed = {}  # cust_id -> (customer, api_key)
        for r in sf_records:
            sf_id_l = (r.get("Id","") or "").lower()
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
            if cust and cust["id"] not in needed:
                needed[cust["id"]] = (cust, cust.get("_api_key",""))

    # ── Step 3: Fetch subscriptions for matched customers only ────────────────
    st.write(f"Step 3/3 — Fetching subscriptions for {len(needed)} matched accounts...")
    prog = st.progress(0)
    subs_map = {}
    needed_list = list(needed.items())
    for idx2, (cid, (c, api_key)) in enumerate(needed_list):
        try:
            rs = requests.get(
                "https://api.stripe.com/v1/subscriptions",
                params={"customer": cid, "limit": 100, "status": "all",
                        "expand[]": "data.items.data.price.product"},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10)
            if rs.ok:
                subs_map[cid] = rs.json().get("data", [])
            else:
                subs_map[cid] = []
        except Exception:
            subs_map[cid] = []
        prog.progress((idx2 + 1) / len(needed_list))

    prog.empty()

    # Attach subs to customer objects
    for cid, (c, _) in needed.items():
        c["_subs"] = subs_map.get(cid, [])

    # ── Match & build results ─────────────────────────────────────────────────
    rows = []
    for r in sf_records:
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

        mrr = 0.0
        for s in subs:
            if s.get("status") not in ["active", "past_due", "unpaid"]:
                continue
            for item in (s.get("items") or {}).get("data") or []:
                pname = get_product_name(item)
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
                pname = get_product_name(item)
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
            "canceled": f"Canceled ({detail})" if detail else "Canceled",
            "not_found": "Not found", "no_subscription": "No subscription"
        }
        sl = sl_map.get(ss, ss)
        cid = (cust or {}).get("id", "")
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

    st.session_state["results"]     = rows
    st.session_state["last_loaded"] = datetime.now().strftime("%b %d, %Y %H:%M")
    st.session_state["from_cache"]  = False
    save_cache(rows)
    st.rerun()

# ── Render ────────────────────────────────────────────────────────────────────
df = pd.DataFrame(st.session_state["results"])
if search:
    df = df[df["Account Name"].str.contains(search, case=False, na=False)]

n_active   = len(df[df["Stripe Status"]=="active"])
n_pastdue  = len(df[df["Stripe Status"]=="past_due"])
n_unpaid   = len(df[df["Stripe Status"]=="unpaid"])
n_cancels  = len(df[df["Stripe Status"]=="cancels_on"])
n_canceled = len(df[df["Stripe Status"]=="canceled"])

if "filter" not in st.session_state:
    st.session_state["filter"] = None

# Stat boxes
st.markdown("<br>", unsafe_allow_html=True)
cols = st.columns(5)
boxes = [("Active",n_active,"num-active","active"),("Past Due",n_pastdue,"num-pastdue","past_due"),
         ("Unpaid",n_unpaid,"num-unpaid","unpaid"),("Cancels w/ Date",n_cancels,"num-cancels","cancels_on"),
         ("Canceled",n_canceled,"num-canceled","canceled")]
for col,(label,num,css,key) in zip(cols,boxes):
    with col:
        sel = st.session_state["filter"]==key
        st.markdown(f"""<div style="background:{'#f5f3ff' if sel else '#fff'};
            border:{'2px solid #6366f1' if sel else '1px solid #e5e7eb'};
            border-radius:12px;padding:1.25rem 1.5rem;text-align:center;margin-bottom:.5rem">
            <div class="stat-num {css}">{num}</div>
            <div class="stat-label">{label}</div></div>""", unsafe_allow_html=True)
        if st.button(f"{'✓ ' if sel else ''}{label}", key=f"b_{key}", use_container_width=True):
            st.session_state["filter"] = None if sel else key
            st.session_state.pop("selected_account",None)
            st.rerun()

# Account detail
if "selected_account" in st.session_state:
    a = st.session_state["selected_account"]
    if st.button("← Back"):
        del st.session_state["selected_account"]
        for k,v in st.session_state.pop("saved_filter_state",{}).items():
            st.session_state[k] = v
        st.rerun()
    st.divider()
    sc = {"active":("#dcfce7","#166534"),"past_due":("#fef3c7","#92400e"),
          "unpaid":("#fef3c7","#92400e"),"cancels_on":("#ede9fe","#5b21b6"),
          "canceled":("#fee2e2","#991b1b")}.get(a["Stripe Status"],("#f3f4f6","#374151"))
    c1,c2 = st.columns([3,1])
    with c1:
        st.markdown(f"## {a['Account Name']}")
        links = []
        if a.get("sf_url"):     links.append(f"[☁️ Salesforce]({a['sf_url']})")
        if a.get("stripe_url"): links.append(f"[💳 Stripe]({a['stripe_url']})")
        if links: st.markdown(" · ".join(links))
        st.caption(f"{a['Billing Email']} · {a['Country']} · Found via: {a['Found Via']} · {a['Stripe Acct']} account")
    with c2:
        st.markdown(f"<br><span style='background:{sc[0]};color:{sc[1]};padding:6px 16px;"
                    f"border-radius:20px;font-size:13px;font-weight:600'>{a['Status Label']}</span>",
                    unsafe_allow_html=True)
    st.divider()
    m1,m2,m3 = st.columns(3)
    m1.metric("Salesforce All-Time ARR",f"${a['SF ARR']:,.2f}")
    m2.metric("Stripe MRR",f"${a['Stripe MRR']:,.2f}")
    m3.metric("Stripe ARR (MRR×12)",f"${a['Stripe ARR']:,.2f}")
    st.divider()
    t1,t2 = st.tabs(["☁️ Salesforce Plans","💳 Stripe Subscriptions"])
    with t1:
        pl = a.get("sf_plans",[])
        if pl:
            pdf = pd.DataFrame(pl)
            pdf["ARR"] = pdf["ARR"].apply(lambda x: f"${x:,.2f}" if x else "—")
            st.dataframe(pdf, use_container_width=True, hide_index=True)
        else:
            st.info("No active plans in Salesforce.")
    with t2:
        sp = a.get("stripe_plans",[])
        if sp:
            st.dataframe(pd.DataFrame(sp), use_container_width=True, hide_index=True)
        else:
            st.info("No subscriptions in Stripe.")

else:
    flt = st.session_state.get("filter")
    display = df[df["Stripe Status"]==flt].copy() if flt else df.copy()
    label_map = {"active":"Active","past_due":"Past Due","unpaid":"Unpaid",
                 "cancels_on":"Cancels w/ Date","canceled":"Canceled"}
    title = f"{label_map.get(flt,flt)} Accounts" if flt else "Account List View"

    # Filters
    with st.expander("🔽 Filters", expanded=False):
        cl1,cl2 = st.columns([5,1])
        with cl2:
            if st.button("✕ Clear all", use_container_width=True):
                for k in ["sel_country","sel_acct","sel_status","min_sf","min_stripe","arr_match_filter","filter"]:
                    st.session_state.pop(k,None)
                st.rerun()
        fc1,fc2,fc3 = st.columns(3)
        with fc1:
            countries = ["All"] + sorted(df["Country"].dropna().unique().tolist())
            sc_val = st.session_state.get("sel_country","All")
            sel_country = st.selectbox("Country", countries,
                index=countries.index(sc_val) if sc_val in countries else 0, key="sel_country")
        with fc2:
            accts = ["All","US","Intl"]
            sa_val = st.session_state.get("sel_acct","All")
            sel_acct = st.selectbox("Stripe Account", accts,
                index=accts.index(sa_val) if sa_val in accts else 0, key="sel_acct")
        with fc3:
            statuses = ["All","Active","Past due","Unpaid","Cancels w/ Date","Canceled","Not found","No subscription"]
            ss_val = st.session_state.get("sel_status","All")
            sel_status = st.selectbox("Stripe Status", statuses,
                index=statuses.index(ss_val) if ss_val in statuses else 0, key="sel_status")
        fc4,fc5,fc6 = st.columns(3)
        with fc4:
            min_sf = st.number_input("Min SF ARR ($)", min_value=0,
                value=int(st.session_state.get("min_sf",0)), step=100, key="min_sf")
        with fc5:
            min_stripe = st.number_input("Min Stripe ARR ($)", min_value=0,
                value=int(st.session_state.get("min_stripe",0)), step=100, key="min_stripe")
        with fc6:
            amf_opts = ["All","✅ Match","❌ No match"]
            amf_val = st.session_state.get("arr_match_filter","All")
            arr_match_filter = st.selectbox("ARR Match", amf_opts,
                index=amf_opts.index(amf_val) if amf_val in amf_opts else 0, key="arr_match_filter")

        smap = {"Active":"active","Past due":"past_due","Unpaid":"unpaid",
                "Cancels w/ Date":"cancels_on","Canceled":"canceled",
                "Not found":"not_found","No subscription":"no_subscription"}
        if sel_country != "All": display = display[display["Country"]==sel_country]
        if sel_acct    != "All": display = display[display["Stripe Acct"]==sel_acct]
        if sel_status  != "All": display = display[display["Stripe Status"]==smap.get(sel_status,sel_status)]
        if min_sf   > 0: display = display[display["SF ARR"]>=min_sf]
        if min_stripe>0: display = display[display["Stripe ARR"]>=min_stripe]
        if arr_match_filter=="✅ Match":    display = display[display["ARR Match"]==True]
        elif arr_match_filter=="❌ No match": display = display[display["ARR Match"]==False]

    st.markdown(f"### {title} ({len(display)})")

    icons = {"active":"✅","past_due":"⚠️","unpaid":"🔶","cancels_on":"🟣",
             "canceled":"🔴","not_found":"⬜","no_subscription":"⬜"}

    h = st.columns([2.5,1,2,1.2,1.2,1.8,0.7,0.8])
    for col,hdr in zip(h,["**Account Name**","**Country**","**Billing Email**",
                            "**SF ARR**","**Stripe ARR**","**Stripe Status**","**Acct**","**ARR ✓**"]):
        col.markdown(hdr)
    st.markdown('<hr style="margin:4px 0 0;border:none;border-top:1.5px solid #e5e7eb">', unsafe_allow_html=True)

    for _,row in display.iterrows():
        c = st.columns([2.5,1,2,1.2,1.2,1.8,0.7,0.8])
        with c[0]:
            if st.button(row["Account Name"], key=f"a_{row['sf_id']}", use_container_width=True):
                st.session_state["selected_account"] = row.to_dict()
                st.session_state["saved_filter_state"] = {
                    "filter":st.session_state.get("filter"),
                    "sel_country":st.session_state.get("sel_country","All"),
                    "sel_acct":st.session_state.get("sel_acct","All"),
                    "sel_status":st.session_state.get("sel_status","All"),
                    "min_sf":st.session_state.get("min_sf",0),
                    "min_stripe":st.session_state.get("min_stripe",0),
                    "arr_match_filter":st.session_state.get("arr_match_filter","All"),
                }
                st.rerun()
        c[1].markdown(f'<div style="padding-top:6px;white-space:nowrap;font-size:14px">{row["Country"] or "—"}</div>', unsafe_allow_html=True)
        c[2].markdown(f'<div style="padding-top:6px;font-size:13px;word-break:break-all">{row["Billing Email"] or "—"}</div>', unsafe_allow_html=True)
        c[3].markdown(f'<div style="padding-top:6px;font-size:14px">${row["SF ARR"]:,.2f}</div>' if row["SF ARR"] else '<div style="padding-top:6px;font-size:14px">—</div>', unsafe_allow_html=True)
        c[4].markdown(f'<div style="padding-top:6px;font-size:14px">${row["Stripe ARR"]:,.2f}</div>' if row["Stripe ARR"] else '<div style="padding-top:6px;font-size:14px">—</div>', unsafe_allow_html=True)
        c[5].markdown(f'<div style="padding-top:6px;font-size:14px">{icons.get(row["Stripe Status"],"⬜")} {row["Status Label"]}</div>', unsafe_allow_html=True)
        c[6].markdown(f'<div style="padding-top:6px;font-size:14px">{row["Stripe Acct"]}</div>', unsafe_allow_html=True)
        c[7].markdown(f'<div style="padding-top:6px;text-align:center;font-size:17px">{"✅" if row.get("ARR Match") else "❌"}</div>', unsafe_allow_html=True)
        st.markdown('<hr style="margin:0;border:none;border-top:1px solid #f0f0f0">', unsafe_allow_html=True)

    st.divider()
    csv = display.drop(columns=["sf_plans","stripe_plans","sf_id","sf_url","stripe_url"],
                       errors="ignore").to_csv(index=False).encode("utf-8")
    st.download_button("⬇ Download CSV", csv,
        f"stripe_audit_{datetime.today().strftime('%Y-%m-%d')}.csv","text/csv")
