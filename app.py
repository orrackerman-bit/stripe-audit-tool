import streamlit as st
import requests
import pandas as pd
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

st.set_page_config(page_title="Stripe × Salesforce", page_icon="💳", layout="wide")

st.markdown("""
<style>
    .block-container { padding-top: 1.5rem; max-width: 1400px; }
    .stat-num { font-size: 2rem; font-weight: 700; margin-bottom: 0.25rem; }
    .stat-label { font-size: 0.8rem; color: #6b7280; text-transform: uppercase; letter-spacing: 0.05em; }
    .num-active  { color: #16a34a; }
    .num-pastdue { color: #d97706; }
    .num-cancels { color: #7c3aed; }
    .num-canceled{ color: #dc2626; }
    div[data-testid="stMetric"] { background: #f9fafb; border-radius: 10px; padding: 0.75rem; }
    div[data-testid="stHorizontalBlock"] button[kind="secondary"] { font-size: 12px !important; }
</style>
""", unsafe_allow_html=True)

# --- Secrets ---
try:
    SFDC_CLIENT_ID     = st.secrets["SFDC_CLIENT_ID"]
    SFDC_CLIENT_SECRET = st.secrets["SFDC_CLIENT_SECRET"]
    SFDC_DOMAIN        = st.secrets["SFDC_DOMAIN"]
    STRIPE_US_KEY      = st.secrets["STRIPE_US_KEY"]
    STRIPE_INTL_KEY    = st.secrets["STRIPE_INTL_KEY"]
except Exception:
    st.error("Missing secrets. Go to app Settings → Secrets.")
    st.stop()

REDIRECT_URI = "https://logz-stripe-audit.streamlit.app/"
AUTH_URL     = f"https://{SFDC_DOMAIN}/services/oauth2/authorize"
TOKEN_URL    = f"https://{SFDC_DOMAIN}/services/oauth2/token"

# --- OAuth ---
def get_auth_url():
    params = {"response_type": "code", "client_id": SFDC_CLIENT_ID,
              "redirect_uri": REDIRECT_URI, "scope": "api refresh_token offline_access"}
    return f"{AUTH_URL}?{urlencode(params)}"

def exchange_code(code):
    r = requests.post(TOKEN_URL, data={"grant_type": "authorization_code",
        "client_id": SFDC_CLIENT_ID, "client_secret": SFDC_CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI, "code": code}, timeout=15)
    if r.status_code != 200: raise Exception(f"Auth failed: {r.text}")
    return r.json()

def refresh_token_fn(rt):
    r = requests.post(TOKEN_URL, data={"grant_type": "refresh_token",
        "client_id": SFDC_CLIENT_ID, "client_secret": SFDC_CLIENT_SECRET,
        "refresh_token": rt}, timeout=15)
    if r.status_code != 200: raise Exception(f"Refresh failed: {r.text}")
    return r.json()

# --- Salesforce ---
def sf_query(soql, access_token, instance_url):
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    all_records = []
    url = f"{instance_url}/services/data/v59.0/query"
    params = {"q": soql}
    while True:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        if r.status_code == 401: raise Exception("TOKEN_EXPIRED")
        if r.status_code != 200: raise Exception(f"SF query failed: {r.text}")
        data = r.json()
        all_records.extend(data.get("records", []))
        if data.get("done"): break
        url = instance_url + data["nextRecordsUrl"]
        params = {}
    return all_records

@st.cache_data(ttl=300, show_spinner=False)
def fetch_sf_accounts(access_token, instance_url):
    soql = """
        SELECT Id, Name, BillingCountry, BillingState,
               Billing_Email_Address__c, Website, All_Time_ARR__c,
               (SELECT Name, ARR__c, Unit__c, Start_Date__c, End_Date__c,
                       Logging_Retention_Days__c, Active__c
                FROM Contract_Assets__r)
        FROM Account
        WHERE Type = 'Customer'
          AND All_Time_ARR__c > 0
          AND Payment_Method_2__c = 'Credit Card'
          AND (NOT Name LIKE '%test%')
          AND (NOT Name LIKE '%Test%')
          AND (NOT Name LIKE '%runrate%')
          AND (NOT Name LIKE '%Runrate%')
          AND (NOT Name LIKE '%run rate%')
          AND (NOT Name LIKE '%on-demand%')
          AND (NOT Name LIKE '%On-Demand%')
          AND (NOT Name LIKE '%support%')
          AND (NOT Name LIKE '%Support%')
          AND (NOT Name LIKE '%logz.io%')
          AND (NOT Name LIKE '%Logz.io%')
          AND (NOT Name LIKE '%logs.io%')
        ORDER BY Name ASC
    """
    return sf_query(soql, access_token, instance_url)

