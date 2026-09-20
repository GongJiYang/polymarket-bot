"""Durable, single-wallet lifecycle for one entry and bounded position reduction."""
from __future__ import annotations

import fcntl
import json
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from polymarket_bot.adapters.public_data import PublicDataError
from polymarket_bot.live.order_executor import SettlementReadUnavailable
from polymarket_bot.live.transaction_cost import fee_per_share
from polymarket_bot.microstructure.models import MarketMetadata, OutcomeSide

D = Decimal
SCALE = D(1_000_000)


class PositionJournal:
    """Atomic journal plus process lock, shared by every session of one wallet."""

    def __init__(self, path: Path, wallet: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.wallet = path, wallet.lower()
        self._lock = path.with_suffix('.lock').open('a')
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.state = json.loads(path.read_text()) if path.exists() else {}
            if self.state and self.state.get('wallet') != self.wallet:
                raise RuntimeError('position journal wallet mismatch')
        except BaseException:
            self._lock.close()
            raise

    def save(self, **values):
        state = {**self.state, **values, 'wallet': self.wallet}
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('w') as stream:
            json.dump(state, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        descriptor = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.state = state

    def require_entry(self):
        if self.state.get('state') not in (None, 'CLOSED', 'ENTRY_REJECTED'):
            raise RuntimeError('unresolved position journal; use --resume-position or reconcile pending submission')

    def close(self):
        self._lock.close()


class JournaledEntry:
    """Persist entry intent before the execution adapter can issue its POST."""

    def __init__(self, execution, journal, transport, authorization_id):
        self.execution, self.journal, self.transport = execution, journal, transport
        self.authorization_id = authorization_id

    def prepare(self, candidate, context):
        self.journal.require_entry()
        balance, _ = self.transport.conditional_balance_allowance(token_id=candidate.token_id)
        if balance:
            raise RuntimeError('existing outcome inventory prevents isolated entry')
        prepared = self.execution.prepare(candidate, context)
        self.context, self.candidate = context, candidate
        return prepared

    def submit(self, prepared):
        context, candidate = self.context, self.candidate
        self.journal.save(
            state='BUY_PENDING', authorization_id=self.authorization_id,
            market=context.market.model_dump(mode='json'), token_id=candidate.token_id,
            outcome=candidate.direction, fee_rate=str(context.fee.rate),
            fee_exponent=str(context.fee.exponent), minimum_size=str(context.fee.minimum_size),
            entry_fee_bound=str(prepared.sdk_order.fee_bound),
            entry_debit_bound=str(prepared.maximum_all_in_debit),
            opened_at=datetime.now(timezone.utc).isoformat(),
            remaining='0', realized_net_bound='0', exit_attempts=0,
        )
        receipt = self.execution.submit(prepared)
        if receipt.state == 'FILLED':
            self.journal.save(
                state='BUY_SETTLING', order_id=receipt.order_id,
                expected_shares=str(receipt.taking_amount), expected_gross=str(receipt.making_amount),
                opened_at=receipt.post_response_at.isoformat(),
            )
        elif receipt.state == 'REJECTED' and not receipt.post_attempted:
            self.journal.save(state='ENTRY_REJECTED')
        return receipt


def liquidation_quote(book, shares, rate, exponent):
    """Full depth quote; absent liquidity is unknown, never zero-price inventory."""
    left, gross, fee, floor = shares, D(0), D(0), None
    for level in book.bids:
        take = min(left, level.size)
        gross += take * level.price
        fee += take * fee_per_share(level.price, rate, exponent)
        left -= take
        floor = level.price
        if left == 0:
            return gross - fee, floor
    return None


def manage_position(journal, transport, read_book, require_unblocked, audit, expires_at,
                    *, read_fee, clock=lambda: datetime.now(timezone.utc), sleep=time.sleep):
    """No new BUYs. Up to three reconciled SELL attempts at freshly quoted floors."""
    state = journal.state
    if state.get('state') not in ('BUY_SETTLING', 'OPEN', 'SELL_SETTLING', 'DUST'):
        raise RuntimeError('submission may be unknown; automatic resubmission forbidden')
    market = MarketMetadata.model_validate(state['market'])
    token, outcome = state['token_id'], OutcomeSide(state['outcome'])
    expected_token = market.up_token_id if outcome == OutcomeSide.UP else market.down_token_id
    if token != expected_token:
        raise RuntimeError('journal token does not match market')
    rate, exponent = D(state['fee_rate']), D(state['fee_exponent'])
    opened = datetime.fromisoformat(state['opened_at'])
    if opened.tzinfo is None:
        raise RuntimeError('position timestamp must be timezone aware')
    settle_deadline = clock() + timedelta(seconds=30)
    while True:
        now, state = clock(), journal.state
        # Settlement reads remain legal after close; only new orders expire.
        if state['state'] in ('BUY_SETTLING', 'SELL_SETTLING'):
            side = 'BUY' if state['state'] == 'BUY_SETTLING' else 'SELL'
            try:
                amounts = transport.confirmed_order_amounts(
                    order_id=state['order_id'], token_id=token, market_id=market.market_id, side=side,
                )
            except SettlementReadUnavailable:
                audit.record({'mode': 'position', 'state': state['state'],
                              'reason': 'settlement_read_unavailable'})
                if clock() >= settle_deadline:
                    return 2
                sleep(1)
                continue
            if amounts is None or amounts[0] != D(state['expected_shares']):
                if now >= settle_deadline:
                    audit.record({'mode': 'position', 'state': state['state'], 'reason': 'settlement_not_confirmed'})
                    return 2
                sleep(1)
                continue
            shares, gross = amounts
            if abs(gross - D(state['expected_gross'])) > D('0.00001'):
                raise RuntimeError('confirmed trade value disagrees with receipt')
            if side == 'BUY':
                # Receipt collateral is authoritative; size * price can differ by rounding.
                # Keep the full signed fee reserve, including for partial fills.
                cost = D(state['expected_gross']) + D(state['entry_fee_bound'])
                if cost > D(state['entry_debit_bound']):
                    raise RuntimeError('journaled entry exceeds debit bound')
                journal.save(state='OPEN', remaining=str(shares), cost_bound=str(cost))
            else:
                remaining = D(state['remaining']) - shares
                if remaining < 0:
                    raise RuntimeError('SELL exceeds managed inventory')
                # Global fee maximum bounds any mix of execution prices.
                net = gross - shares * D(state['exit_fee_per_share_bound'])
                journal.save(state='OPEN', remaining=str(remaining),
                             realized_net_bound=str(D(state['realized_net_bound']) + net))
            continue
        remaining = D(state['remaining'])
        if remaining == 0:
            journal.save(state='CLOSED')
            audit.record({'mode': 'position', **journal.state})
            return 0
        if now >= expires_at or now >= market.window_end:
            balance, _ = transport.conditional_balance_allowance(token_id=token)
            if balance == 0:
                journal.save(state='CLOSED', remaining='0', closure='externally_cleared')
                audit.record({'mode': 'position', **journal.state})
                return 0
            audit.record({'mode': 'position', 'state': 'UNRESOLVED', 'reason': 'authorization_or_market_expired'})
            return 2
        sellable = remaining.quantize(D('0.01'), rounding=ROUND_DOWN)
        if sellable < D(state['minimum_size']) or sellable == 0:
            journal.save(state='DUST')
            audit.record({'mode': 'position', 'state': 'DUST', 'remaining': str(remaining)})
            return 2
        if state['exit_attempts'] >= 3:
            audit.record({'mode': 'position', 'state': 'OPEN', 'reason': 'exit_attempt_limit'})
            return 2
        try:
            fees = read_fee(market)
            rate, exponent = fees.rate, fees.exponent
            book = read_book(market, outcome)
            checked = clock()
            if (book.market_id != market.market_id or book.token_id != token or book.outcome != outcome
                or not book.tradable or any(t > checked or checked - t > timedelta(seconds=2)
                                           for t in (book.exchange_at, book.received_at))):
                raise PublicDataError('position book identity/freshness invalid')
            quote = liquidation_quote(book, sellable, rate, exponent)
            elapsed = (checked - opened).total_seconds()
            if quote is None and elapsed >= 30:
                sellable = min(sellable, sum((level.size for level in book.bids), D(0))).quantize(D('0.01'), rounding=ROUND_DOWN)
                if sellable < fees.minimum_size or sellable == 0:
                    raise PublicDataError('insufficient executable exit size')
                quote = liquidation_quote(book, sellable, rate, exponent)
            if quote is None:
                raise PublicDataError('insufficient full-position exit depth')
            net, floor = quote
            cost = D(state['cost_bound'])
            pnl = D(state['realized_net_bound']) + net - cost
            elapsed = (checked - opened).total_seconds()
            reason = ('timeout' if elapsed >= 30 else
                      'take_profit' if pnl >= cost * D('0.05') else
                      'stop_loss' if pnl <= -cost * D('0.08') else None)
            audit.record({'mode': 'position', 'state': 'OPEN', 'at': checked,
                          'net_liquidation_bound': net, 'pnl_bound': pnl, 'reason': reason})
            if reason is None:
                sleep(1)
                continue
            signed_net_bound = sellable * (floor - rate * D('0.25') ** exponent)
            if reason == 'take_profit' and D(state['realized_net_bound']) + signed_net_bound < cost * D('1.05'):
                sleep(1)
                continue
            require_unblocked()
            balance, allowances = transport.conditional_balance_allowance(token_id=token)
            if balance < remaining * SCALE or not any(a >= sellable * SCALE for a in allowances):
                raise PublicDataError('settled inventory or existing allowance unavailable')
            prepared = transport.prepare_protected_fak_sell(token_id=token, shares=sellable, min_price=floor)
            final = read_book(market, outcome)
            post_at = clock()
            if (post_at >= expires_at or post_at >= market.window_end
                or final.market_id != market.market_id or final.token_id != token or final.outcome != outcome
                or not final.tradable or final.tick_size != book.tick_size
                or any(t > post_at or post_at - t > timedelta(seconds=2)
                       for t in (final.exchange_at, final.received_at))):
                raise PublicDataError('final SELL book invalid')
            final_quote = liquidation_quote(final, sellable, rate, exponent)
            if final_quote is None or final_quote[1] < floor:
                raise PublicDataError('exit liquidity moved below signed floor')
            if reason == 'take_profit' and D(state['realized_net_bound']) + final_quote[0] - cost < cost * D('0.05'):
                raise PublicDataError('take-profit disappeared before POST')
            if read_fee(market) != fees:
                raise PublicDataError('exit fees changed before POST')
            journal.save(state='SELL_PENDING', exit_attempts=state['exit_attempts'] + 1,
                         exit_reason=reason, exit_fee_per_share_bound=str(rate * D('0.25') ** exponent))
            immediate = clock()
            if (immediate >= expires_at or immediate >= market.window_end
                or any(t > immediate or immediate - t > timedelta(seconds=2)
                       for t in (final.exchange_at, final.received_at))):
                journal.save(state='OPEN', exit_attempts=state['exit_attempts'])
                raise PublicDataError('SELL authorization or book expired during journal commit')
            receipt = transport.post_prepared_sell(prepared)
            if not receipt.accepted:
                raise RuntimeError('SELL outcome unresolved; reconciliation required')
            journal.save(state='SELL_SETTLING', order_id=receipt.order_id,
                         expected_shares=str(receipt.making_amount), expected_gross=str(receipt.taking_amount))
            settle_deadline = clock() + timedelta(seconds=30)
        except PublicDataError as exc:
            audit.record({'mode': 'position', 'state': 'WAITING', 'reason': str(exc)})
            sleep(1)
