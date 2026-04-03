from __future__ import annotations

"""
generate_report.py — Generate a cumulative interactive HTML trading report.

Reads all data from data/*.csv (written by csv_tracker.py) and produces
a single self-contained HTML file inside the reports/ folder.

Usage:
    python generate_report.py
    python generate_report.py --out reports/my_report.html
    python generate_report.py --mode PAPER
    python generate_report.py --strategy shortStrangle_Adjust
    python generate_report.py --exchange MCX
    python generate_report.py --exchange INDEX --mode PAPER

Output default: reports/report_YYYY-MM-DD.html
  The reports/ folder is created automatically if it does not exist.
"""

import argparse
from datetime import datetime, date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import plotly.io as pio

# ── Directory / file paths ─────────────────────────────────────────────
BASE_DIR     = Path(__file__).resolve().parent
DATA_DIR     = BASE_DIR / "data"
REPORTS_DIR  = BASE_DIR / "reports"

OPEN_FILE    = DATA_DIR / "open_positions.csv"
HISTORY_FILE = DATA_DIR / "trade_history.csv"
DAILY_FILE   = DATA_DIR / "daily_summary.csv"

# ── Colour palette ─────────────────────────────────────────────────────
CLR_PROFIT   = "#2ecc71"
CLR_LOSS     = "#e74c3c"
CLR_NEUTRAL  = "#3498db"
CLR_BG       = "#0f1117"
CLR_CARD     = "#1e2130"
CLR_TEXT     = "#e0e0e0"
CLR_MUTED    = "#8892a4"
CLR_BORDER   = "#2d3348"

STRATEGY_COLOURS = [
    "#3498db", "#2ecc71", "#e67e22", "#9b59b6",
    "#1abc9c", "#e74c3c", "#f39c12", "#34495e",
]

# ── Data loading ───────────────────────────────────────────────────────

def load_data(mode_filter=None, strategy_filter=None, exchange_filter=None):
    """
    Load all CSVs into DataFrames and apply optional filters.

    mode_filter     : "PAPER" or "LIVE"  — applied to opens and hist
    strategy_filter : strategy name string
    exchange_filter : "INDEX", "MCX", etc.  — filters by exchange column
    """

    def read(path, parse_dates=[]):
        if not path.exists():
            return pd.DataFrame()
        df = pd.read_csv(path)
        for col in parse_dates:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")
        return df

    hist  = read(HISTORY_FILE, parse_dates=["entry_date", "exit_date"])
    daily = read(DAILY_FILE,   parse_dates=["date"])
    opens = read(OPEN_FILE,    parse_dates=["date"])

    # Convert numeric columns that may have been read as strings
    for col in ("pnl", "pnl_pct", "credit_collected", "exit_cost"):
        if col in hist.columns:
            hist[col] = pd.to_numeric(hist[col], errors="coerce")

    # Mode filter — applied to both opens and hist so summary stats are consistent.
    # hist has no mode column; we filter it via the open_positions mode where
    # trade_id appears, matching only trades opened in the requested mode.
    if mode_filter:
        if not opens.empty and "mode" in opens.columns:
            mode_ids = set(
                opens.loc[
                    opens["mode"].str.upper() == mode_filter.upper(), "trade_id"
                ]
            )
            opens = opens[opens["mode"].str.upper() == mode_filter.upper()].copy()
            if not hist.empty and "trade_id" in hist.columns:
                hist = hist[hist["trade_id"].isin(mode_ids)].copy()

    # Strategy filter
    if strategy_filter:
        if not hist.empty  and "strategy" in hist.columns:
            hist  = hist[hist["strategy"]   == strategy_filter].copy()
        if not daily.empty and "strategy" in daily.columns:
            daily = daily[daily["strategy"] == strategy_filter].copy()
        if not opens.empty and "strategy" in opens.columns:
            opens = opens[opens["strategy"] == strategy_filter].copy()

    # Exchange filter — only applies if the exchange column exists (new CSVs)
    if exchange_filter:
        if not hist.empty  and "exchange" in hist.columns:
            hist  = hist[hist["exchange"].str.upper() == exchange_filter.upper()].copy()
        if not opens.empty and "exchange" in opens.columns:
            opens = opens[opens["exchange"].str.upper() == exchange_filter.upper()].copy()

    return hist, daily, opens

