from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from polymarket_bot import cli
from polymarket_bot.shadow_calibration import (
    GammaResolvedMarketLookup,
    ResolvedMarket,
    ShadowCalibrationError,
    build_report,
    load_observations,
)

END = datetime(2026, 8, 31, 10, 5, tzinfo=timezone.utc)
MARKET_ID = "0x" + "ab" * 32


def _record(
    observed_at: datetime,
    *,
    up_probability: str,
    down_probability: str,
    up_fill: str = "0.60",
    down_fill: str = "0.40",
    up_eligible: bool = True,
    down_eligible: bool = False,
    selected_direction: str | None = None,
) -> dict[str, object]:
    return {
        "mode": "live",
        "market_id": MARKET_ID,
        "observed_at": observed_at.isoformat(),
        "strategy_evaluations": [
            {
                "strategy_id": "eth_chainlink_terminal_spot_v5",
                "metrics": [
                    ["up_terminal_probability", up_probability],
                    ["down_terminal_probability", down_probability],
                    ["up_expected_fill_price", up_fill],
                    ["down_expected_fill_price", down_fill],
                    ["up_fee_per_share", "0.01"],
                    ["down_fee_per_share", "0.02"],
                    ["up_eligible", up_eligible],
                    ["down_eligible", down_eligible],
                    ["selected_direction", selected_direction],
                ],
            }
        ],
    }


class Lookup:
    def __init__(self, outcome: str = "up") -> None:
        self.outcome = outcome
        self.calls: list[str] = []

    def resolve(
        self,
        condition_id: str,
        *,
        asset: str,
        window_start: datetime,
    ) -> ResolvedMarket | None:
        assert asset == "ETH"
        self.calls.append(condition_id)
        return ResolvedMarket(condition_id, END, self.outcome)


def _write_audit(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )

