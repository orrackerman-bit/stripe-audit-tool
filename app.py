import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timezone
from urllib.parse import urlencode

st.set_page_config(page_title="Stripe × Salesforce", page_icon="💳", layout="wide")
st.markdown("""
<style>
  .block-container{padding-top:1.5rem;max-width:1400px}
  .stat-num{font-size:2rem;font-weight:700;margin-bottom:.25rem}
  .stat-label{font-size:.8rem;color:#6b7280;text-transform:uppercase;letter-spacing:.05em}
  .num-active{color:#16a34a}.num-pastdue{color:#d97706}.num-cancels{color:#7c3aed}
  .num-canceled{color:#dc2626}.num-unpaid{color:#b45309}
  div[data-testid="stMetric"]{background:#f9fafb;border-radius:10px;padding:.75rem}
</style>
""", unsafe_allow_html=True)

try:
    SFDC_CLIENT_ID     = st.secrets["SFDC_CLIENT_ID"]
    SFDC_CLIENT_SECRET = st.secrets["SFDC_CLIENT_SECRET"]
    SFDC_DOMAIN        = st.secrets["SFDC_DOMAIN"]
    STRIPE_US_KEY      = st.secrets["STRIPE_US_KEY"]
    STRIPE_INTL_KEY    = st.secrets["STRIPE_INTL_KEY"]
except Exception as e:
    st.error(f"Missing secrets: {e}")
    st.stop()

REDIRECT_URI = "https://logz-stripe-audit.streamlit.app/"
AUTH_URL     = f"https://{SFDC_DOMAIN}/services/oauth2/authorize"
TOKEN_URL    = f"https://{SFDC_DOMAIN}/services/oauth2/token"
SF_BASE_URL  = "https://logzio.lightning.force.com"

def is_on_demand(text):
    t = (text or "").lower()
    return "on-demand" in t or "on demand" in t or "ondemand" in t

def get_auth_url():
    return f"{AUTH_URL}?{urlencode({'response_type':'code','client_id':SFDC_CLIENT_ID,'redirect_uri':REDIRECT_URI,'scope':'api refresh_token offline_access'})}"

def exchange_code(code):
    r = requests.post(TOKEN_URL, data={"grant_type":"authorization_code","client_id":SFDC_CLIENT_ID,
        "client_secret":SFDC_CLIENT_SECRET,"redirect_uri":REDIRECT_URI,"code":code}, timeout=15)
    if r.status_code != 200: raise Exception(r.text)
    return r.json()

def refresh_sf_token(rt):
    r = requests.post(TOKEN_URL, data={"grant_type":"refresh_token","client_id":SFDC_CLIENT_ID,
        "client_secret":SFDC_CLIENT_SECRET,"refresh_token":rt}, timeout=15)
    if r.status_code != 200: raise Exception(r.text)
    return r.json()

# OAuth callback
try:
    auth_code = st.query_params.get("code")
except Exception:
    auth_code = None

if auth_code and "sf_token" not in st.session_state:
    try:
        td = exchange_code(auth_code)
        st.session_state.update({"sf_token":td["access_token"],
            "sf_refresh":td.get("refresh_token",""),"sf_instance":td["instance_url"]})
        st.query_params.clear()
        st.rerun()
    except Exception as e:
        st.error(f"Login failed: {e}")
        st.stop()

# Login screen
if "sf_token" not in st.session_state:
    st.markdown("<br><br>", unsafe_allow_html=True)
    c1,c2,c3 = st.columns([1.5,1,1.5])
    with c2:
        st.markdown("### 💳 Stripe × Salesforce")
        st.caption("Live customer billing dashboard")
        st.markdown("<br>", unsafe_allow_html=True)
        st.link_button("🔐 Login with Salesforce", get_auth_url(), use_container_width=True, type="primary")
    st.stop()

token    = st.session_state["sf_token"]
refresh  = st.session_state["sf_refresh"]
instance = st.session_state["sf_instance"]

# ── Always show header immediately ──────────────────────────────────────────
c1,c2,c3 = st.columns([4,2,1])
with c1:
    st.markdown("## 💳 Stripe Account Statuses")
    st.caption(f"Last updated: {st.session_state.get('last_loaded','—')}")
with c2:
    search = st.text_input("🔍 Search", placeholder="Account name...")
with c3:
    st.markdown("<br>", unsafe_allow_html=True)
    b1,b2,b3 = st.columns(3)
    with b1:
        if st.button("🏠"):
            st.session_state.pop("selected_account",None)
            st.session_state["filter"] = None
            st.rerun()
    with b2:
        if st.button("🔄"):
            for k in ["sf_accounts","stripe_index","results","last_loaded"]:
                st.session_state.pop(k,None)
            st.rerun()
    with b3:
        if st.button("↩️"):
            st.session_state.clear()
            st.rerun()

