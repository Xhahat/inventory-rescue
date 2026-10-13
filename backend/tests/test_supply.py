from datetime import date, timedelta

import pytest

from app.services.supply import (ReceiptRecord, effective_lead_time, reorder_point, round_order_qty, safety_stock,
                                 supplier_metrics)


def _r(delay: int, short: int = 0) -> ReceiptRecord:
    po = date(2026, 1, 1)
    exp = po + timedelta(days=7)
    return ReceiptRecord(po, exp, exp + timedelta(days=delay), 100, 100 - short)


def test_supplier_metrics_p80_and_effective_lead_time():
    rs = [_r(d) for d in (0, 0, 0, 0, 0, 1, 2, 3, 5, 10)]
    m = supplier_metrics("S", 7, rs)
    assert m.n_receipts == 10
    assert m.on_time_rate == pytest.approx(0.5)
    # numpy linear percentile of [0,0,0,0,0,1,2,3,5,10] at 80 -> 3 + 0.2*(5-3) = 3.4
    assert m.p80_delay_days == pytest.approx(3.4)
    assert m.mean_delay_days == pytest.approx(2.1)
    assert m.effective_lead_time_days == 7 + 4  # stated + ceil(P80)
    assert m.fill_rate == 1.0


def test_early_arrivals_do_not_count_as_negative_delay_and_fill_rate():
    m = supplier_metrics("S", 7, [_r(-2, short=10), _r(0)])
    assert m.p80_delay_days == 0
    assert m.on_time_rate == 1.0
    assert m.fill_rate == pytest.approx(0.95)


def test_no_receipts_uses_stated():
    m = supplier_metrics("S", 9, [])
    assert m.effective_lead_time_days == 9 and m.on_time_rate == 1.0


def test_effective_lead_time_integer_p80_not_bumped():
    assert effective_lead_time(10, 3.0) == 13
    assert effective_lead_time(10, 3.01) == 14
    assert effective_lead_time(10, 0.0) == 10


def test_safety_stock_and_rop():
    # z * sqrt(LT*sd^2 + d^2*sLT^2) = 1.645 * sqrt(10*16 + 100*4) = 1.645 * sqrt(560)
    ss = safety_stock(1.645, 10, 4.0, 10.0, 2.0)
    assert ss == pytest.approx(1.645 * 560 ** 0.5)
    assert reorder_point(10, 10, ss) == pytest.approx(100 + ss)
    assert safety_stock(1.645, 10, 0.0, 10.0, 0.0) == 0.0


@pytest.mark.parametrize("qty,moq,pack,expected", [
    (0, 48, 24, 0), (-5, 48, 24, 0), (1, 48, 24, 48), (49, 48, 24, 72), (72, 48, 24, 72), (73, 12, 12, 84),
    (10, 0, 1, 10), (10.2, 0, 1, 11),
])
def test_round_order_qty(qty, moq, pack, expected):
    assert round_order_qty(qty, moq, pack) == expected
