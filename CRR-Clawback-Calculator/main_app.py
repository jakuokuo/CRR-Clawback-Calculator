"""CRR Clawback Calculator — Dash app.

Upload
  1. one or more CRR Results files (e.g. "TEAX CRR Results.csv" — monthly and
     annual) so the app knows which Source -> Sink path each CRR ID belongs to;
  2. one or more CAISO "Post-Market CRR Revenue Adjustments Details" reports
     (.ods / .xlsx / .csv) with notional and offset revenue per CRR ID,
     binding constraint and hour.

The app allocates each constraint-hour's (negative-only) offset revenue to the
CRRs on that constraint by MW share and shows the result per CRR ID, the same
way the "CRR Summary" sheet of clawbacks_allocated.xlsx does.

Deploy to Posit Connect (push-deploy from CLI):
    rsconnect deploy dash --entrypoint main_app:app \\
        --server https://connect.teainc.org --api-key <YOUR_API_KEY> \\
        --title "CRR Clawback Calculator" .

Git-backed deploy (preferred): commit manifest.json + push to GitHub, then
in Connect choose New Content -> Import from Git.
"""

from __future__ import annotations

import base64
import io
from typing import List, Tuple

import dash
import dash_bootstrap_components as dbc
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from dash import Input, Output, State, dash_table, dcc, html
from dash.dash_table.Format import Format, Group, Scheme, Sign, Symbol

import clawback_calc as cc

# --------------------------------------------------------------------------- #
# Dash app setup
# --------------------------------------------------------------------------- #

app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.FLATLY, dbc.icons.BOOTSTRAP],
    suppress_callback_exceptions=True,
    title="CRR Clawback Calculator",
)
server = app.server  # WSGI entrypoint Posit Connect looks for

PLOT_TEMPLATE = "plotly_white"
POS_COLOR = "#2ca02c"
NEG_COLOR = "#d62728"
NOTIONAL_COLOR = "#1f77b4"
OFFSET_COLOR = "#d62728"
NET_COLOR = "#212529"

# Columns kept in the browser-side store (keeps the payload small).
STORE_COLS = [
    "Start Date", "End Date", "Operating Date", "Product Type",
    "Transmission Constraint ID", "Constraint Case", "CRR ID", "Hedge Type",
    "CRR Type", "Source", "Sink", "Path", "Portfolio", "TOU",
    "Notional Revenue ($)", "Offset Revenue ($)", "CRR Clawback Revenue ($)",
    "Circular Scheduling Revenue ($)", "Derate Factor", "CRR Award MW",
    "Neg Offset Only ($)", "Constraint-Hour Total MW", "CRRs on Constraint-Hour",
    "CRR MW Share", "Allocated Offset ($)",
]

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _decode_uploads(contents, filenames) -> List[Tuple[bytes, str]]:
    if contents is None:
        return []
    if not isinstance(contents, list):
        contents, filenames = [contents], [filenames]
    out = []
    for c, n in zip(contents, filenames):
        _, b64 = c.split(",", 1)
        out.append((base64.b64decode(b64), n or ""))
    return out


def _df_to_store(df: pd.DataFrame) -> str:
    return df.to_json(date_format="iso", orient="split")


def _store_to_df(data: str, date_cols=()) -> pd.DataFrame:
    df = pd.read_json(io.StringIO(data), orient="split", dtype=False)
    for c in date_cols:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c])
    if "CRR ID" in df.columns:
        df["CRR ID"] = cc.normalize_crr_id(df["CRR ID"])
    return df


def _fmt_money(v: float) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def _fmt_pct(v: float) -> str:
    return "—" if v is None or pd.isna(v) else f"{v:.1%}"


MONEY = Format(precision=2, scheme=Scheme.fixed, group=Group.yes,
               symbol=Symbol.yes, sign=Sign.negative)
MW_FMT = Format(precision=2, scheme=Scheme.fixed, group=Group.yes)
INT_FMT = Format(precision=0, scheme=Scheme.fixed, group=Group.yes)
PCT_FMT = Format(precision=1, scheme=Scheme.percentage)
SHARE_FMT = Format(precision=3, scheme=Scheme.fixed)


def _col(name: str) -> dict:
    if name.endswith("($)"):
        return {"name": name, "id": name, "type": "numeric", "format": MONEY}
    if name in ("Clawback %",):
        return {"name": name, "id": name, "type": "numeric", "format": PCT_FMT}
    if name in ("CRR MW Share",):
        return {"name": name, "id": name, "type": "numeric", "format": SHARE_FMT}
    if "MW" in name or name == "Derate Factor":
        return {"name": name, "id": name, "type": "numeric", "format": MW_FMT}
    if name in ("Rows", "Binding Hours", "CRRs Affected", "CRRs on Constraint-Hour"):
        return {"name": name, "id": name, "type": "numeric", "format": INT_FMT}
    return {"name": name, "id": name}


