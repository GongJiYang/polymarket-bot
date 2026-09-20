"""Read-only calibration of shadow and live audit observations against outcomes."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol, Sequence

import requests

CHECKPOINT_SECONDS = (300, 240, 180, 120, 90, 60)
DEFAULT_MAX_SAMPLE_AGE = timedelta(seconds=20)
_GAMMA_MARKET_URL = "https://gamma-api.polymarket.com/markets/slug"


class ShadowCalibrationError(ValueError):
    """Raised when an audit record or resolved-market response is unsafe to use."""


@dataclass(frozen=True, slots=True)
class ResolvedMarket:
    condition_id: str
    window_end: datetime
    outcome: str


class ResolvedMarketLookup(Protocol):
    def resolve(
        self,
        condition_id: str,
        *,
        asset: str,
        window_start: datetime,
    ) -> ResolvedMarket | None: ...


@dataclass(frozen=True, slots=True)
class ShadowObservation:
    market_id: str
    strategy_id: str
    observed_at: datetime
    up_probability: Decimal
    down_probability: Decimal
    up_fill_price: Decimal | None
    down_fill_price: Decimal | None
    up_fee_per_share: Decimal | None
    down_fee_per_share: Decimal | None
    up_eligible: bool
    up_execution_gate: str
    down_execution_gate: str
    down_eligible: bool
    selected_direction: str | None


@dataclass(frozen=True, slots=True)
class CheckpointSample:
    market_id: str
    strategy_id: str
    checkpoint_seconds: int
    observed_at: datetime
    observation_age_seconds: Decimal
    outcome: str
    favored_direction: str | None
    candidate_direction: str | None
    favored_probability: Decimal
    correct: bool | None
    brier_score: Decimal
    fill_price: Decimal | None
    fee_per_share: Decimal | None
    realized_net_per_share: Decimal | None
    execution_gate: str
    execution_eligible: bool


class GammaResolvedMarketLookup:
    """GET-only Gamma lookup; unresolved markets are deliberately excluded."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._session = session or requests.Session()
        self._owns_session = session is None
        self._timeout_seconds = timeout_seconds

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def resolve(
        self,
        condition_id: str,
        *,
        asset: str,
        window_start: datetime,
    ) -> ResolvedMarket | None:
        slug = f"{asset.casefold()}-updown-5m-{int(window_start.timestamp())}"
        response = self._session.get(
            f"{_GAMMA_MARKET_URL}/{slug}",
            timeout=self._timeout_seconds,
        )
        if response.status_code == 404:
            return None
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ShadowCalibrationError(
                f"Gamma resolved-market lookup failed for {condition_id}"
            ) from exc
        market = response.json()
        if not isinstance(market, dict):
            raise ShadowCalibrationError("Gamma market lookup response must be an object")
        if market.get("conditionId") != condition_id:
            raise ShadowCalibrationError("Gamma condition id does not match lookup")
        if market.get("closed") is not True:
            return None
        end_date = _time(market.get("endDate"), "Gamma endDate")
        outcomes = _string_array(market.get("outcomes"), "Gamma outcomes")
        prices = _string_array(market.get("outcomePrices"), "Gamma outcomePrices")
        if len(outcomes) != 2 or len(prices) != 2:
            raise ShadowCalibrationError("Gamma outcome shape must be binary")
        winners = [
            outcome.casefold()
            for outcome, price in zip(outcomes, prices, strict=True)
            if _decimal(price, "Gamma outcome price") == Decimal("1")
        ]
        if winners not in (["up"], ["down"]):
            raise ShadowCalibrationError("Gamma market does not have one binary winner")
        return ResolvedMarket(
            condition_id=condition_id,
            window_end=end_date,
            outcome=winners[0],
        )


def load_observations(paths: Sequence[Path]) -> tuple[ShadowObservation, ...]:
    observations: list[ShadowObservation] = []
    for path in paths:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ShadowCalibrationError(
                        f"invalid JSON in {path} line {line_number}"
                    ) from exc
                if not isinstance(record, dict) or record.get("mode") not in {
                    "shadow",
                    "live",
                }:
                    continue
                market_id = _string(record.get("market_id"), "market_id")
                observed_at = _time(record.get("observed_at"), "observed_at")
                evaluations = record.get("strategy_evaluations")
                if not isinstance(evaluations, list):
                    raise ShadowCalibrationError(
                        f"invalid strategy_evaluations in {path} line {line_number}"
                    )
                for evaluation in evaluations:
                    observations.append(
                        _observation(
                            evaluation,
                            market_id=market_id,
                            observed_at=observed_at,
                            source=f"{path} line {line_number}",
                        )
                    )
    return tuple(observations)


