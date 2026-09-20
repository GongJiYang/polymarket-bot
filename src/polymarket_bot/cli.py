"""Strategy inspection, public shadow observation, and deterministic replay."""

from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from polymarket_bot.adapters.execution import BoundedExecutionAdapter
from polymarket_bot.adapters.alt_public_data import (
    CurrentDogeSnapshotAdapter,
    CurrentSolSnapshotAdapter,
    CurrentXrpSnapshotAdapter,
)
from polymarket_bot.adapters.eth_public_data import CurrentEthSnapshotAdapter
from polymarket_bot.adapters.public_data import CurrentBtcSnapshotAdapter, PublicDataError
from polymarket_bot.audit import JsonlAudit
from polymarket_bot.contracts import FeeMetadata, Strategy, StrategyContext
from polymarket_bot.dashboard import DashboardPublisher, open_dashboard
from polymarket_bot.live.bounded_bot import (
    LiveSessionAuthorization,
    SessionCapacityLedger,
)
from polymarket_bot.live.official_chainlink import OfficialPriceUnavailable
from polymarket_bot.live.geoblock import require_unblocked
from polymarket_bot.live.macos_keychain import (
    DEFAULT_KEYCHAIN_SERVICE,
    macos_keychain_provider,
    resolve_required,
)
from polymarket_bot.live.order_executor import OfficialOrderTransport
from polymarket_bot.live.position_manager import PositionJournal, JournaledEntry, manage_position
from polymarket_bot.live.sdk_account_readonly import (
    OfficialAccountReadOnlyClient,
    create_official_secure_client,
)
from polymarket_bot.microstructure.models import MarketInterval, MarketMetadata, OrderBook
from polymarket_bot.runners import (
    Decision,
    LiveRunner,
    REQUIRED_LIVE_SIGNAL_CONFIRMATIONS,
    ReplayRunner,
    ShadowRunner,
    StrategyEngine,
)
from polymarket_bot.shadow_calibration import (
    GammaResolvedMarketLookup,
    build_report,
    load_observations,
    render_report,
)
from polymarket_bot.strategy_registry import (
    DEFAULT_LIVE_STRATEGY,
    StrategyRegistryError,
    load_strategies,
    select_live_strategies,
    select_strategies,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("strategies", help="list trusted strategy plugins")
    commands.add_parser("dashboard", help="open the singleton local web dashboard")
    replay = commands.add_parser("replay", help="evaluate JSONL market snapshots")
    replay.add_argument("input", type=Path)
    replay.add_argument("--strategy", default="all")
    shadow = commands.add_parser("shadow", help="evaluate one current public snapshot")
    shadow.add_argument("--strategy", default="all")
    shadow.add_argument(
        "--interval",
        choices=tuple(interval.value for interval in MarketInterval),
        default=MarketInterval.FIVE_MINUTES.value,
    )
    shadow.add_argument("--threshold", type=Decimal, default=Decimal("0.04"))
    shadow.add_argument("--quantity", type=Decimal, default=Decimal("5"))
    shadow.add_argument("--audit", type=Path, required=True)
    shadow.add_argument(
        "--monitor-seconds",
        type=int,
        default=0,
        help="repeat read-only observations for this duration; zero runs one snapshot",
    )
    shadow.add_argument(
        "--sample-seconds",
        type=int,
        default=5,
        help="delay between read-only observations",
    )
    calibration = commands.add_parser(
        "calibrate",
        help="read audit JSONL and calculate resolved-outcome shadow calibration",
    )
    calibration.add_argument("audits", nargs="+", type=Path)
    calibration.add_argument("--max-sample-age-seconds", type=int, default=20)
    calibration.add_argument("--output", type=Path)
    live = commands.add_parser(
        "live", help="run one armed, debit-bounded live strategy session"
    )
    live.add_argument(
        "--strategy",
        action="append",
        help=(
            "approved live strategy id; repeat to share one session across assets "
            f"(default: {DEFAULT_LIVE_STRATEGY})"
        ),
    )
    live.add_argument(
        "--interval",
        choices=tuple(interval.value for interval in MarketInterval),
        default=MarketInterval.FIVE_MINUTES.value,
    )
    live.add_argument("--threshold", type=Decimal, default=Decimal("0.04"))
    live.add_argument(
        "--quantity",
        type=Decimal,
        default=Decimal("5"),
        help="minimum visible share depth for signal eligibility; not order size",
    )
    live.add_argument("--audit", type=Path, required=True)
    live.add_argument("--wallet", required=True)
    live.add_argument("--relayer-api-key-address", required=True)
    live.add_argument("--authorization-id", required=True)
    live.add_argument("--approved-by", required=True)
    live.add_argument("--authorization-expires-at", type=_time, required=True)
    live.add_argument("--max-order-debit", type=Decimal, default=Decimal("2"))
    live.add_argument("--max-session-debit", type=Decimal, default=Decimal("2"))
    live.add_argument("--monitor-seconds", type=int, default=900)
    live.add_argument("--sample-seconds", type=int, default=5)
    live.add_argument("--keychain-service", default=DEFAULT_KEYCHAIN_SERVICE)
    live.add_argument("--private-key-label", default="signer-private-key")
    live.add_argument("--relayer-api-key-label", default="relayer-api-key")
    live.add_argument("--compliance-confirmed", action="store_true")
    live.add_argument("--resume-position", action="store_true",
                      help="resume only the recorded position under fresh authorization; never buy")
    live.add_argument("--submit", action="store_true")
    return parser


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


def _decimal(value: object) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("decimal must be finite")
    return result


def _context(record: dict[str, Any]) -> StrategyContext:
    prices = tuple((_time(at), _decimal(price)) for at, price in record["prices"])
    spot_at, spot = record["current_spot"]
    fee = record["fee"]
    if not isinstance(fee, dict):
        raise ValueError("fee must be an object")
    return StrategyContext(
        market=MarketMetadata.model_validate(record["market"]),
        up_book=OrderBook.model_validate(record["up_book"]),
        down_book=OrderBook.model_validate(record["down_book"]),
        fee=FeeMetadata(
            rate=_decimal(fee["rate"]),
            exponent=_decimal(fee["exponent"]),
            minimum_size=_decimal(fee["minimum_size"]),
        ),
        source=str(record["source"]),
        opening=_decimal(record["opening"]),
        prices=prices,
        current_spot=(_time(spot_at), _decimal(spot)),
        threshold=_decimal(record["threshold"]),
        quantity=_decimal(record["quantity"]),
    )


def _load(path: Path) -> tuple[StrategyContext, ...]:
    contexts: list[StrategyContext] = []
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            try:
                record = json.loads(line, parse_float=Decimal)
                if not isinstance(record, dict):
                    raise ValueError("record must be an object")
                contexts.append(_context(record))
            except Exception as exc:
                raise ValueError(f"invalid replay record at line {number}") from exc
    return tuple(contexts)


def _print_decision(decision: Decision) -> None:
    candidate = decision.candidate
    print(
        json.dumps(
            {
                "mode": decision.mode,
                "market_id": decision.market_id,
                "observed_at": decision.observed_at.isoformat(),
                "execution_state": decision.execution_state,
                "post_attempted": getattr(decision.receipt, "post_attempted", None),
                "rejection_code": getattr(decision.receipt, "code", None),
                "rejection_message": getattr(decision.receipt, "message", None),
                "strategy_id": candidate.strategy_id if candidate else None,
                "direction": candidate.direction if candidate else None,
                "max_price": str(candidate.max_price) if candidate else None,
                "expected_fill_price": (
                    str(candidate.expected_fill_price) if candidate else None
                ),
                "net_edge": str(candidate.net_edge) if candidate else None,
            },
            sort_keys=True,
        )
    )


def _live_decision_exit_code(decision: Decision) -> int | None:
    if decision.candidate is None:
        return None
    if getattr(decision.receipt, "code", None) == "SESSION_CAPACITY_UNAVAILABLE":
        return 0
    if getattr(decision.receipt, "post_attempted", None) is False:
        return None
    return 0 if decision.execution_state == "FILLED" else 2





def _run_calibration(arguments: argparse.Namespace) -> int:
    if arguments.max_sample_age_seconds < 0:
        raise ValueError("max-sample-age-seconds must not be negative")
    lookup = GammaResolvedMarketLookup()
    try:
        report = build_report(
            load_observations(arguments.audits),
            lookup,
            max_sample_age=timedelta(seconds=arguments.max_sample_age_seconds),
        )
    finally:
        lookup.close()
    rendered = render_report(report)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0





def _run_shadow(
    arguments: argparse.Namespace,
    strategies: tuple[Strategy, ...],
) -> int:
    if arguments.monitor_seconds < 0:
        raise ValueError("monitor-seconds must not be negative")
    if arguments.sample_seconds <= 0:
        raise ValueError("sample-seconds must be positive")

    interval = MarketInterval(arguments.interval)
    dashboard = DashboardPublisher.connect(open_browser=True)
    print(f"dashboard: {dashboard.base_url}", file=sys.stderr)
    deadline = (
        time.monotonic() + arguments.monitor_seconds
        if arguments.monitor_seconds
        else None
    )
    with CurrentBtcSnapshotAdapter.connect(
        interval=interval,
        threshold=arguments.threshold,
        quantity=arguments.quantity,
    ) as data, JsonlAudit(arguments.audit) as audit:
        runner = ShadowRunner(data, StrategyEngine(strategies), audit, observer=dashboard.publish)
        while True:
            cycle_started = time.monotonic()
            try:
                _print_decision(runner.run_once())
            except (OfficialPriceUnavailable, PublicDataError) as exc:
                if deadline is None:
                    raise
                print(f"waiting for complete public snapshot: {exc}", file=sys.stderr)
            if deadline is None:
                return 0
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 0
            sleep_seconds = min(
                max(0.0, arguments.sample_seconds - (time.monotonic() - cycle_started)),
                remaining,
            )
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)