# ── Chart builders ─────────────────────────────────────────────────────

def _plotly_layout(title="", height=380):
    return dict(
        title=dict(text=title, font=dict(color=CLR_TEXT, size=14)),
        paper_bgcolor=CLR_CARD,
        plot_bgcolor=CLR_CARD,
        font=dict(color=CLR_TEXT, family="Inter, Arial, sans-serif"),
        height=height,
        margin=dict(l=50, r=30, t=50, b=40),
        legend=dict(
            bgcolor=CLR_CARD, bordercolor=CLR_BORDER, borderwidth=1,
            font=dict(color=CLR_TEXT),
        ),
        xaxis=dict(gridcolor=CLR_BORDER, zerolinecolor=CLR_BORDER),
        yaxis=dict(gridcolor=CLR_BORDER, zerolinecolor=CLR_BORDER),
    )


def _empty_chart(title):
    fig = go.Figure()
    fig.add_annotation(
        text="No data available yet",
        xref="paper", yref="paper", x=0.5, y=0.5,
        showarrow=False, font=dict(color=CLR_MUTED, size=14),
    )
    fig.update_layout(**_plotly_layout(title))
    return fig


def chart_equity_curve(hist):
    if hist.empty or "exit_date" not in hist.columns:
        return _empty_chart("Equity Curve — No data yet")

    df = hist.dropna(subset=["exit_date", "pnl"]).copy()
    df["exit_date"] = pd.to_datetime(df["exit_date"])
    df = df.sort_values("exit_date")

    # Group by instrument so INDEX and MCX equity curves are separate lines
    group_col = "instrument" if "instrument" in df.columns else "strategy"
    groups    = df[group_col].unique()

    fig = go.Figure()
    for i, grp in enumerate(groups):
        sub = df[df[group_col] == grp].copy()
        sub["cumulative_pnl"] = sub["pnl"].cumsum()
        colour = STRATEGY_COLOURS[i % len(STRATEGY_COLOURS)]
        fig.add_trace(go.Scatter(
            x=sub["exit_date"], y=sub["cumulative_pnl"],
            mode="lines+markers", name=grp,
            line=dict(color=colour, width=2), marker=dict(size=5),
            hovertemplate=(
                "<b>%{x|%d %b %Y}</b><br>"
                "Cumulative P&L: &#x20B9;%{y:,.0f}"
                "<extra>%{fullData.name}</extra>"
            ),
        ))

    fig.update_layout(**_plotly_layout(
        "Equity Curve — Cumulative P&L by Instrument", height=400
    ))
    fig.update_xaxes(tickformat="%d %b")
    return fig


def chart_daily_pnl(daily):
    if daily.empty or "net_pnl" not in daily.columns:
        return _empty_chart("Daily P&L — No data yet")

    df = daily.dropna(subset=["date", "net_pnl"]).copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")

    fig = go.Figure()
    strategies = df["strategy"].unique() if "strategy" in df.columns else ["all"]

    for i, strat in enumerate(strategies):
        sub     = df[df["strategy"] == strat].copy() if "strategy" in df.columns else df.copy()
        colours = [CLR_PROFIT if v >= 0 else CLR_LOSS for v in sub["net_pnl"]]
        fig.add_trace(go.Bar(
            x=sub["date"], y=sub["net_pnl"],
            name=strat, marker_color=colours,
            hovertemplate=(
                "<b>%{x|%d %b %Y}</b><br>"
                "Net P&L: &#x20B9;%{y:,.0f}"
                "<extra>%{fullData.name}</extra>"
            ),
        ))

    fig.update_layout(**_plotly_layout("Daily Net P&L", height=360))
    fig.update_xaxes(tickformat="%d %b")
    fig.update_layout(barmode="relative")
    return fig


