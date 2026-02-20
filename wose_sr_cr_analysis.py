"""
Script to produce a csv with both record UTs and their Cited References from WoS Expanded API.  
This script uses the parameter optionView=SR to return WoS Short Records.
"""
from dotenv import load_dotenv
import os
import csv
import sys
import argparse
import wosesrclient_robust
import datetime
import re
import pandas as pd
from wosereferencesclient_robust import get_all_records as get_cited_refs
from wosesrclient_robust import InvalidWoSQueryError
from collections import Counter, defaultdict

#Timing returns
start_wall = datetime.datetime.now()

# Load .env file
load_dotenv()

# API key loaded from environment (.env supported via load_dotenv)
apikey = os.getenv('EXPANDED_APIKEY')


params = {'databaseId': 'WOS',
          'usrQuery': 'UT=WOS:000222471500001',
          'firstRecord': 1,
          'count': 50,
          'optionView': 'SR'
          }

# Script options (non-API parameters)
INCLUDE_ZERO_REF_UIDS = False  # default: do not append zero-ref UIDs to the CSV
MAKE_EXCEL = True             # default: create the Excel analysis workbook



def parse_args(args):
    parser = argparse.ArgumentParser()
    parser.add_argument('-q', '--query', help="Query to send to WoS API. "
                        "e.g. TS=CRISPR")
    parser.add_argument('-k', '--key', help="WoS Starter API token.")

    parser.add_argument(
        '--include-zero-ref-uids',
        action='store_true',
        help='Include Source UIDs that have zero cited references as blank rows in the output CSV (default: off).'
    )
    parser.add_argument(
        '--no-excel',
        action='store_true',
        help='Skip creating the Excel analysis workbook (default: Excel is created).'
    )

    return parser.parse_args(args)


def safeget(dct, *keys):
    for key in keys:
        try:
            dct = dct[key]
        except KeyError:
            return None
        except TypeError:
            pass
    if dct:
        try:
            if isinstance(dct, list):
                if isinstance(dct[0], dict):
                    try:
                        return dct[0][key]
                    except:
                        return dct[0]
                return dct[0]
            else:
                return dct
        except:
            return None
            
def parse_uid(rec):
    return rec.get("UID", "")
    
def extract_rec_list(data):
    """
    Normalize wosereferencesclient.get_all_records(...) output into a flat list of
    cited-reference dicts. Handles all of these shapes:

      1) {'Data': [ {UID:..., CitedWork:..., ...}, {...}, ... ], 'QueryResult': {...}}
      2) {'Data': {'Records': {'records': {'REC': [...]}}}, 'QueryResult': {...}}
      3) A list of pages, each either a list of ref dicts or a dict with Records/REC
      4) Already a flat list of ref dicts

    A dict counts as a reference if it has at least one of:
      UID / Year / CitedWork / CitedTitle / DOI / CitedAuthor
    """
    KEY_HINTS = {"UID", "Year", "CitedWork", "CitedTitle", "DOI", "CitedAuthor"}

    def looks_like_ref(d):
        return isinstance(d, dict) and any(k in d for k in KEY_HINTS)

    # Case 4: already a flat list → keep items that look like refs
    if isinstance(data, list):
        out = []
        for item in data:
            if looks_like_ref(item):
                out.append(item)
            elif isinstance(item, dict):
                # Page object: try Records/records/REC
                recs = item.get('Records', {}).get('records', {}).get('REC', [])
                if isinstance(recs, dict):
                    recs = [recs]
                # Or nested Data list
                if not recs and 'Data' in item and isinstance(item['Data'], list):
                    recs = [x for x in item['Data'] if looks_like_ref(x)]
                out.extend([x for x in recs if looks_like_ref(x)])
        return out

    # Dict cases
    if isinstance(data, dict):
        # Unwrap Data if present
        payload = data.get('Data', data)

        # If Data is a list of ref dicts
        if isinstance(payload, list):
            return [x for x in payload if looks_like_ref(x)]

        # Else Records/records/REC path
        recs = payload.get('Records', {}).get('records', {}).get('REC', [])
        if isinstance(recs, dict):
            recs = [recs]
        return [x for x in recs if looks_like_ref(x)]

    # Fallback
    return []

