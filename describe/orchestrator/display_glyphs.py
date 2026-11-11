"""
display_glyphs.py — Describe's copy of the PremHist equipment symbol catalog (design_displays.md
§5.7.9 D3.5 item 1 + the review rulings R1, R5, R10, R12, R14, R20; §5.7.8 D3.4 items 1, 2, 4, 5).

One table (CATALOG) is the source for everything Describe needs to know about a glyph: its id, the
palette area, the catalog (tile) name, its class, its default size and floor on 1600 x 900, its
words (the notes' / summary's name for it), which `states` keys it accepts, which class fields it
accepts, how many bindings Describe gives it, and the request words that decide it. `ph-types`
(`glyphs.rs`) and the viewer (`display_glyphs_*.js`) carry their own copies; each copy's tests check
it against design_displays §5.7.9 item 1.

Pure data + pure functions (no I/O, no LLM). `orchestrator/display_authoring.py` imports from here
and re-exports the D3.4 names (GLYPHS, GLYPH_CLASS, glyph_of, request_glyphs, EQUIPMENT_SIZES, ...).
"""

from __future__ import annotations

import re
from fractions import Fraction
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

GRID = 8

# ── The catalog (item 1; sizes amended by R20) ─────────────────────────
# Areas in palette order: (area id, area name).
AREAS: Tuple[Tuple[str, str], ...] = (
    ("pumps", "Pumps, compressors and drives"),
    ("valves", "Valves and dampers"),
    ("vessels", "Vessels and tanks"),
    ("heat", "Heat transfer"),
    ("combined", "Combined cycle"),
    ("steam", "Steam cycle"),
    ("boiler", "Boiler and emissions"),
    ("electrical", "Electrical (one-line)"),
    ("renewables", "Renewables"),
    ("instruments", "Instruments and indicators"),
)
AREA_NAMES: Dict[str, str] = dict(AREAS)