def chart_instrument_breakdown(hist):
    """
    Bar chart breaking down P&L, win rate, and trade count by instrument.
    Useful when trading both INDEX and MCX instruments simultaneously.
    """
    if hist.empty or "instrument" not in hist.columns:
        return _empty_chart("Instrument Breakdown — No data yet")

    grp = hist.groupby("instrument").agg(
        total_trades=("pnl", "count"),
        total_pnl   =("pnl", "sum"),
        win_rate    =("pnl", lambda x: (x > 0).mean()),
        avg_pnl     =("pnl", "mean"),
    ).reset_index()

    # Add exchange label if available
    if "exchange" in hist.columns:
        exch = hist.groupby("instrument")["exchange"].first().reset_index()
        grp  = grp.merge(exch, on="instrument", how="left")
        grp["label"] = grp["instrument"] + "\n(" + grp["exchange"].fillna("?") + ")"
    else:
        grp["label"] = grp["instrument"]

    fig = make_subplots(
        rows=1, cols=3,
        subplot_titles=["Total P&L (&#x20B9;)", "Win Rate (%)", "Avg P&L / Trade (&#x20B9;)"],
    )
    colours = STRATEGY_COLOURS[:len(grp)]

    fig.add_trace(go.Bar(
        x=grp["label"], y=grp["total_pnl"], marker_color=colours, name="Total P&L",
        hovertemplate="%{x}<br>&#x20B9;%{y:,.0f}<extra></extra>",
    ), row=1, col=1)
    fig.add_trace(go.Bar(
        x=grp["label"], y=(grp["win_rate"] * 100).round(1), marker_color=colours, name="Win Rate",
        hovertemplate="%{x}<br>%{y:.1f}%<extra></extra>",
    ), row=1, col=2)
    fig.add_trace(go.Bar(
        x=grp["label"], y=grp["avg_pnl"].round(0), marker_color=colours, name="Avg P&L",
        hovertemplate="%{x}<br>&#x20B9;%{y:,.0f}<extra></extra>",
    ), row=1, col=3)

    fig.update_layout(**_plotly_layout("Instrument Breakdown", height=360))
    fig.update_layout(showlegend=False)
    for ax in ["xaxis", "xaxis2", "xaxis3"]:
        fig.update_layout(**{ax: dict(gridcolor=CLR_BORDER)})
    for ax in ["yaxis", "yaxis2", "yaxis3"]:
        fig.update_layout(**{ax: dict(gridcolor=CLR_BORDER)})
    return fig


def chart_strategy_comparison(hist):
    if hist.empty or "strategy" not in hist.columns:
        return _empty_chart("Strategy Comparison — No data yet")

    grp = hist.groupby("strategy").agg(
        total_trades=("pnl", "count"),
        total_pnl   =("pnl", "sum"),
        win_rate    =("pnl", lambda x: (x > 0).mean()),
        avg_pnl     =("pnl", "mean"),
    ).reset_index()

    fig = make_subplots(
        rows=1, cols=3,
        subplot_titles=["Total P&L (&#x20B9;)", "Win Rate (%)", "Avg P&L / Trade (&#x20B9;)"],
    )
    colours = STRATEGY_COLOURS[:len(grp)]

    fig.add_trace(go.Bar(
        x=grp["strategy"], y=grp["total_pnl"], marker_color=colours, name="Total P&L",
        hovertemplate="%{x}<br>&#x20B9;%{y:,.0f}<extra></extra>",
    ), row=1, col=1)
    fig.add_trace(go.Bar(
        x=grp["strategy"], y=(grp["win_rate"] * 100).round(1), marker_color=colours, name="Win Rate",
        hovertemplate="%{x}<br>%{y:.1f}%<extra></extra>",
    ), row=1, col=2)
    fig.add_trace(go.Bar(
        x=grp["strategy"], y=grp["avg_pnl"].round(0), marker_color=colours, name="Avg P&L",
        hovertemplate="%{x}<br>&#x20B9;%{y:,.0f}<extra></extra>",
    ), row=1, col=3)

    fig.update_layout(**_plotly_layout("Strategy Comparison", height=380))
    fig.update_layout(showlegend=False)
    for ax in ["xaxis", "xaxis2", "xaxis3"]:
        fig.update_layout(**{ax: dict(gridcolor=CLR_BORDER)})
    for ax in ["yaxis", "yaxis2", "yaxis3"]:
        fig.update_layout(**{ax: dict(gridcolor=CLR_BORDER)})
    return fig


