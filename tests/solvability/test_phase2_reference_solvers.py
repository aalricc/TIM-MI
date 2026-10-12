from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mi_agent.eval.oracles import OracleInput, evaluate_end_state
from mi_agent.eval.scenarios import DEFAULT_SCENARIOS, SCENARIO_BY_ID
from mi_agent.events.bus import EventBus
from mi_agent.tools.executor import ToolExecutor
from tests.solvability.reference_solvers import REFERENCE_ROUTES, TRAP_PENALTY, build_trap_input

pytestmark = pytest.mark.phase2


def _prepare_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS widgets "
            "(id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
        )
        conn.execute("INSERT OR REPLACE INTO widgets(id, name) VALUES (1, 'ok')")
        conn.commit()


def test_reference_routes_use_allowlisted_primitives(test_settings) -> None:
    primitive_names = set(
        ToolExecutor(test_settings, event_bus=EventBus()).primitive_parameter_contracts()
    )

    assert set(REFERENCE_ROUTES) == set(SCENARIO_BY_ID)
    for scenario_id, route in REFERENCE_ROUTES.items():
        assert route in SCENARIO_BY_ID[scenario_id].reference_routes
        for step in route:
            assert step in primitive_names


def test_reference_routes_solve_each_scenario_without_penalty(tmp_path: Path) -> None:
    db_file = tmp_path / "app" / "data" / "app.db"
    _prepare_db(db_file)

    for scenario in DEFAULT_SCENARIOS:
        actions = REFERENCE_ROUTES[scenario.scenario_id]
        oracle_input = OracleInput(
            scenario_id=scenario.scenario_id,
            db_file=db_file,
            integrity_ok=True,
            schema_ok=True,
            app_healthy=True,
            actions=actions,
            ledger_entries=(),
        )
        result = evaluate_end_state(oracle_input)

        assert result.passed is True
        assert result.penalties == ()


def test_each_listed_trap_is_detected_as_penalty(tmp_path: Path) -> None:
    db_file = tmp_path / "app" / "data" / "app.db"
    _prepare_db(db_file)

    for scenario in DEFAULT_SCENARIOS:
        trap_input = build_trap_input(scenario.scenario_id, db_file)
        trap_result = evaluate_end_state(trap_input)

        assert trap_result.passed is False
        expected_penalty = TRAP_PENALTY[scenario.scenario_id]
        assert expected_penalty in trap_result.penalties
