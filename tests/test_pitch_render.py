"""Headless render checks for the tactical pitch -- Step 1 of the sandbox work.

Everything else in the suite asserts on the figure OBJECT. That catches wrong
numbers and misses wrong pictures: the shot map shipped stretched for weeks
because `scaleanchor` without `constrain="domain"` is invisible in the figure
spec and obvious the moment you look at a PNG. These tests rasterise through
kaleido at the two container widths the page is actually used at, and assert
on the geometry that only exists once something has been laid out.

kaleido is a dev dependency, so every test here skips cleanly without it rather
than failing a machine that only installed `requirements.txt`.

What is checked, and why each one is a real failure mode:

* the export succeeds at all -- a figure that raises on render is broken
* aspect and node spacing survive a 2x container width change
* no node collides with another, at the widest formation the rules allow
* nodes stay inside the pitch, including the bench rail and its FDR pills
"""
from __future__ import annotations

import math
from itertools import pairwise

import pytest

from fpl_assistant.ui import charts
from fpl_assistant.ui import pitch as pitch_mod

pytestmark = pytest.mark.skipif(not charts.available(), reason="needs plotly")

DESKTOP_PX = 1400
TABLET_PX = 700
HEIGHT_PX = 620


def _kaleido() -> bool:
    try:
        import kaleido  # noqa: F401
    except ImportError:
        return False
    return True


needs_kaleido = pytest.mark.skipif(
    not _kaleido(), reason="kaleido not installed (dev dependency)")


def _squad(defs: int = 4, mids: int = 4, fwds: int = 2) -> list:
    """A legal 15 in the requested shape, with realistic node content."""
    players, pid = [], 1
    players.append(pitch_mod.PitchPlayer(
        pid, "Raya", "GKP", team="ARS", cost=5.6, starting=True,
        xp=4.1, next_fdr=2, next_opponent="BUR (H)"))
    pid += 1
    for position, count in (("DEF", defs), ("MID", mids), ("FWD", fwds)):
        for i in range(count):
            players.append(pitch_mod.PitchPlayer(
                pid, f"Player{pid}", position, team="TOT",
                cost=4.5 + i, starting=True, xp=3.0 + i * 0.7,
                next_fdr=(i % 5) + 1, next_opponent="MCI (A)",
                badges=["OOP"] if i == 0 else []))
            pid += 1
    bench = [("GKP", 0), ("DEF", 1), ("MID", 2), ("FWD", 3)]
    for position, order in bench[:15 - len(players)]:
        players.append(pitch_mod.PitchPlayer(
            pid, f"Sub{pid}", position, team="EVE", cost=4.0,
            starting=False, bench_order=order, xp=1.2,
            next_fdr=4, next_opponent="LIV (A)"))
        pid += 1
    players[1].is_captain = True
    players[2].is_vice = True
    return players


# ==========================================================================
# Rasterised export
# ==========================================================================
@needs_kaleido
@pytest.mark.parametrize("width", [DESKTOP_PX, TABLET_PX])
def test_pitch_exports_at_both_container_widths(tmp_path, width):
    """The figure must actually rasterise, at desktop and at tablet."""
    out = tmp_path / f"pitch_{width}.png"
    fig = pitch_mod.figure(_squad(), height=HEIGHT_PX)
    fig.write_image(str(out), width=width, height=HEIGHT_PX)

    assert out.exists() and out.stat().st_size > 5_000, (
        "the export produced no meaningful image")


@needs_kaleido
@pytest.mark.parametrize("formation", [(3, 4, 3), (3, 5, 2), (4, 4, 2),
                                       (4, 3, 3), (5, 3, 2), (5, 4, 1)])
def test_every_legal_formation_renders(tmp_path, formation):
    """All six shapes the spec names, rasterised rather than merely built."""
    defs, mids, fwds = formation
    fig = pitch_mod.figure(_squad(defs, mids, fwds), height=HEIGHT_PX)
    out = tmp_path / f"pitch_{defs}{mids}{fwds}.png"
    fig.write_image(str(out), width=TABLET_PX, height=HEIGHT_PX)
    assert out.stat().st_size > 5_000