def test_calibration_accepts_shadow_audit_records(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    record = _record(
        END - timedelta(seconds=305),
        up_probability="0.70",
        down_probability="0.30",
    )
    record["mode"] = "shadow"
    _write_audit(audit, [record])

    assert len(load_observations((audit,))) == 1


def test_calibration_uses_only_observations_available_at_checkpoint(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    _write_audit(
        audit,
        [
            _record(
                END - timedelta(seconds=310),
                up_probability="0.70",
                down_probability="0.30",
            ),
            _record(
                END - timedelta(seconds=299),
                up_probability="0.99",
                down_probability="0.01",
            ),
            _record(
                END - timedelta(seconds=255),
                up_probability="0.20",
                down_probability="0.80",
                up_eligible=False,
                down_eligible=True,
            ),
        ],
    )

    report = build_report(
        load_observations((audit,)),
        Lookup(),
        checkpoints=(300, 240),
        max_sample_age=timedelta(seconds=20),
    )

    first, second = report["checkpoints"]
    assert first == {
        "seconds_to_close": 300,
        "samples": 1,
        "markets": 1,
        "direction_samples": 1,
        "direction_correct": 1,
        "direction_accuracy": "1",
        "favored_execution_gate_counts": {"unrecorded": 1},
        "mean_favored_probability": "0.70",
        "mean_brier_score": "0.0900",
        "quoteable_samples": 1,
        "quoteable_mean_realized_net_per_share": "0.39",
        "eligible_samples": 1,
        "eligible_mean_realized_net_per_share": "0.39",
        "candidate_outcome_samples": 0,
        "candidate_outcome_correct": 0,
        "candidate_outcome_accuracy": None,
    }
    assert second == {
        "seconds_to_close": 240,
        "samples": 1,
        "markets": 1,
        "direction_samples": 1,
        "direction_correct": 0,
        "direction_accuracy": "0",
        "favored_execution_gate_counts": {"unrecorded": 1},
        "mean_favored_probability": "0.80",
        "mean_brier_score": "0.6400",
        "quoteable_samples": 1,
        "quoteable_mean_realized_net_per_share": "-0.42",
        "eligible_samples": 1,
        "eligible_mean_realized_net_per_share": "-0.42",
        "candidate_outcome_samples": 0,
        "candidate_outcome_correct": 0,
        "candidate_outcome_accuracy": None,
    }


def test_calibration_reports_selected_candidate_outcome_accuracy(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    _write_audit(
        audit,
        [
            _record(
                END - timedelta(seconds=305),
                up_probability="0.70",
                down_probability="0.30",
                selected_direction="up",
            )
        ],
    )

    report = build_report(load_observations((audit,)), Lookup(), checkpoints=(300,))

    checkpoint = report["checkpoints"][0]
    assert checkpoint["candidate_outcome_samples"] == 1
    assert checkpoint["candidate_outcome_correct"] == 1
    assert checkpoint["candidate_outcome_accuracy"] == "1"


def test_calibration_counts_favored_execution_gates(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    record = _record(
        END - timedelta(seconds=305),
        up_probability="0.70",
        down_probability="0.30",
    )
    metrics = record["strategy_evaluations"][0]["metrics"]  # type: ignore[index]
    metrics.extend(  # type: ignore[union-attr]
        [
            ["up_execution_gate", "net_edge_below_threshold"],
            ["down_execution_gate", "eligible"],
        ]
    )
    _write_audit(audit, [record])

    report = build_report(load_observations((audit,)), Lookup(), checkpoints=(300,))

    assert report["checkpoints"][0]["favored_execution_gate_counts"] == {
        "net_edge_below_threshold": 1
    }


def test_calibration_excludes_unresolved_markets(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    _write_audit(
        audit,
        [
            _record(
                END - timedelta(seconds=305),
                up_probability="0.70",
                down_probability="0.30",
            )
        ],
    )

    class Unresolved:
        def resolve(
            self,
            condition_id: str,
            *,
            asset: str,
            window_start: datetime,
        ) -> ResolvedMarket | None:
            assert condition_id == MARKET_ID
            assert asset == "ETH"
            return None

    report = build_report(load_observations((audit,)), Unresolved(), checkpoints=(300,))

    assert report["resolved_markets"] == 0
    assert report["unresolved_markets"] == 1
    assert report["unresolved_market_ids"] == [MARKET_ID]
    assert report["checkpoints"][0]["samples"] == 0


class _Response:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class _Session:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[tuple[str, float]] = []

    def get(self, url: str, *, timeout: float) -> _Response:
        self.calls.append((url, timeout))
        return _Response(self.payload)


def test_gamma_lookup_accepts_only_closed_binary_market() -> None:
    session = _Session(
        {
            "conditionId": MARKET_ID,
            "closed": True,
            "endDate": "2026-08-31T10:05:00Z",
            "outcomes": '["Up", "Down"]',
            "outcomePrices": '["1", "0"]',
        }
    )

    resolved = GammaResolvedMarketLookup(session=session).resolve(
        MARKET_ID,
        asset="ETH",
        window_start=END - timedelta(minutes=5),
    )

    assert resolved == ResolvedMarket(MARKET_ID, END, "up")
    assert session.calls[0][0].endswith("/eth-updown-5m-1788170400")

def test_calibrate_cli_is_read_only_report(tmp_path: Path, monkeypatch, capsys) -> None:
    audit = tmp_path / "audit.jsonl"
    _write_audit(
        audit,
        [
            _record(
                END - timedelta(seconds=305),
                up_probability="0.70",
                down_probability="0.30",
            )
        ],
    )

    class FakeGammaLookup(Lookup):
        def close(self) -> None:
            return None

    monkeypatch.setattr(cli, "GammaResolvedMarketLookup", FakeGammaLookup)

    assert cli.main(["calibrate", str(audit)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "SHADOW_CALIBRATION_READ_ONLY"
    assert report["order_submission"] is False


def test_gamma_lookup_rejects_ambiguous_winner() -> None:
    session = _Session(
        {
            "conditionId": MARKET_ID,
            "closed": True,
            "endDate": "2026-08-31T10:05:00Z",
            "outcomes": ["Up", "Down"],
            "outcomePrices": ["1", "1"],
        }
    )

    with pytest.raises(ShadowCalibrationError, match="one binary winner"):
        GammaResolvedMarketLookup(session=session).resolve(
            MARKET_ID,
            asset="ETH",
            window_start=END - timedelta(minutes=5),
        )


def test_calibration_excludes_unquoted_directional_sample(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    record = _record(
        END - timedelta(seconds=305),
        up_probability="0.70",
        down_probability="0.30",
    )
    record["strategy_evaluations"][0]["metrics"][2][1] = None
    _write_audit(audit, [record])

    report = build_report(load_observations((audit,)), Lookup(), checkpoints=(300,))

    assert report["checkpoints"][0]["direction_samples"] == 1
    assert report["checkpoints"][0]["quoteable_samples"] == 0
    assert report["checkpoints"][0]["eligible_samples"] == 0
