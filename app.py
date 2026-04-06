import streamlit as st
import requests
import pandas as pd
import time
from datetime import datetime
from urllib.parse import urlencode, urlparse, parse_qs

st.set_page_config(page_title="Stripe Audit Tool", page_icon="💳", layout="wide")

st.markdown("""
<style>
    .block-container { padding-top: 2rem; }
    div[data-testid="stMetric"] { background: #f8f9fa; border-radius: 10px; padding: 1rem; }
    .login-box { max-width: 400px; margin: 80px auto; text-align: center; padding: 40px;
                 border: 1px solid #e0e0e0; border-radius: 12px; background: #fff; }
</style>
""", unsafe_allow_html=True)

# --- Load secrets ---
try:
    SFDC_CLIENT_ID     = st.secrets["SFDC_CLIENT_ID"]
    SFDC_CLIENT_SECRET = st.secrets["SFDC_CLIENT_SECRET"]
    SFDC_DOMAIN        = st.secrets["SFDC_DOMAIN"]
    STRIPE_US_KEY      = st.secrets["STRIPE_US_KEY"]
    STRIPE_INTL_KEY    = st.secrets["STRIPE_INTL_KEY"]
except Exception:
    st.error("Missing secrets. Go to app Settings → Secrets and add all five credentials.")
    st.stop()

SFDC_BASE_URL     = f"https://{SFDC_DOMAIN}"
REDIRECT_URI      = "https://logz-stripe-audit.streamlit.app/"
AUTH_URL          = f"{SFDC_BASE_URL}/services/oauth2/authorize"
TOKEN_URL         = f"{SFDC_BASE_URL}/services/oauth2/token"

# --- OAuth helpers ---
def get_auth_url():
    params = {
        "response_type": "code",
        "client_id":     SFDC_CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "scope":         "api refresh_token offline_access",
    }
    return f"{AUTH_URL}?{urlencode(params)}"

def exchange_code_for_token(code):
    resp = requests.post(TOKEN_URL, data={
        "grant_type":    "authorization_code",
        "client_id":     SFDC_CLIENT_ID,
        "client_secret": SFDC_CLIENT_SECRET,
        "redirect_uri":  REDIRECT_URI,
        "code":          code,
    }, timeout=15)
    if resp.status_code != 200:
        raise Exception(f"Token exchange failed: {resp.text}")
    return resp.json()

def refresh_access_token(refresh_token):
    resp = requests.post(TOKEN_URL, data={
        "grant_type":    "refresh_token",
        "client_id":     SFDC_CLIENT_ID,
        "client_secret": SFDC_CLIENT_SECRET,
        "refresh_token": refresh_token,
    }, timeout=15)
    if resp.status_code != 200:
        raise Exception(f"Token refresh failed: {resp.text}")
    return resp.json()

# --- Salesforce query ---
def fetch_salesforce_accounts(access_token, instance_url):
    query = """
        SELECT Name, BillingCountry, BillingState,
               Billing_Email_Address__c, Website, All_Time_ARR__c
        FROM Account
        WHERE Type = 'Customer'
          AND All_Time_ARR__c > 0
          AND Payment_Method__c = 'Credit Card'
          AND Name NOT LIKE '%test%'
          AND Name NOT LIKE '%Test%'
          AND Name NOT LIKE '%runrate%'
          AND Name NOT LIKE '%on-demand%'
          AND Name NOT LIKE '%support%'
          AND Name NOT LIKE '%logz.io%'
          AND Name NOT LIKE '%logs.io%'
        ORDER BY Name ASC
    """
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    all_records = []
    url = f"{instance_url}/services/data/v59.0/query"
    params = {"q": query}
    while True:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 401:
            raise Exception("TOKEN_EXPIRED")
        if resp.status_code != 200:
            raise Exception(f"Salesforce query failed: {resp.text}")
        data = resp.json()
        all_records.extend(data.get("records", []))
        if data.get("done"):
            break
        url = instance_url + data["nextRecordsUrl"]
        params = {}
    rows = []
    for r in all_records:
        rows.append({
            "Account Name":          r.get("Name", ""),
            "Billing Country":       r.get("BillingCountry", "") or "",
            "Billing State":         r.get("BillingState", "") or "",
            "Billing Email Address": r.get("Billing_Email_Address__c", "") or "",
            "Website":               r.get("Website", "") or "",
            "All Time ARR":          r.get("All_Time_ARR__c", 0) or 0,
        })
    return pd.DataFrame(rows)