TABLE_STYLE = dict(
    style_table={"overflowX": "auto"},
    style_cell={
        "padding": "6px 10px",
        "fontSize": 13,
        "fontFamily": "Inter, system-ui, sans-serif",
        "whiteSpace": "nowrap",
    },
    style_cell_conditional=[
        {"if": {"column_id": c}, "textAlign": "left"}
        for c in ("Source", "Sink", "CRR ID", "Portfolio", "TOU", "Start Date",
                  "Product Type", "Transmission Constraint ID", "Constraint Case")
    ],
    style_header={
        "fontWeight": "bold",
        "backgroundColor": "#f1f3f5",
        "border": "none",
    },
)


def _neg_pos_styles(cols: List[str]) -> list:
    styles = []
    for c in cols:
        styles += [
            {"if": {"filter_query": f"{{{c}}} < 0", "column_id": c}, "color": NEG_COLOR},
            {"if": {"filter_query": f"{{{c}}} > 0", "column_id": c}, "color": POS_COLOR},
        ]
    return styles


def _placeholder_figure(message: str, height: int = 320) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        template=PLOT_TEMPLATE,
        xaxis={"visible": False},
        yaxis={"visible": False},
        annotations=[
            dict(
                text=message,
                showarrow=False,
                xref="paper",
                yref="paper",
                x=0.5,
                y=0.5,
                font=dict(size=16, color="#6c757d"),
            )
        ],
        height=height,
    )
    return fig


def _graph(graph_id: str, height: int = 360) -> dcc.Loading:
    return dcc.Loading(
        dcc.Graph(id=graph_id, style={"height": f"{height}px"}),
        type="circle",
    )


def _upload_box(upload_id: str, hint: str) -> dcc.Upload:
    return dcc.Upload(
        id=upload_id,
        children=html.Div(
            [
                html.I(className="bi bi-cloud-upload fs-3 d-block"),
                html.Span("Drag & drop or "),
                html.A("browse for files", className="text-primary"),
                html.Div(hint, className="text-muted small"),
            ]
        ),
        style={
            "width": "100%",
            "padding": "20px",
            "borderWidth": "2px",
            "borderStyle": "dashed",
            "borderRadius": "10px",
            "borderColor": "#adb5bd",
            "textAlign": "center",
            "backgroundColor": "#f8f9fa",
            "cursor": "pointer",
        },
        multiple=True,
    )


def _kpi(title: str, value: str, color: str, icon: str) -> dbc.Col:
    return dbc.Col(
        dbc.Card(
            dbc.CardBody(
                [
                    html.Div(
                        [
                            html.I(className=f"bi {icon} me-2"),
                            html.Span(title, className="text-muted small"),
                        ]
                    ),
                    html.H4(value, className=f"fw-bold mb-0 {color}"),
                ]
            ),
            className="shadow-sm h-100",
        ),
        lg=2,
        md=4,
        sm=6,
    )


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #

navbar = dbc.Navbar(
    dbc.Container(
        dbc.NavbarBrand(
            [
                html.I(className="bi bi-lightning-charge me-2"),
                "CRR Clawback Calculator",
            ],
            className="fw-semibold text-white",
        ),
        fluid=True,
    ),
    color="primary",
    dark=True,
    sticky="top",
    className="mb-3",
)

upload_row = dbc.Row(
    [
        dbc.Col(
            dbc.Card(
                dbc.CardBody(
                    [
                        html.H5("1 · CRR Results", className="card-title"),
                        html.P(
                            "Auction results that map each CRR ID to its "
                            "Source → Sink path (e.g. TEAX CRR Results.csv). "
                            "Add the annual results too so annual CRR IDs get a path.",
                            className="text-muted small mb-2",
                        ),
                        _upload_box("upload-results", ".csv / .xlsx / .xls / .ods · multiple allowed"),
                        html.Div(id="results-status", className="mt-2"),
                    ]
                ),
                className="shadow-sm h-100",
            ),
            md=6,
        ),
        dbc.Col(
            dbc.Card(
                dbc.CardBody(
                    [
                        html.H5("2 · Revenue Adjustments Details", className="card-title"),
                        html.P(
                            "CAISO CMRI Post-Market CRR Revenue Adjustments "
                            "Details report with offset revenue by binding "
                            "constraint and CRR ID.",
                            className="text-muted small mb-2",
                        ),
                        _upload_box("upload-revenue", ".ods / .xlsx / .xls / .csv · multiple allowed"),
                        html.Div(id="revenue-status", className="mt-2"),
                    ]
                ),
                className="shadow-sm h-100",
            ),
            md=6,
        ),
    ],
    className="g-3 mb-3",
)