# (glyph, area, catalog name, class, default (w, h), floor (w, h), words)
_ROWS: Tuple[Tuple[str, str, str, str, Tuple[int, int], Tuple[int, int], str], ...] = (
    ("pump", "pumps", "Centrifugal pump", "rotating", (120, 120), (64, 64), "pump"),
    ("pd_pump", "pumps", "Positive-displacement pump", "rotating", (120, 120), (64, 64), "positive-displacement pump"),
    ("vertical_pump", "pumps", "Vertical (can) pump", "rotating", (100, 160), (64, 100), "vertical pump"),
    ("compressor", "pumps", "Compressor", "rotating", (120, 120), (64, 64), "compressor"),
    ("fan", "pumps", "Fan / blower", "rotating", (120, 120), (64, 64), "fan"),
    ("agitator", "pumps", "Agitator / mixer", "rotating", (100, 140), (64, 88), "agitator"),
    ("conveyor", "pumps", "Conveyor", "rotating", (240, 80), (160, 48), "conveyor"),
    ("motor", "pumps", "Motor", "rotating", (120, 120), (64, 64), "motor"),
    ("vfd", "pumps", "Variable-frequency drive", "rotating", (100, 120), (64, 72), "variable-frequency drive"),
    ("gate_valve", "valves", "Gate / isolation valve", "valve", (120, 120), (64, 64), "gate valve"),
    ("control_valve", "valves", "Globe control valve", "valve", (120, 120), (64, 64), "control valve"),
    ("motor_operated_valve", "valves", "Motor-operated valve", "valve", (120, 120), (64, 64), "motor-operated valve"),
    ("solenoid_valve", "valves", "Solenoid valve", "valve", (120, 120), (64, 64), "solenoid valve"),
    ("ball_valve", "valves", "Ball valve", "valve", (120, 120), (64, 64), "ball valve"),
    ("butterfly_valve", "valves", "Butterfly valve", "valve", (120, 120), (64, 64), "butterfly valve"),
    ("check_valve", "valves", "Check valve", "valve", (120, 120), (64, 64), "check valve"),
    ("three_way_valve", "valves", "Three-way valve", "valve", (120, 120), (64, 64), "three-way valve"),
    ("relief_valve", "valves", "Relief / safety valve", "valve", (120, 120), (64, 64), "relief valve"),
    ("pressure_reducing_valve", "valves", "Pressure-reducing valve", "measured", (120, 120), (64, 64),
     "pressure-reducing valve"),
    ("damper", "valves", "Damper", "valve", (120, 120), (64, 64), "damper"),
    ("tank", "vessels", "Vertical tank", "vessel", (120, 200), (64, 112), "tank"),
    ("horizontal_tank", "vessels", "Horizontal tank", "vessel", (220, 120), (120, 64), "horizontal tank"),
    ("silo", "vessels", "Silo / hopper", "vessel", (120, 200), (64, 112), "silo"),
    ("column", "vessels", "Column / tower", "passive", (100, 240), (56, 140), "column"),
    ("steam_drum", "vessels", "Steam drum", "vessel", (240, 100), (140, 56), "steam drum"),
    ("deaerator", "vessels", "Deaerator", "vessel", (240, 160), (140, 96), "deaerator"),
    ("shell_tube_hx", "heat", "Shell-and-tube heat exchanger", "passive", (240, 100), (140, 56), "heat exchanger"),
    ("plate_hx", "heat", "Plate heat exchanger", "passive", (120, 140), (64, 80), "plate heat exchanger"),
    ("air_cooler", "heat", "Air cooler (fin-fan)", "rotating", (200, 120), (120, 72), "air cooler"),
    ("feedwater_heater", "heat", "Feedwater heater (LP / HP)", "vessel", (240, 100), (140, 56), "feedwater heater"),
    ("attemperator", "heat", "Attemperator / desuperheater", "measured", (160, 100), (96, 56), "attemperator"),
    ("heater", "heat", "Heater", "passive", (120, 140), (64, 80), "heater"),
    ("gas_turbine", "combined", "Gas turbine", "rotating", (240, 160), (160, 96), "gas turbine"),
    ("inlet_filter", "combined", "Inlet filter house", "measured", (160, 160), (96, 96), "inlet filter house"),
    ("duct_burner", "combined", "Duct burner", "rotating", (120, 120), (64, 64), "duct burner"),
    ("hrsg", "combined", "HRSG", "passive", (200, 240), (120, 140), "HRSG"),
    ("hrsg_superheater", "combined", "HRSG superheater section", "passive", (100, 200), (56, 112), "superheater section"),
    ("hrsg_reheater", "combined", "HRSG reheater section", "passive", (100, 200), (56, 112), "reheater section"),
    ("hrsg_evaporator", "combined", "HRSG evaporator section", "passive", (100, 200), (56, 112), "evaporator section"),
    ("hrsg_economizer", "combined", "HRSG economizer section", "passive", (100, 200), (56, 112), "economizer section"),
    ("exhaust_stack", "combined", "Exhaust stack with damper", "valve", (100, 240), (56, 140), "exhaust stack"),
    ("fuel_gas_skid", "combined", "Fuel-gas skid", "measured", (200, 120), (120, 72), "fuel-gas skid"),
    ("steam_turbine", "steam", "Steam turbine (HP / IP / LP)", "rotating", (240, 160), (160, 96), "steam turbine"),
    ("steam_turbine_single", "steam", "Single-casing steam turbine", "rotating", (160, 140), (96, 80),
     "single-casing steam turbine"),
    ("stop_valve", "steam", "Turbine stop valve", "valve", (120, 120), (64, 64), "turbine stop valve"),
    ("turbine_control_valve", "steam", "Turbine control valve", "valve", (120, 120), (64, 64), "turbine control valve"),
    ("bypass_valve", "steam", "Turbine bypass valve", "valve", (120, 120), (64, 64), "turbine bypass valve"),
    ("generator", "steam", "Generator", "rotating", (120, 120), (64, 64), "generator"),
    ("exciter", "steam", "Exciter", "rotating", (100, 100), (56, 56), "exciter"),
    ("condenser", "steam", "Condenser", "passive", (200, 120), (120, 72), "condenser"),
    ("air_cooled_condenser", "steam", "Air-cooled condenser", "rotating", (240, 160), (140, 96), "air-cooled condenser"),
    ("cooling_tower", "steam", "Wet cooling tower", "passive", (140, 160), (80, 96), "cooling tower"),
    ("bfp", "steam", "Boiler-feed pump", "rotating", (120, 120), (64, 64), "boiler-feed pump"),
    ("condensate_pump", "steam", "Condensate pump", "rotating", (100, 160), (64, 100), "condensate pump"),
    ("cw_pump", "steam", "Circulating-water pump", "rotating", (100, 160), (64, 100), "circulating-water pump"),
    ("lube_oil_skid", "steam", "Lube-oil skid", "measured", (200, 120), (120, 72), "lube-oil skid"),
    ("boiler", "boiler", "Boiler / furnace", "rotating", (200, 260), (120, 160), "boiler"),
    ("burner", "boiler", "Burner", "rotating", (100, 100), (56, 56), "burner"),
    ("coal_mill", "boiler", "Coal mill / pulverizer", "rotating", (140, 160), (80, 96), "coal mill"),
    ("coal_feeder", "boiler", "Coal feeder", "rotating", (200, 100), (120, 56), "coal feeder"),
    ("air_preheater", "boiler", "Air preheater", "rotating", (140, 140), (80, 80), "air preheater"),
    ("fd_fan", "boiler", "Forced-draft fan", "rotating", (120, 120), (64, 64), "forced-draft fan"),
    ("id_fan", "boiler", "Induced-draft fan", "rotating", (120, 120), (64, 64), "induced-draft fan"),
    ("pa_fan", "boiler", "Primary-air fan", "rotating", (120, 120), (64, 64), "primary-air fan"),
    ("scr", "boiler", "SCR reactor", "passive", (140, 200), (80, 112), "SCR reactor"),
    ("esp", "boiler", "Electrostatic precipitator", "rotating", (220, 160), (120, 96), "electrostatic precipitator"),
    ("baghouse", "boiler", "Baghouse", "measured", (200, 160), (120, 96), "baghouse"),
    ("fgd", "boiler", "FGD scrubber", "passive", (140, 220), (80, 128), "FGD scrubber"),
    ("stack", "boiler", "Stack", "passive", (80, 260), (48, 160), "stack"),
    ("ash_hopper", "boiler", "Ash hopper", "vessel", (120, 140), (64, 80), "ash hopper"),
    ("generator_breaker", "electrical", "Generator breaker", "breaker", (120, 120), (64, 64), "generator breaker"),
    ("breaker", "electrical", "High-voltage breaker", "breaker", (120, 120), (64, 64), "breaker"),
    ("disconnect", "electrical", "Disconnect switch", "breaker", (120, 120), (64, 64), "disconnect switch"),
    ("grounding_switch", "electrical", "Grounding switch", "breaker", (120, 120), (64, 64), "grounding switch"),
    ("transformer", "electrical", "Generator step-up transformer", "passive", (120, 120), (64, 64), "transformer"),
    ("aux_transformer", "electrical", "Unit / auxiliary transformer", "passive", (120, 120), (64, 64),
     "auxiliary transformer"),
    ("current_transformer", "electrical", "Current transformer", "passive", (80, 80), (48, 48), "current transformer"),
    ("voltage_transformer", "electrical", "Voltage transformer", "passive", (80, 100), (48, 56), "voltage transformer"),
    ("surge_arrester", "electrical", "Surge arrester", "passive", (80, 120), (48, 64), "surge arrester"),
    ("feeder", "electrical", "Line / feeder exit", "passive", (80, 120), (48, 64), "feeder exit"),
    ("sync_point", "electrical", "Synchronizing point", "breaker", (100, 100), (56, 56), "synchronizing point"),
    ("mcc_bucket", "electrical", "MCC bucket", "rotating", (120, 80), (64, 48), "MCC bucket"),
    ("battery", "electrical", "Battery / UPS", "vessel", (100, 140), (56, 80), "battery"),
    ("wind_turbine", "renewables", "Wind turbine", "rotating", (120, 200), (64, 112), "wind turbine"),
    # R20: the bubble's and the tile's sizes are raised (the caption row and the line under the
    # symbol take 14 px each off the drawing)
    ("instrument_bubble", "instruments", "Instrument bubble", "measured", (96, 120), (64, 92), "instrument bubble"),
    ("flow_element", "instruments", "Flow element (orifice)", "measured", (120, 80), (64, 48), "flow element"),
    ("strainer", "instruments", "Strainer / filter", "measured", (120, 100), (64, 56), "strainer"),
    ("analog_indicator", "instruments", "Moving analog indicator", "indicator", (240, 64), (160, 48), "analog indicator"),
    ("alarm_indicator", "instruments", "Alarm indicator", "indicator", (40, 40), (24, 24), "alarm indicator"),
    ("status_tile", "instruments", "Status tile", "rotating", (160, 64), (112, 48), "status tile"),
)

