import streamlit as st
import pandas as pd
import io

# Set Streamlit page configuration
st.set_page_config(page_title="Carrier Cost Allocation Tool", page_icon="📦", layout="wide")

# ==============================================================================
# 1. CARRIER REGISTRY CONFIGURATION
# ==============================================================================
CARRIER_REGISTRY = {
    "DHL": {
        "bq_carrier_codes": ["DHL", "DHD"],
        "encoding": "utf-8",
        "has_header": True,
        "delimiter": ",",
        "field_map": {
            "shipment_id": "Sendungsnummer",
            "dest_country": "Empfänger - Land",
            "handover_date": "Einlieferdatum",
            "description": "Bezeichnung",
            "amount_net": "Nettobetrag"
        },
        "constants": {},
        "numeric_format": "german"  # Handles comma-to-dot replacement
    }
}

CHARGE_CATEGORY_RULES = {
    "surcharge": ["zuschlag", "fuel", "fsc", "energy", "maut"],
    "return": ["rücksend", "retour", "rse"],
    "adjustment": ["rabatt", "codierentgelt", "correction", "accrual"],
    "tax": ["steuer", "tax", "19.000%"],
    "freight": ["paket", "frt", "shipping", "base"]
}

# ==============================================================================
# 2. PROCESSING PIPELINE HELPER FUNCTIONS
# ==============================================================================
def parse_net_amount(series, num_format):
    """Normalizes localized numeric string types into clean floats safely."""
    clean_series = series.astype(str).str.strip()
    if num_format == "german":
        clean_series = clean_series.str.replace('.', '', regex=False)
        clean_series = clean_series.str.replace(',', '.', regex=False)
    return pd.to_numeric(clean_series, errors='coerce').fillna(0.0)

def categorize_charge_description(desc):
    """Classifies charge lines into canonical categories using text keywords."""
    desc_lower = str(desc).lower()
    for category, keywords in CHARGE_CATEGORY_RULES.items():
        if any(kw in desc_lower for kw in keywords):
            return category
    return "freight"

def normalize_invoice(carrier_code, uploaded_files):
    """Ingests raw carrier files (Excel or CSV) and maps them to the canonical schema."""
    config = CARRIER_REGISTRY[carrier_code]
    f_map = config["field_map"]
    consts = config["constants"]
    
    canonical_rows = []
    
    for uploaded_file in uploaded_files:
        header_setting = 0 if config["has_header"] else None
        
        if uploaded_file.name.endswith(('.xlsx', '.xls')):
            df_raw = pd.read_excel(uploaded_file, header=header_setting, dtype=str)
        else:
            df_raw = pd.read_csv(uploaded_file, header=header_setting, encoding=config["encoding"], delimiter=config["delimiter"], dtype=str)
        
        # Clean up input headers
        df_raw.columns = df_raw.columns.astype(str).str.strip()
        
        # DEFENSIVE AUDIT 1: Verify all mapped columns exist in the uploaded file
        missing_invoice_cols = [str(col).strip() for col in f_map.values() if str(col).strip() not in df_raw.columns]
        if missing_invoice_cols:
            st.error(f"❌ **Invoice Layout Error:** The file `{uploaded_file.name}` is missing expected columns: `{missing_invoice_cols}`. "
                     f"Columns found in file were: `{list(df_raw.columns)[:10]}...` (If using Excel, ensure data starts on the very first row).")
            st.stop()
        
        df_canonical = pd.DataFrame()
        
        # Extract mapped source columns
        for canonical_field, source_field in f_map.items():
            s_field_clean = str(source_field).strip()
            if config["has_header"]:
                df_canonical[canonical_field] = df_raw[s_field_clean]
            else:
                df_canonical[canonical_field] = df_raw.iloc[:, int(s_field_clean)]
                
        # Inject constants if applicable
        for const_field, const_val in consts.items():
            df_canonical[const_field] = const_val
            
        # Standardize calculations and string types to secure aggregation groups
        df_canonical["amount_net"] = parse_net_amount(df_canonical["amount_net"], config["numeric_format"])
        df_canonical["dest_country"] = df_canonical["dest_country"].astype(str).str.strip()
        df_canonical["handover_date"] = df_canonical["handover_date"].astype(str).str.strip()
        
        if "description" not in df_canonical.columns:
            df_canonical["description"] = "Base Freight"
            
        df_canonical["charge_category"] = df_canonical["description"].apply(categorize_charge_description)
        df_canonical["carrier"] = carrier_code
        df_canonical["alloc_month"] = df_canonical["handover_date"].astype(str).str[:7]
        df_canonical["source_ref"] = f"{uploaded_file.name} | Row " + (df_raw.index + 2).astype(str)
        
        canonical_rows.append(df_canonical)
        
    return pd.concat(canonical_rows, ignore_index=True)