# --- Stripe lookup ---
def stripe_search(query_str, api_key):
    if not api_key or len(api_key) < 10:
        return []
    try:
        resp = requests.get(
            "https://api.stripe.com/v1/customers/search",
            params={"query": query_str, "limit": 3, "expand[]": "data.subscriptions"},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10
        )
        if resp.status_code != 200:
            return []
        return resp.json().get("data", [])
    except Exception:
        return []

def resolve_customer(customers):
    if not customers:
        return None
    c = customers[0]
    subs = c.get("subscriptions", {}).get("data", [])
    if not subs:
        return {"status": "no_subscription", "customer_id": c["id"], "sub_id": ""}
    active = next((s for s in subs if s["status"] in ["active", "trialing"]), None)
    if active:
        return {"status": active["status"], "customer_id": c["id"], "sub_id": active["id"]}
    latest = subs[0]
    return {"status": latest["status"], "customer_id": c["id"], "sub_id": latest["id"]}

def lookup_in_account(name, email, api_key):
    if not api_key or len(api_key) < 10:
        return None
    if name:
        result = resolve_customer(stripe_search(f"name:'{name}'", api_key))
        if result:
            result["matched_by"] = "name"
            return result
    if email and "@" in email:
        result = resolve_customer(stripe_search(f"email:'{email}'", api_key))
        if result:
            result["matched_by"] = "email"
            return result
    return None

def lookup_both(name, email, key_us, key_intl):
    res_us   = lookup_in_account(name, email, key_us)
    res_intl = lookup_in_account(name, email, key_intl)
    found_in = []
    if res_us:   found_in.append("US")
    if res_intl: found_in.append("Intl")
    if not found_in:
        return {"status": "not_found", "found_in": "none", "matched_by": "—", "customer_id": "", "sub_id": ""}
    priority = ["active", "trialing", "past_due", "unpaid", "no_subscription", "canceled"]
    candidates = [r for r in [res_us, res_intl] if r]
    candidates.sort(key=lambda r: priority.index(r["status"]) if r["status"] in priority else 99)
    best = candidates[0]
    best["found_in"] = "+".join(found_in)
    return best

def flag_reason(status):
    return {
        "canceled": "Subscription canceled", "past_due": "Payment past due",
        "unpaid": "Invoice unpaid", "not_found": "Not in either Stripe account",
        "no_subscription": "No subscription found", "active": "", "trialing": "",
    }.get(status, status)

FLAGGED = {"canceled", "past_due", "unpaid"}

# --- Check for OAuth callback code in URL ---
query_params = st.query_params
auth_code = query_params.get("code", None)

# --- Handle token exchange from callback ---
if auth_code and "sf_access_token" not in st.session_state:
    with st.spinner("Logging in with Salesforce..."):
        try:
            token_data = exchange_code_for_token(auth_code)
            st.session_state["sf_access_token"]  = token_data["access_token"]
            st.session_state["sf_refresh_token"]  = token_data.get("refresh_token", "")
            st.session_state["sf_instance_url"]   = token_data["instance_url"]
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Salesforce login failed: {e}")
            st.stop()