filter_card = dbc.Card(
    dbc.CardBody(
        [
            html.H6("Filters", className="card-title"),
            html.P(
                "Offsets are allocated across all uploaded CRRs first; "
                "filters only change what is shown.",
                className="text-muted small",
            ),
            dbc.Label("Portfolio", className="mt-1 fw-semibold"),
            dcc.Dropdown(id="portfolio-filter", multi=True, placeholder="All portfolios"),
            dbc.Label("CRR IDs", className="mt-2 fw-semibold"),
            dcc.Dropdown(id="crr-filter", multi=True, placeholder="All CRR IDs"),
            dbc.Label("Product type", className="mt-2 fw-semibold"),
            dcc.Dropdown(id="product-filter", multi=True, placeholder="All (EN / IRU / IRD …)"),
            dbc.Label("Operating date range", className="mt-2 fw-semibold"),
            dcc.DatePickerRange(
                id="date-filter",
                display_format="YYYY-MM-DD",
                style={"width": "100%"},
            ),
            html.Hr(),
            html.H6("Look up CRR ID", className="card-title"),
            dcc.Dropdown(id="lookup-crr", placeholder="Pick a CRR ID"),
            html.Div(id="lookup-panel", className="mt-2"),
        ]
    ),
    className="shadow-sm",
)

app.layout = html.Div(
    [
        navbar,
        dcc.Store(id="store-paths"),
        dcc.Store(id="store-revenue"),
        dcc.Store(id="store-alloc"),
        dcc.Download(id="download-xlsx"),
        dcc.Download(id="download-csv"),
        dbc.Container(
            [
                upload_row,
                html.Div(id="alloc-status", className="mb-3"),
                dbc.Row(
                    [
                        dbc.Col(filter_card, lg=3, md=4),
                        dbc.Col(
                            [
                                dbc.Row(id="kpi-row", className="g-3 mb-3"),
                                dbc.Card(
                                    dbc.CardBody(_graph("crr-graph", 400)),
                                    className="shadow-sm mb-3",
                                ),
                                dbc.Card(
                                    dbc.CardBody(_graph("daily-graph", 340)),
                                    className="shadow-sm mb-3",
                                ),
                                dbc.Card(
                                    dbc.CardBody(_graph("constraint-graph", 480)),
                                    className="shadow-sm",
                                ),
                            ],
                            lg=9,
                            md=8,
                        ),
                    ],
                    className="g-3",
                ),
                dbc.Row(
                    dbc.Col(
                        dbc.Card(
                            dbc.CardBody(
                                [
                                    html.Div(
                                        [
                                            html.H5("CRR Summary", className="card-title mb-0"),
                                            html.Div(
                                                [
                                                    dbc.Button(
                                                        [html.I(className="bi bi-file-earmark-excel me-1"), "Excel"],
                                                        id="btn-xlsx", color="success", size="sm",
                                                        className="me-2", disabled=True,
                                                    ),
                                                    dbc.Button(
                                                        [html.I(className="bi bi-filetype-csv me-1"), "CSV"],
                                                        id="btn-csv", color="secondary", size="sm",
                                                        outline=True, disabled=True,
                                                    ),
                                                ]
                                            ),
                                        ],
                                        className="d-flex justify-content-between align-items-center mb-1",
                                    ),
                                    html.P(
                                        "Allocated negative CRR offset by CRR ID. Offset Revenue "
                                        "repeats on every CRR ID that hits the same constraint in "
                                        "the same hour; each CRR is charged its MW share of that "
                                        "number and positive offsets count as zero. Naive Offset "
                                        "Pull is the raw sum of negatives (reference only — it "
                                        "double counts shared constraints).",
                                        className="text-muted small mb-2",
                                    ),
                                    dcc.Loading(html.Div(id="summary-table")),
                                    html.Div(id="recon-panel", className="mt-2"),
                                ]
                            ),
                            className="shadow-sm",
                        ),
                    ),
                    className="my-3",
                ),
                dbc.Row(
                    dbc.Col(
                        dbc.Card(
                            dbc.CardBody(
                                [
                                    html.H5("Binding Constraints", className="card-title"),
                                    html.P(
                                        "Allocated offset by transmission constraint "
                                        "for the CRRs and dates in view.",
                                        className="text-muted small mb-2",
                                    ),
                                    dcc.Loading(html.Div(id="constraint-table")),
                                ]
                            ),
                            className="shadow-sm",
                        ),
                    ),
                    className="my-3",
                ),
                dbc.Row(
                    dbc.Col(
                        dbc.Card(
                            dbc.CardBody(
                                [
                                    html.H5("Allocation Detail", className="card-title"),
                                    html.P(
                                        "One row per CRR ID × constraint × hour, with the "
                                        "MW share used for the allocation.",
                                        className="text-muted small mb-2",
                                    ),
                                    dcc.Loading(html.Div(id="detail-table")),
                                ]
                            ),
                            className="shadow-sm",
                        ),
                    ),
                    className="my-3",
                ),
                html.Footer(
                    html.Small(
                        "Built with Dash · Neg Offset = MIN(Offset Revenue, 0) · "
                        "Constraint-hour = Start Date × Product Type × Constraint ID × "
                        "Constraint Case · Allocated Offset = Neg Offset × (CRR MW ÷ "
                        "constraint-hour total MW) · Net Revenue = Notional + Allocated "
                        "Offset + CRR Clawback + Circular Scheduling · Clawback % = "
                        "|Allocated Offset| ÷ Notional.",
                        className="text-muted",
                    ),
                    className="text-center my-4",
                ),
            ],
            fluid=True,
        ),
    ]
)