def chart_pnl_distribution(hist):
    if hist.empty or "pnl" not in hist.columns:
        return _empty_chart("P&L Distribution — No data yet")

    fig       = go.Figure()
    group_col = "instrument" if "instrument" in hist.columns else "strategy"
    groups    = hist[group_col].unique()

    for i, grp in enumerate(groups):
        sub = hist[hist[group_col] == grp]["pnl"]
        fig.add_trace(go.Histogram(
            x=sub, name=grp,
            marker_color=STRATEGY_COLOURS[i % len(STRATEGY_COLOURS)],
            opacity=0.75, nbinsx=20,
            hovertemplate=(
                "P&L: &#x20B9;%{x:,.0f}<br>Count: %{y}"
                "<extra>%{fullData.name}</extra>"
            ),
        ))

    fig.update_layout(**_plotly_layout("P&L Distribution per Trade", height=340))
    fig.update_layout(barmode="overlay")
    return fig


def chart_win_loss_pie(hist):
    if hist.empty or "pnl" not in hist.columns:
        return _empty_chart("Win/Loss — No data yet")

    wins   = int((hist["pnl"] > 0).sum())
    losses = int((hist["pnl"] <= 0).sum())

    fig = go.Figure(go.Pie(
        labels=["Wins", "Losses"], values=[wins, losses],
        hole=0.55, marker_colors=[CLR_PROFIT, CLR_LOSS],
        textinfo="label+percent",
        hovertemplate="%{label}: %{value} trades (%{percent})<extra></extra>",
    ))
    fig.update_layout(**_plotly_layout("Overall Win / Loss", height=320))
    return fig

# ── Summary stats ──────────────────────────────────────────────────────

def build_summary_stats(hist, opens):
    if hist.empty:
        return {
            "total_pnl": 0, "total_trades": 0, "win_rate": 0,
            "avg_pnl": 0, "best_trade": 0, "worst_trade": 0,
            "open_positions": 0,
        }

    pnl   = pd.to_numeric(hist["pnl"], errors="coerce").dropna()
    wins  = (pnl > 0).sum()
    total = len(pnl)

    # NaN-safe status check — fillna so .str.upper() never crashes
    if not opens.empty and "status" in opens.columns:
        open_count = (
            opens["status"].fillna("").str.upper() == "OPEN"
        ).sum()
    else:
        open_count = 0

    return {
        "total_pnl":      round(float(pnl.sum()),  2),
        "total_trades":   total,
        "win_rate":       round(wins / total * 100, 1) if total else 0,
        "avg_pnl":        round(float(pnl.mean()),  2) if total else 0,
        "best_trade":     round(float(pnl.max()),   2) if total else 0,
        "worst_trade":    round(float(pnl.min()),   2) if total else 0,
        "open_positions": int(open_count),
    }

# ── Table HTML builders ────────────────────────────────────────────────

def _fmt_pnl(val):
    try:
        v      = float(val)
        colour = CLR_PROFIT if v >= 0 else CLR_LOSS
        return '<span style="color:{};font-weight:600">&#x20B9;{:,.0f}</span>'.format(colour, v)
    except (ValueError, TypeError):
        return str(val)


def _fmt_pct(val):
    try:
        v      = float(val) * 100
        colour = CLR_PROFIT if v >= 0 else CLR_LOSS
        return '<span style="color:{}">{}%</span>'.format(colour, round(v, 1))
    except (ValueError, TypeError):
        return str(val)


