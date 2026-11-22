from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from zeus.backtest import run_backtest
from zeus.config import load_config
from zeus.data import synthetic
from zeus.features import FEATURE_COLUMNS, build_features
from zeus.indicators import atr, rsi
from zeus.model import ZeusModel, triple_barrier_labels
from zeus.risk import RiskManager, lot_size
from zeus.strategy import NONE, Strategy


@pytest.fixture(scope="module")
def cfg():
    return load_config("config.yaml")


@pytest.fixture(scope="module")
def df():
    return synthetic(4000, seed=1)


def test_rsi_bounds(df):
    r = rsi(df["close"])
    assert r.between(0, 100).all()


def test_atr_positive(df):
    assert (atr(df).dropna() > 0).all()


def test_features_no_lookahead(df, cfg):
    """Modifier le futur ne doit pas changer les features du passé."""
    f1 = build_features(df, cfg["strategy"])
    df2 = df.copy()
    df2.loc[3000:, ["open", "high", "low", "close"]] *= 1.5
    f2 = build_features(df2, cfg["strategy"])
    pd.testing.assert_frame_equal(f1.loc[:2999, FEATURE_COLUMNS], f2.loc[:2999, FEATURE_COLUMNS])


def test_lot_size_respects_risk():
    # 1000 $ de capital, 1 % de risque, SL à 3 $ sur l'or (1 lot = 100 oz → 300 $ par lot)
    lots = lot_size(1000, 1.0, 3.0, tick_value=1.0, tick_size=0.01,
                    vol_min=0.01, vol_max=100, vol_step=0.01)
    assert lots == pytest.approx(0.03)
    assert lots * 300 <= 10 + 1e-9


def test_lot_size_zero_if_min_too_big():
    assert lot_size(50, 0.5, 10.0, 1.0, 0.01, 0.01, 100, 0.01) == 0.0


def test_daily_loss_blocks(cfg):
    rm = RiskManager(cfg["risk"], [])
    now = datetime(2026, 1, 5, 10, tzinfo=timezone.utc)
    rm.new_day_check(now, 1000)
    ok, _ = rm.can_open("X", now, 1000, 10)
    assert ok
    ok, why = rm.can_open("X", now, 960, 10)   # -4 % > 3 %
    assert not ok and "perte" in why


def test_consecutive_losses_pause(cfg):
    rm = RiskManager(cfg["risk"], [])
    now = datetime(2026, 1, 5, 10, tzinfo=timezone.utc)
    rm.new_day_check(now, 1000)
    for _ in range(cfg["risk"]["max_consecutive_losses"]):
        rm.on_close(-5, now)
    ok, why = rm.can_open("X", now + timedelta(minutes=1), 1000, 10)
    assert not ok and "pause" in why


def test_spread_blocks(cfg):
    rm = RiskManager(cfg["risk"], [])
    now = datetime(2026, 1, 5, 10, tzinfo=timezone.utc)
    rm.new_day_check(now, 1000)
    assert not rm.can_open("X", now, 1000, 999)[0]


def test_trailing_only_tightens(cfg):
    rm = RiskManager(cfg["risk"], [])
    assert rm.trail(1, 100, 100.5, 98.5, 1.0) is None          # pas encore +1 ATR
    new = rm.trail(1, 100, 102, 98.5, 1.0)
    assert new is not None and new >= 100                      # break-even au minimum
    assert rm.trail(1, 100, 101.5, new, 1.0) is None           # ne recule jamais


def test_triple_barrier_shapes(df, cfg):
    f = build_features(df, cfg["strategy"])
    ly, sy = triple_barrier_labels(f, 1.5, 2.0, 30)
    valid = np.isfinite(ly)
    assert set(np.unique(ly[valid])) <= {0.0, 1.0}
    assert not np.isfinite(ly[-1])


def test_strategy_returns_signal(df, cfg):
    s = Strategy(cfg["strategy"], None)
    f = s.prepare(df)
    sigs = [s.evaluate(f, i) for i in range(200, 1200)]
    assert any(x.direction != NONE for x in sigs)


def test_backtest_runs(df, cfg):
    res = run_backtest(df, cfg, None)
    assert res["trades"] > 0
    assert np.isfinite(res["final_balance"])
    assert res["max_drawdown_%"] >= 0


def test_model_train_and_predict(cfg, tmp_path):
    data = synthetic(6000, seed=3)
    m = ZeusModel()
    m.fit(data, cfg["strategy"], cfg["risk"], horizon=20, n_splits=2)
    path = tmp_path / "m.joblib"
    m.save(path)
    m2 = ZeusModel.load(path)
    f = build_features(data, cfg["strategy"]).dropna(subset=FEATURE_COLUMNS)
    p = m2.predict_proba(f.iloc[[-1]], 1)
    assert 0.0 <= p <= 1.0
    assert m2.train_end is not None
