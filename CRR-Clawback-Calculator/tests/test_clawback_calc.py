"""Run with:  python -m pytest tests  (from the repo root)."""

import io
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import clawback_calc as cc  # noqa: E402

HOUR = "2026-10-01 06:00:00"


def _rev_bytes(rows):
    cols = ["Start Date", "End Date", "SC ID", "Product Type",
            "Transmission Constraint ID", "Constraint Case", "CRR ID",
            "Hedge Type", "CRR Type", "Notional Revenue ($)", "Offset Revenue ($)",
            "CRR Clawback Revenue ($)", "Circular Scheduling Revenue ($)",
            "Derate Factor", "CRR Award MW"]
    df = pd.DataFrame(rows, columns=cols)
    return df.to_csv(index=False).encode()


def _row(crr, constraint, notional, offset, mw, product="EN", hour=HOUR):
    return [hour, "2026-10-01 07:00:00", "TEAX", product, constraint, "Base Case",
            crr, "OBLIGATIONS", "AUC", notional, offset, 0, 0, 1, mw]


def test_shared_constraint_is_split_by_mw():
    # Two CRRs on the same constraint-hour; CAISO repeats the -100 offset on both.
    rev = cc.parse_revenue_adjustments([(_rev_bytes([
        _row(1, "C1", 50, -100, 30),
        _row(2, "C1", 20, -100, 10),
    ]), "rev.csv")])
    s = cc.summarize_by_crr(cc.allocate(rev, None)).set_index("CRR ID")
    assert s.loc["1", "Allocated Offset ($)"] == pytest.approx(-75)
    assert s.loc["2", "Allocated Offset ($)"] == pytest.approx(-25)
    assert s.loc["1", "Naive Offset Pull ($)"] == pytest.approx(-100)
    assert s.loc["1", "Net Revenue ($)"] == pytest.approx(-25)


def test_positive_offset_counts_as_zero():
    rev = cc.parse_revenue_adjustments([(_rev_bytes([
        _row(1, "C1", 50, 40, 30),
    ]), "rev.csv")])
    s = cc.summarize_by_crr(cc.allocate(rev, None))
    assert s["Allocated Offset ($)"].iloc[0] == 0
    assert s["Net Revenue ($)"].iloc[0] == 50


def test_different_product_type_is_a_different_group():
    rev = cc.parse_revenue_adjustments([(_rev_bytes([
        _row(1, "C1", 0, -10, 30, product="EN"),
        _row(2, "C1", 0, -10, 10, product="IRU"),
    ]), "rev.csv")])
    s = cc.summarize_by_crr(cc.allocate(rev, None)).set_index("CRR ID")
    assert s.loc["1", "Allocated Offset ($)"] == pytest.approx(-10)
    assert s.loc["2", "Allocated Offset ($)"] == pytest.approx(-10)


def test_reconciliation_and_path_map():
    rev = cc.parse_revenue_adjustments([(_rev_bytes([
        _row(1, "C1", 50, -100, 30),
        _row(2, "C1", 20, -100, 10),
        _row(2, "C2", 5, -8, 10),
    ]), "rev.csv")])
    results = (
        "BID ID,CRR ID,Source,Sink,Awarded MW,Bid Type,TOU\n"
        "9,1,A,B,30,BUY,ON\n"
        "10,,X,Y,0,BUY,ON\n"
    ).encode()
    paths, _ = cc.parse_crr_results([(results, "res.csv")])
    alloc = cc.allocate(rev, paths)
    allocated, once = cc.reconciliation(alloc)
    assert allocated == pytest.approx(once) == pytest.approx(-108)
    s = cc.summarize_by_crr(alloc).set_index("CRR ID")
    assert s.loc["1", "Source"] == "A"
    assert s.loc["2", "Source"] == cc.UNMAPPED