# --- Not logged in: show login screen ---
if "sf_access_token" not in st.session_state:
    st.markdown("""
    <div class="login-box">
        <h2>💳 Stripe Audit Tool</h2>
        <p style="color:#666; margin-bottom:2rem;">Sign in with your Salesforce account to run a live audit of your credit card customers against Stripe.</p>
    </div>
    """, unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.link_button("🔐 Login with Salesforce", get_auth_url(), use_container_width=True, type="primary")
    st.stop()

# --- Logged in: show audit tool ---
access_token  = st.session_state["sf_access_token"]
refresh_token = st.session_state["sf_refresh_token"]
instance_url  = st.session_state["sf_instance_url"]

col1, col2 = st.columns([6, 1])
with col1:
    st.title("💳 Stripe × Salesforce Audit")
    st.caption("Live check — pulls active credit card accounts from Salesforce and verifies each one against Stripe.")
with col2:
    if st.button("Logout"):
        for k in ["sf_access_token", "sf_refresh_token", "sf_instance_url"]:
            st.session_state.pop(k, None)
        st.rerun()

st.divider()
run_btn = st.button("▶ Run Audit", type="primary")

if run_btn:
    with st.spinner("Pulling accounts from Salesforce..."):
        try:
            df = fetch_salesforce_accounts(access_token, instance_url)
        except Exception as e:
            if "TOKEN_EXPIRED" in str(e):
                try:
                    token_data = refresh_access_token(refresh_token)
                    st.session_state["sf_access_token"] = token_data["access_token"]
                    access_token = token_data["access_token"]
                    df = fetch_salesforce_accounts(access_token, instance_url)
                except Exception as e2:
                    st.error(f"Session expired. Please logout and login again. ({e2})")
                    st.stop()
            else:
                st.error(f"Salesforce error: {e}")
                st.stop()

    st.success(f"✓ Pulled {len(df)} active credit card accounts from Salesforce")

    results = []
    progress  = st.progress(0, text="Checking against Stripe...")
    status_txt = st.empty()

    for i, row in df.iterrows():
        name    = str(row.get("Account Name", "")).strip()
        email   = str(row.get("Billing Email Address", "")).strip().lower()
        arr     = row.get("All Time ARR", 0)
        country = str(row.get("Billing Country", "")).strip()
        status_txt.caption(f"Checking {i+1}/{len(df)}: {name}")
        progress.progress((i + 1) / len(df))
        result = lookup_both(name, email, STRIPE_US_KEY, STRIPE_INTL_KEY)
        results.append({
            "Account Name":  name, "Billing Email": email, "ARR": arr, "Country": country,
            "Stripe Status": result.get("status", "error"),
            "Found In":      result.get("found_in", "none"),
            "Matched By":    result.get("matched_by", "—"),
            "Flag Reason":   flag_reason(result.get("status", "")),
            "Flagged":       result.get("status") in FLAGGED,
            "Customer ID":   result.get("customer_id", ""),
            "Subscription ID": result.get("sub_id", ""),
        })
        time.sleep(0.08)

    progress.empty()
    status_txt.empty()
    results_df = pd.DataFrame(results)

    st.divider()
    total    = len(results_df)
    active   = len(results_df[results_df["Stripe Status"].isin(["active", "trialing"])])
    canceled = len(results_df[results_df["Stripe Status"] == "canceled"])
    past_due = len(results_df[results_df["Stripe Status"].isin(["past_due", "unpaid"])])
    flagged  = len(results_df[results_df["Flagged"] == True])
    in_us    = len(results_df[results_df["Found In"].str.contains("US", na=False)])
    in_intl  = len(results_df[results_df["Found In"].str.contains("Intl", na=False)])

    c1,c2,c3,c4,c5,c6,c7 = st.columns(7)
    c1.metric("Total",      total)
    c2.metric("Active",     active)
    c3.metric("🚨 Flagged", flagged)
    c4.metric("Canceled",   canceled)
    c5.metric("Past due",   past_due)
    c6.metric("US acct",    in_us)
    c7.metric("Intl acct",  in_intl)

    st.divider()
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        f"🚨 Flagged ({flagged})", f"All ({total})", f"Canceled ({canceled})",
        f"Past due ({past_due})", f"Not found ({len(results_df[results_df['Stripe Status'] == 'not_found'])})"
    ])

    def show_table(data):
        if data.empty:
            st.info("No records for this filter.")
            return
        display = data[["Account Name","Billing Email","ARR","Country","Stripe Status","Found In","Matched By","Flag Reason"]].copy()
        display["ARR"] = display["ARR"].apply(lambda x: f"${x:,.0f}" if x else "—")
        st.dataframe(display, use_container_width=True, hide_index=True)

    with tab1: show_table(results_df[results_df["Flagged"] == True])
    with tab2: show_table(results_df)
    with tab3: show_table(results_df[results_df["Stripe Status"] == "canceled"])
    with tab4: show_table(results_df[results_df["Stripe Status"].isin(["past_due","unpaid"])])
    with tab5: show_table(results_df[results_df["Stripe Status"] == "not_found"])

    st.divider()
    csv = results_df.to_csv(index=False).encode("utf-8")
    st.download_button("⬇ Download full report", data=csv,
        file_name=f"stripe_audit_{datetime.today().strftime('%Y-%m-%d')}.csv", mime="text/csv")

else:
    st.info("Click **Run Audit** to pull live data from Salesforce and check against Stripe.")
    st.markdown("""
    **How it works:**
    1. Connects to Salesforce using your login and pulls all active credit card accounts (ARR > 0)
    2. Checks each account against your US and Non-US Stripe accounts (by name first, then email as fallback)
    3. Flags anyone canceled or past due in Stripe but still active in Salesforce
    4. Download the full report as CSV
    """)
