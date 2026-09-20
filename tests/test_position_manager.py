from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest

from polymarket_bot.live.position_manager import PositionJournal, liquidation_quote, manage_position
from polymarket_bot.live.order_executor import OfficialOrderTransport, OfficialOrderError, SettlementReadUnavailable
from polymarket_bot.microstructure.models import MarketMetadata, OrderBook

NOW = datetime(2026, 9, 8, 15, 1, 30, tzinfo=timezone.utc)
MARKET = MarketMetadata(
    market_id='condition', title='BTC', rules='TWAP', asset='BTC', interval='5m',
    window_start=NOW.replace(minute=0, second=0), window_end=NOW.replace(minute=5, second=0),
    up_token_id='up', down_token_id='down', outcomes=('Up', 'Down'),
)


class Clock:
    def __init__(self):
        self.now = NOW
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


class Audit:
    def __init__(self):
        self.events = []
    def record(self, event):
        self.events.append(event)


def book(clock, bid='0.76', size='10'):
    return OrderBook(market_id='condition', token_id='down', outcome='down', sequence=0,
                     bids=({'price': bid, 'size': size},), asks=({'price': '0.99', 'size': '10'},),
                     tick_size=D('0.01'), exchange_at=clock(), received_at=clock(), tradable=True)


def position(tmp_path, **changes):
    journal = PositionJournal(tmp_path / 'position.json', 'wallet')
    journal.save(state='OPEN', market=MARKET.model_dump(mode='json'), token_id='down',
                 outcome='down', fee_rate='0.07', fee_exponent='1', minimum_size='0.01',
                 opened_at=NOW.isoformat(), remaining='5', cost_bound='3.5',
                 realized_net_bound='0', exit_attempts=0, **changes)
    return journal


class Transport:
    def __init__(self, *, partial=False, unknown=False):
        self.posts = []
        self.partial, self.unknown = partial, unknown
        self.fills = {}
    def conditional_balance_allowance(self, **kwargs):
        return 5_000_000, (10_000_000,)
    def prepare_protected_fak_sell(self, **kwargs):
        return NS(**kwargs)
    def post_prepared_sell(self, order):
        self.posts.append(order)
        if self.unknown:
            raise OfficialOrderError('outcome unknown')
        quantity = D('2') if self.partial and len(self.posts) == 1 else order.shares
        order_id = str(len(self.posts))
        gross = quantity * order.min_price
        self.fills[order_id] = quantity, gross
        return NS(accepted=True, order_id=order_id, making_amount=quantity, taking_amount=gross)
    def confirmed_order_amounts(self, **kwargs):
        return self.fills[kwargs['order_id']]


def test_entry_settlement_accepts_receipt_trade_rounding_within_tolerance(tmp_path):
    journal = PositionJournal(tmp_path / 'position.json', 'wallet')
    journal.save(
        state='BUY_SETTLING', market=MARKET.model_dump(mode='json'), token_id='down',
        outcome='down', fee_rate='.07', fee_exponent='1', minimum_size='.01',
        opened_at=NOW.isoformat(), remaining='0', realized_net_bound='0', exit_attempts=0,
        order_id='buy', expected_shares='5', expected_gross='3.74',
        entry_fee_bound='.259182', entry_debit_bound='3.999182',
    )
    transport, clock = Transport(), Clock()
    transport.fills['buy'] = D('5'), D('3.740001')
    try:
        assert manage_position(
            journal, transport, lambda *_: book(clock), lambda: None, Audit(), NOW,
            read_fee=lambda _: None, clock=clock, sleep=clock.sleep,
        ) == 2
        assert journal.state['state'] == 'OPEN'
        assert journal.state['cost_bound'] == '3.999182'
    finally:
        journal.close()