# mapping helpers (CSV Column A = variant, Column B = canonical) ---
def _norm_key_basic(s: str) -> str:
    """
    Case-insensitive key used for:
      - CSV Column A lookups
      - Bucketing titles into families

    We only normalize minimally:
      - lowercase/casefold
      - strip periods in abbreviations (e.g., 'Phys. Rev.' -> 'phys rev')
      - collapse whitespace
    """
    s = str(s).strip().casefold()
    s = s.replace('.', '')           # remove periods used in abbreviations
    s = " ".join(s.split())          # collapse internal whitespace
    return s

def load_variant_map(csv_filename: str = "JCR 2025.csv") -> dict:
    """
    Reads a two-column CSV (A=variant, B=canonical). Header allowed.
    Returns dict normalized_variant -> canonical_display (Column B as-is).
    Looks for the file in the same folder as this script.
    """
    path = os.path.join(os.path.dirname(__file__), csv_filename)
    if not os.path.exists(path):
        return {}
    try:
        df_map = pd.read_csv(path, dtype=str, header=0)
    except Exception:
        df_map = pd.read_csv(path, dtype=str, header=None)
    if df_map.shape[1] < 2:
        return {}
    A = df_map.iloc[:, 0].astype(str).str.strip()
    B = df_map.iloc[:, 1].astype(str).str.strip()
    mapping = {}
    for a, b in zip(A, B):
        if a and b and a.lower() != "nan" and b.lower() != "nan":
            mapping[_norm_key_basic(a)] = b
    return mapping

if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    if args.key:
        apikey = args.key
    else:
        print("Using API key from .env file")
    if args.query:
        params['usrQuery'] = args.query
    else:
        print("Using query set in parameters")
    print('Using query: {}'.format(params['usrQuery']))

    # Script option overrides (can be set in the params section above and overridden via CLI flags)
    include_zero_ref_uids = bool(INCLUDE_ZERO_REF_UIDS or args.include_zero_ref_uids)
    make_excel = bool(MAKE_EXCEL and (not args.no_excel))


    if not apikey:
        raise ValueError('No API key was supplied')
    
    # 1) Initial search: get short records and collect UIDs
    try:
        search_records = wosesrclient_robust.get_all_records(
            apikey, params, params['firstRecord'], params['count']
        )
    except InvalidWoSQueryError as e:
        print(e)
        sys.exit(1)

    if not search_records:
        print("*** No records returned for this query. ***")
        sys.exit(0)

    initial_count = len(search_records)
    print(f"Retrieved {len(search_records)} records from query.")
    
    uids = [parse_uid(r) for r in search_records if isinstance(r, dict) and parse_uid(r)]
    uids = list(dict.fromkeys(uids))  # order-preserving dedupe
    if not uids:
        print("*** No UIDs found. ***")
        sys.exit(0)

    # 2) Prepare CSV filename from the query
    raw_query = params['usrQuery']
    clean_query = re.sub(r'[^a-zA-Z0-9 ]', '', raw_query)
    safe_query = clean_query.strip().replace(' ', '_')[:40] or "query"
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"WOS_CitedRefs_{safe_query}_{timestamp}.csv"

    # 3) Open CSV and write header
    # utf-8-sig helps Excel detect UTF-8
    with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
        header = [
            'Source UID', 'Cited Reference UID', 'Cited Author',
            'Year', 'Volume', 'Page', 'Cited Work', 'Cited Title', 'DOI'
        ]
        writer.writerow(header)

        total_cited_refs = 0
        zero_ref_uids = []  # <-- collect UIDs with zero cited references

        # 4) For each UID, fetch its cited references and append rows
        for uid in uids:
            cited_params = {'databaseId': 'WOS', 'uniqueId': uid, 'firstRecord': 1, 'count': 50}
            print(f"Retrieving cited references for {uid}")
            try:
                cited_blob = get_cited_refs(apikey, cited_params)
            except Exception as e:
                print(f"  Skipping {uid} due to error: {e}")
                continue

            recs = extract_rec_list(cited_blob)
            if not recs:
                # No cited references for this UID — record it for later appending
                zero_ref_uids.append(uid)
                continue

            total_cited_refs += len(recs)
            
            for cref in recs:
                # Build row; use safeget/cleanups where helpful
                cited_uid   = (cref.get('UID') or '').strip()
                cited_auth  = (cref.get('CitedAuthor') or '').strip().replace(',', '')  # remove comma in names (e.g., "Cho, JP" -> "Cho JP")
                cited_year  = (cref.get('Year') or '').strip()
                cited_vol   = (cref.get('Volume') or '').strip()
                cited_page  = (cref.get('Page') or '').strip()
                cited_work  = (cref.get('CitedWork') or '').strip()
                cited_title = (cref.get('CitedTitle') or '').strip()
                cited_doi   = re.sub(r"^https?://(dx\.)?doi\.org/", "", (cref.get('DOI') or '').strip(), flags=re.IGNORECASE)

                writer.writerow([
                    uid, cited_uid, cited_auth,
                    cited_year, cited_vol, cited_page, cited_work, cited_title, cited_doi
                ])
        # 5) Optionally append zero-reference UIDs at the end (one row per UID), aligned to header
        if include_zero_ref_uids and zero_ref_uids:
            # Dedupe in case a UID was encountered more than once (safety)
            zero_ref_uids = list(dict.fromkeys(zero_ref_uids))
            for zuid in zero_ref_uids:
                writer.writerow([zuid, '', '', '', '', '', '', '', ''])
                

    print(f"Retrieved {initial_count} results in total.")
    print(f"Retrieved {total_cited_refs} cited references in total.")
    print(f"CSV written to {filename}")