# --------------------------------------------------------------------------- #
# Callbacks — uploads
# --------------------------------------------------------------------------- #

@app.callback(
    Output("store-paths", "data"),
    Output("results-status", "children"),
    Input("upload-results", "contents"),
    State("upload-results", "filename"),
    prevent_initial_call=True,
)
def on_results_upload(contents, filenames):
    try:
        files = _decode_uploads(contents, filenames)
        path_map, warnings = cc.parse_crr_results(files)
    except Exception as exc:  # noqa: BLE001 — surface any parse error to the UI
        return dash.no_update, dbc.Alert(f"Could not read file: {exc}", color="danger", className="mb-0")

    if path_map.empty:
        return dash.no_update, dbc.Alert(
            "No rows with a CRR ID were found (only awarded bids get a CRR ID).",
            color="warning", className="mb-0",
        )
    names = ", ".join(n for _, n in files)
    msgs = [
        html.I(className="bi bi-check-circle me-2"),
        f"Loaded {names} — {len(path_map):,} CRR IDs with paths.",
    ]
    for w in warnings:
        msgs += [html.Br(), html.Small(w)]
    return _df_to_store(path_map), dbc.Alert(msgs, color="success", className="mb-0")


@app.callback(
    Output("store-revenue", "data"),
    Output("revenue-status", "children"),
    Input("upload-revenue", "contents"),
    State("upload-revenue", "filename"),
    prevent_initial_call=True,
)
def on_revenue_upload(contents, filenames):
    try:
        files = _decode_uploads(contents, filenames)
        rev = cc.parse_revenue_adjustments(files)
    except Exception as exc:  # noqa: BLE001
        return dash.no_update, dbc.Alert(f"Could not read file: {exc}", color="danger", className="mb-0")

    if rev.empty:
        return dash.no_update, dbc.Alert("No revenue adjustment rows found.", color="warning", className="mb-0")

    names = ", ".join(n for _, n in files)
    d0, d1 = rev["Start Date"].min(), rev["Start Date"].max()
    status = dbc.Alert(
        [
            html.I(className="bi bi-check-circle me-2"),
            f"Loaded {names} — {len(rev):,} rows, {rev['CRR ID'].nunique()} CRR IDs, "
            f"{d0:%Y-%m-%d} to {d1:%Y-%m-%d}.",
        ],
        color="success",
        className="mb-0",
    )
    return _df_to_store(rev), status