def build_report(
    observations: Sequence[ShadowObservation],
    lookup: ResolvedMarketLookup,
    *,
    checkpoints: Sequence[int] = CHECKPOINT_SECONDS,
    max_sample_age: timedelta = DEFAULT_MAX_SAMPLE_AGE,
) -> dict[str, object]:
    if not checkpoints or any(
        type(value) is not int or value <= 0 for value in checkpoints
    ):
        raise ValueError("checkpoints must contain positive integer seconds")
    if len(set(checkpoints)) != len(checkpoints):
        raise ValueError("checkpoints must be unique")
    if max_sample_age < timedelta(0):
        raise ValueError("max_sample_age must not be negative")

    grouped: dict[tuple[str, str], list[ShadowObservation]] = defaultdict(list)
    for observation in observations:
        grouped[(observation.market_id, observation.strategy_id)].append(observation)

    resolved: dict[tuple[str, str], ResolvedMarket | None] = {}
    for key, group in grouped.items():
        market_id, strategy_id = key
        resolved[key] = lookup.resolve(
            market_id,
            asset=_asset(strategy_id),
            window_start=_window_start(group),
        )

    samples: list[CheckpointSample] = []
    unresolved_market_ids = sorted(
        {market_id for (market_id, _), market in resolved.items() if market is None}
    )
    for key, group in grouped.items():
        market = resolved[key]
        if market is None:
            continue
        ordered = sorted(group, key=lambda value: value.observed_at)
        for checkpoint in sorted(checkpoints, reverse=True):
            target = market.window_end - timedelta(seconds=checkpoint)
            candidates = [
                observation
                for observation in ordered
                if target - max_sample_age <= observation.observed_at <= target
            ]
            if not candidates:
                continue
            samples.append(
                _sample(
                    max(candidates, key=lambda value: value.observed_at),
                    market,
                    checkpoint,
                )
            )

    summaries = [
        _checkpoint_summary(
            checkpoint,
            [sample for sample in samples if sample.checkpoint_seconds == checkpoint],
        )
        for checkpoint in sorted(checkpoints, reverse=True)
    ]
    return {
        "mode": "SHADOW_CALIBRATION_READ_ONLY",
        "order_submission": False,
        "observations": len(observations),
        "resolved_markets": sum(market is not None for market in resolved.values()),
        "unresolved_markets": len(unresolved_market_ids),
        "unresolved_market_ids": unresolved_market_ids,
        "max_sample_age_seconds": str(Decimal(max_sample_age.total_seconds())),
        "checkpoints": summaries,
    }


def render_report(report: dict[str, object]) -> str:
    return json.dumps(report, indent=2, sort_keys=True)


def _observation(
    value: object,
    *,
    market_id: str,
    observed_at: datetime,
    source: str,
) -> ShadowObservation:
    if not isinstance(value, dict):
        raise ShadowCalibrationError(f"invalid strategy evaluation in {source}")
    strategy_id = _string(value.get("strategy_id"), f"strategy_id in {source}")
    raw_metrics = value.get("metrics")
    if not isinstance(raw_metrics, list):
        raise ShadowCalibrationError(f"invalid metrics in {source}")
    metrics: dict[str, object] = {}
    for pair in raw_metrics:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not isinstance(pair[0], str)
            or pair[0] in metrics
        ):
            raise ShadowCalibrationError(f"invalid metrics in {source}")
        metrics[pair[0]] = pair[1]


    return ShadowObservation(
        market_id=market_id,
        strategy_id=strategy_id,
        observed_at=observed_at,
        up_probability=_decimal(_required(metrics, "up_terminal_probability", source), "up probability"),
        down_probability=_decimal(_required(metrics, "down_terminal_probability", source), "down probability"),
        up_fill_price=_optional_decimal(metrics.get("up_expected_fill_price"), "up fill price"),
        down_fill_price=_optional_decimal(metrics.get("down_expected_fill_price"), "down fill price"),
        up_fee_per_share=_optional_decimal(metrics.get("up_fee_per_share"), "up fee per share"),
        down_fee_per_share=_optional_decimal(metrics.get("down_fee_per_share"), "down fee per share"),
        up_execution_gate=_execution_gate(
            metrics.get("up_execution_gate"), "up execution gate"
        ),
        down_execution_gate=_execution_gate(
            metrics.get("down_execution_gate"), "down execution gate"
        ),
        up_eligible=_required(metrics, "up_eligible", source) is True,
        down_eligible=_required(metrics, "down_eligible", source) is True,
        selected_direction=_optional_direction(
            metrics.get("selected_direction"), "selected direction"
        ),
    )


def _asset(strategy_id: str) -> str:
    if strategy_id == "chainlink_terminal_spot_v9":
        return "BTC"
    asset = strategy_id.split("_", 1)[0].upper()
    if asset in {"ETH", "SOL", "XRP", "DOGE"}:
        return asset
    raise ShadowCalibrationError(
        f"cannot derive supported Up/Down asset from {strategy_id}"
    )


def _window_start(observations: Sequence[ShadowObservation]) -> datetime:
    latest = max(observations, key=lambda observation: observation.observed_at)
    return datetime.fromtimestamp(
        int(latest.observed_at.timestamp()) // 300 * 300,
        tz=timezone.utc,
    )