# --- Stripe ---
def stripe_request(endpoint, api_key, params=None):
    try:
        r = requests.get(
            f"https://api.stripe.com/v1/{endpoint}",
            params=params or {},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10
        )
        if r.status_code != 200:
            return None
        return r.json()
    except:
        return None

def stripe_search_customer(query_str, api_key):
    d = stripe_request("customers/search", api_key, {
        "query": query_str,
        "limit": 1,
        "expand[]": "data.subscriptions"
    })
    if d and d.get("data"):
        return d["data"][0]
    return None

def stripe_list_customer_by_email(email, api_key):
    """Use list endpoint as fallback — more reliable than search for email."""
    d = stripe_request("customers", api_key, {
        "email": email,
        "limit": 1,
        "expand[]": "data.subscriptions"
    })
    if d and d.get("data"):
        return d["data"][0]
    return None

def get_stripe_customer(sf_id, name, email, key_us, key_intl):
    for api_key, source in [(key_us, "US"), (key_intl, "Intl")]:
        if not api_key or len(api_key) < 10:
            continue
        # 1. Metadata: salesforce_id
        c = stripe_search_customer(f"metadata['salesforce_id']:'{sf_id}'", api_key)
        if c: return c, source, "sf_id"
        # 2. Metadata: our-account-id
        c = stripe_search_customer(f"metadata['our-account-id']:'{sf_id}'", api_key)
        if c: return c, source, "sf_id(alt)"

    # 3. Name search both accounts
    for api_key, source in [(key_us, "US"), (key_intl, "Intl")]:
        if not api_key or len(api_key) < 10: continue
        if name:
            c = stripe_search_customer(f"name:'{name}'", api_key)
            if c: return c, source, "name"

    # 4. Email — use list endpoint (exact match, more reliable)
    for api_key, source in [(key_us, "US"), (key_intl, "Intl")]:
        if not api_key or len(api_key) < 10: continue
        if email and "@" in email:
            c = stripe_list_customer_by_email(email, api_key)
            if c: return c, source, "email"

    return None, None, None

def get_stripe_status(customer):
    if not customer: return "not_found", None
    subs = customer.get("subscriptions", {}).get("data", [])
    if not subs: return "no_subscription", None
    # Active + scheduled to cancel
    for s in subs:
        if s.get("status") == "active" and s.get("cancel_at_period_end"):
            ts = s.get("cancel_at")
            date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d") if ts else "?"
            return "cancels_on", date
    for s in subs:
        if s.get("status") == "active": return "active", None
    for s in subs:
        if s.get("status") == "past_due": return "past_due", None
    for s in subs:
        if s.get("status") == "canceled":
            ts = s.get("canceled_at")
            date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d, %Y") if ts else "?"
            return "canceled", date
    return subs[0].get("status", "unknown"), None

def get_mrr(customer):
    if not customer: return 0
    total = 0
    for s in (customer.get("subscriptions", {}).get("data", []) or []):
        if s.get("status") in ["active", "past_due"]:
            for item in s.get("items", {}).get("data", []):
                price = item.get("price", {})
                amount = (price.get("unit_amount", 0) or 0) / 100
                qty = item.get("quantity", 1) or 1
                interval = price.get("recurring", {}).get("interval", "month")
                interval_count = price.get("recurring", {}).get("interval_count", 1) or 1
                monthly = amount * qty
                if interval == "year": monthly /= (12 * interval_count)
                elif interval == "week": monthly = monthly * 4.33 / interval_count
                elif interval == "day": monthly = monthly * 30 / interval_count
                else: monthly /= interval_count
                total += monthly
    return round(total, 2)