@app.callback(
    Output("store-alloc", "data"),
    Output("alloc-status", "children"),
    Output("portfolio-filter", "options"),
    Output("crr-filter", "options"),
    Output("product-filter", "options"),
    Output("lookup-crr", "options"),
    Output("date-filter", "min_date_allowed"),
    Output("date-filter", "max_date_allowed"),
    Output("date-filter", "start_date"),
    Output("date-filter", "end_date"),
    Output("btn-xlsx", "disabled"),
    Output("btn-csv", "disabled"),
    Input("store-revenue", "data"),
    Input("store-paths", "data"),
)
def on_data_ready(rev_data, path_data):
    empty = (None, None, [], [], [], [], None, None, None, None, True, True)
    if not rev_data:
        return empty

    rev = _store_to_df(rev_data, ("Start Date", "End Date"))
    path_map = _store_to_df(path_data) if path_data else None
    alloc = cc.allocate(rev, path_map)

    unmapped = sorted(alloc.loc[alloc["Source"] == cc.UNMAPPED, "CRR ID"].unique())
    alerts = []
    if path_map is None:
        alerts.append(
            dbc.Alert(
                [html.I(className="bi bi-info-circle me-2"),
                 "Clawbacks are calculated. Upload the CRR Results file(s) to "
                 "attach Source → Sink paths to each CRR ID."],
                color="info", className="mb-2",
            )
        )
    elif unmapped:
        alerts.append(
            dbc.Alert(
                [html.I(className="bi bi-exclamation-triangle me-2"),
                 f"{len(unmapped)} CRR ID(s) in the revenue report have no path in the "
                 f"uploaded CRR Results: {', '.join(unmapped)}. These are usually "
                 "annual (or prior-month) CRRs — add that results file to the "
                 "CRR Results upload. Their clawbacks are still calculated."],
                color="warning", className="mb-2",
            )
        )
    if path_map is not None:
        no_rev = sorted(set(path_map["CRR ID"]) - set(alloc["CRR ID"]))
        if no_rev:
            alerts.append(
                dbc.Alert(
                    f"{len(no_rev)} CRR ID(s) in the CRR Results have no rows in the "
                    f"revenue report (not binding / different period): {', '.join(no_rev)}.",
                    color="light", className="mb-2 small",
                )
            )

    crr_opts = (
        alloc.drop_duplicates("CRR ID")
        .sort_values("CRR ID")
        .apply(lambda r: {"label": f"{r['CRR ID']} · {r['Source']} → {r['Sink']}"
                          + (f" ({r['TOU']})" if r["TOU"] else ""),
                          "value": r["CRR ID"]}, axis=1)
        .tolist()
    )
    portfolios = sorted(p for p in alloc["Portfolio"].unique() if p)
    port_opts = [{"label": p, "value": p} for p in portfolios]
    if (alloc["Portfolio"] == "").any():
        port_opts.append({"label": "(no portfolio)", "value": "__none__"})
    prod_opts = [{"label": p, "value": p} for p in sorted(alloc["Product Type"].unique())]

    d0 = alloc["Operating Date"].min().date()
    d1 = alloc["Operating Date"].max().date()

    store = _df_to_store(alloc[[c for c in STORE_COLS if c in alloc.columns]])
    return (store, alerts, port_opts, crr_opts, prod_opts, crr_opts,
            d0, d1, d0, d1, False, False)


# --------------------------------------------------------------------------- #
# Callbacks — views
# --------------------------------------------------------------------------- #

def _filtered(data, portfolios, crrs, products, start_d, end_d) -> pd.DataFrame:
    df = _store_to_df(data, ("Start Date", "End Date", "Operating Date"))
    for c in ("Portfolio", "TOU"):
        df[c] = df[c].fillna("")
    if portfolios:
        want = set(portfolios)
        mask = df["Portfolio"].isin(want)
        if "__none__" in want:
            mask |= df["Portfolio"] == ""
        df = df[mask]
    if crrs:
        df = df[df["CRR ID"].isin(crrs)]
    if products:
        df = df[df["Product Type"].isin(products)]
    if start_d:
        df = df[df["Operating Date"] >= pd.to_datetime(start_d)]
    if end_d:
        df = df[df["Operating Date"] <= pd.to_datetime(end_d)]
    return df


FILTER_INPUTS = [
    Input("store-alloc", "data"),
    Input("portfolio-filter", "value"),
    Input("crr-filter", "value"),
    Input("product-filter", "value"),
    Input("date-filter", "start_date"),
    Input("date-filter", "end_date"),
]