def _run_live(
    arguments: argparse.Namespace,
    strategies: tuple[Strategy, ...],
) -> int:
    if not arguments.compliance_confirmed:
        raise ValueError("live execution requires explicit compliance confirmation")
    if not arguments.submit:
        raise ValueError("live execution requires explicit --submit")
    if arguments.monitor_seconds <= 0:
        raise ValueError("monitor-seconds must be positive")
    if not 0 < arguments.sample_seconds <= arguments.monitor_seconds:
        raise ValueError("sample-seconds must be positive and not exceed monitor-seconds")
    if not strategies:
        raise ValueError("live execution requires at least one strategy")

    adapters_by_asset = {
        "BTC": CurrentBtcSnapshotAdapter,
        "ETH": CurrentEthSnapshotAdapter,
        "SOL": CurrentSolSnapshotAdapter,
        "XRP": CurrentXrpSnapshotAdapter,
        "DOGE": CurrentDogeSnapshotAdapter,
    }
    assets: list[str] = []
    for strategy in strategies:
        asset = getattr(strategy, "asset", None)
        if asset not in adapters_by_asset:
            raise ValueError(f"live CLI does not support {asset!r} market data")
        if asset in assets:
            raise ValueError(f"live execution has duplicate {asset} strategies")
        assets.append(asset)

    now = datetime.now(timezone.utc)
    authorizations: dict[str, LiveSessionAuthorization] = {}
    for asset, strategy in zip(assets, strategies, strict=True):
        child_id = (
            arguments.authorization_id
            if len(strategies) == 1
            else f"{arguments.authorization_id}-{asset.lower()}"
        )
        authorizations[asset] = LiveSessionAuthorization.create(
            authorization_id=child_id,
            approved_by=arguments.approved_by,
            wallet=arguments.wallet,
            market_family=f"{asset} Up/Down {arguments.interval}",
            model_version=strategy.strategy_id,
            minimum_threshold=arguments.threshold,
            max_order_debit=arguments.max_order_debit,
            max_session_debit=arguments.max_session_debit,
            max_post_attempts=1,
            approved_at=now,
            expires_at=arguments.authorization_expires_at,
        )
    print(
        "LIVE authorization "
        f"id={arguments.authorization_id} "
        f"assets={','.join(assets)} "
        f"strategies={','.join(strategy.strategy_id for strategy in strategies)} "
        f"target_all_in_debit={arguments.max_order_debit} "
        f"max_session_debit={arguments.max_session_debit} "
        f"confirmations={REQUIRED_LIVE_SIGNAL_CONFIRMATIONS} "
        f"expires_at={arguments.authorization_expires_at.isoformat()}",
        f"position_policy=net_tp_5pct_sl_8pct_timeout_30s max_exit_posts=3 "
        f"resume_position={arguments.resume_position}",
        file=sys.stderr,
    )
    armed = input(
        f"Type 'ARM {arguments.authorization_id}' to arm one session: "
    ).strip()
    if armed != f"ARM {arguments.authorization_id}":
        raise RuntimeError("live session was not armed")

    provider = macos_keychain_provider(arguments.keychain_service)
    private_key = resolve_required(provider, arguments.private_key_label)
    relayer_api_key = resolve_required(provider, arguments.relayer_api_key_label)
    client = create_official_secure_client(
        private_key=private_key,
        wallet=arguments.wallet,
        relayer_api_key=relayer_api_key,
        relayer_api_key_address=arguments.relayer_api_key_address,
    )
    account = OfficialAccountReadOnlyClient(client, expected_wallet=arguments.wallet)
    required_raw = arguments.max_session_debit * Decimal(1_000_000)
    require_unblocked()
    account_snapshot = account.snapshot(execution_only=True)
    if not arguments.resume_position and account_snapshot.collateral_balance_raw < required_raw:
        raise RuntimeError("collateral balance is below the session debit cap")
    if not arguments.resume_position and not any(
        allowance >= required_raw
        for _, allowance in account_snapshot.collateral_allowances_raw
    ):
        raise RuntimeError("collateral allowance is below the session debit cap")
    print(
        "preflight: geoblock=unblocked balance=sufficient allowance=sufficient",
        file=sys.stderr,
    )

    transport = OfficialOrderTransport(client)
    budget = SessionCapacityLedger(authorizations[assets[0]])
    dashboard = DashboardPublisher.connect(open_browser=True)
    print(f"dashboard: {dashboard.base_url}", file=sys.stderr)
    interval = MarketInterval(arguments.interval)
    deadline = time.monotonic() + arguments.monitor_seconds
    with ExitStack() as stack:
        stack.callback(account.close)
        journal = PositionJournal(
            Path.home() / ".local/state/polymarket-bot" / f"position-{arguments.wallet.lower()}.json",
            arguments.wallet,
        )
        stack.callback(journal.close)
        if not arguments.resume_position:
            journal.require_entry()
        elif journal.state.get("authorization_id") == arguments.authorization_id:
            raise RuntimeError("position recovery requires a fresh authorization id")
        used_authorizations = journal.state.get("used_authorizations", [])
        if arguments.authorization_id in used_authorizations:
            raise RuntimeError("authorization id was already consumed")
        journal.save(used_authorizations=[*used_authorizations, arguments.authorization_id])
        audit = stack.enter_context(JsonlAudit(arguments.audit))
        data_by_asset: dict[str, Any] = {}
        for asset in assets:
            adapter = adapters_by_asset[asset]
            data_by_asset[asset] = stack.enter_context(
                adapter.connect(
                    interval=interval,
                    threshold=arguments.threshold,
                    quantity=arguments.quantity,
                    target_all_in_debit=arguments.max_order_debit,
                )
            )
        if arguments.resume_position:
            market = MarketMetadata.model_validate(journal.state.get("market", {}))
            if market.asset not in data_by_asset or market.interval != interval:
                raise RuntimeError("resume authorization does not cover recorded market")
            journal.save(authorization_id=arguments.authorization_id)
            return manage_position(
                journal, transport, data_by_asset[market.asset].book,
                require_unblocked, audit, arguments.authorization_expires_at,
                read_fee=data_by_asset[market.asset].fee,
            )

        runners: list[tuple[str, LiveRunner]] = []
        for asset, strategy in zip(assets, strategies, strict=True):
            data = data_by_asset[asset]
            execution = BoundedExecutionAdapter(
                transport=transport,
                account=account,
                authorization=authorizations[asset],
                require_unblocked=require_unblocked,
                read_book=data.book,
                clock=lambda: datetime.now(timezone.utc),
            )
            execution = JournaledEntry(execution, journal, transport, arguments.authorization_id)
            runners.append(
                (
                    asset,
                    LiveRunner(
                        data,
                        StrategyEngine((strategy,)),
                        execution,
                        budget,
                        audit,
                        observer=dashboard.publish,
                    ),
                )
            )

        cycle = 0
        while True:
            cycle_started = time.monotonic()
            offset = cycle % len(runners)
            ordered_runners = runners[offset:] + runners[:offset]
            for asset, runner in ordered_runners:
                decision = None
                try:
                    decision = runner.run_once()
                except (OfficialPriceUnavailable, PublicDataError) as exc:
                    print(
                        f"{asset}: waiting for complete public snapshot: {exc}",
                        file=sys.stderr,
                    )
                if decision is None:
                    continue
                _print_decision(decision)
                if decision.execution_state == "FILLED":
                    return manage_position(
                        journal, transport, data_by_asset[asset].book,
                        require_unblocked, audit, arguments.authorization_expires_at,
                        read_fee=data_by_asset[asset].fee,
                    )
                if decision.candidate is None:
                    continue
                rejection_code = getattr(decision.receipt, "code", None)
                if rejection_code == "ORDER_DEBIT_CAP_EXCEEDED":
                    print(
                        f"{asset}: skipping signal above authorized debit cap: "
                        f"{getattr(decision.receipt, 'message', '')}",
                        file=sys.stderr,
                    )
                elif rejection_code == "SESSION_CAPACITY_UNAVAILABLE":
                    print(
                        f"{asset}: ending session without submission: "
                        f"{getattr(decision.receipt, 'message', '')}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"{asset}: continuing after pre-POST rejection: "
                        f"{rejection_code}: "
                        f"{getattr(decision.receipt, 'message', '')}",
                        file=sys.stderr,
                    )
                exit_code = _live_decision_exit_code(decision)
                if exit_code is not None:
                    return exit_code

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print(
                    json.dumps(
                        {
                            "mode": "live",
                            "state": "CLOSED_NO_CANDIDATE",
                            "submissions": 0,
                        },
                        sort_keys=True,
                    )
                )
                return 0
            cycle += 1
            sleep_seconds = min(
                max(0.0, arguments.sample_seconds - (time.monotonic() - cycle_started)),
                remaining,
            )
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "dashboard":
        print(open_dashboard())
        return 0
    if arguments.command == "calibrate":
        return _run_calibration(arguments)
    registry = load_strategies()
    if arguments.command == "strategies":
        for strategy_id in registry:
            print(strategy_id)
        return 0
    try:
        if arguments.command == "live":
            requested = arguments.strategy or [DEFAULT_LIVE_STRATEGY]
            selected: list[Strategy] = []
            for selection in requested:
                selected.extend(select_live_strategies(registry, selection))
            strategy_ids = [strategy.strategy_id for strategy in selected]
            if len(set(strategy_ids)) != len(strategy_ids):
                raise StrategyRegistryError(
                    "each live strategy may be selected only once"
                )
            strategies = tuple(selected)
        else:
            strategies = select_strategies(registry, arguments.strategy)
    except StrategyRegistryError as exc:
        parser.error(str(exc))
    if arguments.command == "live":
        return _run_live(arguments, strategies)

    if arguments.command == "shadow":
        return _run_shadow(arguments, strategies)
    engine = StrategyEngine(strategies)
    for decision in ReplayRunner(engine).run(_load(arguments.input)):
        _print_decision(decision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