def build_history_table(hist):
    if hist.empty:
        return "<p style='color:{};padding:20px'>No closed trades yet.</p>".format(CLR_MUTED)

    df = hist.copy()
    if "exit_date" in df.columns:
        df = df.sort_values("exit_date", ascending=False)

    cols_show = [
        ("trade_id",         "Trade ID"),
        ("instrument",       "Instrument"),
        ("exchange",         "Exchange"),      # ← new
        ("strategy",         "Strategy"),
        ("entry_date",       "Entry"),
        ("exit_date",        "Exit"),
        ("ce_strike",        "CE Strike"),
        ("pe_strike",        "PE Strike"),
        ("credit_collected", "Credit (&#x20B9;)"),
        ("exit_cost",        "Exit Cost (&#x20B9;)"),
        ("pnl",              "P&L (&#x20B9;)"),
        ("pnl_pct",          "P&L %"),
        ("exit_reason",      "Exit Reason"),
        ("days_held",        "Days"),
    ]
    # Only show columns that exist in this CSV (graceful for old files without exchange)
    available = [(c, h) for c, h in cols_show if c in df.columns]

    rows_html = ""
    for _, row in df.iterrows():
        pnl_val = float(row.get("pnl", 0) or 0)
        row_bg  = "rgba(46,204,113,0.05)" if pnl_val >= 0 else "rgba(231,76,60,0.05)"
        cells   = ""
        for col, _ in available:
            val = row.get(col, "")
            if col == "pnl":
                val = _fmt_pnl(val)
            elif col == "pnl_pct":
                val = _fmt_pct(val)
            elif col in ("credit_collected", "exit_cost"):
                try:
                    val = "&#x20B9;{:,.0f}".format(float(val))
                except (ValueError, TypeError):
                    pass
            elif col in ("entry_date", "exit_date") and pd.notna(val):
                try:
                    val = pd.to_datetime(val).strftime("%d %b %Y")
                except Exception:
                    pass
            cells += "<td>{}</td>".format(val)
        rows_html += '<tr style="background:{}">{}</tr>'.format(row_bg, cells)

    headers = "".join("<th>{}</th>".format(h) for _, h in available)
    return """
<div style="overflow-x:auto">
  <table class="data-table" id="history-table">
    <thead><tr>{}</tr></thead>
    <tbody>{}</tbody>
  </table>
</div>""".format(headers, rows_html)


def build_open_table(opens):
    if opens.empty:
        return "<p style='color:{};padding:20px'>No open positions.</p>".format(CLR_MUTED)

    df = opens[
        opens["status"].fillna("").str.upper() == "OPEN"
    ].copy() if "status" in opens.columns else opens.copy()

    if df.empty:
        return "<p style='color:{};padding:20px'>No open positions.</p>".format(CLR_MUTED)

    cols_show = [
        ("trade_id",         "Trade ID"),
        ("date",             "Entry Date"),
        ("instrument",       "Instrument"),
        ("exchange",         "Exchange"),      # ← new
        ("strategy",         "Strategy"),
        ("ce_symbol",        "CE Symbol"),
        ("ce_strike",        "CE Strike"),
        ("ce_entry_premium", "CE Prem (&#x20B9;)"),
        ("pe_symbol",        "PE Symbol"),
        ("pe_strike",        "PE Strike"),
        ("pe_entry_premium", "PE Prem (&#x20B9;)"),
        ("total_credit",     "Total Credit (&#x20B9;)"),
        ("expiry",           "Expiry"),
        ("mode",             "Mode"),
    ]
    available = [(c, h) for c, h in cols_show if c in df.columns]

    rows_html = ""
    for _, row in df.iterrows():
        cells = ""
        for col, _ in available:
            val = row.get(col, "")
            if col in ("total_credit", "ce_entry_premium", "pe_entry_premium"):
                try:
                    val = "&#x20B9;{:,.0f}".format(float(val))
                except (ValueError, TypeError):
                    pass
            elif col == "date" and pd.notna(val):
                try:
                    val = pd.to_datetime(val).strftime("%d %b %Y")
                except Exception:
                    pass
            cells += "<td>{}</td>".format(val)
        rows_html += "<tr>{}</tr>".format(cells)

    headers = "".join("<th>{}</th>".format(h) for _, h in available)
    return """
<div style="overflow-x:auto">
  <table class="data-table">
    <thead><tr>{}</tr></thead>
    <tbody>{}</tbody>
  </table>
</div>""".format(headers, rows_html)