@app.callback(
    Output("kpi-row", "children"),
    Output("crr-graph", "figure"),
    Output("daily-graph", "figure"),
    Output("constraint-graph", "figure"),
    Output("summary-table", "children"),
    Output("recon-panel", "children"),
    Output("constraint-table", "children"),
    Output("detail-table", "children"),
    *FILTER_INPUTS,
)
def update_views(data, portfolios, crrs, products, start_d, end_d):
    if not data:
        ph = _placeholder_figure("Upload a Revenue Adjustments Details report to get started.")
        msg = html.Div("Upload files above to populate the dashboard.", className="text-muted")
        return [], ph, ph, ph, msg, None, msg, msg

    df = _filtered(data, portfolios, crrs, products, start_d, end_d)
    if df.empty:
        ph = _placeholder_figure("No rows match the current filters.")
        msg = html.Div("No rows match the current filters.", className="text-muted")
        return [], ph, ph, ph, msg, None, msg, msg

    summary = cc.summarize_by_crr(df)
    total = cc.summary_total_row(summary)

    # ---- KPIs ---- #
    net = total["Net Revenue ($)"]
    kpis = [
        _kpi("CRRs", f"{len(summary):,}", "text-dark", "bi-diagram-3"),
        _kpi("Award MW", f"{total['Award MW']:,.1f}", "text-dark", "bi-lightning"),
        _kpi("Notional Revenue", _fmt_money(total["Notional Revenue ($)"]),
             "text-primary", "bi-cash-coin"),
        _kpi("Allocated Offset", _fmt_money(total["Allocated Offset ($)"]),
             "text-danger", "bi-arrow-down-right"),
        _kpi("Net Revenue", _fmt_money(net),
             "text-success" if net >= 0 else "text-danger", "bi-wallet2"),
        _kpi("Clawback %", _fmt_pct(total["Clawback %"]), "text-warning", "bi-percent"),
    ]

    # ---- Per-CRR revenue bridge ---- #
    s = summary.copy()
    s["Label"] = s["CRR ID"] + np.where(s["TOU"] != "", "<br>" + s["TOU"], "")
    s["PathText"] = s["Source"] + " → " + s["Sink"]
    other = s["CRR Clawback Revenue ($)"] + s["Circular Scheduling Revenue ($)"]
    hover = "<b>%{x}</b><br>%{customdata}<br>"
    fig_crr = go.Figure()
    fig_crr.add_bar(x=s["Label"], y=s["Notional Revenue ($)"], name="Notional Revenue",
                    marker_color=NOTIONAL_COLOR, customdata=s["PathText"],
                    hovertemplate=hover + "Notional: $%{y:,.2f}<extra></extra>")
    fig_crr.add_bar(x=s["Label"], y=s["Allocated Offset ($)"], name="Allocated Offset",
                    marker_color=OFFSET_COLOR, customdata=s["PathText"],
                    hovertemplate=hover + "Allocated offset: $%{y:,.2f}<extra></extra>")
    if (other != 0).any():
        fig_crr.add_bar(x=s["Label"], y=other, name="Clawback + Circular Sched.",
                        marker_color="#ff7f0e", customdata=s["PathText"],
                        hovertemplate=hover + "Other: $%{y:,.2f}<extra></extra>")
    fig_crr.add_scatter(x=s["Label"], y=s["Net Revenue ($)"], name="Net Revenue",
                        mode="markers", customdata=s["PathText"],
                        marker=dict(symbol="diamond", size=12, color=NET_COLOR,
                                    line=dict(color="white", width=1)),
                        hovertemplate=hover + "Net: $%{y:,.2f}<extra></extra>")
    fig_crr.update_layout(template=PLOT_TEMPLATE, barmode="group",
                          title="Notional vs Allocated Offset by CRR ID (hover for path)",
                          yaxis_title=None, xaxis_title=None, legend_title=None,
                          legend=dict(orientation="h", yanchor="bottom", y=1.0,
                                      x=1, xanchor="right"),
                          margin=dict(t=80), height=400)
    fig_crr.update_xaxes(type="category", tickangle=0)
    fig_crr.update_yaxes(tickformat="$,.0f")
    fig_crr.add_hline(y=0, line_width=1, line_color="#adb5bd")

    # ---- Daily allocated offset, stacked by CRR ---- #
    daily = (
        df.groupby(["Operating Date", "CRR ID", "Path"])["Allocated Offset ($)"]
        .sum().reset_index()
    )
    daily["CRR"] = daily["CRR ID"] + " · " + daily["Path"]
    fig_daily = px.bar(daily, x="Operating Date", y="Allocated Offset ($)", color="CRR",
                       template=PLOT_TEMPLATE, title="Daily Allocated Offset by CRR")
    fig_daily.update_traces(hovertemplate="<b>%{x|%b %d, %Y}</b><br>%{fullData.name}"
                                          "<br>Allocated: $%{y:,.2f}<extra></extra>")
    fig_daily.update_layout(yaxis_title="$", xaxis_title=None, legend_title=None,
                            bargap=0.15, showlegend=len(summary) <= 15, height=340)
    fig_daily.update_yaxes(tickformat="$,.0f")

    # ---- Top binding constraints ---- #
    cons = cc.constraint_summary(df)
    if cons.empty:
        fig_cons = _placeholder_figure("No negative offsets in view.", 480)
    else:
        top = cons.head(15).iloc[::-1].copy()
        full = top["Transmission Constraint ID"] + np.where(
            top["Constraint Case"] != "Base Case", " [" + top["Constraint Case"] + "]", "")
        top["Constraint"] = [f if len(f) <= 48 else f[:46] + "…" for f in full]
        # keep labels unique after truncation
        top["Constraint"] = top["Constraint"] + [" " * i for i in range(len(top))]
        fig_cons = go.Figure(go.Bar(
            x=top["Allocated Offset ($)"], y=top["Constraint"], orientation="h",
            marker_color=NEG_COLOR,
            customdata=np.column_stack([top["Binding Hours"], top["CRRs Affected"], full]),
            hovertemplate="<b>%{customdata[2]}</b><br>Allocated: $%{x:,.2f}<br>Binding hours: "
                          "%{customdata[0]}<br>CRRs affected: %{customdata[1]}<extra></extra>",
        ))
        fig_cons.update_layout(template=PLOT_TEMPLATE, title="Top Binding Constraints by Allocated Offset",
                               xaxis_title=None, yaxis_title=None, margin=dict(l=10), height=480)
        fig_cons.update_yaxes(tickmode="linear", automargin=True, tickfont=dict(size=11))
        fig_cons.update_xaxes(tickformat="$,.0f")

    # ---- Summary table (with Total row) ---- #
    display = pd.concat([summary, pd.DataFrame([total])], ignore_index=True)
    summary_table = dash_table.DataTable(
        data=display.to_dict("records"),
        columns=[_col(c) for c in display.columns],
        sort_action="none",
        page_action="none",
        style_data_conditional=_neg_pos_styles(["Net Revenue ($)"]) + [
            {"if": {"filter_query": "{Clawback %} > 1", "column_id": "Clawback %"},
             "color": NEG_COLOR, "fontWeight": "600"},
            {"if": {"column_id": "Allocated Offset ($)"}, "color": NEG_COLOR},
            {"if": {"filter_query": '{CRR ID} = "Total"'},
             "fontWeight": "700", "backgroundColor": "#f1f3f5",
             "borderTop": "2px solid #adb5bd"},
            {"if": {"filter_query": f'{{Source}} = "{cc.UNMAPPED}"', "column_id": ["Source", "Sink"]},
             "color": "#adb5bd", "fontStyle": "italic"},
        ],
        **{
            **TABLE_STYLE,
            "style_header": {**TABLE_STYLE["style_header"], "whiteSpace": "normal",
                             "height": "auto", "textAlign": "center"},
            "style_cell_conditional": TABLE_STYLE["style_cell_conditional"] + [
                {"if": {"column_id": "Net Revenue ($)"}, "backgroundColor": "#f8f9fa",
                 "fontWeight": "600"},
            ],
        },
    )

    # ---- Reconciliation check ---- #
    allocated, once = cc.reconciliation(df)
    diff = round(allocated - once, 2) + 0.0  # avoid '-0.00'
    recon = html.Small(
        [
            html.I(className=f"bi {'bi-check-circle text-success' if diff == 0 else 'bi-x-circle text-danger'} me-1"),
            f"Check: allocated total {_fmt_money(allocated)} vs. negative offsets counted once "
            f"per constraint-hour {_fmt_money(once)} → difference ${abs(diff):,.2f}",
        ],
        className="text-muted",
    )

    # ---- Constraint table ---- #
    if cons.empty:
        cons_table = html.Div("No negative offsets in view.", className="text-muted")
    else:
        cons_table = dash_table.DataTable(
            data=cons.to_dict("records"),
            columns=[_col(c) for c in cons.columns],
            page_size=10,
            sort_action="native",
            filter_action="native",
            style_data_conditional=[{"if": {"column_id": "Allocated Offset ($)"}, "color": NEG_COLOR}],
            **TABLE_STYLE,
        )

    # ---- Detail table ---- #
    det_cols = ["Start Date", "CRR ID", "Source", "Sink", "TOU", "Product Type",
                "Transmission Constraint ID", "Constraint Case", "CRR Award MW",
                "Notional Revenue ($)", "Offset Revenue ($)", "Neg Offset Only ($)",
                "Constraint-Hour Total MW", "CRR MW Share", "Allocated Offset ($)"]
    det = df[det_cols].sort_values(["Start Date", "Transmission Constraint ID", "CRR ID"]).copy()
    det["Start Date"] = det["Start Date"].dt.strftime("%Y-%m-%d %H:%M")
    detail_table = dash_table.DataTable(
        data=det.to_dict("records"),
        columns=[_col(c) for c in det.columns],
        page_size=15,
        sort_action="native",
        filter_action="native",
        style_data_conditional=_neg_pos_styles(["Notional Revenue ($)", "Offset Revenue ($)"]) + [
            {"if": {"filter_query": "{Allocated Offset ($)} < 0", "column_id": "Allocated Offset ($)"},
             "color": NEG_COLOR, "fontWeight": "600"},
        ],
        **TABLE_STYLE,
    )

    return (kpis, fig_crr, fig_daily, fig_cons, summary_table, recon,
            cons_table, detail_table)