# ── Load Salesforce ──────────────────────────────────────────────────────────
if "sf_accounts" not in st.session_state:
    with st.status("📊 Loading Salesforce accounts...", expanded=True) as status:
        try:
            soql = """SELECT Id,Name,BillingCountry,Billing_Email_Address__c,
                All_Time_ARR__c,Logz_Io_Parent_Account_Key__c,Email_Domain__c,
                (SELECT Name,ARR__c,Unit__c,Start_Date__c,End_Date__c,
                 Logging_Retention_Days__c,Active__c FROM Contract_Assets__r WHERE Active__c=TRUE)
                FROM Account WHERE Type='Customer' AND All_Time_ARR__c>0
                AND Payment_Method_2__c='Credit Card'
                AND (NOT Name LIKE '%test%') AND (NOT Name LIKE '%Test%')
                AND (NOT Name LIKE '%runrate%') AND (NOT Name LIKE '%Runrate%')
                AND (NOT Name LIKE '%on-demand%') AND (NOT Name LIKE '%On-Demand%')
                AND (NOT Name LIKE '%support%') AND (NOT Name LIKE '%Support%')
                AND (NOT Name LIKE '%logz.io%') AND (NOT Name LIKE '%Logz.io%')
                AND (NOT Name LIKE '%logs.io%') ORDER BY Name ASC"""
            headers = {"Authorization": f"Bearer {token}"}
            records, url, params = [], f"{instance}/services/data/v59.0/query", {"q": soql}
            while True:
                r = requests.get(url, headers=headers, params=params, timeout=30)
                if r.status_code == 401:
                    td = refresh_sf_token(refresh)
                    st.session_state["sf_token"] = td["access_token"]
                    token = td["access_token"]
                    headers = {"Authorization": f"Bearer {token}"}
                    r = requests.get(url, headers=headers, params=params, timeout=30)
                if r.status_code != 200: raise Exception(r.text)
                d = r.json()
                records.extend(d.get("records",[]))
                st.write(f"✓ Loaded {len(records)} accounts...")
                if d.get("done"): break
                url = instance + d["nextRecordsUrl"]
                params = {}
            st.session_state["sf_accounts"] = records
            st.session_state["last_loaded"] = datetime.now().strftime("%b %d, %Y %H:%M")
            status.update(label=f"✅ Salesforce: {len(records)} accounts loaded", state="complete")
        except Exception as e:
            st.error(f"Salesforce error: {e}")
            st.stop()

# ── Load Stripe ──────────────────────────────────────────────────────────────
if "stripe_index" not in st.session_state:
    with st.status("💳 Fetching Stripe customers...", expanded=True) as status:
        try:
            sf_id_idx, parent_idx, domain_idx = {}, {}, {}
            for api_key, src in [(STRIPE_US_KEY,"US"),(STRIPE_INTL_KEY,"Intl")]:
                if not api_key or len(api_key) < 10: continue
                last_id, count = None, 0
                while True:
                    params = {"limit":100,"expand[]":"data.subscriptions"}
                    if last_id: params["starting_after"] = last_id
                    r = requests.get("https://api.stripe.com/v1/customers",
                        params=params, headers={"Authorization":f"Bearer {api_key}"}, timeout=15)
                    if not r.ok: break
                    d = r.json()
                    batch = d.get("data",[])
                    if not batch: break
                    for c in batch:
                        c["_src"] = src
                        meta = c.get("metadata") or {}
                        sid = (meta.get("salesforce_id") or "").strip()
                        if sid and sid not in sf_id_idx: sf_id_idx[sid] = c
                        for k in ["main_account_id","our-account-id"]:
                            v = str(meta.get(k) or "").strip()
                            if v and v not in parent_idx: parent_idx[v] = c
                        email = (c.get("email") or "").lower()
                        dom = email.split("@")[-1] if "@" in email else ""
                        if dom and dom not in domain_idx: domain_idx[dom] = c
                    count += len(batch)
                    st.write(f"✓ {src}: {count} customers fetched...")
                    if not d.get("has_more"): break
                    last_id = batch[-1]["id"]
            st.session_state["stripe_index"] = {"sf":sf_id_idx,"par":parent_idx,"dom":domain_idx}
            total = len(sf_id_idx)
            status.update(label=f"✅ Stripe: customers indexed", state="complete")
        except Exception as e:
            st.error(f"Stripe error: {e}")
            st.stop()