@needs_kaleido
def test_selected_and_detailed_variants_render(tmp_path):
    squad = _squad()
    fig = pitch_mod.figure(squad, height=HEIGHT_PX,
                           selected_id=squad[3].player_id,
                           density=pitch_mod.DENSITY_DETAILED)
    out = tmp_path / "pitch_detailed.png"
    fig.write_image(str(out), width=TABLET_PX, height=HEIGHT_PX)
    assert out.stat().st_size > 5_000


# ==========================================================================
# Geometry -- the reason the render matters
# ==========================================================================
class TestPitchGeometry:
    def test_nodes_never_collide_in_the_widest_band(self):
        """A back five is the tightest legal row; nodes must still clear.

        Node spacing is in axis units and node size is in pixels, so the two
        only meet once a width is chosen. At the narrow container a five-band
        is where they first threaten to overlap.
        """
        squad = _squad(5, 4, 1)
        placed = pitch_mod._layout(squad)
        # x is 0-100 across a plot roughly `TABLET_PX` wide.
        px_per_unit = TABLET_PX / 108.0        # xaxis range is [-4, 104]
        for position in ("DEF", "MID", "FWD"):
            band = sorted(x for p, x, _y in placed
                          if p.starting and p.position == position)
            for left, right in pairwise(band):
                gap_px = (right - left) * px_per_unit
                assert gap_px >= pitch_mod.NODE_PX, (
                    f"{position} nodes are {gap_px:.0f}px apart at "
                    f"{TABLET_PX}px wide but are {pitch_mod.NODE_PX}px across")

    def test_every_node_sits_inside_the_drawn_area(self):
        """Including the FDR pill above and the badge line below each node."""
        placed = pitch_mod._layout(_squad(5, 4, 1))
        for _player, x, y in placed:
            assert -4 <= x <= 104, f"node at x={x} is outside the pitch"
            # Pills sit at y + 7.4 and badges at y - 6.6.
            assert -6 <= y - 6.6 and y + 7.4 <= 108, (
                f"node furniture at y={y} escapes the drawn range")

    def test_bench_is_below_every_outfield_band(self):
        placed = pitch_mod._layout(_squad())
        starters = [y for p, _x, y in placed if p.starting]
        bench = [y for p, _x, y in placed if not p.starting]
        assert min(bench) > max(starters)

    def test_bench_rail_is_ordered_keeper_first(self):
        """Auto-sub priority is the only thing the bench encodes."""
        squad = _squad()
        rail = pitch_mod._bench_order(squad)
        assert rail[0].position == "GKP", "the keeper must head the bench rail"
        outfield = [p.bench_order for p in rail[1:]]
        assert outfield == sorted(outfield), "bench 1/2/3 is out of order"

    def test_formation_snaps_to_the_squad_it_is_given(self):
        for defs, mids, fwds in ((3, 4, 3), (5, 3, 2), (4, 3, 3)):
            squad = _squad(defs, mids, fwds)
            starters = [p for p in squad if p.starting]
            assert pitch_mod.formation_string(starters) == f"{defs}-{mids}-{fwds}"

    def test_bands_are_evenly_spread_and_centred(self):
        """A back three and a back five must both look deliberate."""
        for count in (3, 4, 5):
            placed = pitch_mod._layout(_squad(count, 4, 1))
            band = sorted(x for p, x, _y in placed
                          if p.starting and p.position == "DEF")
            assert len(band) == count
            assert band[0] + band[-1] == pytest.approx(100.0), "band off-centre"
            gaps = [b - a for a, b in pairwise(band)]
            assert all(g == pytest.approx(gaps[0]) for g in gaps), \
                "uneven spacing inside a band"


# ==========================================================================
# Click mapping -- the pitch is an input, not just a picture
# ==========================================================================
class TestClickMapping:
    def test_node_index_matches_the_plotted_order(self):
        """A click is (curve, point); it must resolve to the right player.

        If this ordering drifts from the one `figure()` plots, every click
        selects the wrong player -- and it would look like a state bug rather
        than a layout one.
        """
        squad = _squad()
        fig = pitch_mod.figure(squad)
        mapping = pitch_mod.node_player_ids(squad)

        assert len(mapping) == len(fig.data)
        for trace, ids in zip(fig.data, mapping):
            assert len(trace.x) == len(ids)
            # customdata carries the name at index 6; check it lines up.
            by_id = {p.player_id: p for p in squad}
            for point, pid in zip(trace.customdata, ids):
                assert point[6] == by_id[pid].name

    def test_mapping_survives_a_formation_change(self):
        for shape in ((3, 4, 3), (5, 4, 1)):
            squad = _squad(*shape)
            flat = [pid for trace in pitch_mod.node_player_ids(squad)
                    for pid in trace]
            assert sorted(flat) == sorted(p.player_id for p in squad), \
                "a player is unreachable by click"