def run_allocation_engine(canonical_df, df_bq):
    """Splits carrier country totals across target channels using BQ parcel shares."""
    work_df = canonical_df[canonical_df["charge_category"] != "tax"].copy()
    
    # Core aggregation
    invoice_geo = work_df.groupby(["carrier", "dest_country", "alloc_month"])["amount_net"].sum().reset_index()
    df_bq_parcels = df_bq[df_bq["ship_type"].astype(str).str.strip() == 'parcel'].copy()
    
    allocated_chunks = []
    
    for (carrier, country, month), group in invoice_geo.groupby(["carrier", "dest_country", "alloc_month"]):
        net_amount = group["amount_net"].values[0]
        bq_codes = CARRIER_REGISTRY[carrier]["bq_carrier_codes"]
        
        try:
            year_val = int(str(month).split("-")[0])
            month_val = int(str(month).split("-")[1])
        except Exception:
            year_val, month_val = 2026, 6
            
        # Match against BQ extract metrics securely
        bq_subset = df_bq_parcels[
            (df_bq_parcels["carrier"].astype(str).str.strip().isin(bq_codes)) &
            (df_bq_parcels["shiptocountry"].astype(str).str.strip() == str(country).strip()) &
            (pd.to_numeric(df_bq_parcels["year"], errors='coerce') == year_val) &
            (pd.to_numeric(df_bq_parcels["month"], errors='coerce') == month_val)
        ]
        
        if bq_subset.empty:
            allocated_chunks.append(pd.DataFrame({
                "carrier": [carrier], "dest_country": [country], "alloc_month": [month],
                "channel_corr": ["UNALLOCATED / NO JOIN"], "amount_channel": [net_amount], "parcels": [0.0]
            }))
            continue
            
        channel_shares = bq_subset.groupby("channel_corr")["parcels"].sum().reset_index()
        total_parcels = channel_shares["parcels"].sum()
        
        channel_shares["carrier"] = carrier
        channel_shares["dest_country"] = country
        channel_shares["alloc_month"] = month
        channel_shares["amount_channel"] = net_amount * (channel_shares["parcels"] / total_parcels)
        
        allocated_chunks.append(channel_shares)
        
    return pd.concat(allocated_chunks, ignore_index=True)

# ==============================================================================
# 3. STREAMLIT USER INTERFACE UI
# ==============================================================================
st.title("📦 Carrier Cost Allocation Tool")
st.markdown("Automated logistics pipeline for transforming raw invoices into normalized multi-channel splits.")

st.sidebar.header("Data Ingestion Panel")
selected_carrier = st.sidebar.selectbox("Select Target Carrier Pipeline", list(CARRIER_REGISTRY.keys()))

uploaded_invoices = st.sidebar.file_uploader(
    f"Upload raw {selected_carrier} Invoice (Excel or CSV)", 
    type=["csv", "xlsx"], 
    accept_multiple_files=True
)

uploaded_bq = st.sidebar.file_uploader(
    "Upload BigQuery Parcel Extract (Excel or CSV)", 
    type=["csv", "xlsx"]
)