@pytest.mark.parametrize('failures', [2, 100])
def test_transient_settlement_reads_retry_without_reposting(tmp_path, failures):
    journal, clock, transport = position(tmp_path), Clock(), Transport()
    journal.save(state='BUY_SETTLING', order_id='buy', expected_shares='5',
                 expected_gross='3.74', entry_fee_bound='.259182', entry_debit_bound='3.999182')
    attempts = 0
    def trades(**_):
        nonlocal attempts
        attempts += 1
        if attempts <= failures:
            raise TimeoutError('GET timed out')
        return NS(iter_items=lambda: iter([NS(
            id='trade', taker_order_id='buy', trader_side='TAKER', token_id='down',
            condition_id='condition', side='BUY', status='CONFIRMED',
            size=D('5'), price=D('.7480002'),
        )]))
    reader = OfficialOrderTransport(NS(list_account_trades=trades))
    original = transport.confirmed_order_amounts
    transport.confirmed_order_amounts = lambda **kw: (
        reader.confirmed_order_amounts(**kw) if kw['side'] == 'BUY' else original(**kw))
    try:
        result = manage_position(
            journal, transport, lambda *_: book(clock, bid='.82'), lambda: None, Audit(),
            NOW + timedelta(minutes=1),
            read_fee=lambda _: NS(rate=D('.07'), exponent=D(1), minimum_size=D('.01')),
            clock=clock, sleep=clock.sleep,
        )
        if failures == 2:
            assert result == 0
            assert len(transport.posts) == 1
            assert journal.state['exit_reason'] == 'timeout'
            assert journal.state['state'] == 'CLOSED'
        else:
            assert result == 2
            assert not transport.posts
            assert journal.state['state'] == 'BUY_SETTLING'
        assert clock() == NOW + timedelta(seconds=30)
    finally:
        journal.close()


def test_partial_entry_uses_receipt_cost_not_unspent_budget(tmp_path):
    journal, clock, transport = position(tmp_path), Clock(), Transport()
    journal.save(state='BUY_SETTLING', order_id='buy', expected_shares='5',
                 expected_gross='2', entry_fee_bound='.25', entry_debit_bound='3.99')
    transport.fills['buy'] = D('5'), D('2.000001')
    try:
        assert manage_position(
            journal, transport, lambda *_: book(clock, bid='.45'), lambda: None, Audit(),
            NOW + timedelta(seconds=1),
            read_fee=lambda _: NS(rate=D('.07'), exponent=D(1), minimum_size=D('.01')),
            clock=clock, sleep=clock.sleep,
        ) == 2
        assert not transport.posts
        assert journal.state['state'] == 'OPEN'
        assert journal.state['cost_bound'] == '2.25'
    finally:
        journal.close()


def test_invalid_settlement_is_never_classified_as_retryable():
    reader = OfficialOrderTransport(NS(list_account_trades=lambda **_: NS(
        iter_items=lambda: iter([NS(taker_order_id='buy', trader_side='MAKER')]))))
    with pytest.raises(OfficialOrderError) as error:
        reader.confirmed_order_amounts(order_id='buy', token_id='down', market_id='condition', side='BUY')
    assert not isinstance(error.value, SettlementReadUnavailable)


@pytest.mark.parametrize(('bid', 'reason'), [('0.76', 'take_profit'), ('0.60', 'stop_loss'), ('0.71', 'timeout')])
def test_position_reduces_on_net_profit_loss_or_elapsed_time(tmp_path, bid, reason):
    journal, clock, audit, transport = position(tmp_path), Clock(), Audit(), Transport()
    try:
        result = manage_position(journal, transport, lambda *_: book(clock, bid), lambda: None,
                                 audit, NOW + timedelta(minutes=1), read_fee=lambda _: NS(rate=D("0.07"), exponent=D("1"), minimum_size=D("0.01")), clock=clock, sleep=clock.sleep)
        assert result == 0
        assert journal.state['state'] == 'CLOSED'
        assert len(transport.posts) == 1
        assert transport.posts[0].shares == D('5')
        assert journal.state['exit_reason'] == reason
        if reason == 'timeout':
            assert clock() - NOW == timedelta(seconds=30)
    finally:
        journal.close()


def test_partial_sell_only_reduces_confirmed_remaining_quantity(tmp_path):
    journal, clock, transport = position(tmp_path), Clock(), Transport(partial=True)
    try:
        assert manage_position(journal, transport, lambda *_: book(clock), lambda: None,
                               Audit(), NOW + timedelta(minutes=1), read_fee=lambda _: NS(rate=D("0.07"), exponent=D("1"), minimum_size=D("0.01")), clock=clock, sleep=clock.sleep) == 0
        assert [order.shares for order in transport.posts] == [D('5'), D('3')]
    finally:
        journal.close()