def test_node_text_never_overflows_its_shirt():
    """A long name must be truncated, not left to collide with its neighbour."""
    long_name = pitch_mod.PitchPlayer(
        1, "Wan-Bissaka-Longname", "DEF", cost=5.0, xp=3.2)
    text = pitch_mod._node_text(long_name, False, pitch_mod.DENSITY_CLEAN)
    head = text.split("<br>")[0].replace("<b>", "").replace("</b>", "")
    assert len(head) <= 11 and head.endswith("…")

    # A name that fits is left alone.
    short = pitch_mod.PitchPlayer(2, "Saka", "MID", cost=10.0, xp=6.1)
    assert "Saka" in pitch_mod._node_text(short, False, pitch_mod.DENSITY_CLEAN)


def test_detailed_density_adds_price_and_clean_does_not():
    player = pitch_mod.PitchPlayer(1, "Saka", "MID", cost=10.4, xp=6.1)
    clean = pitch_mod._node_text(player, False, pitch_mod.DENSITY_CLEAN)
    detailed = pitch_mod._node_text(player, False, pitch_mod.DENSITY_DETAILED)
    assert clean.count("<br>") == 1 and detailed.count("<br>") == 2
    assert "10.4" in detailed


def test_pixel_budget_of_a_node_is_honest():
    """Three short lines at 9px must physically fit inside the node.

    Plotly will happily draw text larger than its marker; the result is a
    label spilling over the neighbouring shirt. ~11px per line at font size 9
    plus padding is the budget the truncation above is sized against.
    """
    line_px = 11
    assert 3 * line_px <= pitch_mod.NODE_PX, (
        "detailed density writes three lines that do not fit the node")
    assert math.isclose(pitch_mod.NODE_PX, 52)


class TestSelectionResolution:
    """A click is the pitch's only input, and every failure mode is silent.

    Wrong index -> the wrong player is lined up for a transfer, which reads as
    a state bug. Renamed key -> nothing is selectable and the pitch looks
    dead. Neither raises, so neither shows up in a mount test.
    """

    def _squad(self):
        return _squad()

    def _selection(self, curve, index, key="point_index"):
        return {"points": [{"curve_number": curve, key: index}]}

    def test_resolves_a_click_to_the_right_player(self):
        squad = self._squad()
        mapping = pitch_mod.node_player_ids(squad)
        for curve, ids in enumerate(mapping):
            for index, expected in enumerate(ids):
                got = pitch_mod.player_from_selection(
                    self._selection(curve, index), squad)
                assert got == expected

    @pytest.mark.parametrize("key", ["point_index", "point_number"])
    def test_accepts_both_streamlit_index_spellings(self, key):
        """Streamlit has renamed this between versions."""
        squad = self._squad()
        expected = pitch_mod.node_player_ids(squad)[0][2]
        assert pitch_mod.player_from_selection(
            self._selection(0, 2, key), squad) == expected

    @pytest.mark.parametrize("selection", [
        None, {}, {"points": []},
        {"points": [{"curve_number": 0}]},                  # no index
        {"points": [{"point_index": 0}]},                   # no curve
        {"points": [{"curve_number": 9, "point_index": 0}]},  # curve OOR
        {"points": [{"curve_number": 0, "point_index": 99}]},  # index OOR
    ])
    def test_malformed_selections_return_none_rather_than_raising(
            self, selection):
        """A bad payload must not blow up inside a rerun."""
        assert pitch_mod.player_from_selection(selection, self._squad()) is None

    def test_bench_clicks_resolve_to_bench_players(self):
        """The bench is its own trace; off-by-one here swaps the wrong man."""
        squad = self._squad()
        mapping = pitch_mod.node_player_ids(squad)
        assert len(mapping) == 2, "expected a starters trace and a bench trace"
        bench_ids = {p.player_id for p in squad if not p.starting}
        for index in range(len(mapping[1])):
            got = pitch_mod.player_from_selection(
                self._selection(1, index), squad)
            assert got in bench_ids
