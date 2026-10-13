"""Core CRR clawback math + file parsers (no Dash code in here).

Allocation logic (mirrors the `clawbacks_allocated` workbook):

  1. CAISO's Post-Market CRR Revenue Adjustments Details report has one row per
     CRR ID x binding constraint x hour. The "Offset Revenue ($)" on that row is
     the offset for the *whole constraint-hour* and is repeated on every CRR ID
     that touches the same constraint in the same hour.
  2. Offset revenue can only cost money, so positive offsets are floored to 0:
         Neg Offset = MIN(Offset Revenue, 0)
  3. A "constraint-hour" is the group
         (Start Date, Product Type, Transmission Constraint ID, Constraint Case)
     Constraint-Hour Total MW = sum of CRR Award MW of every CRR in the group.
  4. Each CRR is charged its MW share of that constraint-hour's negative offset:
         CRR MW Share     = CRR Award MW / Constraint-Hour Total MW
         Allocated Offset = Neg Offset x CRR MW Share
  5. Per CRR ID:
         Net Revenue = Notional + Allocated Offset + CRR Clawback + Circular Scheduling
         Clawback %  = |Allocated Offset| / Notional Revenue
"""

from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Column names
# --------------------------------------------------------------------------- #

GROUP_KEYS = [
    "Start Date",
    "Product Type",
    "Transmission Constraint ID",
    "Constraint Case",
]

REV_REQUIRED = [
    "Start Date",
    "End Date",
    "Product Type",
    "Transmission Constraint ID",
    "Constraint Case",
    "CRR ID",
    "Notional Revenue ($)",
    "Offset Revenue ($)",
    "CRR Award MW",
]
REV_OPTIONAL_NUMERIC = [
    "CRR Clawback Revenue ($)",
    "Circular Scheduling Revenue ($)",
    "Derate Factor",
]
REV_OPTIONAL_TEXT = ["SC ID", "Hedge Type", "CRR Type"]

RESULTS_REQUIRED = ["CRR ID", "Source", "Sink"]
RESULTS_OPTIONAL = [
    "Awarded MW",
    "TOU",
    "Bid Type",
    "Portfolio",
    "Bid Description",
    "Start Date",
    "End Date",
    "CRR Type",
    "Hedge Type",
    "ClearingPrice",
]

UNMAPPED = "(not in CRR results)"


