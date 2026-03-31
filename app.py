import streamlit as st
import requests
import pandas as pd
from simple_salesforce import Salesforce
import time
from datetime import datetime

st.set_page_config(
    page_title="Stripe Audit Tool",
    page_icon="💳",
    layout="wide"
)

# --- Styling ---
st.markdown("""
<style>
    .block-container { padding-top: 2rem; }
    .metric-container { background: #f8f9fa; border-radius: 10px; padding: 1rem; text-align: center; }
    .flagged-row { background-color: #fff3cd; }
    div[data-testid="stMetric"] { background: #f8f9fa; border-radius: 10px; padding: 1rem; }
</style>
""", unsafe_allow_html=True)

st.title("💳 Stripe × Salesforce Audit")
st.caption("Live check — pulls active credit card accounts from Salesforce and verifies each one against Stripe.")

# --- Sidebar: Credentials ---
with st.sidebar:
    st.header("🔑 Credentials")
    st.caption("These are stored only in your browser session.")

    st.subheader("Salesforce")
    sf_username    = st.text_input("Username", key="sf_user")
    sf_password    = st.text_input("Password", type="password", key="sf_pass")
    sf_token       = st.text_input("Security Token", type="password", key="sf_token",
                                   help="Found in Salesforce → Settings → Reset My Security Token")
    sf_domain      = st.selectbox("Domain", ["login", "test"], key="sf_domain",
                                  help="Use 'test' for sandbox, 'login' for production")

    st.divider()
    st.subheader("Stripe")
    stripe_key_us   = st.text_input("US Account Key (sk_live_...)",   type="password", key="stripe_us")
    stripe_key_intl = st.text_input("Non-US Account Key (sk_live_...)", type="password", key="stripe_intl")

    st.divider()
    run_btn = st.button("▶ Run Audit", type="primary", use_container_width=True)