@app.callback(
    Output("lookup-panel", "children"),
    Input("lookup-crr", "value"),
    *FILTER_INPUTS[:1],
    *FILTER_INPUTS[4:],
)
def update_lookup(crr_id, data, start_d, end_d):
    if not data or not crr_id:
        return html.Small("Totals for one CRR ID (respects the date range).", className="text-muted")
    df = _filtered(data, None, [crr_id], None, start_d, end_d)
    if df.empty:
        return html.Small("No rows for this CRR ID in the date range.", className="text-muted")
    row = cc.summarize_by_crr(df).iloc[0]
    items = [
        ("Path", f"{row['Source']} → {row['Sink']}"),
        ("Award MW", f"{row['Award MW']:,.2f}"),
        ("Notional Revenue", _fmt_money(row["Notional Revenue ($)"])),
        ("Allocated Offset", _fmt_money(row["Allocated Offset ($)"])),
        ("CRR Clawback Revenue", _fmt_money(row["CRR Clawback Revenue ($)"])),
        ("Circular Scheduling", _fmt_money(row["Circular Scheduling Revenue ($)"])),
        ("Net Revenue", _fmt_money(row["Net Revenue ($)"])),
        ("Clawback %", _fmt_pct(row["Clawback %"])),
        ("Naive offset pull", _fmt_money(row["Naive Offset Pull ($)"])),
    ]
    top = cc.constraint_summary(df).head(3)
    return html.Div(
        [
            dbc.Table(
                html.Tbody([
                    html.Tr([html.Td(k, className="text-muted small py-1"),
                             html.Td(v, className="small fw-semibold text-end py-1")])
                    for k, v in items
                ]),
                size="sm", borderless=True, className="mb-1",
            ),
            html.Small("Top constraints:", className="text-muted fw-semibold") if not top.empty else None,
            html.Ul(
                [html.Li(html.Small(f"{r['Transmission Constraint ID']}: {_fmt_money(r['Allocated Offset ($)'])}"))
                 for _, r in top.iterrows()],
                className="ps-3 mb-0",
            ) if not top.empty else None,
        ]
    )