def test_unknown_sell_survives_restart_and_prevents_repost(tmp_path):
    journal, clock, transport = position(tmp_path), Clock(), Transport(unknown=True)
    with pytest.raises(OfficialOrderError):
        manage_position(journal, transport, lambda *_: book(clock), lambda: None,
                        Audit(), NOW + timedelta(minutes=1), read_fee=lambda _: NS(rate=D("0.07"), exponent=D("1"), minimum_size=D("0.01")), clock=clock, sleep=clock.sleep)
    journal.close()
    recovered = PositionJournal(tmp_path / 'position.json', 'wallet')
    try:
        assert recovered.state['state'] == 'SELL_PENDING'
        with pytest.raises(RuntimeError, match='unresolved'):
            recovered.require_entry()
        with pytest.raises(RuntimeError, match='unknown'):
            manage_position(recovered, transport, lambda *_: book(clock), lambda: None,
                            Audit(), NOW + timedelta(minutes=1), read_fee=lambda _: NS(rate=D("0.07"), exponent=D("1"), minimum_size=D("0.01")), clock=clock, sleep=clock.sleep)
        assert len(transport.posts) == 1
    finally:
        recovered.close()


def test_wallet_lock_excludes_concurrent_process_owner(tmp_path):
    journal = position(tmp_path)
    try:
        with pytest.raises(BlockingIOError):
            PositionJournal(tmp_path / 'position.json', 'wallet')
    finally:
        journal.close()


def test_depth_shortage_does_not_value_missing_shares_at_zero():
    assert liquidation_quote(book(Clock(), size='4'), D('5'), D('0.07'), D('1')) is None


def test_stale_book_never_posts(tmp_path):
    journal, clock, transport = position(tmp_path), Clock(), Transport()
    stale = book(clock)
    clock.sleep(3)
    try:
        assert manage_position(journal, transport, lambda *_: stale, lambda: None,
                               Audit(), NOW + timedelta(seconds=5), read_fee=lambda _: NS(rate=D("0.07"), exponent=D("1"), minimum_size=D("0.01")), clock=clock, sleep=clock.sleep) == 2
        assert transport.posts == []
        assert journal.state['state'] == 'OPEN'
    finally:
        journal.close()


def test_sell_wire_uses_shares_and_receipt_reverses_buy_units():
    posts = []
    client = NS(
        create_market_order=lambda **kw: NS(maker_amount=5_000_000, taker_amount=3_500_000,
                                           token_id=kw['token_id'], side=kw['side']),
        post_order=lambda signed: posts.append(signed) or NS(ok=True, status='matched', order_id='sell',
                                                            making_amount=D('5'), taking_amount=D('3.5')),
    )
    transport = OfficialOrderTransport(client)
    prepared = transport.prepare_protected_fak_sell(token_id='down', shares=D('5'), min_price=D('0.70'))
    assert transport.post_prepared_sell(prepared).average_price == D('0.70')
    with pytest.raises(OfficialOrderError, match='already submitted'):
        transport.post_prepared_sell(prepared)
    assert len(posts) == 1


@pytest.mark.parametrize('status', ['MATCHED', 'MINED', 'RETRYING', 'FAILED'])
def test_unconfirmed_trades_never_become_sellable_inventory(status):
    trade = NS(id='trade', taker_order_id='buy', trader_side='TAKER', token_id='down',
               condition_id='condition', side='BUY', status=status, size=D('5'), price=D('0.7'))
    transport = OfficialOrderTransport(NS(list_account_trades=lambda **_: NS(iter_items=lambda: iter([trade]))))
    if status == 'FAILED':
        with pytest.raises(OfficialOrderError, match='failed settlement'):
            transport.confirmed_order_amounts(order_id='buy', token_id='down', market_id='condition', side='BUY')
    else:
        assert transport.confirmed_order_amounts(order_id='buy', token_id='down', market_id='condition', side='BUY') is None


