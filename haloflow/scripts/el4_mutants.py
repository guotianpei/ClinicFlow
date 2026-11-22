"""Eligibility E4 mutation controls, group ``el4`` (M-E4-1..5, M-E4-7, M-E4-8). SYNTHETIC ONLY.

Targets predeclared in the E4 test packet (v1 "Mutants", v1r1 R4): the eligibility profile
builds the default registry (M01); a profile mismatch is not refused (M02); the `eligibility`
scheduler is left out of the drain (M03, killed by P08); the view token is omitted from the
close form (M04); CASE_CHANGED rendered as success (M05); the clearinghouse validator accepts
a non-synthetic member id (M07); a label lookup falls back to the raw code (M08).

M06 (coinsurance rendered in the view) is NOT run here: the screen renders only the t121 case
view summary, which carries the OD-EL4 fields only, so an E4-layer edit is an equivalent
mutant. The exclusion it protects is t121's and is killed at E3a (scripts/el3a_mutants.py M07,
"the case view summary leaks coinsurance").

Judged by the declared-rows rule of scripts/b3_mutants.py (the synthetic PostgreSQL rows start
a lifecycle per row, so the part-A whole-module rule does not fit): the union of the declared
killing rows runs once on the clean source and must pass completely; a mutant is KILLED when
at least one of its declared rows fails or errors; every target is restored byte-identically.
The machinery is b3_mutants.py's, loaded unchanged with this mutant list.

Usage (from haloflow/):
    python3 -I scripts/el4_mutants.py --pytest "$VENV/bin/pytest" \\
        --evidence "$RUNNER_TEMP/verify-evidence/el4-mutation" [--mutant M-E4-1 ...] [--check]
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Final

HERE = Path(__file__).resolve().parent
SYN: Final = Path("src/haloflow/synthetic")
PROFILES, LIFECYCLE, EL_UI, SIM_CH, UI = (
    SYN / "profiles.py", SYN / "lifecycle.py", SYN / "el_ui.py", SYN / "sim_clearinghouse.py",
    SYN / "ui.py")
EU: Final = "tests/synthetic/test_el4_units.py::"
EP: Final = "tests/synthetic/test_el4_postgres.py::"


def _b3() -> Any:
    spec = importlib.util.spec_from_file_location("_b3_mutants", HERE / "b3_mutants.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def mutants(b3: Any) -> tuple[Any, ...]:
    m = b3.Mutant
    return (
        m("M-E4-1", "the eligibility profile builds the default registry", PROFILES,
          "    if valid(profile) == DEFAULT:\n        return build_synthetic_registry()\n",
          "    if True:\n        return build_synthetic_registry()\n",
          (f"{EU}test_el4_u01_registry_targets",)),
        m("M-E4-2", "a profile mismatch is not refused", PROFILES,
          "    if valid(installed) != valid(requested):\n",
          "    if False:\n",
          (f"{EU}test_el4_u01_request_check[default-eligibility-False]",
           f"{EU}test_el4_u01_request_check[eligibility-default-False]",
           f"{EP}test_el4_p01d3_eligibility_file_default_request_refused")),
        m("M-E4-3", "the eligibility scheduler is left out of the drain", LIFECYCLE,
          "        for name in (\"eligibility\", *PRODUCERS):           # E4: eligibility stops"
          " first\n",
          "        for name in PRODUCERS:\n",
          (f"{EP}test_el4_p08_drain_before_pool_close",)),
        m("M-E4-4", "the view token is omitted from the close form", EL_UI,
          "action=\\\"{item_path}/close\\\">{base}\"\n",
          "action=\\\"{item_path}/close\\\">{base.replace(token, '')}\"\n",
          (f"{EP}test_el4_p05a_screen_shows_kind[eligibility_manual]",
           f"{EP}test_el4_p05c_close_form[el_self_pay]")),
        m("M-E4-5", "CASE_CHANGED is rendered as success (a redirect to the item)", EL_UI,
          "            return _case_changed(item_id)\n        return ui._redirect(item_id)\n",
          "            return ui._redirect(item_id)\n        return ui._redirect(item_id)\n",
          (f"{EP}test_el4_p05d_stale_form_case_changed",)),
        m("M-E4-7", "the clearinghouse validator accepts a non-synthetic member id", SIM_CH,
          "MEMBER_ID: Final = re.compile(r\"SYN-[A-Z0-9]{1,12}(?:-[A-Z0-9]{1,8})?\")\n",
          "MEMBER_ID: Final = re.compile(r\"[A-Za-z0-9-]{1,40}\")\n",
          (f"{EU}test_el4_u03_control_refuses[value12]",
           f"{EU}test_el4_u03_control_refuses[value13]")),
        m("M-E4-8", "a label lookup falls back to the raw code", UI,
          "        return esc(mapping[code])\n    return UNRECOGNIZED\n",
          "        return esc(mapping[code])\n    return esc(str(code))\n",
          (f"{EU}test_el4_u05_unknown_kind_unrecognized_control",)),
    )


def main(argv: list[str] | None = None) -> int:
    b3 = _b3()
    b3.MUTANTS = mutants(b3)
    return int(b3.main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