def _norm(name: str) -> str:
    """Loose header key: lower-case, alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _rename_loose(df: pd.DataFrame, wanted: List[str]) -> pd.DataFrame:
    """Rename columns that match `wanted` ignoring case/spacing/punctuation."""
    lookup = {_norm(w): w for w in wanted}
    if "ClearingPrice" in wanted:
        lookup.setdefault(_norm("Clearing Price"), "ClearingPrice")
    mapping = {}
    for c in df.columns:
        key = _norm(c)
        if key in lookup and lookup[key] not in mapping.values():
            mapping[c] = lookup[key]
    return df.rename(columns=mapping)


def normalize_crr_id(series: pd.Series) -> pd.Series:
    """CRR IDs as clean strings ('125635136'), never '125635136.0'."""
    num = pd.to_numeric(series, errors="coerce")
    out = series.astype(str).str.strip()
    mask = num.notna()
    out[mask] = num[mask].round(0).astype("int64").astype(str)
    out[~mask] = np.nan
    return out


# --------------------------------------------------------------------------- #
# Fast .ods reader (pandas' odf engine takes ~10 s on an 8k-row CAISO export)
# --------------------------------------------------------------------------- #

_NS = {
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
}
_T = "{%s}" % _NS["table"]
_O = "{%s}" % _NS["office"]
_X = "{%s}" % _NS["text"]
_MAX_REPEAT = 200  # cap on table:number-*-repeated padding


def _ods_cell_value(cell):
    vtype = cell.get(_O + "value-type")
    if vtype in ("float", "percentage", "currency"):
        return float(cell.get(_O + "value"))
    if vtype == "date":
        return cell.get(_O + "date-value")
    if vtype == "boolean":
        return cell.get(_O + "boolean-value")
    sval = cell.get(_O + "string-value")
    if sval is not None:
        return sval
    parts = []
    for p in cell.iter(_X + "p"):
        chunk = []
        if p.text:
            chunk.append(p.text)
        for child in p:
            if child.tag == _X + "s":
                chunk.append(" " * int(child.get(_X + "c", "1")))
            elif child.text:
                chunk.append(child.text)
            if child.tail:
                chunk.append(child.tail)
        parts.append("".join(chunk))
    return "\n".join(parts) if parts else None


def read_ods_first_sheet(buffer: bytes) -> pd.DataFrame:
    """Read the first sheet of an .ods file into a header-less DataFrame."""
    with zipfile.ZipFile(io.BytesIO(buffer)) as z:
        xml_bytes = z.read("content.xml")
    root = ET.fromstring(xml_bytes)
    table = root.find(".//table:table", _NS)
    if table is None:
        raise ValueError("No sheet found in .ods file.")

    rows = []
    for row in table.iter(_T + "table-row"):
        values = []
        for cell in row:
            if cell.tag not in (_T + "table-cell", _T + "covered-table-cell"):
                continue
            rep = min(int(cell.get(_T + "number-columns-repeated", "1")), _MAX_REPEAT)
            values.extend([_ods_cell_value(cell)] * rep)
        while values and values[-1] in (None, ""):
            values.pop()
        rep_rows = min(int(row.get(_T + "number-rows-repeated", "1")), _MAX_REPEAT)
        if not values:
            continue  # skip blank (often massively repeated) padding rows
        rows.extend([values] * rep_rows)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Generic file loader
# --------------------------------------------------------------------------- #

def _read_raw(buffer: bytes, filename: str) -> pd.DataFrame:
    """Header-less DataFrame from csv / xlsx / xlsm / xls / ods."""
    ext = (filename or "").lower().rsplit(".", 1)[-1]
    if ext in ("csv", "txt"):
        return pd.read_csv(io.BytesIO(buffer), header=None, dtype=str,
                           skip_blank_lines=True, encoding_errors="replace")
    if ext == "ods":
        try:
            return read_ods_first_sheet(buffer)
        except Exception:  # noqa: BLE001 — fall back to pandas' reader
            return pd.read_excel(io.BytesIO(buffer), engine="odf", header=None)
    engine = "xlrd" if ext == "xls" else "openpyxl"
    return pd.read_excel(io.BytesIO(buffer), engine=engine, header=None)


def _promote_header(raw: pd.DataFrame, must_have: List[str]) -> pd.DataFrame:
    """Find the header row (within the first 30 rows) and promote it."""
    targets = {_norm(m) for m in must_have}
    for i in range(min(30, len(raw))):
        row_keys = {_norm(v) for v in raw.iloc[i].tolist() if pd.notna(v)}
        if targets.issubset(row_keys):
            df = raw.iloc[i + 1:].copy()
            df.columns = [str(c).strip() if pd.notna(c) else f"col_{j}"
                          for j, c in enumerate(raw.iloc[i].tolist())]
            return df.reset_index(drop=True)
    raise ValueError(
        "Could not find a header row containing: " + ", ".join(must_have)
    )


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #

def parse_crr_results(files: List[Tuple[bytes, str]]) -> Tuple[pd.DataFrame, List[str]]:
    """Parse one or more CRR Results files (monthly + annual) into a path map.

    Returns (path_map, warnings). path_map has one row per CRR ID.
    """
    frames, warnings = [], []
    for buffer, name in files:
        raw = _read_raw(buffer, name)
        df = _promote_header(raw, RESULTS_REQUIRED)
        df = _rename_loose(df, RESULTS_REQUIRED + RESULTS_OPTIONAL)
        df["CRR ID"] = normalize_crr_id(df["CRR ID"])
        df = df.dropna(subset=["CRR ID"])
        df["Results File"] = name
        frames.append(df)

    if not frames:
        return pd.DataFrame(), warnings

    df = pd.concat(frames, ignore_index=True)
    for col in RESULTS_OPTIONAL:
        if col not in df.columns:
            df[col] = np.nan
    df["Awarded MW"] = pd.to_numeric(df["Awarded MW"], errors="coerce")
    for col in ("Source", "Sink", "TOU", "Bid Type", "Portfolio",
                "Bid Description", "CRR Type", "Hedge Type"):
        df[col] = df[col].astype("string").str.strip()

    # A CRR ID can appear more than once (e.g. an annual CRR you later SELL in
    # the monthly auction shows the original ID on the SELL row). Prefer BUY
    # rows with awarded MW, then anything else, for the path lookup.
    df["_rank"] = 0
    df.loc[df["Bid Type"].str.upper().fillna("") != "BUY", "_rank"] += 1
    df.loc[~(df["Awarded MW"].fillna(0) > 0), "_rank"] += 2
    df = df.sort_values(["CRR ID", "_rank"])

    dup_paths = (
        df.groupby("CRR ID")[["Source", "Sink"]]
        .nunique()
        .query("Source > 1 or Sink > 1")
    )
    if not dup_paths.empty:
        warnings.append(
            "CRR IDs with conflicting paths across files (first one used): "
            + ", ".join(dup_paths.index[:10])
        )

    path_map = (
        df.drop_duplicates("CRR ID", keep="first")
        .drop(columns="_rank")
        .loc[:, ["CRR ID", "Source", "Sink", "Portfolio", "Bid Description",
                 "TOU", "Bid Type", "Awarded MW", "Start Date", "End Date",
                 "Results File"]]
        .rename(columns={"Start Date": "CRR Start", "End Date": "CRR End",
                         "Awarded MW": "Results Awarded MW"})
        .reset_index(drop=True)
    )
    return path_map, warnings


def parse_revenue_adjustments(files: List[Tuple[bytes, str]]) -> pd.DataFrame:
    """Parse one or more CAISO Post-Market CRR Revenue Adjustments Details files."""
    frames = []
    for buffer, name in files:
        raw = _read_raw(buffer, name)
        df = _promote_header(raw, ["CRR ID", "Offset Revenue ($)", "Transmission Constraint ID"])
        df = _rename_loose(df, REV_REQUIRED + REV_OPTIONAL_NUMERIC + REV_OPTIONAL_TEXT)
        missing = [c for c in REV_REQUIRED if c not in df.columns]
        if missing:
            raise ValueError(f"{name}: missing required columns: {', '.join(missing)}")
        df["Revenue File"] = name
        frames.append(df)

    df = pd.concat(frames, ignore_index=True)
    for col in REV_OPTIONAL_NUMERIC:
        if col not in df.columns:
            df[col] = 0.0
    for col in REV_OPTIONAL_TEXT:
        if col not in df.columns:
            df[col] = ""

    df["CRR ID"] = normalize_crr_id(df["CRR ID"])
    df["Start Date"] = pd.to_datetime(df["Start Date"], errors="coerce")
    df["End Date"] = pd.to_datetime(df["End Date"], errors="coerce")
    # Drops CAISO's disclaimer footer and any blank rows.
    df = df.dropna(subset=["CRR ID", "Start Date"])

    for col in ["Notional Revenue ($)", "Offset Revenue ($)", "CRR Award MW"] + REV_OPTIONAL_NUMERIC:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    for col in ["Product Type", "Transmission Constraint ID", "Constraint Case"] + REV_OPTIONAL_TEXT:
        df[col] = df[col].fillna("").astype(str).str.strip()

    # Same report uploaded twice (or overlapping date ranges) -> keep one copy.
    dedupe_cols = GROUP_KEYS + ["CRR ID"]
    df = df.drop_duplicates(subset=dedupe_cols, keep="last")
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Allocation
# --------------------------------------------------------------------------- #

def allocate(rev: pd.DataFrame, path_map: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Row-level allocation of negative offset revenue by MW share."""
    df = rev.copy()
    df["Neg Offset Only ($)"] = df["Offset Revenue ($)"].clip(upper=0.0)
    df["Constraint-Hour Total MW"] = df.groupby(GROUP_KEYS, dropna=False)[
        "CRR Award MW"
    ].transform("sum")
    df["CRRs on Constraint-Hour"] = df.groupby(GROUP_KEYS, dropna=False)[
        "CRR ID"
    ].transform("nunique")
    df["CRR MW Share"] = np.where(
        df["Constraint-Hour Total MW"] != 0,
        df["CRR Award MW"] / df["Constraint-Hour Total MW"],
        0.0,
    )
    df["Allocated Offset ($)"] = df["Neg Offset Only ($)"] * df["CRR MW Share"]
    df["Operating Date"] = df["Start Date"].dt.normalize()

    if path_map is not None and not path_map.empty:
        df = df.merge(path_map[["CRR ID", "Source", "Sink", "Portfolio", "TOU"]],
                      on="CRR ID", how="left")
    else:
        for c in ("Source", "Sink", "Portfolio", "TOU"):
            df[c] = np.nan
    for c in ("Source", "Sink"):
        df[c] = df[c].astype("object").where(df[c].notna(), UNMAPPED)
    df["Portfolio"] = df["Portfolio"].astype("object").where(df["Portfolio"].notna(), "")
    df["TOU"] = df["TOU"].astype("object").where(df["TOU"].notna(), "")
    df["Path"] = df["Source"].astype(str) + " → " + df["Sink"].astype(str)
    return df