# --- Salesforce: Pull live data ---
@st.cache_data(ttl=300, show_spinner=False)
def fetch_salesforce_accounts(username, password, token, domain):
    sf = Salesforce(username=username, password=password, security_token=token, domain=domain)
    query = """
        SELECT 
            Name, 
            BillingCountry, 
            BillingState, 
            Billing_Email_Address__c,
            Website,
            All_Time_ARR__c,
            Account_Owner__c
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
    result = sf.query_all(query)
    records = result.get("records", [])
    rows = []
    for r in records:
        rows.append({
            "Account Name":          r.get("Name", ""),
            "Billing Country":       r.get("BillingCountry", ""),
            "Billing State":         r.get("BillingState", ""),
            "Billing Email Address": r.get("Billing_Email_Address__c", "") or "",
            "Website":               r.get("Website", "") or "",
            "All Time ARR":          r.get("All_Time_ARR__c", 0) or 0,
        })
    return pd.DataFrame(rows)

# --- Stripe: Search customer ---
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
    # Name first
    if name:
        customers = stripe_search(f"name:'{name}'", api_key)
        result = resolve_customer(customers)
        if result:
            result["matched_by"] = "name"
            return result
    # Email fallback
    if email and "@" in email:
        customers = stripe_search(f"email:'{email}'", api_key)
        result = resolve_customer(customers)
        if result:
            result["matched_by"] = "email"
            return result
    return None

def lookup_both_accounts(name, email, key_us, key_intl):
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
        "canceled":        "Subscription canceled",
        "past_due":        "Payment past due",
        "unpaid":          "Invoice unpaid",
        "not_found":       "Not in either Stripe account",
        "no_subscription": "No subscription found",
        "active":          "",
        "trialing":        "",
    }.get(status, status)

FLAGGED = {"canceled", "past_due", "unpaid"}

# --- Main logic ---
if run_btn:
    # Validate inputs
    missing = []
    if not sf_username: missing.append("Salesforce username")
    if not sf_password: missing.append("Salesforce password")
    if not sf_token:    missing.append("Salesforce security token")
    if not stripe_key_us and not stripe_key_intl:
        missing.append("at least one Stripe key")
    if missing:
        st.error(f"Missing: {', '.join(missing)}")
        st.stop()

    # Step 1: Pull Salesforce data
    with st.spinner("Connecting to Salesforce and pulling accounts..."):
        try:
            df = fetch_salesforce_accounts(sf_username, sf_password, sf_token, sf_domain)
        except Exception as e:
            st.error(f"Salesforce error: {e}")
            st.stop()

    st.success(f"✓ Pulled {len(df)} active credit card accounts from Salesforce")

    # Step 2: Check each account against Stripe
    results = []
    progress = st.progress(0, text="Checking against Stripe...")
    status_text = st.empty()

    for i, row in df.iterrows():
        name  = str(row.get("Account Name", "")).strip()
        email = str(row.get("Billing Email Address", "")).strip().lower()
        arr   = row.get("All Time ARR", 0)
        country = str(row.get("Billing Country", "")).strip()

        status_text.caption(f"Checking {i+1}/{len(df)}: {name}")
        progress.progress((i + 1) / len(df), text=f"Checking {i+1} of {len(df)}...")

        result = lookup_both_accounts(name, email, stripe_key_us, stripe_key_intl)

        results.append({
            "Account Name":    name,
            "Billing Email":   email,
            "ARR":             arr,
            "Country":         country,
            "Stripe Status":   result.get("status", "error"),
            "Found In":        result.get("found_in", "none"),
            "Matched By":      result.get("matched_by", "—"),
            "Flag Reason":     flag_reason(result.get("status", "")),
            "Flagged":         result.get("status") in FLAGGED,
            "Customer ID":     result.get("customer_id", ""),
            "Subscription ID": result.get("sub_id", ""),
        })

        time.sleep(0.08)

    progress.empty()
    status_text.empty()

    results_df = pd.DataFrame(results)

    # --- Summary metrics ---
    st.divider()
    st.subheader("Results")

    total    = len(results_df)
    active   = len(results_df[results_df["Stripe Status"].isin(["active", "trialing"])])
    canceled = len(results_df[results_df["Stripe Status"] == "canceled"])
    past_due = len(results_df[results_df["Stripe Status"].isin(["past_due", "unpaid"])])
    flagged  = len(results_df[results_df["Flagged"] == True])
    in_us    = len(results_df[results_df["Found In"].str.contains("US", na=False)])
    in_intl  = len(results_df[results_df["Found In"].str.contains("Intl", na=False)])

    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    c1.metric("Total checked", total)
    c2.metric("Active",    active,    delta=None)
    c3.metric("Flagged 🚨", flagged,  delta=None)
    c4.metric("Canceled",  canceled,  delta=None)
    c5.metric("Past due",  past_due,  delta=None)
    c6.metric("US acct",   in_us,     delta=None)
    c7.metric("Intl acct", in_intl,   delta=None)

    # --- Flagged table ---
    st.divider()
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        f"🚨 Flagged ({flagged})",
        f"All results ({total})",
        f"Canceled ({canceled})",
        f"Past due ({past_due})",
        f"Not found ({len(results_df[results_df['Stripe Status'] == 'not_found'])})"
    ])

    def show_table(data):
        if data.empty:
            st.info("No records for this filter.")
            return
        display = data[[
            "Account Name", "Billing Email", "ARR",
            "Country", "Stripe Status", "Found In", "Matched By", "Flag Reason"
        ]].copy()
        display["ARR"] = display["ARR"].apply(lambda x: f"${x:,.0f}" if x else "—")
        st.dataframe(display, use_container_width=True, hide_index=True)

    with tab1: show_table(results_df[results_df["Flagged"] == True])
    with tab2: show_table(results_df)
    with tab3: show_table(results_df[results_df["Stripe Status"] == "canceled"])
    with tab4: show_table(results_df[results_df["Stripe Status"].isin(["past_due", "unpaid"])])
    with tab5: show_table(results_df[results_df["Stripe Status"] == "not_found"])

    # --- Download ---
    st.divider()
    csv = results_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        label="⬇ Download full report as CSV",
        data=csv,
        file_name=f"stripe_audit_{datetime.today().strftime('%Y-%m-%d')}.csv",
        mime="text/csv"
    )

else:
    st.info("👈 Fill in your credentials in the sidebar and click **Run Audit** to start.")
    st.markdown("""
    **How it works:**
    1. Connects live to Salesforce and pulls all active credit card accounts (ARR > 0)
    2. Checks each account against your US and Non-US Stripe accounts (by company name, then email as fallback)
    3. Flags anyone that is canceled or past due in Stripe but still active in Salesforce
    4. Shows results instantly — no CSV export needed
    """)