if make_excel:
    # === Post-process: Excel pivot-style summaries ===
    try:
        # Read the CSV we just wrote
        df = pd.read_csv(filename, dtype=str)

        CITED_WORK_COL = "Cited Work"   # from your CSV header
        RAW_YEAR_COL   = "Year"         # raw year in your CSV; output uses "Cited Year"

        # ----- Sheet 1: Cited Work analysis -----
        # Build counts with explicit labels so columns are ["Cited Work", "Count"]
        cw_series = (
            df.get(CITED_WORK_COL, pd.Series(dtype=str))
              .fillna("")
              .astype(str)
              .str.strip()
        )
        cw_series = cw_series[cw_series != ""]

        # load optional variant→canonical map from sidecar CSV (Column A -> Column B) ---
        VARIANT_MAP = load_variant_map("JCR 2025.csv")  # same folder; safe if missing

        # base normalizer for keys (case-insensitive + collapse whitespace) ---
        def norm_key(s: str) -> str:
            return _norm_key_basic(s)

        # bucket into families, applying mapping when available ---
        families = defaultdict(lambda: {"display": None, "variants": Counter()})
        for val in cw_series:
            original = val
            nk = norm_key(original)
            mapped_display = VARIANT_MAP.get(nk)
            if mapped_display:
                family_key = norm_key(mapped_display)   # group by normalized canonical (Column B)
                families[family_key]["display"] = mapped_display  # keep Column B for display
            else:
                family_key = nk  # no explicit mapping; own family
            families[family_key]["variants"][original] += 1

        # Choose a canonical display name, build rows and human-readable merge lines
        rows = []
        merged_lines = []  # for Search Summary: "Canonical merged with a, b, c"
        for fkey, data in families.items():
            variants = data["variants"]
            total_count = sum(variants.values())

            # Display preference: CSV's Column B if provided, else most frequent original (ties → alpha)
            if data["display"]:
                canonical = data["display"]
            else:
                most_common_count = max(variants.values())
                top_variants = [v for v, c in variants.items() if c == most_common_count]
                canonical = sorted(top_variants)[0]

            rows.append([canonical, total_count])

            distinct_variants = sorted(variants.keys())
            others = [v for v in distinct_variants if v != canonical]
            if others:
                if len(others) == 1:
                    merged_lines.append(f"{canonical} merged with {others[0]}")
                else:
                    merged_lines.append(f"{canonical} merged with " + " and ".join(others))

        # Build the merged/normalized counts table
        cited_work_counts = (
            pd.DataFrame(rows, columns=["Cited Work", "Count"])
              .sort_values(["Count", "Cited Work"], ascending=[False, True])
              .reset_index(drop=True)
        )

        # Percent of total (as float; we'll format in Excel)
        if not cited_work_counts.empty:
            total_cw = int(pd.to_numeric(cited_work_counts["Count"], errors="coerce").fillna(0).sum())
            cited_work_counts["Count"] = pd.to_numeric(cited_work_counts["Count"], errors="coerce").fillna(0).astype(int)
            cited_work_counts["Percent"] = cited_work_counts["Count"] / total_cw if total_cw > 0 else 0.0
        else:
            cited_work_counts["Percent"] = pd.Series([], dtype=float)

        # === Bradford zones (unchanged logic) ===
        if not cited_work_counts.empty:
            cited_work_counts["Count"] = pd.to_numeric(cited_work_counts["Count"], errors="coerce").fillna(0).astype(int)
            _total = int(cited_work_counts["Count"].sum())
            cited_work_counts["Cumulative Count"] = cited_work_counts["Count"].cumsum()
            cited_work_counts["Cumulative %"] = (
                cited_work_counts["Cumulative Count"] / _total if _total > 0 else 0.0
            )
            cited_work_counts["Bradford Zone"] = cited_work_counts["Cumulative %"].apply(
                lambda p: "Zone 1 (Core)" if p <= (1/3) else ("Zone 2" if p <= (2/3) else "Zone 3")
            )
        # ----- Sheet 2: Cited Year analysis -----
        # Extract 1–4 digit year from the raw year col, coerce to int, then count
        cy_series = (
            df.get(RAW_YEAR_COL, pd.Series(dtype=str))
              .fillna("")
              .astype(str)
              .str.extract(r"(\d{1,4})")[0]
        )
        cy_series = pd.to_numeric(cy_series, errors="coerce").dropna().astype(int)

        cited_year_counts = (
            cy_series.value_counts()
                     .rename_axis("Cited Year")
                     .reset_index(name="Count")
                     .sort_values("Cited Year", ascending=False)
                     .reset_index(drop=True)
        )

        if not cited_year_counts.empty:
            total_cy = int(pd.to_numeric(cited_year_counts["Count"], errors="coerce").fillna(0).sum())
            cited_year_counts["Count"] = pd.to_numeric(cited_year_counts["Count"], errors="coerce").fillna(0).astype(int)
            cited_year_counts["Percent"] = cited_year_counts["Count"] / total_cy if total_cy > 0 else 0.0
        else:
            cited_year_counts["Percent"] = pd.Series([], dtype=float)

        # build Top X% coverage summary for year analysis ===
        def build_top_ranges(df_counts, thresholds=(0.50, 0.60, 0.75, 0.90, 0.95)):
            if df_counts.empty:
                return pd.DataFrame(columns=["Percent Cutoff", "Coverage"])
            df_ = (
                df_counts[["Cited Year", "Count"]]
                .copy()
                .sort_values("Cited Year", ascending=False)
                .reset_index(drop=True)
            )
            df_["Count"] = pd.to_numeric(df_["Count"], errors="coerce").fillna(0).astype(int)
            total = int(df_["Count"].sum())
            if total == 0:
                return pd.DataFrame(columns=["Percent Cutoff", "Coverage"])
            df_["cum_pct"] = df_["Count"].cumsum() / total
            rows_ = []
            for t in thresholds:
                idx = df_.index[df_["cum_pct"] >= t]
                if len(idx) == 0:
                    continue
                cutoff_year = int(df_.loc[idx[0], "Cited Year"])
                rows_.append([f"Top {int(t*100)}%", f"{cutoff_year}–Present"])
            return pd.DataFrame(rows_, columns=["Percent Cutoff", "Coverage"])

        top_ranges_df = build_top_ranges(cited_year_counts)

        # ----- Write Excel -----
        excel_filename = f"CR_Pivot_Analysis_{timestamp}.xlsx"

        try:
            import xlsxwriter
            excel_engine = "xlsxwriter"
        except ImportError:
            excel_engine = "openpyxl"

        with pd.ExcelWriter(excel_filename, engine=excel_engine) as writer:
            cited_work_counts.to_excel(writer, index=False, sheet_name="Cited Work analysis")

            
            # Cited Year sheet: write summary first, then the full table below it
            startrow_year_summary = 0
            top_ranges_df.to_excel(
                writer,
                index=False,
                sheet_name="Cited Year analysis",
                startrow=startrow_year_summary
            )

            # Leave a blank row between summary and table
            table_startrow = startrow_year_summary + len(top_ranges_df) + 2

            cited_year_counts.to_excel(
                writer,
                index=False,
                sheet_name="Cited Year analysis",
                startrow=table_startrow
            )
            
            
            df.to_excel(writer, index=False, sheet_name="Raw Data")

            # Search Summary sheet ---
            original_query = params.get('usrQuery', '')
            run_timestamp = timestamp
            total_records_retrieved = initial_count
            total_cited_references = total_cited_refs
            records_with_zero_cited_refs = len(zero_ref_uids) if 'zero_ref_uids' in locals() else 0

            summary_rows = [
                ["Original Query", original_query],              # Row 1
                ["Local Time", run_timestamp],                   # Row 2
                ["Total Records Retrieved", total_records_retrieved],  # Row 3
                ["Total Cited References", total_cited_references],    # Row 4
                ["Records with Zero Cited References", records_with_zero_cited_refs],  # Row 5
                ["", ""],  # Row 6: intentional blank separator so merges start at Row 7
            ]

            # merged_lines was built in the Cited Work section above
            # Each becomes one row, starting row 7
            for line in merged_lines:
                summary_rows.append([line, ""])

            summary_df = pd.DataFrame(summary_rows)

            summary_df.to_excel(writer, index=False, header=False, sheet_name="Search Summary")

            # --- Formatting (xlsxwriter only) ---
            try:
                wb = writer.book
                pct_fmt = None
                try:
                    pct_fmt = wb.add_format({"num_format": "0.00%"})
                except Exception:
                    pct_fmt = None

                # Optional: bold header for the Top X% summary
                try:
                    ws_year = writer.sheets["Cited Year analysis"]
                    header_fmt = wb.add_format({"bold": True})
                    ws_year.set_row(startrow_year_summary, None, header_fmt)
                except Exception:
                    pass

                for sheet_name, df_tbl in [
                    ("Cited Work analysis", cited_work_counts),
                    # Keep using the main table for width/format decisions
                    ("Cited Year analysis", cited_year_counts),
                    ("Cited Year analysis", top_ranges_df),
                    ("Raw Data", df),
                    ("Search Summary", summary_df),
                ]:
                    ws = writer.sheets[sheet_name]
                    if not df_tbl.empty:
                        # Freeze rows 1–8 for "Cited Year analysis"; row 1 for others
                        try:
                            # xlsxwriter (method takes zero-based row/col)
                            ws.freeze_panes(8, 0) if sheet_name == "Cited Year analysis" else ws.freeze_panes(1, 0)
                        except Exception:
                            # openpyxl fallback (set first *unfrozen* cell)
                            ws.freeze_panes = "A9" if sheet_name == "Cited Year analysis" else "A2"

                        # Column widths + % format for "Percent" columns
                        for i, col in enumerate(df_tbl.columns):
                            # Sample up to 1000 values for width calc
                            sample_vals = df_tbl[col].head(1000)
                            max_len = max([len(str(col))] + [len(str(v)) for v in sample_vals])
                            width = min(max(12, max_len + 2), 60)

                            fmt_to_use = None
                            if sheet_name != "Raw Data" and pct_fmt is not None and col in ("Percent", "Cumulative %"):
                                fmt_to_use = pct_fmt

                            try:
                                ws.set_column(i, i, width, fmt_to_use)
                            except Exception:
                                pass
            except Exception:
                pass

        print(f"Excel analysis written to {excel_filename}")

    except Exception as e:
        print(f"Skipped Excel analysis due to error: {e}")
else:
    print("Skipping Excel analysis (--no-excel set).")

#Timing requests
end_wall = datetime.datetime.now()

elapsed_minutes = (end_wall - start_wall).total_seconds() / 60
print(f"Started : {start_wall:%Y-%m-%d %H:%M:%S}")
print(f"Finished: {end_wall:%Y-%m-%d %H:%M:%S}")
print(f"Elapsed : {elapsed_minutes:.2f} minutes")