# ── Match & build results ────────────────────────────────────────────────────
if "results" not in st.session_state:
    raw = st.session_state["sf_accounts"]
    idx = st.session_state["stripe_index"]
    si, pi, di = idx["sf"], idx["par"], idx["dom"]

    prog = st.progress(0, text="Matching accounts...")
    rows = []
    for i, r in enumerate(raw):
        sf_id  = r.get("Id","")
        name   = r.get("Name","")
        arr    = r.get("All_Time_ARR__c") or 0
        country= r.get("BillingCountry") or ""
        email  = (r.get("Billing_Email_Address__c") or "").lower()
        pkey   = str(r.get("Logz_Io_Parent_Account_Key__c") or "").strip()
        dom    = str(r.get("Email_Domain__c") or "").strip().lower()

        plans = []
        for p in ((r.get("Contract_Assets__r") or {}).get("records") or []):
            if not p.get("Active__c"): continue
            plans.append({"Plan Name":p.get("Name",""),"Active":"✓",
                "Units":p.get("Unit__c","") or "","ARR":p.get("ARR__c",0) or 0,
                "Start Date":p.get("Start_Date__c","") or "",
                "End Date":p.get("End_Date__c","") or "",
                "Log Retention (days)":p.get("Logging_Retention_Days__c","") or ""})

        # Match
        cust, via = None, "—"
        if sf_id and sf_id in si:   cust, via = si[sf_id], "Salesforce ID"
        elif pkey and pkey in pi:   cust, via = pi[pkey],  "Parent Key"
        elif dom and dom in di:     cust, via = di[dom],   "Email Domain"

        # Status
        ss, detail = "not_found", None
        if cust:
            subs = (cust.get("subscriptions") or {}).get("data") or []
            if not subs:
                ss = "no_subscription"
            else:
                for s in subs:
                    if s.get("status")=="active" and s.get("cancel_at_period_end"):
                        ts = s.get("cancel_at")
                        detail = datetime.fromtimestamp(ts,tz=timezone.utc).strftime("%b %d") if ts else "?"
                        ss = "cancels_on"; break
                if ss == "not_found":
                    for s in subs:
                        if s.get("status")=="active":   ss="active"; break
                if ss == "not_found":
                    for s in subs:
                        if s.get("status")=="past_due": ss="past_due"; break
                if ss == "not_found":
                    for s in subs:
                        if s.get("status")=="unpaid":   ss="unpaid"; break
                if ss == "not_found":
                    for s in subs:
                        if s.get("status")=="canceled":
                            ts = s.get("canceled_at")
                            detail = datetime.fromtimestamp(ts,tz=timezone.utc).strftime("%b %d, %Y") if ts else "?"
                            ss="canceled"; break
                if ss == "not_found":
                    ss = (subs[0].get("status") or "unknown")

        # MRR — skip on-demand by fetching product name
        mrr = 0.0
        if cust:
            for s in ((cust.get("subscriptions") or {}).get("data") or []):
                if s.get("status") not in ["active","past_due","unpaid"]: continue
                for item in (s.get("items") or {}).get("data") or []:
                    price = item.get("price") or {}
                    nick  = price.get("nickname") or ""
                    # Fetch product name to check on-demand
                    prod_obj = price.get("product")
                    prod_name = ""
                    if isinstance(prod_obj, dict):
                        prod_name = prod_obj.get("name","") or ""
                    elif isinstance(prod_obj, str) and prod_obj:
                        # fetch product
                        pr = requests.get(f"https://api.stripe.com/v1/products/{prod_obj}",
                            headers={"Authorization":f"Bearer {cust.get('_src','') and (STRIPE_US_KEY if cust.get('_src')=='US' else STRIPE_INTL_KEY)}"},
                            timeout=5)
                        if pr.ok: prod_name = pr.json().get("name","") or ""
                    if is_on_demand(nick) or is_on_demand(prod_name): continue
                    amt = (price.get("unit_amount") or 0)/100
                    qty = item.get("quantity") or 1
                    iv  = (price.get("recurring") or {}).get("interval","month")
                    ic  = (price.get("recurring") or {}).get("interval_count",1) or 1
                    mo  = amt*qty
                    if iv=="year":  mo/=(12*ic)
                    elif iv=="week": mo=mo*4.33/ic
                    elif iv=="day":  mo=mo*30/ic
                    else: mo/=ic
                    mrr += mo
        mrr = round(mrr,2)

        # Stripe plans
        stripe_plans = []
        if cust:
            for s in ((cust.get("subscriptions") or {}).get("data") or []):
                st_status = s.get("status","")
                ca = s.get("cancel_at")
                cd = datetime.fromtimestamp(ca,tz=timezone.utc).strftime("%b %d, %Y") if ca else None
                ds = f"Cancels {cd}" if (st_status=="active" and cd) else st_status.replace("_"," ").title()
                for item in (s.get("items") or {}).get("data") or []:
                    price = item.get("price") or {}
                    nick  = price.get("nickname") or ""
                    prod_obj = price.get("product")
                    prod_name = ""
                    if isinstance(prod_obj, dict): prod_name = prod_obj.get("name","") or ""
                    display_name = nick or prod_name or price.get("id","")
                    if is_on_demand(nick) or is_on_demand(prod_name): continue
                    amt = (price.get("unit_amount") or 0)/100
                    qty = item.get("quantity") or 1
                    iv  = (price.get("recurring") or {}).get("interval","month")
                    stripe_plans.append({"Product":display_name,"Price":f"${amt:,.2f}",
                        "Qty":qty,"Total/mo":f"${round(amt*qty,2):,.2f}",
                        "Frequency":iv.capitalize(),"Status":ds})

        sl = {"active":"Active","past_due":"Past due","unpaid":"Unpaid",
              "cancels_on":f"Cancels {detail}","canceled":f"Canceled ({detail})" if detail else "Canceled",
              "not_found":"Not found","no_subscription":"No subscription"}.get(ss, ss)

        cid = (cust or {}).get("id","")
        src = (cust or {}).get("_src","—") if cust else "—"
        rows.append({"sf_id":sf_id,"Account Name":name,"Country":country,
            "Billing Email":email,"SF ARR":arr,"Stripe MRR":mrr,"Stripe ARR":round(mrr*12,2),
            "Stripe Status":ss,"Status Label":sl,"Found Via":via,"Stripe Acct":src,
            "sf_plans":plans,"stripe_plans":stripe_plans,
            "sf_url":f"{SF_BASE_URL}/lightning/r/Account/{sf_id}/view" if sf_id else "",
            "stripe_url":f"https://dashboard.stripe.com/customers/{cid}" if cid else ""})
        prog.progress((i+1)/len(raw), text=f"Matching {i+1}/{len(raw)}: {name}")

    prog.empty()
    st.session_state["results"] = rows
    st.rerun()

