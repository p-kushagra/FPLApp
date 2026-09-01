"""Shared Streamlit widgets for the v2 decision pages.

The only module in the package that imports Streamlit. Pages compose these;
services never import them, which is what keeps a future FastAPI/React front end
a pure addition rather than a rewrite (ADR-001).
"""
from __future__ import annotations

import contextlib
import traceback
from collections.abc import Iterable

import pandas as pd
import streamlit as st

from ..services.degrade import DataQuality


# --------------------------------------------------------------------------
# Error boundary
# --------------------------------------------------------------------------
@contextlib.contextmanager
def error_boundary(label: str, *, quality: DataQuality | None = None,
                   fatal: bool = False):
    """Contain a failure to one panel.

    A missing player attribute or an empty rival query must degrade that panel,
    never take down the page. The operator gets a named card with the detail
    folded away -- enough to act on, without a traceback as the whole UI.
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001 - containing the blast radius is the point
        st.error(f"**{label}** could not be rendered - {type(exc).__name__}: {exc}")
        with st.expander("Detail"):
            st.code("".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__))[-3000:], language="text")
            if quality is not None and quality.notes:
                st.caption(" | ".join(quality.notes))
        if fatal:
            st.stop()


def guard(label: str):
    """Decorator form of `error_boundary`, for render functions."""
    def wrap(fn):
        def inner(*a, **kw):
            with error_boundary(label):
                return fn(*a, **kw)
        inner.__name__ = fn.__name__
        return inner
    return wrap


# --------------------------------------------------------------------------
# Skeletons and empty states
# --------------------------------------------------------------------------
def skeleton_table(rows: int = 6, cols: int = 5, label: str = "Loading") -> None:
    """Placeholder at the final table's dimensions.

    Rendering a fixed-size block rather than nothing keeps the page from
    reflowing when real data arrives -- the layout shift is what makes a slow
    panel feel broken rather than merely slow.
    """
    st.caption(f"{label}...")
    st.dataframe(
        pd.DataFrame(
            [{f"col{c}": "     " for c in range(cols)}
             for _ in range(rows)]
        ),
        width='stretch', hide_index=True,
    )


def skeleton_cards(n: int = 3, height: int = 260) -> None:
    for col in st.columns(n):
        with col, st.container(border=True, height=height):
            st.caption("Solving...")
            st.progress(0.0)


def empty_state(title: str, action: str, icon: str = "\U0001F4A1") -> None:
    """An empty panel must always name the exact action that fills it."""
    with st.container(border=True):
        st.markdown(f"{icon} **{title}**")
        st.caption(action)


# --------------------------------------------------------------------------
# Headers and quality badges
# --------------------------------------------------------------------------
_PHASE_STYLE = {
    "LIVE": ("\U0001F534 LIVE", "Matches in progress - transfers closed"),
    "SETTLING": ("\U0001F7E1 SETTLING", "Bonus and auto-subs not final"),
    "UPCOMING": ("\U0001F7E2 UPCOMING", "Transfers open"),
    "PRE_SEASON": ("⚪ PRE-SEASON", "No gameweek scoring yet"),
}


def temporal_header(state, *, planning: bool = False,
                    horizon: int | None = None) -> None:
    """Gameweek context strip. `state` is a temporal.GWState."""
    badge, note = _PHASE_STYLE.get(state.phase.value, ("⚪", ""))

    if planning:
        window = state.planning_window(horizon or 5)
        headline = f"Planning GW{window[0]}–GW{window[-1]}"
    else:
        headline = f"GW{state.scoring_gw}"

    left, right = st.columns([3, 2])
    left.markdown(f"### {headline} &nbsp; {badge}", unsafe_allow_html=True)

    countdown = ""
    if state.seconds_to_deadline is not None and state.seconds_to_deadline > 0:
        secs = int(state.seconds_to_deadline)
        days, rem = divmod(secs, 86400)
        hours, rem = divmod(rem, 3600)
        countdown = (f" · deadline in {days}d {hours}h"
                     if days else f" · deadline in {hours}h {rem // 60}m")
    right.caption(f"{note}{countdown}")


def quality_bar(quality: DataQuality, *, show_ok: bool = False) -> None:
    """Global badge strip. Panel-level badges are separate and additional."""
    badges: list[str] = []

    if quality.understat_badge:
        badges.append(f"⚠ {quality.understat_badge}")
    elif quality.on_baseline:
        badges.append("ℹ Baseline stats (no Understat data ingested)")

    if quality.stale_projections and quality.projection_age_hours:
        badges.append(f"⚠ Projections {quality.projection_age_hours:.0f}h old")

    for name, state in sorted(quality.sources.items()):
        if state.quality == "down" and name != "understat":
            badges.append(f"⚠ {name} unavailable")

    if not badges:
        if show_ok:
            st.caption("✅ All sources fresh")
        return

    st.warning(" · ".join(badges))


def panel_badge(text: str, kind: str = "info") -> None:
    """A badge pinned to one panel, at the point of use."""
    icon = {"warn": "⚠", "info": "ℹ", "ok": "✅"}.get(kind, "ℹ")
    st.caption(f"{icon} {text}")


def metric_card(col, label: str, value, delta=None, help_text: str | None = None,
                caption: str | None = None) -> None:
    with col:
        st.metric(label, value, delta=delta, help=help_text)
        if caption:
            st.caption(caption)


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------
def safe_frame(rows: Iterable[dict], columns: list[str] | None = None
               ) -> pd.DataFrame:
    """Build a DataFrame that tolerates missing keys.

    Solver and projection rows are assembled from several joins; one absent
    attribute must not raise mid-render. Missing columns arrive as blanks.
    """
    data = list(rows)
    if not data:
        return pd.DataFrame(columns=columns or [])
    frame = pd.DataFrame(data)
    if columns:
        for col in columns:
            if col not in frame.columns:
                frame[col] = None
        frame = frame[columns]
    return frame


def dataframe(rows: Iterable[dict], columns: list[str] | None = None,
              column_config: dict | None = None, height: int | None = None,
              empty: str = "Nothing to show.") -> None:
    frame = safe_frame(rows, columns)
    if frame.empty:
        st.caption(empty)
        return
    # Streamlit 1.62 rejects height=None outright, so branch rather than
    # unpacking kwargs -- **kwargs also defeats the overload resolution and
    # hides real type errors in the call.
    if height:
        st.dataframe(frame, width="stretch", hide_index=True,
                     column_config=column_config or {}, height=height)
    else:
        st.dataframe(frame, width="stretch", hide_index=True,
                     column_config=column_config or {})


def assumptions_drawer(items: dict[str, object], title: str = "Assumptions in play"
                       ) -> None:
    """Every weight and constant behind what is on screen.

    A recommendation the operator cannot interrogate is not prescriptive, it is
    just assertive.
    """
    with st.expander(title):
        st.dataframe(
            pd.DataFrame([{"Setting": k, "Value": str(v)} for k, v in items.items()]),
            width='stretch', hide_index=True,
        )