def get_stripe_plans(customer):
    if not customer: return []
    plans = []
    for s in (customer.get("subscriptions", {}).get("data", []) or []):
        status = s.get("status", "")
        cancel_at = s.get("cancel_at")
        cancel_date = datetime.fromtimestamp(cancel_at, tz=timezone.utc).strftime("%b %d, %Y") if cancel_at else None
        display_status = f"Cancels {cancel_date}" if (status == "active" and cancel_date) else status.replace("_", " ").title()
        for item in s.get("items", {}).get("data", []):
            price = item.get("price", {})
            product = price.get("nickname") or price.get("id", "")
            amount = (price.get("unit_amount", 0) or 0) / 100
            qty = item.get("quantity", 1) or 1
            interval = price.get("recurring", {}).get("interval", "month")
            total = round(amount * qty, 2)
            plans.append({
                "Product":   product,
                "Price":     f"${amount:,.2f}",
                "Qty":       qty,
                "Total/mo":  f"${total:,.2f}",
                "Frequency": interval.capitalize(),
                "Status":    display_status,
            })
    return plans

# --- OAuth callback ---
auth_code = st.query_params.get("code", None)
if auth_code and "sf_access_token" not in st.session_state:
    with st.spinner("Logging in with Salesforce..."):
        try:
            td = exchange_code(auth_code)
            st.session_state["sf_access_token"] = td["access_token"]
            st.session_state["sf_refresh_token"] = td.get("refresh_token", "")
            st.session_state["sf_instance_url"]  = td["instance_url"]
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Login failed: {e}")
            st.stop()

# --- Login screen ---
if "sf_access_token" not in st.session_state:
    st.markdown("<br><br>", unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1.5, 1, 1.5])
    with col2:
        st.markdown("### 💳 Stripe × Salesforce")
        st.caption("Live customer billing dashboard")
        st.markdown("<br>", unsafe_allow_html=True)
        st.link_button("🔐 Login with Salesforce", get_auth_url(),
                       use_container_width=True, type="primary")
    st.stop()

access_token  = st.session_state["sf_access_token"]
refresh_token = st.session_state["sf_refresh_token"]
instance_url  = st.session_state["sf_instance_url"]

# --- Load SF data ---
if "data_loaded" not in st.session_state:
    with st.spinner("Loading accounts from Salesforce..."):
        try:
            raw = fetch_sf_accounts(access_token, instance_url)
            st.session_state["sf_raw"] = raw
            st.session_state["data_loaded"] = True
            st.session_state["last_loaded"] = datetime.now()
        except Exception as e:
            if "TOKEN_EXPIRED" in str(e):
                td = refresh_token_fn(refresh_token)
                st.session_state["sf_access_token"] = td["access_token"]
                st.rerun()
            st.error(f"Salesforce error: {e}")
            st.stop()

# --- Header ---
col1, col2, col3 = st.columns([4, 2, 1])
with col1:
    st.markdown("## 💳 Stripe Account Statuses")
    if "last_loaded" in st.session_state:
        st.caption(f"Last updated: {st.session_state['last_loaded'].strftime('%b %d, %Y %H:%M')}")
with col2:
    search = st.text_input("🔍 Search accounts", placeholder="Type account name...")
with col3:
    st.markdown("<br>", unsafe_allow_html=True)
    c3a, c3b, c3c = st.columns(3)
    with c3a:
        if st.button("🏠", help="Home"):
            st.session_state.pop("selected_account", None)
            st.session_state["status_filter"] = None
            st.rerun()
    with c3b:
        if st.button("🔄", help="Refresh data"):
            fetch_sf_accounts.clear()
            for k in ["data_loaded","sf_raw","all_results"]:
                st.session_state.pop(k, None)
            st.rerun()
    with c3c:
        if st.button("↩️", help="Logout"):
            for k in ["sf_access_token","sf_refresh_token","sf_instance_url",
                      "data_loaded","sf_raw","all_results","status_filter","selected_account"]:
                st.session_state.pop(k, None)
            st.rerun()