# --------------------------------------------------------------------------- #
# Callbacks — downloads
# --------------------------------------------------------------------------- #

def _export_frames(data, portfolios, crrs, products, start_d, end_d):
    df = _filtered(data, portfolios, crrs, products, start_d, end_d)
    summary = cc.summarize_by_crr(df)
    if not summary.empty:
        summary = pd.concat([summary, pd.DataFrame([cc.summary_total_row(summary)])],
                            ignore_index=True)
    return df, summary


@app.callback(
    Output("download-xlsx", "data"),
    Input("btn-xlsx", "n_clicks"),
    State("store-alloc", "data"),
    State("portfolio-filter", "value"),
    State("crr-filter", "value"),
    State("product-filter", "value"),
    State("date-filter", "start_date"),
    State("date-filter", "end_date"),
    prevent_initial_call=True,
)
def download_xlsx(_n, data, portfolios, crrs, products, start_d, end_d):
    if not data:
        return dash.no_update
    df, summary = _export_frames(data, portfolios, crrs, products, start_d, end_d)
    cons = cc.constraint_summary(df)
    detail = df.drop(columns=["Operating Date", "Path"], errors="ignore").copy()

    def writer(buf):
        with pd.ExcelWriter(buf, engine="openpyxl") as xw:
            summary.to_excel(xw, sheet_name="CRR Summary", index=False)
            cons.to_excel(xw, sheet_name="Constraints", index=False)
            detail.to_excel(xw, sheet_name="Allocation Detail", index=False)
            for ws in xw.book.worksheets:
                ws.freeze_panes = "A2"
                for col in ws.columns:
                    header = str(col[0].value or "")
                    width = max(len(header), *(len(str(c.value)) for c in col[1:200] if c.value is not None), 8)
                    ws.column_dimensions[col[0].column_letter].width = min(width + 2, 48)
                    if header.endswith("($)"):
                        for c in col[1:]:
                            c.number_format = '#,##0.00;[Red]-#,##0.00'
                    elif header == "Clawback %":
                        for c in col[1:]:
                            c.number_format = "0.0%"
                    elif header in ("Start Date", "End Date"):
                        for c in col[1:]:
                            c.number_format = "yyyy-mm-dd hh:mm"

    stamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M")
    return dcc.send_bytes(writer, f"crr_clawbacks_{stamp}.xlsx")


@app.callback(
    Output("download-csv", "data"),
    Input("btn-csv", "n_clicks"),
    State("store-alloc", "data"),
    State("portfolio-filter", "value"),
    State("crr-filter", "value"),
    State("product-filter", "value"),
    State("date-filter", "start_date"),
    State("date-filter", "end_date"),
    prevent_initial_call=True,
)
def download_csv(_n, data, portfolios, crrs, products, start_d, end_d):
    if not data:
        return dash.no_update
    _, summary = _export_frames(data, portfolios, crrs, products, start_d, end_d)
    stamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M")
    return dcc.send_data_frame(summary.to_csv, f"crr_summary_{stamp}.csv", index=False)


if __name__ == "__main__":
    app.run(debug=True)