GLYPHS: Tuple[str, ...] = tuple(r[0] for r in _ROWS)
GLYPH_AREA: Dict[str, str] = {r[0]: r[1] for r in _ROWS}
GLYPH_CATALOG_NAMES: Dict[str, str] = {r[0]: r[2] for r in _ROWS}
GLYPH_CLASS: Dict[str, str] = {r[0]: r[3] for r in _ROWS}
EQUIPMENT_SIZES: Dict[str, Tuple[int, int]] = {r[0]: r[4] for r in _ROWS}
EQUIPMENT_FLOORS: Dict[str, Tuple[int, int]] = {r[0]: r[5] for r in _ROWS}
# Item 1 "Words": how notes and the summary name a glyph ("the pump symbol", "a control valve
# symbol 'FV-101'"); the 17 of D3.4 keep their D3.4 words.
GLYPH_WORDS: Dict[str, str] = {r[0]: r[6] for r in _ROWS}
GLYPHS_BY_AREA: Dict[str, Tuple[str, ...]] = {a: tuple(g for g in GLYPHS if GLYPH_AREA[g] == a) for a, _n in AREAS}
CLASSES: Tuple[str, ...] = ("rotating", "valve", "breaker", "vessel", "passive", "measured", "indicator")
GLYPHS_BY_CLASS: Dict[str, Tuple[str, ...]] = {c: tuple(g for g in GLYPHS if GLYPH_CLASS[g] == c) for c in CLASSES}
# The D3.4 absolute floor: four fifths of the floor, up to the grid (the other kinds' ratio).
EQUIPMENT_ABS_FLOORS: Dict[str, Tuple[int, int]] = {
    g: (-int(-(Fraction(f[0] * 4, 5)) // GRID) * GRID, -int(-(Fraction(f[1] * 4, 5)) // GRID) * GRID)
    for g, f in EQUIPMENT_FLOORS.items()}

ROTATIONS: Tuple[int, ...] = (0, 90, 180, 270)

# ── Connection points (D3.6) ───────────────────────────────────────────
# The glyphs the viewer gives NO connection points: `PH.DisplayGlyphs.ports(g)` is empty for
# these, so a pipe can never attach to one. It belongs in this table and not in display_pipes,
# where it was a second hand-maintained list of glyph ids that nothing checked against the
# catalog: the two could name different glyphs, or one could miss a glyph the other added.
# Being here, it also travels into the generated Rust and JavaScript copies
# (tools/gen_glyph_catalog.py), so all three stay in step by construction.
#
# The VALUES still have to be confirmed against the viewer's own tables - last done 2026-10-20 -
# and `gen_glyph_catalog.py --check` is what tells you when a copy has drifted from this one.
PORTLESS_GLYPHS: frozenset = frozenset(("analog_indicator", "alarm_indicator", "status_tile"))


def glyph_has_ports(glyph: Any) -> bool:
    """Whether a pipe can attach to `glyph` at all."""
    return str(glyph) in GLYPH_AREA and str(glyph) not in PORTLESS_GLYPHS

# ── States (§5.7.8 item 1; §5.7.9 item 7) ──────────────────────────────
# The `states` keys a glyph's class may map ("only the keys of the glyph's class"); measured,
# passive, vessel and indicator glyphs have none.
CLASS_STATES: Dict[str, Tuple[str, ...]] = {
    "rotating": ("running", "stopped", "tripped", "transit"),
    "valve": ("open", "closed", "transit", "fault"),
    "breaker": ("closed", "open", "tripped", "transit"),
}
# Item 7: `synchronized` is accepted for these two glyphs only.
SYNC_GLYPHS: Tuple[str, ...] = ("generator", "sync_point")


def glyph_states(glyph: Any) -> Tuple[str, ...]:
    """The `states` keys `glyph` accepts: its class's, plus `synchronized` for the generator and the
    synchronizing point (item 7); () for a glyph without digital states or an unknown glyph."""
    keys = CLASS_STATES.get(GLYPH_CLASS.get(str(glyph), ""), ())
    return keys + ("synchronized",) if glyph in SYNC_GLYPHS else keys


# ── Class fields (R1; item 3) ──────────────────────────────────────────
VESSEL_GLYPHS: Tuple[str, ...] = GLYPHS_BY_CLASS["vessel"]
# `min` / `max` are required on every vessel glyph and the analog indicator.
RANGE_GLYPHS: Tuple[str, ...] = VESSEL_GLYPHS + ("analog_indicator",)
# `normal` on every vessel glyph and the analog indicator; `limits` on every vessel glyph;
# `letters` on the instrument bubble only.
NORMAL_GLYPHS: Tuple[str, ...] = RANGE_GLYPHS
LIMITS_GLYPHS: Tuple[str, ...] = VESSEL_GLYPHS
LETTERS_GLYPHS: Tuple[str, ...] = ("instrument_bubble",)


def glyph_fields(glyph: Any) -> FrozenSet[str]:
    """The optional / class fields `glyph` accepts besides the ones every glyph has (rotate, flip):
    a subset of min, max, normal, limits, letters, states (R1, item 3, item 7)."""
    out = set()
    if glyph in RANGE_GLYPHS:
        out |= {"min", "max", "normal"}
    if glyph in LIMITS_GLYPHS:
        out.add("limits")
    if glyph in LETTERS_GLYPHS:
        out.add("letters")
    if glyph_states(glyph):
        out.add("states")
    return frozenset(out)


def glyph_requires_range(glyph: Any) -> bool:
    """R1: `min` / `max` are required (`missing_field` when absent)."""
    return glyph in RANGE_GLYPHS


# ── Bindings (R5; item 4) ──────────────────────────────────────────────
# Binding 2 (shown under the symbol) exists on exactly these five; the noun a note uses for it.
SECOND_BINDING: Dict[str, str] = {
    "control_valve": "position %", "turbine_control_valve": "position %", "bypass_valve": "position %",
    "vfd": "speed", "coal_feeder": "feed rate",
}


def glyph_bind_max(glyph: Any) -> int:
    """R5: the bindings Describe gives an equipment symbol of `glyph` — 2 for exactly
    control_valve, turbine_control_valve, bypass_valve, vfd and coal_feeder (binding 1 a digital
    state, binding 2 shown under the symbol), 1 for every other glyph."""
    return 2 if glyph in SECOND_BINDING else 1


# What binding 1 of each class must reach (§5.7.8 item 2; item 3); None = anything (optional).
CLASS_NEEDS: Dict[str, Optional[Tuple[str, ...]]] = {
    "rotating": ("digital", "numeric"), "valve": ("digital", "numeric"), "breaker": ("digital",),
    "vessel": ("numeric",), "passive": None, "measured": None, "indicator": None,
}


def binding_need(glyph: Any) -> Optional[Tuple[str, ...]]:
    """What binding 1 of `glyph` must reach: ("digital",), ("numeric",), ("digital", "numeric"),
    or None (anything; the binding is optional or decides by rules)."""
    if glyph == "analog_indicator":
        return ("numeric",)
    return CLASS_NEEDS.get(GLYPH_CLASS.get(str(glyph), "passive"))


# ── Letters (item 3 / R10) ─────────────────────────────────────────────
_LETTERS_RE = re.compile(r"^[A-Z]{1,4}$")
_TOKEN_SPLIT_RE = re.compile(r"[\-._ ]+")


def letters_valid(v: Any) -> bool:
    """`letters`: 1–4 characters A–Z (item 3)."""
    return isinstance(v, str) and bool(_LETTERS_RE.match(v))


def letters_of(name: Any) -> Optional[str]:
    """Item 3 / R10 — the instrument bubble's prefilled `letters` from a point's name: the first
    token (tokens split on `-`, `.`, `_`, space) that is 2–4 uppercase letters and is directly
    followed by a token that starts with a digit ("131-FT-002.PV" → "FT", "SIM-PMP-501.STATE" →
    "PMP"); None when there is none."""
    if not isinstance(name, str):
        return None
    toks = [t for t in _TOKEN_SPLIT_RE.split(name) if t != ""]
    for i in range(len(toks) - 1):
        if re.fullmatch(r"[A-Z]{2,4}", toks[i]) and toks[i + 1][:1].isdigit():
            return toks[i]
    return None


# ── A model's glyph text → the glyph (`glyph_of`) ──────────────────────

def _fold(v: str) -> str:
    t = v.strip().lower()
    t = re.sub(r"\([^)]*\)", " ", t)                   # "Air cooler (fin-fan)" → "air cooler"
    t = re.sub(r"[^a-z0-9]+", "_", t).strip("_")
    t = re.sub(r"_symbol$", "", t)
    return t


# Synonyms a model may write (folded), beyond each glyph's id, catalog name and words.
_EXTRA_ALIASES: Dict[str, str] = {
    # D3.4's
    "valve": "gate_valve", "isolation_valve": "gate_valve", "block_valve": "gate_valve",
    "non_return_valve": "check_valve", "nonreturn_valve": "check_valve", "circuit_breaker": "breaker",
    "vessel": "tank", "tower": "cooling_tower", "heat_recovery_steam_generator": "hrsg",
    "combustion_turbine": "gas_turbine", "analog": "analog_indicator", "analogue_indicator": "analog_indicator",
    "moving_indicator": "analog_indicator", "pointer_indicator": "analog_indicator",
    "alarm": "alarm_indicator", "alarm_light": "alarm_indicator",
    # D3.5
    "centrifugal_pump": "pump", "positive_displacement_pump": "pd_pump", "pd_pump": "pd_pump",
    "can_pump": "vertical_pump", "blower": "fan", "mixer": "agitator", "belt_conveyor": "conveyor",
    "variable_frequency_drive": "vfd", "variable_speed_drive": "vfd", "vsd": "vfd",
    "globe_valve": "control_valve", "globe_control_valve": "control_valve", "mov": "motor_operated_valve",
    "motor_valve": "motor_operated_valve", "solenoid": "solenoid_valve", "3_way_valve": "three_way_valve",
    "threeway_valve": "three_way_valve", "safety_valve": "relief_valve", "psv": "relief_valve",
    "prv": "pressure_reducing_valve", "pressure_reducing_station": "pressure_reducing_valve",
    "vertical_tank": "tank", "hopper": "silo", "distillation_column": "column", "drum": "steam_drum",
    "shell_and_tube_heat_exchanger": "shell_tube_hx", "shell_and_tube_exchanger": "shell_tube_hx",
    "heat_exchanger": "shell_tube_hx", "exchanger": "shell_tube_hx", "hx": "shell_tube_hx",
    "plate_exchanger": "plate_hx", "phe": "plate_hx", "fin_fan": "air_cooler", "fin_fan_cooler": "air_cooler",
    "cooler": "air_cooler", "feed_water_heater": "feedwater_heater", "lp_heater": "feedwater_heater",
    "hp_heater": "feedwater_heater", "desuperheater": "attemperator", "inlet_filter_house": "inlet_filter",
    "superheater": "hrsg_superheater", "hrsg_superheater_section": "hrsg_superheater",
    "reheater": "hrsg_reheater", "hrsg_reheater_section": "hrsg_reheater",
    "evaporator": "hrsg_evaporator", "hrsg_evaporator_section": "hrsg_evaporator",
    "economizer": "hrsg_economizer", "hrsg_economizer_section": "hrsg_economizer",
    "exhaust_stack_with_damper": "exhaust_stack", "fuel_gas_skid": "fuel_gas_skid",
    "hp_ip_lp_steam_turbine": "steam_turbine", "single_casing_turbine": "steam_turbine_single",
    "single_steam_turbine": "steam_turbine_single", "stop_valve": "stop_valve",
    "main_stop_valve": "stop_valve", "governor_valve": "turbine_control_valve",
    "acc": "air_cooled_condenser", "wet_cooling_tower": "cooling_tower",
    "boiler_feed_pump": "bfp", "feedwater_pump": "bfp", "feed_water_pump": "bfp", "bfw_pump": "bfp",
    "condensate_extraction_pump": "condensate_pump", "cep": "condensate_pump",
    "circulating_water_pump": "cw_pump", "cooling_water_pump": "cw_pump",
    "lube_oil_skid": "lube_oil_skid", "lube_skid": "lube_oil_skid", "furnace": "boiler",
    "pulverizer": "coal_mill", "pulveriser": "coal_mill", "mill": "coal_mill",
    "aph": "air_preheater", "preheater": "air_preheater",
    "forced_draft_fan": "fd_fan", "induced_draft_fan": "id_fan", "primary_air_fan": "pa_fan",
    "scr_reactor": "scr", "selective_catalytic_reduction": "scr", "precipitator": "esp",
    "electrostatic_precipitator": "esp", "bag_house": "baghouse", "fabric_filter": "baghouse",
    "fgd_scrubber": "fgd", "scrubber": "fgd", "flue_gas_desulfurization": "fgd", "chimney": "stack",
    "hv_breaker": "breaker", "high_voltage_breaker": "breaker", "disconnect_switch": "disconnect",
    "disconnector": "disconnect", "isolator": "disconnect", "ground_switch": "grounding_switch",
    "earthing_switch": "grounding_switch", "gsu": "transformer", "step_up_transformer": "transformer",
    "generator_step_up_transformer": "transformer", "aux_transformer": "aux_transformer",
    "auxiliary_transformer": "aux_transformer", "unit_transformer": "aux_transformer",
    "unit_auxiliary_transformer": "aux_transformer", "potential_transformer": "voltage_transformer",
    "arrester": "surge_arrester", "lightning_arrester": "surge_arrester", "feeder_exit": "feeder",
    "line_exit": "feeder", "line_feeder_exit": "feeder", "synchronizing_point": "sync_point",
    "synchronising_point": "sync_point", "mcc": "mcc_bucket", "ups": "battery",
    "battery_ups": "battery", "wind_generator": "wind_turbine", "bubble": "instrument_bubble",
    "orifice": "flow_element", "orifice_plate": "flow_element",
    "flow_element_orifice": "flow_element", "filter": "strainer", "moving_analog_indicator": "analog_indicator",
}


def _build_aliases() -> Dict[str, str]:
    out: Dict[str, str] = {}

    def put(k: str, g: str) -> None:
        if k and k not in GLYPH_AREA:                  # an id is always itself
            out.setdefault(k, g)

    for g in GLYPHS:
        put(_fold(GLYPH_CATALOG_NAMES[g]), g)
        put(_fold(GLYPH_WORDS[g]), g)
    for k, g in _EXTRA_ALIASES.items():
        out[k] = g
    return out


GLYPH_ALIASES: Dict[str, str] = _build_aliases()


def glyph_of(v: Any) -> Optional[str]:
    """A glyph as a model may write it — the id, the catalog name, the words or a synonym, any
    case, spaces / hyphens / slashes for "_", an optional "symbol" and plural ("Control valve",
    "Boiler-feed pump", "BFP", "FD fan", "deaerator", "HRSGs") → one of GLYPHS, else None."""
    if not isinstance(v, str) or not v.strip():
        return None
    t = _fold(v)
    if not t:
        return None
    for cand in (t, t[:-1] if t.endswith("s") else None, t[:-2] if t.endswith("es") else None):
        if not cand:
            continue
        if cand in GLYPH_AREA:
            return cand
        if cand in GLYPH_ALIASES:
            return GLYPH_ALIASES[cand]
    return None


# ── The request's glyph words (item 13 / §5.7.8 item 5) ────────────────
# Each entry: (phrases, the glyphs it may mean, the glyph it decides or None). A phrase is plain
# words; between words a space or hyphen (or none) is read, the last word may take a plural.
# Matching is longest phrase first, so "boiler feed pump" beats "pump", "steam drum" beats "drum",
# "control valve" beats "valve", "wind turbine" beats "turbine", "FD fan" beats "fan".
_ALL_VALVES = tuple(g for g in GLYPHS if g.endswith("_valve"))
_WORD_ENTRIES: Tuple[Tuple[Tuple[str, ...], Tuple[str, ...], Optional[str]], ...] = (
    # Pumps, compressors and drives
    (("pump",), ("pump",), "pump"),
    (("centrifugal pump",), ("pump",), "pump"),
    (("positive displacement pump", "pd pump"), ("pd_pump",), "pd_pump"),
    (("vertical pump", "can pump", "vertical can pump"), ("vertical_pump",), "vertical_pump"),
    (("compressor",), ("compressor",), "compressor"),
    (("fan", "blower"), ("fan",), "fan"),
    (("agitator", "mixer"), ("agitator",), "agitator"),
    (("conveyor", "belt conveyor"), ("conveyor",), "conveyor"),
    (("motor",), ("motor",), "motor"),
    (("vfd", "variable frequency drive", "variable speed drive", "vsd"), ("vfd",), "vfd"),
    # Valves and dampers
    (("valve",), _ALL_VALVES, "gate_valve"),
    (("gate valve", "isolation valve", "block valve", "on off valve", "on/off valve"), ("gate_valve",), "gate_valve"),
    (("control valve", "globe valve", "globe control valve"), ("control_valve",), "control_valve"),
    (("motor operated valve", "mov"), ("motor_operated_valve",), "motor_operated_valve"),
    (("solenoid valve",), ("solenoid_valve",), "solenoid_valve"),
    (("ball valve",), ("ball_valve",), "ball_valve"),
    (("butterfly valve",), ("butterfly_valve",), "butterfly_valve"),
    (("check valve", "non return valve", "nonreturn valve"), ("check_valve",), "check_valve"),
    (("three way valve", "3 way valve"), ("three_way_valve",), "three_way_valve"),
    (("relief valve", "safety valve", "safety relief valve", "psv"), ("relief_valve",), "relief_valve"),
    (("pressure reducing valve", "prv"), ("pressure_reducing_valve",), "pressure_reducing_valve"),
    (("damper",), ("damper",), "damper"),
    # Vessels and tanks
    (("tank", "vessel", "vertical tank"), ("tank",), "tank"),
    (("horizontal tank",), ("horizontal_tank",), "horizontal_tank"),
    (("silo",), ("silo",), "silo"),
    (("hopper",), ("silo", "ash_hopper"), None),
    (("column symbol", "distillation column", "fractionation column", "absorber column", "stripper column"),
     ("column",), "column"),
    (("steam drum", "drum"), ("steam_drum",), "steam_drum"),
    (("deaerator",), ("deaerator",), "deaerator"),
    # Heat transfer
    (("heat exchanger", "shell and tube heat exchanger", "shell and tube exchanger", "exchanger"),
     ("shell_tube_hx",), "shell_tube_hx"),
    (("plate heat exchanger", "plate exchanger"), ("plate_hx",), "plate_hx"),
    (("air cooler", "fin fan cooler", "fin fan", "cooler"), ("air_cooler",), "air_cooler"),
    (("feedwater heater", "feed water heater", "lp heater", "hp heater"), ("feedwater_heater",), "feedwater_heater"),
    (("attemperator", "desuperheater"), ("attemperator",), "attemperator"),
    (("heater",), ("heater",), "heater"),
    # Combined cycle
    (("gas turbine", "combustion turbine"), ("gas_turbine",), "gas_turbine"),
    (("inlet filter house", "inlet filter"), ("inlet_filter",), "inlet_filter"),
    (("duct burner",), ("duct_burner",), "duct_burner"),
    (("hrsg", "heat recovery steam generator"), ("hrsg",), "hrsg"),
    (("superheater section", "hrsg superheater", "hrsg superheater section", "superheater"),
     ("hrsg_superheater",), "hrsg_superheater"),
    (("reheater section", "hrsg reheater", "hrsg reheater section", "reheater"), ("hrsg_reheater",), "hrsg_reheater"),
    (("evaporator section", "hrsg evaporator", "hrsg evaporator section", "evaporator"),
     ("hrsg_evaporator",), "hrsg_evaporator"),
    (("economizer section", "hrsg economizer", "hrsg economizer section", "economizer"),
     ("hrsg_economizer",), "hrsg_economizer"),
    (("exhaust stack",), ("exhaust_stack",), "exhaust_stack"),
    (("fuel gas skid", "fuel skid"), ("fuel_gas_skid",), "fuel_gas_skid"),
    (("skid",), ("fuel_gas_skid", "lube_oil_skid"), None),
    # Steam cycle
    (("steam turbine",), ("steam_turbine",), "steam_turbine"),
    (("single casing steam turbine", "single casing turbine"), ("steam_turbine_single",), "steam_turbine_single"),
    (("turbine stop valve", "stop valve", "main stop valve"), ("stop_valve",), "stop_valve"),
    (("turbine control valve", "governor valve"), ("turbine_control_valve",), "turbine_control_valve"),
    (("turbine bypass valve", "bypass valve"), ("bypass_valve",), "bypass_valve"),
    (("generator",), ("generator",), "generator"),
    (("exciter",), ("exciter",), "exciter"),
    (("condenser",), ("condenser",), "condenser"),
    (("air cooled condenser",), ("air_cooled_condenser",), "air_cooled_condenser"),
    (("cooling tower", "wet cooling tower"), ("cooling_tower",), "cooling_tower"),
    (("tower",), ("cooling_tower", "column"), "cooling_tower"),
    (("boiler feed pump", "feedwater pump", "feed water pump", "bfp", "bfw pump"), ("bfp",), "bfp"),
    (("condensate pump", "condensate extraction pump", "cep"), ("condensate_pump",), "condensate_pump"),
    (("circulating water pump", "cw pump"), ("cw_pump",), "cw_pump"),
    (("lube oil skid", "lube skid"), ("lube_oil_skid",), "lube_oil_skid"),
    # Boiler and emissions
    (("boiler", "furnace"), ("boiler",), "boiler"),
    (("burner",), ("burner",), "burner"),
    (("coal mill", "pulverizer", "mill"), ("coal_mill",), "coal_mill"),
    (("coal feeder",), ("coal_feeder",), "coal_feeder"),
    (("feeder",), ("coal_feeder", "feeder"), None),
    (("air preheater", "preheater"), ("air_preheater",), "air_preheater"),
    (("fd fan", "forced draft fan"), ("fd_fan",), "fd_fan"),
    (("id fan", "induced draft fan"), ("id_fan",), "id_fan"),
    (("pa fan", "primary air fan"), ("pa_fan",), "pa_fan"),
    (("scr", "scr reactor", "selective catalytic reduction"), ("scr",), "scr"),
    (("esp", "electrostatic precipitator", "precipitator"), ("esp",), "esp"),
    (("baghouse", "bag house", "fabric filter"), ("baghouse",), "baghouse"),
    (("fgd", "fgd scrubber", "scrubber"), ("fgd",), "fgd"),
    (("stack", "chimney"), ("stack",), "stack"),
    (("ash hopper",), ("ash_hopper",), "ash_hopper"),
    # Electrical (one-line)
    (("generator breaker",), ("generator_breaker",), "generator_breaker"),
    (("breaker", "circuit breaker", "hv breaker", "high voltage breaker"), ("breaker",), "breaker"),
    (("disconnect", "disconnect switch", "disconnector"), ("disconnect",), "disconnect"),
    (("grounding switch", "ground switch", "earthing switch"), ("grounding_switch",), "grounding_switch"),
    (("transformer", "step up transformer", "generator step up transformer", "gsu"), ("transformer",), "transformer"),
    (("auxiliary transformer", "aux transformer", "unit transformer", "unit auxiliary transformer"),
     ("aux_transformer",), "aux_transformer"),
    (("current transformer",), ("current_transformer",), "current_transformer"),
    (("voltage transformer", "potential transformer"), ("voltage_transformer",), "voltage_transformer"),
    (("surge arrester", "lightning arrester", "arrester"), ("surge_arrester",), "surge_arrester"),
    (("feeder exit", "line exit", "line feeder exit"), ("feeder",), "feeder"),
    (("synchronizing point", "synchronising point", "sync point"), ("sync_point",), "sync_point"),
    (("mcc bucket", "mcc"), ("mcc_bucket",), "mcc_bucket"),
    (("battery", "ups"), ("battery",), "battery"),
    # Renewables
    (("wind turbine", "wind generator"), ("wind_turbine",), "wind_turbine"),
    # Instruments and indicators
    (("instrument bubble",), ("instrument_bubble",), "instrument_bubble"),
    (("flow element", "orifice", "orifice plate"), ("flow_element",), "flow_element"),
    (("strainer", "filter"), ("strainer", "inlet_filter"), "strainer"),
    (("analog indicator", "analogue indicator", "moving indicator", "moving pointer indicator", "pointer indicator",
      "moving analog indicator"), ("analog_indicator",), "analog_indicator"),
    (("alarm indicator",), ("alarm_indicator",), "alarm_indicator"),
    (("status tile",), ("status_tile",), "status_tile"),
    # Words that alone cannot tell which glyph (they decide nothing)
    (("turbine",), ("gas_turbine", "steam_turbine", "steam_turbine_single", "wind_turbine"), None),
    (("indicator",), ("analog_indicator", "alarm_indicator"), None),
)

# The bare words that decide nothing (each may mean two or more glyphs).
UNDECIDED_WORDS: Tuple[str, ...] = tuple(p for ps, _gs, d in _WORD_ENTRIES if d is None for p in ps)


def _phrase_re(phrase: str) -> str:
    words = phrase.split(" ")
    out = []
    for w in words[:-1]:
        out.append(re.escape(w).replace("/", r"\s*/\s*"))
    last = re.escape(words[-1])
    last = last + ("(?:es)?" if words[-1].endswith(("sh", "ch", "x")) else "s?")
    out.append(last)
    return r"[\s\-_]*".join(out)


# (regex, may mean, decides), longest plain phrase first (ties alphabetical, so the order is fixed).
# Every id read as words is a phrase of its glyph too ("pd_pump", "hrsg superheater"), except the two
# ids that are bare words deciding nothing in a request ("column" is layout language, "feeder" may be
# the coal feeder).
_SAID = {p for ps, _gs, _d in _WORD_ENTRIES for p in ps}
_ID_PHRASES = tuple(((g.replace("_", " "),), (g,), g) for g in GLYPHS
                    if g not in ("column", "feeder") and g.replace("_", " ") not in _SAID)
_PLAIN = sorted({(p, gs, d) for ps, gs, d in _WORD_ENTRIES + _ID_PHRASES for p in ps},
                key=lambda e: (-len(e[0]), e[0]))
GLYPH_PHRASES: Tuple[Tuple[str, Tuple[str, ...], Optional[str]], ...] = tuple(
    (_phrase_re(p), gs, d) for p, gs, d in _PLAIN)
GLYPH_PHRASE_RE = re.compile(r"(?<![\w\-])(?:" + "|".join(f"({p})" for p, _gs, _d in GLYPH_PHRASES)
                             + r")(?![\w\-])", re.I)


def request_glyphs(request: str) -> List[Tuple[Tuple[str, ...], Optional[str], int]]:
    """The glyph words of a request (item 13 / §5.7.8 item 5), in order, quoted words left out:
    (the glyphs it may mean, the glyph it decides or None, its offset). Longest phrase first."""
    text = re.sub(r"[\"“][^\"”]*[\"”]", lambda m: " " * len(m.group(0)), request or "")
    out: List[Tuple[Tuple[str, ...], Optional[str], int]] = []
    for m in GLYPH_PHRASE_RE.finditer(text):
        i = m.lastindex - 1 if m.lastindex else next(k for k in range(len(GLYPH_PHRASES))
                                                      if m.group(k + 1) is not None)
        out.append((GLYPH_PHRASES[i][1], GLYPH_PHRASES[i][2], m.start()))
    return out


def decided_glyph(text: str) -> Optional[str]:
    """The glyph a single phrase decides ("boiler feed pump" → bfp, "turbine" → None)."""
    hits = request_glyphs(text)
    return hits[0][1] if hits else None


# ── Reference words ("the pump", "both valves") ────────────────────────
# One word → the glyphs a follow-up refers to by it. Words that are also layout language
# ("column", "tile", "element", "drive", "switch") are left out on purpose.
_PUMPS = ("pump", "pd_pump", "vertical_pump", "bfp", "condensate_pump", "cw_pump")
_REF_ENTRIES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("pump", _PUMPS), ("motor", ("motor",)), ("fan", ("fan", "fd_fan", "id_fan", "pa_fan")),
    ("blower", ("fan",)), ("compressor", ("compressor",)), ("agitator", ("agitator",)), ("mixer", ("agitator",)),
    ("conveyor", ("conveyor",)), ("vfd", ("vfd",)),
    ("valve", _ALL_VALVES), ("damper", ("damper", "exhaust_stack")),
    ("tank", ("tank", "horizontal_tank")), ("vessel", ("tank", "horizontal_tank")), ("silo", ("silo",)),
    ("hopper", ("silo", "ash_hopper")), ("drum", ("steam_drum",)), ("deaerator", ("deaerator",)),
    ("exchanger", ("shell_tube_hx", "plate_hx")), ("cooler", ("air_cooler",)),
    ("heater", ("heater", "feedwater_heater")), ("attemperator", ("attemperator",)),
    ("desuperheater", ("attemperator",)),
    ("turbine", ("gas_turbine", "steam_turbine", "steam_turbine_single", "wind_turbine")),
    ("burner", ("burner", "duct_burner")), ("hrsg", ("hrsg",)), ("superheater", ("hrsg_superheater",)),
    ("reheater", ("hrsg_reheater",)), ("evaporator", ("hrsg_evaporator",)), ("economizer", ("hrsg_economizer",)),
    ("stack", ("stack", "exhaust_stack")), ("skid", ("fuel_gas_skid", "lube_oil_skid")),
    ("generator", ("generator",)), ("exciter", ("exciter",)),
    ("condenser", ("condenser", "air_cooled_condenser")), ("tower", ("cooling_tower",)), ("bfp", ("bfp",)),
    ("boiler", ("boiler",)), ("furnace", ("boiler",)), ("mill", ("coal_mill",)), ("pulverizer", ("coal_mill",)),
    ("feeder", ("coal_feeder", "feeder")), ("preheater", ("air_preheater",)), ("scr", ("scr",)),
    ("esp", ("esp",)), ("precipitator", ("esp",)), ("baghouse", ("baghouse",)), ("fgd", ("fgd",)),
    ("scrubber", ("fgd",)),
    ("breaker", ("breaker", "generator_breaker")), ("disconnect", ("disconnect",)),
    ("transformer", ("transformer", "aux_transformer", "current_transformer", "voltage_transformer")),
    ("arrester", ("surge_arrester",)), ("battery", ("battery",)), ("ups", ("battery",)),
    ("bubble", ("instrument_bubble",)), ("strainer", ("strainer",)), ("filter", ("strainer", "inlet_filter")),
    ("indicator", ("analog_indicator", "alarm_indicator")),
)
GLYPH_REF_WORDS: Dict[str, FrozenSet[str]] = {}
for _w, _gs in _REF_ENTRIES:
    GLYPH_REF_WORDS[_w] = frozenset(_gs)
    GLYPH_REF_WORDS[_w + ("es" if _w.endswith(("sh", "ch", "x")) else "s")] = frozenset(_gs)
GLYPH_REF_WORDS["equipment"] = frozenset(GLYPHS)
del _w, _gs


# ── The prompt's EQUIPMENT help (item 13: by area as ids, short; the binding hint per class) ──

def equipment_help() -> str:
    """The prompt's equipment block: the glyph ids by area, the binding hint per class, the few
    words that are not their id, and the optional fields. Kept compact (gpt-oss:20b, 16k context)."""
    areas = "; ".join(f"{a}: {', '.join(GLYPHS_BY_AREA[a])}" for a, _n in AREAS)
    vessels = ", ".join(VESSEL_GLYPHS)
    breakers = ", ".join(GLYPHS_BY_CLASS["breaker"])
    two = ", ".join(SECOND_BINDING)
    return (
        "EQUIPMENT (kind \"equipment\" + \"glyph\", one of these ids by area) — " + areas + ".\n"
        "Binding by class: valves (the *_valve ids except pressure_reducing_valve, damper, exhaust_stack) bind "
        "the position (a digital open / closed state or a % open); " + breakers + " bind a digital state; "
        "vessels (" + vessels + ") and analog_indicator bind the level / value, with \"min\", \"max\" (its "
        "range), \"normal\"?: {\"lo\", \"hi\"} and (vessels) \"limits\"?: {\"hi\", \"lo\"}; alarm_indicator binds "
        "the point whose rules decide; passive / measured equipment (exchangers, hrsg sections, column, stack, "
        "scr, fgd, transformers, skids, strainer, flow_element, instrument_bubble, ...): a binding is "
        "optional (its value is shown); every other glyph binds the run state (a digital point or attribute "
        "named like state / run / status; a number > 0 = running). A 2nd binding only on " + two + " (the "
        "position % / speed / feed rate after the state).\n"
        "Words: \"pump symbol\" = pump, \"valve\" = gate_valve, \"control valve\", \"gas turbine\", "
        "\"steam turbine\", \"cooling tower\", \"boiler feed pump\" / BFP = bfp, \"FD / ID / PA fan\" = "
        "fd_fan / id_fan / pa_fan, \"heat exchanger\" = shell_tube_hx, \"analog indicator\" / \"moving "
        "indicator\" = analog_indicator, \"alarm indicator\" = alarm_indicator; otherwise the id read as "
        "words. Optional: \"rotate\" 0|90|180|270, \"flip\" true|false.\n"
    )


__all__ = [
    "AREAS", "AREA_NAMES", "GLYPHS", "GLYPH_AREA", "GLYPH_CATALOG_NAMES", "GLYPH_CLASS", "GLYPH_WORDS",
    "GLYPHS_BY_AREA", "GLYPHS_BY_CLASS", "CLASSES", "EQUIPMENT_SIZES", "EQUIPMENT_FLOORS",
    "EQUIPMENT_ABS_FLOORS", "ROTATIONS", "PORTLESS_GLYPHS", "glyph_has_ports", "CLASS_STATES", "SYNC_GLYPHS", "glyph_states", "VESSEL_GLYPHS",
    "RANGE_GLYPHS", "NORMAL_GLYPHS", "LIMITS_GLYPHS", "LETTERS_GLYPHS", "glyph_fields", "glyph_requires_range",
    "SECOND_BINDING", "glyph_bind_max", "CLASS_NEEDS", "binding_need", "letters_valid", "letters_of",
    "GLYPH_ALIASES", "glyph_of", "GLYPH_PHRASES", "GLYPH_PHRASE_RE", "UNDECIDED_WORDS", "request_glyphs",
    "decided_glyph", "GLYPH_REF_WORDS", "equipment_help",
]