# --- Check Stripe ---
if "all_results" not in st.session_state:
    raw = st.session_state.get("sf_raw", [])
    results = []
    prog = st.progress(0, text="Checking Stripe...")
    txt  = st.empty()
    for i, r in enumerate(raw):
        name    = r.get("Name", "")
        email   = (r.get("Billing_Email_Address__c") or "").strip().lower()
        arr     = r.get("All_Time_ARR__c", 0) or 0
        country = r.get("BillingCountry", "") or ""
        sf_id   = r.get("Id", "")

        # SF Plans
        plans_data = r.get("Contract_Assets__r") or {}
        plans = []
        for p in (plans_data.get("records") or []):
            plans.append({
                "Plan Name":            p.get("Name", ""),
                "Active":               "✓" if p.get("Active__c") else "—",
                "Units":                p.get("Unit__c", "") or "",
                "ARR":                  p.get("ARR__c", 0) or 0,
                "Start Date":           p.get("Start_Date__c", "") or "",
                "End Date":             p.get("End_Date__c", "") or "",
                "Log Retention (days)": p.get("Logging_Retention_Days__c", "") or "",
            })

        txt.caption(f"Checking {i+1}/{len(raw)}: {name}")
        prog.progress((i+1)/len(raw))

        customer, stripe_source, matched_by = get_stripe_customer(
            sf_id, name, email, STRIPE_US_KEY, STRIPE_INTL_KEY)
        stripe_status, status_detail = get_stripe_status(customer)
        mrr = get_mrr(customer)
        stripe_plans = get_stripe_plans(customer)

        if stripe_status == "active":            status_label = "Active"
        elif stripe_status == "past_due":        status_label = "Past due"
        elif stripe_status == "cancels_on":      status_label = f"Cancels {status_detail}"
        elif stripe_status == "canceled":        status_label = f"Canceled ({status_detail})" if status_detail else "Canceled"
        elif stripe_status == "not_found":       status_label = "Not found"
        elif stripe_status == "no_subscription": status_label = "No subscription"
        else:                                    status_label = stripe_status.replace("_"," ").title()

        results.append({
            "sf_id":            sf_id,
            "Account Name":     name,
            "Country":          country,
            "Billing Email":    email,
            "SF ARR":           arr,
            "Stripe MRR":       mrr,
            "Stripe ARR":       round(mrr * 12, 2),
            "Stripe Status":    stripe_status,
            "Status Label":     status_label,
            "Matched By":       matched_by or "—",
            "Stripe Account":   stripe_source or "—",
            "sf_plans":         plans,
            "stripe_plans":     stripe_plans,
        })
        time.sleep(0.05)

    prog.empty()
    txt.empty()
    st.session_state["all_results"] = results

results = st.session_state["all_results"]
df = pd.DataFrame(results)

if search:
    df = df[df["Account Name"].str.contains(search, case=False, na=False)]

n_active   = len(df[df["Stripe Status"] == "active"])
n_pastdue  = len(df[df["Stripe Status"] == "past_due"])
n_cancels  = len(df[df["Stripe Status"] == "cancels_on"])
n_canceled = len(df[df["Stripe Status"] == "canceled"])

if "status_filter" not in st.session_state:
    st.session_state["status_filter"] = None

# --- Stat boxes ---
st.markdown("<br>", unsafe_allow_html=True)
c1, c2, c3, c4 = st.columns(4)

def stat_card(col, label, num, css_class, status_key):
    with col:
        selected = st.session_state["status_filter"] == status_key
        border = "2px solid #6366f1" if selected else "1px solid #e5e7eb"
        bg = "#f5f3ff" if selected else "#ffffff"
        st.markdown(f"""
        <div style="background:{bg}; border:{border}; border-radius:12px;
                    padding:1.25rem 1.5rem; text-align:center; margin-bottom:0.5rem;">
            <div class="stat-num {css_class}">{num}</div>
            <div class="stat-label">{label}</div>
        </div>""", unsafe_allow_html=True)
        btn_label = f"✓ {label}" if selected else label
        if st.button(btn_label, key=f"btn_{status_key}", use_container_width=True):
            st.session_state["status_filter"] = None if selected else status_key
            st.session_state.pop("selected_account", None)
            st.rerun()

stat_card(c1, "Active",          n_active,   "num-active",   "active")
stat_card(c2, "Past Due",        n_pastdue,  "num-pastdue",  "past_due")
stat_card(c3, "Cancels w/ Date", n_cancels,  "num-cancels",  "cancels_on")
stat_card(c4, "Canceled",        n_canceled, "num-canceled", "canceled")