def _sample(
    observation: ShadowObservation,
    market: ResolvedMarket,
    checkpoint_seconds: int,
) -> CheckpointSample:
    up_wins = market.outcome == "up"
    brier_score = (observation.up_probability - Decimal(int(up_wins))) ** 2
    if observation.up_probability == observation.down_probability:
        direction = None
        probability = observation.up_probability
        correct = None
        fill_price = None
        fee_per_share = None
        realized_net = None
        eligible = False
        execution_gate = "tied_probability"
    else:
        direction = "up" if observation.up_probability > observation.down_probability else "down"
        probability = max(observation.up_probability, observation.down_probability)
        correct = direction == market.outcome
        fill_price = (
            observation.up_fill_price if direction == "up" else observation.down_fill_price
        )
        fee_per_share = (
            observation.up_fee_per_share
            if direction == "up"
            else observation.down_fee_per_share
        )
        execution_gate = (
            observation.up_execution_gate
            if direction == "up"
            else observation.down_execution_gate
        )
        realized_net = None
        if fill_price is not None and fee_per_share is not None:
            realized_net = (
                Decimal("1") - fill_price - fee_per_share
                if correct
                else -fill_price - fee_per_share
            )
        eligible = observation.up_eligible if direction == "up" else observation.down_eligible
    return CheckpointSample(
        market_id=observation.market_id,
        strategy_id=observation.strategy_id,
        checkpoint_seconds=checkpoint_seconds,
        observed_at=observation.observed_at,
        observation_age_seconds=Decimal(
            str((market.window_end - timedelta(seconds=checkpoint_seconds) - observation.observed_at).total_seconds())
        ),
        outcome=market.outcome,
        favored_direction=direction,
        candidate_direction=observation.selected_direction,
        favored_probability=probability,
        correct=correct,
        brier_score=brier_score,
        fill_price=fill_price,
        fee_per_share=fee_per_share,
        realized_net_per_share=realized_net,
        execution_gate=execution_gate,
        execution_eligible=eligible,
    )


def _checkpoint_summary(
    checkpoint: int,
    samples: Sequence[CheckpointSample],
) -> dict[str, object]:
    directed = [sample for sample in samples if sample.correct is not None]
    quoteable = [sample for sample in directed if sample.realized_net_per_share is not None]
    eligible = [sample for sample in quoteable if sample.execution_eligible]
    candidate = [
        sample for sample in samples if sample.candidate_direction is not None
    ]
    candidate_correct = sum(
        sample.candidate_direction == sample.outcome for sample in candidate
    )
    return {
        "seconds_to_close": checkpoint,
        "samples": len(samples),
        "markets": len({sample.market_id for sample in samples}),
        "direction_samples": len(directed),
        "direction_correct": sum(sample.correct is True for sample in directed),
        "direction_accuracy": _ratio(sum(sample.correct is True for sample in directed), len(directed)),
        "favored_execution_gate_counts": _counts(
            [sample.execution_gate for sample in directed]
        ),
        "mean_favored_probability": _mean(
            [sample.favored_probability for sample in directed]
        ),
        "mean_brier_score": _mean([sample.brier_score for sample in samples]),
        "quoteable_samples": len(quoteable),
        "quoteable_mean_realized_net_per_share": _mean(
            [sample.realized_net_per_share for sample in quoteable if sample.realized_net_per_share is not None]
        ),
        "eligible_samples": len(eligible),
        "eligible_mean_realized_net_per_share": _mean(
            [sample.realized_net_per_share for sample in eligible if sample.realized_net_per_share is not None]
        ),
        "candidate_outcome_samples": len(candidate),
        "candidate_outcome_correct": candidate_correct,
        "candidate_outcome_accuracy": _ratio(candidate_correct, len(candidate)),
    }


def _ratio(numerator: int, denominator: int) -> str | None:
    if denominator == 0:
        return None
    return str(Decimal(numerator) / Decimal(denominator))


def _mean(values: Sequence[Decimal]) -> str | None:
    if not values:
        return None
    return str(sum(values) / Decimal(len(values)))




def _counts(values: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _execution_gate(value: object, name: str) -> str:
    if value is None:
        return "unrecorded"
    if not isinstance(value, str) or not value.strip():
        raise ShadowCalibrationError(f"{name} must be a non-blank string or null")
    return value


def _optional_direction(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in {"up", "down"}:
        raise ShadowCalibrationError(f"{name} must be 'up', 'down', or null")
    return value

def _required(metrics: dict[str, object], name: str, source: str) -> object:
    if name not in metrics:
        raise ShadowCalibrationError(f"missing {name} in {source}")
    return metrics[name]


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ShadowCalibrationError(f"{name} must be a non-empty string")
    return value


def _string_array(value: object, name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ShadowCalibrationError(f"{name} must be a string array") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ShadowCalibrationError(f"{name} must be a string array")
    return tuple(value)


def _time(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ShadowCalibrationError(f"{name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ShadowCalibrationError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ShadowCalibrationError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ShadowCalibrationError(f"{name} must be a decimal") from exc
    if not result.is_finite():
        raise ShadowCalibrationError(f"{name} must be finite")
    return result


def _optional_decimal(value: object, name: str) -> Decimal | None:
    return None if value is None else _decimal(value, name)