# ── Display ──────────────────────────────────────────────────────────────────
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
    # Filters
    flt = st.session_state.get("filter")
    display = df[df["Stripe Status"]==flt].copy() if flt else df.copy()

    label_map = {"active":"Active","past_due":"Past Due","unpaid":"Unpaid",
                 "cancels_on":"Cancels w/ Date","canceled":"Canceled"}
    title = f"{label_map.get(flt,flt)} Accounts" if flt else "Account List View"

    with st.expander("🔽 Filters", expanded=False):
        fc1,fc2,fc3,fc4 = st.columns(4)
        with fc1:
            countries = ["All"] + sorted(display["Country"].dropna().unique().tolist())
            sel_country = st.selectbox("Country", countries)
        with fc2:
            stripe_accts = ["All","US","Intl"]
            sel_acct = st.selectbox("Stripe Account", stripe_accts)
        with fc3:
            min_sf = st.number_input("Min SF ARR ($)", min_value=0, value=0, step=100)
        with fc4:
            min_stripe = st.number_input("Min Stripe ARR ($)", min_value=0, value=0, step=100)

        if sel_country != "All":
            display = display[display["Country"]==sel_country]
        if sel_acct != "All":
            display = display[display["Stripe Acct"]==sel_acct]
        if min_sf > 0:
            display = display[display["SF ARR"]>=min_sf]
        if min_stripe > 0:
            display = display[display["Stripe ARR"]>=min_stripe]

    st.markdown(f"### {title} ({len(display)})")

    icons = {"active":"✅","past_due":"⚠️","unpaid":"🔶","cancels_on":"🟣",
             "canceled":"🔴","not_found":"⬜","no_subscription":"⬜"}

    h = st.columns([2.5,1,2,1.2,1.2,1.8,0.8])
    for col,hdr in zip(h,["**Account Name**","**Country**","**Billing Email**",
                            "**SF ARR**","**Stripe ARR**","**Stripe Status**","**Acct**"]):
        col.markdown(hdr)
    st.divider()

    for _,row in display.iterrows():
        c = st.columns([2.5,1,2,1.2,1.2,1.8,0.8])
        with c[0]:
            if st.button(row["Account Name"], key=f"a_{row['sf_id']}", use_container_width=True):
                st.session_state["selected_account"] = row.to_dict()
                st.rerun()
        c[1].write(row["Country"] or "—")
        c[2].write(row["Billing Email"] or "—")
        c[3].write(f"${row['SF ARR']:,.2f}" if row["SF ARR"] else "—")
        c[4].write(f"${row['Stripe ARR']:,.2f}" if row["Stripe ARR"] else "—")
        c[5].write(f"{icons.get(row['Stripe Status'],'⬜')} {row['Status Label']}")
        c[6].write(row["Stripe Acct"])

    st.divider()
    csv = display.drop(columns=["sf_plans","stripe_plans","sf_id","sf_url","stripe_url"],
                       errors="ignore").to_csv(index=False).encode("utf-8")
    st.download_button("⬇ Download CSV", csv,
        f"stripe_audit_{datetime.today().strftime('%Y-%m-%d')}.csv","text/csv")