# --- Account detail ---
if "selected_account" in st.session_state:
    acct = st.session_state["selected_account"]
    if st.button("← Back to list"):
        del st.session_state["selected_account"]
        st.rerun()
    st.divider()

    status_colors = {
        "active":      ("#dcfce7","#166534"),
        "past_due":    ("#fef3c7","#92400e"),
        "cancels_on":  ("#ede9fe","#5b21b6"),
        "canceled":    ("#fee2e2","#991b1b"),
    }
    bg_c, txt_c = status_colors.get(acct["Stripe Status"], ("#f3f4f6","#374151"))

    col1, col2 = st.columns([3, 1])
    with col1:
        st.markdown(f"## {acct['Account Name']}")
        st.caption(f"{acct['Billing Email']} · {acct['Country']} · Matched by: {acct['Matched By']} · {acct['Stripe Account']} Stripe account")
    with col2:
        st.markdown(f"<br><span style='background:{bg_c}; color:{txt_c}; padding:6px 16px; border-radius:20px; font-size:13px; font-weight:600;'>{acct['Status Label']}</span>", unsafe_allow_html=True)

    st.divider()
    m1, m2, m3 = st.columns(3)
    m1.metric("Salesforce All-Time ARR", f"${acct['SF ARR']:,.0f}")
    m2.metric("Stripe MRR",              f"${acct['Stripe MRR']:,.2f}")
    m3.metric("Stripe ARR (MRR×12)",     f"${acct['Stripe ARR']:,.0f}")

    st.divider()
    tab1, tab2 = st.tabs(["📋 Salesforce Plans", "💳 Stripe Subscriptions"])

    with tab1:
        plans = acct.get("sf_plans", [])
        if plans:
            pdf = pd.DataFrame(plans)
            pdf["ARR"] = pdf["ARR"].apply(lambda x: f"${x:,.0f}" if x else "—")
            st.dataframe(pdf, use_container_width=True, hide_index=True)
        else:
            st.info("No plans found in Salesforce for this account.")

    with tab2:
        sp = acct.get("stripe_plans", [])
        if sp:
            st.dataframe(pd.DataFrame(sp), use_container_width=True, hide_index=True)
        else:
            st.info("No subscriptions found in Stripe for this account.")

else:
    # --- List view ---
    status_filter = st.session_state.get("status_filter")
    if status_filter:
        display_df = df[df["Stripe Status"] == status_filter].copy()
        label_map = {"active":"Active","past_due":"Past Due","cancels_on":"Cancels w/ Date","canceled":"Canceled"}
        st.markdown(f"### {label_map.get(status_filter,'')} Accounts ({len(display_df)})")
    else:
        display_df = df.copy()
        st.markdown(f"### Account List View ({len(display_df)} accounts)")

    status_icons = {
        "active":"✅","past_due":"⚠️","cancels_on":"🟣",
        "canceled":"🔴","not_found":"⬜","no_subscription":"⬜"
    }

    # Headers
    h1,h2,h3,h4,h5,h6,h7 = st.columns([2.5,1,2,1.2,1.2,1.8,0.8])
    h1.markdown("**Account Name**")
    h2.markdown("**Country**")
    h3.markdown("**Billing Email**")
    h4.markdown("**SF ARR**")
    h5.markdown("**Stripe ARR**")
    h6.markdown("**Stripe Status**")
    h7.markdown("**Stripe Acct**")
    st.divider()

    for _, row in display_df.iterrows():
        c1,c2,c3,c4,c5,c6,c7 = st.columns([2.5,1,2,1.2,1.2,1.8,0.8])
        with c1:
            if st.button(row["Account Name"], key=f"acct_{row['sf_id']}", use_container_width=True):
                st.session_state["selected_account"] = row.to_dict()
                st.rerun()
        c2.write(row["Country"] or "—")
        c3.write(row["Billing Email"] or "—")
        c4.write(f"${row['SF ARR']:,.0f}" if row["SF ARR"] else "—")
        c5.write(f"${row['Stripe ARR']:,.0f}" if row["Stripe ARR"] else "—")
        icon = status_icons.get(row["Stripe Status"], "⬜")
        c6.write(f"{icon} {row['Status Label']}")
        c7.write(row["Stripe Account"])

    st.divider()
    csv = display_df.drop(columns=["sf_plans","stripe_plans","sf_id"], errors="ignore").to_csv(index=False).encode("utf-8")
    st.download_button("⬇ Download CSV", data=csv,
        file_name=f"stripe_audit_{datetime.today().strftime('%Y-%m-%d')}.csv", mime="text/csv")