def build_strategy_summary_table(hist):
    if hist.empty or "strategy" not in hist.columns:
        return ""

    grp = hist.groupby("strategy").agg(
        Trades   =("pnl", "count"),
        Total_PnL=("pnl", "sum"),
        Win_Rate =("pnl", lambda x: round((x > 0).mean() * 100, 1)),
        Avg_PnL  =("pnl", "mean"),
        Best     =("pnl", "max"),
        Worst    =("pnl", "min"),
    ).reset_index().rename(columns={"strategy": "Strategy"})

    rows_html = ""
    for _, row in grp.iterrows():
        rows_html += """
    <tr>
      <td><strong>{}</strong></td>
      <td>{}</td><td>{}</td><td>{}%</td>
      <td>{}</td><td>{}</td><td>{}</td>
    </tr>""".format(
            row["Strategy"], int(row["Trades"]),
            _fmt_pnl(row["Total_PnL"]), row["Win_Rate"],
            _fmt_pnl(row["Avg_PnL"]), _fmt_pnl(row["Best"]), _fmt_pnl(row["Worst"]),
        )

    return """
<div style="overflow-x:auto">
  <table class="data-table">
    <thead><tr>
      <th>Strategy</th><th>Trades</th><th>Total P&L</th>
      <th>Win Rate</th><th>Avg P&L</th><th>Best</th><th>Worst</th>
    </tr></thead>
    <tbody>{}</tbody>
  </table>
</div>""".format(rows_html)