def test_timeout_reduces_available_depth_without_assuming_missing_value(tmp_path):
    journal, clock, transport = position(tmp_path), Clock(), Transport()
    try:
        result = manage_position(
            journal, transport, lambda *_: book(clock, size='2'), lambda: None,
            Audit(), NOW + timedelta(minutes=1),
            read_fee=lambda _: NS(rate=D('0.07'), exponent=D('1'), minimum_size=D('0.01')),
            clock=clock, sleep=clock.sleep,
        )
        assert result == 0
        assert [order.shares for order in transport.posts] == [D('2'), D('2'), D('1')]
        assert clock() - NOW >= timedelta(seconds=30)
    finally:
        journal.close()


def test_settlement_can_close_position_after_market_end(tmp_path):
    journal, clock, transport = position(tmp_path), Clock(), Transport()
    journal.save(state='SELL_SETTLING', order_id='sell', expected_shares='5',
                 expected_gross='3.8', exit_fee_per_share_bound='0.0175')
    transport.fills['sell'] = D('5'), D('3.8')
    clock.now = MARKET.window_end + timedelta(seconds=2)
    try:
        assert manage_position(
            journal, transport, lambda *_: book(clock), lambda: None, Audit(), clock(),
            read_fee=lambda _: None, clock=clock, sleep=clock.sleep,
        ) == 0
        assert not transport.posts
        assert journal.state['state'] == 'CLOSED'
    finally:
        journal.close()


def test_normalized_rejection_is_not_retried():
    client = NS(
        create_market_order=lambda **kw: NS(maker_amount=5_000_000, taker_amount=3_500_000,
                                           token_id=kw['token_id'], side=kw['side']),
        post_order=lambda _: NS(ok=False, code='unknown'),
    )
    transport = OfficialOrderTransport(client)
    prepared = transport.prepare_protected_fak_sell(token_id='down', shares=D('5'), min_price=D('.7'))
    with pytest.raises(OfficialOrderError, match='ambiguous'):
        transport.post_prepared_sell(prepared)
    with pytest.raises(OfficialOrderError, match='already submitted'):
        transport.post_prepared_sell(prepared)


@pytest.mark.parametrize('conflict', ['market', 'status', 'amount'])
def test_settlement_rejects_wrong_market_and_conflicting_duplicate(conflict):
    values = dict(id='trade', taker_order_id='buy', trader_side='TAKER', token_id='down',
                  condition_id='condition', side='BUY', status='CONFIRMED', size=D('5'), price=D('.7'))
    first = NS(**values)
    values.update({'condition_id': 'other'} if conflict == 'market' else
                  {'status': 'FAILED'} if conflict == 'status' else {'size': D('4')})
    client = NS(list_account_trades=lambda **_: NS(iter_items=lambda: iter([first, NS(**values)])))
    with pytest.raises(OfficialOrderError):
        OfficialOrderTransport(client).confirmed_order_amounts(
            order_id='buy', token_id='down', market_id='condition', side='BUY')


def test_take_profit_does_not_sign_loss_making_bottom_bid(tmp_path):
    journal, clock, transport = position(tmp_path), Clock(), Transport()
    def uneven_book(*_):
        return book(clock).model_copy(update={'bids': (
            book(clock, bid='0.95', size='4').bids[0],
            book(clock, bid='0.20', size='5').bids[0],
        )})
    try:
        assert manage_position(
            journal, transport, uneven_book, lambda: None, Audit(), NOW + timedelta(seconds=3),
            read_fee=lambda _: NS(rate=D('.07'), exponent=D(1), minimum_size=D('.01')),
            clock=clock, sleep=clock.sleep,
        ) == 2
        assert not transport.posts
    finally:
        journal.close()


def test_slow_journal_commit_cannot_post_after_authorization_expiry(tmp_path):
    journal, clock, transport = position(tmp_path), Clock(), Transport()
    save = journal.save
    def slow_save(**values):
        save(**values)
        if values.get('state') == 'SELL_PENDING':
            clock.sleep(3)
    journal.save = slow_save
    try:
        assert manage_position(
            journal, transport, lambda *_: book(clock), lambda: None, Audit(), NOW + timedelta(seconds=2),
            read_fee=lambda _: NS(rate=D('.07'), exponent=D(1), minimum_size=D('.01')),
            clock=clock, sleep=clock.sleep,
        ) == 2
        assert not transport.posts
        assert journal.state['state'] == 'OPEN'
    finally:
        journal.close()