def summarize_by_crr(alloc: pd.DataFrame) -> pd.DataFrame:
    """One row per CRR ID — the 'CRR Summary' sheet."""
    if alloc is None or alloc.empty:
        return pd.DataFrame()
    g = alloc.groupby("CRR ID", sort=False)
    out = g.agg(
        **{
            "Source": ("Source", "first"),
            "Sink": ("Sink", "first"),
            "Portfolio": ("Portfolio", "first"),
            "TOU": ("TOU", "first"),
            "Award MW": ("CRR Award MW", "first"),
            "Notional Revenue ($)": ("Notional Revenue ($)", "sum"),
            "Allocated Offset ($)": ("Allocated Offset ($)", "sum"),
            "CRR Clawback Revenue ($)": ("CRR Clawback Revenue ($)", "sum"),
            "Circular Scheduling Revenue ($)": ("Circular Scheduling Revenue ($)", "sum"),
            "Naive Offset Pull ($)": ("Neg Offset Only ($)", "sum"),
            "Rows": ("CRR ID", "size"),
        }
    ).reset_index()
    out["Net Revenue ($)"] = (
        out["Notional Revenue ($)"]
        + out["Allocated Offset ($)"]
        + out["CRR Clawback Revenue ($)"]
        + out["Circular Scheduling Revenue ($)"]
    )
    out["Clawback %"] = np.where(
        out["Notional Revenue ($)"] != 0,
        out["Allocated Offset ($)"].abs() / out["Notional Revenue ($)"],
        np.nan,
    )
    out["Offset Overstatement ($)"] = out["Naive Offset Pull ($)"] - out["Allocated Offset ($)"]
    cols = [
        "Source", "Sink", "CRR ID", "Portfolio", "TOU", "Award MW",
        "Net Revenue ($)", "Clawback %", "Notional Revenue ($)",
        "Allocated Offset ($)", "CRR Clawback Revenue ($)",
        "Circular Scheduling Revenue ($)", "Naive Offset Pull ($)",
        "Offset Overstatement ($)", "Rows",
    ]
    return out[cols].sort_values("CRR ID").reset_index(drop=True)


