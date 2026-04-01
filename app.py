import streamlit as st
import requests
import pandas as pd
import time
from datetime import datetime

st.set_page_config(
    page_title="Stripe Audit Tool",
    page_icon="💳",
    layout="wide"
)

st.markdown("""
<style>
    .block-container { padding-top: 2rem; }
    div[data-testid="stMetric"] { background: #f8f9fa; border-radius: 10px; padding: 1rem; }
</style>
""", unsafe_allow_html=True)

st.title("💳 Stripe × Salesforce Audit")
st.caption("Live check — pulls active credit card accounts from Salesforce and verifies each one against Stripe.")

@st.cache_data(ttl=3000, show_spinner=False)
def get_sf_token():
    client_id     = st.secrets["SFDC_CLIENT_ID"]
    client_secret = st.secrets["SFDC_CLIENT_SECRET"]
    domain        = st.secrets["SFDC_DOMAIN"]
    resp = requests.post(
        f"https://{domain}/services/oauth2/token",
        data={
            "grant_type":    "client_credentials",
            "client_id":     client_id,
            "client_secret": client_secret,
        },
        timeout=15
    )
    if resp.status_code != 200:
        st.error(f"Salesforce auth failed: {resp.text}")
        st.stop()
    data = resp.json()
    return data["access_token"], data["instance_url"]

@st.cache_data(ttl=300, show_spinner=False)
def fetch_salesforce_accounts():
    access_token, instance_url = get_sf_token()
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
    headers = {"Authorization": f"Bearer {access_token}"}
    all_records = []
    url = f"{instance_url}/services/data/v59.0/query?q={requests.utils.quote(query)}"
    while url:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            st.error(f"Salesforce query failed: {resp.text}")
            st.stop()
        data = resp.json()
        all_records.extend(data.get("records", []))
        next_url = data.get("nextRecordsUrl")
        url = f"{instance_url}{next_url}" if next_url else None

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
    found_in = (["US"] if res_us else []) + (["Intl"] if res_intl else [])
    if not found_in:
        return {"status": "not_found", "found_in": "none", "matched_by": "—", "customer_id": "", "sub_id": ""}
    priority = ["active", "trialing", "past_due", "unpaid", "no_subscription", "canceled"]
    best = sorted([r for r in [res_us, res_intl] if r],
                  key=lambda r: priority.index(r["status"]) if r["status"] in priority else 99)[0]
    best["found_in"] = "+".join(found_in)
    return best

def flag_reason(status):
    return {
        "canceled": "Subscription canceled",
        "past_due": "Payment past due",
        "unpaid": "Invoice unpaid",
        "not_found": "Not in either Stripe account",
        "no_subscription": "No subscription found",
        "active": "", "trialing": "",
    }.get(status, status)

FLAGGED = {"canceled", "past_due", "unpaid"}

with st.sidebar:
    st.header("🔑 Stripe Keys")
    st.caption("Salesforce connects automatically via saved credentials.")
    stripe_key_us   = st.text_input("US Account (sk_live_...)",     type="password")
    stripe_key_intl = st.text_input("Non-US Account (sk_live_...)", type="password")
    st.divider()
    run_btn = st.button("▶ Run Audit", type="primary", use_container_width=True)

if run_btn:
    if not stripe_key_us and not stripe_key_intl:
        st.error("Please enter at least one Stripe key.")
        st.stop()

    with st.spinner("Connecting to Salesforce..."):
        try:
            df = fetch_salesforce_accounts()
        except Exception as e:
            st.error(f"Salesforce error: {e}")
            st.stop()

    st.success(f"✓ Pulled {len(df)} active credit card accounts from Salesforce")

    results = []
    progress = st.progress(0, text="Checking against Stripe...")
    status_text = st.empty()

    for i, row in df.iterrows():
        name    = str(row.get("Account Name", "")).strip()
        email   = str(row.get("Billing Email Address", "")).strip().lower()
        arr     = row.get("All Time ARR", 0)
        country = str(row.get("Billing Country", "")).strip()

        status_text.caption(f"Checking {i+1}/{len(df)}: {name}")
        progress.progress((i + 1) / len(df))

        result = lookup_both(name, email, stripe_key_us, stripe_key_intl)
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

    st.divider()
    total    = len(results_df)
    active   = len(results_df[results_df["Stripe Status"].isin(["active", "trialing"])])
    flagged  = len(results_df[results_df["Flagged"]])
    canceled = len(results_df[results_df["Stripe Status"] == "canceled"])
    past_due = len(results_df[results_df["Stripe Status"].isin(["past_due", "unpaid"])])
    in_us    = len(results_df[results_df["Found In"].str.contains("US", na=False)])
    in_intl  = len(results_df[results_df["Found In"].str.contains("Intl", na=False)])

    c1,c2,c3,c4,c5,c6,c7 = st.columns(7)
    c1.metric("Checked",    total)
    c2.metric("Active",     active)
    c3.metric("🚨 Flagged", flagged)
    c4.metric("Canceled",   canceled)
    c5.metric("Past due",   past_due)
    c6.metric("US acct",    in_us)
    c7.metric("Intl acct",  in_intl)

    st.divider()
    not_found = len(results_df[results_df["Stripe Status"] == "not_found"])
    tab1,tab2,tab3,tab4,tab5 = st.tabs([
        f"🚨 Flagged ({flagged})",
        f"All ({total})",
        f"Canceled ({canceled})",
        f"Past due ({past_due})",
        f"Not found ({not_found})"
    ])

    def show_table(data):
        if data.empty:
            st.info("No records for this filter.")
            return
        display = data[["Account Name","Billing Email","ARR","Country",
                         "Stripe Status","Found In","Matched By","Flag Reason"]].copy()
        display["ARR"] = display["ARR"].apply(lambda x: f"${x:,.0f}" if x else "—")
        st.dataframe(display, use_container_width=True, hide_index=True)

    with tab1: show_table(results_df[results_df["Flagged"]])
    with tab2: show_table(results_df)
    with tab3: show_table(results_df[results_df["Stripe Status"] == "canceled"])
    with tab4: show_table(results_df[results_df["Stripe Status"].isin(["past_due","unpaid"])])
    with tab5: show_table(results_df[results_df["Stripe Status"] == "not_found"])

    st.divider()
    st.download_button(
        "⬇ Download full report",
        data=results_df.to_csv(index=False).encode("utf-8"),
        file_name=f"stripe_audit_{datetime.today().strftime('%Y-%m-%d')}.csv",
        mime="text/csv"
    )

else:
    st.info("👈 Enter your Stripe keys in the sidebar and click **Run Audit**.")
    st.markdown("""
    **How it works:**
    1. Connects to Salesforce automatically using saved credentials
    2. Pulls all active credit card accounts (ARR > 0)
    3. Checks each against your US and Non-US Stripe accounts
    4. Flags anyone canceled or past due in Stripe but active in Salesforce
    """)