if st.sidebar.button("Run Cost Allocation Matrix", type="primary"):
    if not uploaded_invoices or not uploaded_bq:
        st.error("Missing Data: Please make sure both carrier invoices and the BigQuery parcel extract are uploaded.")
    else:
        with st.spinner("Executing normalization and allocation processes..."):
            try:
                # Step 1 & 2: Load and normalize invoices
                df_canonical = normalize_invoice(selected_carrier, uploaded_invoices)
                
                # Dynamic extension branching for BigQuery extract ingestion
                if uploaded_bq.name.endswith(('.xlsx', '.xls')):
                    df_bq_raw = pd.read_excel(uploaded_bq)
                else:
                    df_bq_raw = pd.read_csv(uploaded_bq)
                
                # Clean up BigQuery input headers
                df_bq_raw.columns = df_bq_raw.columns.astype(str).str.strip()
                
                # DEFENSIVE AUDIT 2: Verify BigQuery columns match engine rules
                required_bq_cols = ["ship_type", "carrier", "shiptocountry", "year", "month", "channel_corr", "parcels"]
                missing_bq_cols = [col for col in required_bq_cols if col not in df_bq_raw.columns]
                if missing_bq_cols:
                    st.error(f"❌ **BigQuery Extract Layout Error:** The file `{uploaded_bq.name}` is missing required tracking columns: `{missing_bq_cols}`. "
                             f"Columns found were: `{list(df_bq_raw.columns)}`.")
                    st.stop()
                
                # Step 3 & 4: Run allocation calculations
                df_allocated = run_allocation_engine(df_canonical, df_bq_raw)
                
                # Pipeline Audit Compliance Checks
                pre_split_total = df_canonical[df_canonical["charge_category"] != "tax"].["amount_net"].sum()
                post_split_total = df_allocated["amount_channel"].sum() if not df_allocated.empty else 0.0
                
                country_reconciliation = df_allocated.groupby("dest_country")["amount_channel"].sum().reset_index()
                invoice_geo_totals = df_canonical[df_canonical["charge_category"] != "tax"].groupby("dest_country")["amount_net"].sum().reset_index()
                merged_check = pd.merge(invoice_geo_totals, country_reconciliation, on="dest_country", how="left").fillna(0.0)
                merged_check["diff"] = (merged_check["amount_net"] - merged_check["amount_channel"]).abs()
                
                country_totals_match = merged_check["diff"].max() < 0.01
                grand_totals_match = abs(pre_split_total - post_split_total) < 0.01
                check_1_passed = country_totals_match and grand_totals_match
                
                # Display status indicators
                st.subheader("Data Pipeline Verification Status")
                col1, col2, col3 = st.columns(3)
                
                with col1:
                    st.metric("Invoice Net Total (Excl. Tax)", f"€{pre_split_total:,.2f}")
                with col2:
                    st.metric("Allocated Multi-Channel Total", f"€{post_split_total:,.2f}")
                with col3:
                    if check_1_passed:
                        st.success("✅ INTEGRITY CHECK 1: PASSED")
                    else:
                        st.error("❌ INTEGRITY CHECK 1: FAILED (Cent Leakage Detected)")
                
                # Format export layout
                output_cols = ["carrier", "dest_country", "alloc_month", "channel_corr", "parcels", "amount_channel"]
                final_display_df = df_allocated[output_cols].rename(
                    columns={
                        "channel_corr": "Channel",
                        "parcels": "BQ Parcel Volume",
                        "amount_channel": "Allocated Cost (EUR)"
                    }
                )
                
                # Output Results Tables
                st.subheader("Final Country × Channel Cost Allocation Matrix")
                st.dataframe(final_display_df.style.format({"Allocated Cost (EUR)": "€{:.2f}", "BQ Parcel Volume": "{:,.0f}"}), use_container_width=True)
                
                # Setup in-memory Excel downloader
                excel_buffer = io.BytesIO()
                with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                    final_display_df.to_excel(writer, index=False, sheet_name="Allocated Costs")
                excel_buffer.seek(0)
                
                st.download_button(
                    label="📥 Download Cost Allocation Sheet (.xlsx)",
                    data=excel_buffer,
                    file_name=f"{selected_carrier}_Channel_Allocation_Matrix.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
                
            except Exception as e:
                st.error(f"Execution Error: {str(e)}")