def summary_total_row(summary: pd.DataFrame) -> dict:
    """The 'Total' row at the bottom of the CRR Summary."""
    tot = {c: "" for c in summary.columns}
    tot["CRR ID"] = "Total"
    for c in summary.columns:
        if c.endswith("($)") or c == "Award MW":
            tot[c] = float(summary[c].sum())
    tot["Rows"] = int(summary["Rows"].sum())
    notional = summary["Notional Revenue ($)"].sum()
    tot["Clawback %"] = (
        abs(summary["Allocated Offset ($)"].sum()) / notional if notional else np.nan
    )
    return tot


def reconciliation(alloc: pd.DataFrame) -> Tuple[float, float]:
    """(allocated total, sum of negative offsets counted once per constraint-hour).

    Constraint-hours whose CRRs all have 0 MW allocate nothing, so the check
    uses only groups with non-zero total MW.
    """
    allocated = float(alloc["Allocated Offset ($)"].sum())
    grp = alloc[alloc["Constraint-Hour Total MW"] != 0]
    once = float(
        (grp["Neg Offset Only ($)"] / grp["CRRs on Constraint-Hour"]).sum()
    )
    return allocated, once


def constraint_summary(alloc: pd.DataFrame) -> pd.DataFrame:
    """Allocated offset by binding constraint (all CRRs)."""
    if alloc is None or alloc.empty:
        return pd.DataFrame()
    out = (
        alloc.groupby(["Transmission Constraint ID", "Constraint Case"])
        .agg(
            **{
                "Allocated Offset ($)": ("Allocated Offset ($)", "sum"),
                "Binding Hours": ("Start Date", "nunique"),
                "CRRs Affected": ("CRR ID", "nunique"),
                "Notional Revenue ($)": ("Notional Revenue ($)", "sum"),
            }
        )
        .reset_index()
        .sort_values("Allocated Offset ($)")
    )
    return out[out["Allocated Offset ($)"] < 0].reset_index(drop=True)