# ── HTML template ──────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Options Trading Report — {report_date}</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  background: {bg}; color: {text};
  font-family: Inter, -apple-system, Arial, sans-serif;
  font-size: 14px; line-height: 1.5;
}}
.header {{
  background: linear-gradient(135deg, #1a2035 0%, #0f1117 100%);
  border-bottom: 1px solid {border};
  padding: 24px 32px; display: flex;
  align-items: center; justify-content: space-between;
  flex-wrap: wrap; gap: 12px;
}}
.header h1 {{ font-size: 22px; font-weight: 700; color: #fff; }}
.header .meta {{ font-size: 12px; color: {muted}; }}
.mode-badge {{
  padding: 4px 12px; border-radius: 20px; font-size: 11px;
  font-weight: 700; letter-spacing: 0.5px;
}}
.mode-paper {{ background: rgba(230,126,34,0.2); color: #e67e22; border: 1px solid #e67e22; }}
.mode-live  {{ background: rgba(46,204,113,0.2); color: #2ecc71; border: 1px solid #2ecc71; }}
.container {{ max-width: 1400px; margin: 0 auto; padding: 24px 20px; }}
.section {{ margin-bottom: 32px; }}
.section-title {{
  font-size: 13px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 1px; color: {muted}; margin-bottom: 16px;
  padding-bottom: 8px; border-bottom: 1px solid {border};
}}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; }}
.card {{
  background: {card}; border: 1px solid {border}; border-radius: 10px;
  padding: 20px 16px; text-align: center;
}}
.card .label {{ font-size: 11px; color: {muted}; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }}
.card .value {{ font-size: 24px; font-weight: 700; }}
.card .value.positive {{ color: {profit}; }}
.card .value.negative {{ color: {loss}; }}
.card .value.neutral  {{ color: {neutral}; }}
.chart-grid {{ display: grid; gap: 16px; }}
.chart-box {{ background: {card}; border: 1px solid {border}; border-radius: 10px; overflow: hidden; }}
.data-table {{ width: 100%; border-collapse: collapse; font-size: 12.5px; }}
.data-table th {{
  background: rgba(255,255,255,0.05); color: {muted};
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px;
  padding: 10px 12px; text-align: left; border-bottom: 1px solid {border};
  position: sticky; top: 0; white-space: nowrap;
}}
.data-table td {{ padding: 9px 12px; border-bottom: 1px solid rgba(255,255,255,0.04); white-space: nowrap; }}
.data-table tr:hover td {{ background: rgba(255,255,255,0.04); }}
.table-box {{
  background: {card}; border: 1px solid {border}; border-radius: 10px;
  overflow: hidden; max-height: 480px; overflow-y: auto;
}}
.search-bar {{
  width: 100%; padding: 10px 14px; margin-bottom: 12px;
  background: {card}; border: 1px solid {border}; border-radius: 8px;
  color: {text}; font-size: 13px; outline: none;
}}
.search-bar:focus {{ border-color: {neutral}; }}
@media (max-width: 768px) {{
  .chart-grid.cols-2 {{ grid-template-columns: 1fr; }}
  .cards {{ grid-template-columns: repeat(2, 1fr); }}
}}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>&#x1F4C8; Options Trading Report</h1>
    <div class="meta">Generated: {generated_at} &nbsp;|&nbsp; Data up to: {data_end}</div>
  </div>
  <span class="mode-badge {mode_class}">{mode_label}</span>
</div>

<div class="container">

  <div class="section">
    <div class="section-title">Summary</div>
    <div class="cards">
      <div class="card"><div class="label">Total Realised P&L</div><div class="value {pnl_class}">&#x20B9;{total_pnl:,.0f}</div></div>
      <div class="card"><div class="label">Total Trades</div><div class="value neutral">{total_trades}</div></div>
      <div class="card"><div class="label">Win Rate</div><div class="value neutral">{win_rate}%</div></div>
      <div class="card"><div class="label">Avg P&L / Trade</div><div class="value {avg_class}">&#x20B9;{avg_pnl:,.0f}</div></div>
      <div class="card"><div class="label">Best Trade</div><div class="value positive">&#x20B9;{best_trade:,.0f}</div></div>
      <div class="card"><div class="label">Worst Trade</div><div class="value negative">&#x20B9;{worst_trade:,.0f}</div></div>
      <div class="card"><div class="label">Open Positions</div><div class="value neutral">{open_positions}</div></div>
    </div>
  </div>

  <div class="section">
    <div class="section-title">Instrument Breakdown</div>
    <div class="chart-box">{chart_instrument}</div>
  </div>

  <div class="section">
    <div class="section-title">Strategy Comparison</div>
    <div class="chart-box">{chart_strategy}</div>
    <br>{strategy_table}
  </div>

  <div class="section">
    <div class="section-title">Performance Over Time</div>
    <div class="chart-grid" style="grid-template-columns:2fr 1fr">
      <div class="chart-box">{chart_equity}</div>
      <div class="chart-box">{chart_pie}</div>
    </div>
    <br>
    <div class="chart-box">{chart_daily}</div>
  </div>

  <div class="section">
    <div class="section-title">P&L Distribution</div>
    <div class="chart-box">{chart_dist}</div>
  </div>

  <div class="section">
    <div class="section-title">Open Positions ({open_positions})</div>
    <div class="table-box">{open_table}</div>
  </div>

  <div class="section">
    <div class="section-title">Trade History ({total_trades} closed trades)</div>
    <input class="search-bar" type="text" id="hist-search"
           placeholder="Filter by trade ID, instrument, exchange, strategy, exit reason..."
           oninput="filterTable()">
    <div class="table-box">{history_table}</div>
  </div>

</div>

<script>
function filterTable() {{
  const q = document.getElementById("hist-search").value.toLowerCase();
  document.querySelectorAll("#history-table tbody tr").forEach(row => {{
    row.style.display = row.textContent.toLowerCase().includes(q) ? "" : "none";
  }});
}}
</script>
</body>
</html>"""

# ── Main report builder ────────────────────────────────────────────────

def build_report(output_path, mode_filter=None, strategy_filter=None,
                 exchange_filter=None):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    hist, daily, opens = load_data(mode_filter, strategy_filter, exchange_filter)
    stats = build_summary_stats(hist, opens)

    # Mode badge
    if mode_filter:
        mode_label = "● {}".format(mode_filter.upper())
        mode_class = "mode-paper" if mode_filter.upper() == "PAPER" else "mode-live"
    elif not opens.empty and "mode" in opens.columns:
        modes      = opens["mode"].dropna().unique()
        mode_label = " + ".join(modes) if len(modes) else "ALL"
        mode_class = "mode-paper" if "PAPER" in modes else "mode-live"
    else:
        mode_label = "ALL"
        mode_class = "mode-paper"

    # Exchange badge suffix
    if exchange_filter:
        mode_label += " / {}".format(exchange_filter.upper())

    data_end = "—"
    if not hist.empty and "exit_date" in hist.columns:
        last = hist["exit_date"].dropna().max()
        if pd.notna(last):
            data_end = pd.to_datetime(last).strftime("%d %b %Y")

    def _fig_html(fig):
        return pio.to_html(
            fig, full_html=False, include_plotlyjs=False,
            config={"displaylogo": False, "responsive": True},
        )

    html = HTML_TEMPLATE.format(
        report_date    = date.today().strftime("%d %b %Y"),
        generated_at   = datetime.now().strftime("%d %b %Y %H:%M"),
        data_end       = data_end,
        mode_label     = mode_label,
        mode_class     = mode_class,
        bg=CLR_BG, card=CLR_CARD, text=CLR_TEXT,
        muted=CLR_MUTED, border=CLR_BORDER,
        profit=CLR_PROFIT, loss=CLR_LOSS, neutral=CLR_NEUTRAL,
        total_pnl      = stats["total_pnl"],
        total_trades   = stats["total_trades"],
        win_rate       = stats["win_rate"],
        avg_pnl        = stats["avg_pnl"],
        best_trade     = stats["best_trade"],
        worst_trade    = stats["worst_trade"],
        open_positions = stats["open_positions"],
        pnl_class      = "positive" if stats["total_pnl"] >= 0 else "negative",
        avg_class      = "positive" if stats["avg_pnl"]   >= 0 else "negative",
        chart_instrument = _fig_html(chart_instrument_breakdown(hist)),
        chart_equity     = _fig_html(chart_equity_curve(hist)),
        chart_daily      = _fig_html(chart_daily_pnl(daily)),
        chart_strategy   = _fig_html(chart_strategy_comparison(hist)),
        chart_dist       = _fig_html(chart_pnl_distribution(hist)),
        chart_pie        = _fig_html(chart_win_loss_pie(hist)),
        strategy_table   = build_strategy_summary_table(hist),
        open_table       = build_open_table(opens),
        history_table    = build_history_table(hist),
    )

    output_path.write_text(html, encoding="utf-8")
    print("Report generated: {}".format(output_path))
    return output_path

# ── CLI ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate options trading HTML report"
    )
    parser.add_argument(
        "--out", default=None,
        help="Output file path (default: reports/report_YYYY-MM-DD.html)",
    )
    parser.add_argument(
        "--mode", default=None,
        help="Filter by trading mode: PAPER or LIVE",
    )
    parser.add_argument(
        "--strategy", default=None,
        help="Filter by strategy name (e.g. shortStrangle_Adjust)",
    )
    parser.add_argument(
        "--exchange", default=None,
        help="Filter by exchange: INDEX, MCX, NFO, etc.",
    )
    args = parser.parse_args()

    if args.out:
        out = Path(args.out)
    else:
        filename = "report_{}.html".format(date.today().strftime("%Y-%m-%d"))
        out = REPORTS_DIR / filename

    build_report(
        out,
        mode_filter     = args.mode,
        strategy_filter = args.strategy,
        exchange_filter = args.exchange,
    )