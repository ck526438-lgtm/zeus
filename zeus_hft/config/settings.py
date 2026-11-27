"""Paramètres typés (Pydantic) chargés depuis l'environnement ou un fichier .env.

Préfixe : ``ZEUS_HFT_`` (ex. ``ZEUS_HFT_SYMBOL=XAUUSD``).
Les identifiants MT5 réutilisent les variables de Zeus v1 : ``ZEUS_MT5_LOGIN``,
``ZEUS_MT5_PASSWORD``, ``ZEUS_MT5_SERVER``, ``ZEUS_MT5_PATH``. Aucune clé n'est
écrite en dur dans le code.
"""
from __future__ import annotations

from enum import Enum

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Mode(str, Enum):
    DRY_RUN = "DRY_RUN"     # flux MT5 réel, exécution simulée (paper trading)
    LIVE = "LIVE"           # ordres réels sur MT5
    BACKTEST = "BACKTEST"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ZEUS_HFT_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    mode: Mode = Mode.DRY_RUN
    symbol: str = "XAUUSD"

    # --- Connexion MT5 ---
    mt5_login: int | None = Field(None, validation_alias=AliasChoices("ZEUS_MT5_LOGIN", "ZEUS_HFT_MT5_LOGIN"))
    mt5_password: SecretStr = Field(SecretStr(""), validation_alias=AliasChoices("ZEUS_MT5_PASSWORD", "ZEUS_HFT_MT5_PASSWORD"))
    mt5_server: str = Field("", validation_alias=AliasChoices("ZEUS_MT5_SERVER", "ZEUS_HFT_MT5_SERVER"))
    mt5_path: str = Field("", validation_alias=AliasChoices("ZEUS_MT5_PATH", "ZEUS_HFT_MT5_PATH"))
    magic: int = 26_11_22
    deviation_points: int = Field(10, ge=0)
    poll_interval_ms: float = Field(2.0, ge=0.1, description="période de lecture tick/DOM")
    use_market_book: bool = True          # DOM MT5 (si le broker le fournit)
    reconcile_interval_ms: float = Field(50.0, ge=5)

    # --- Reconnexion / heartbeat ---
    reconnect_base_delay: float = Field(0.5, gt=0)
    reconnect_max_delay: float = Field(30.0, gt=0)
    reconnect_max_attempts: int = Field(0, ge=0, description="0 = infini")
    heartbeat_interval: float = Field(1.0, gt=0)

    # --- Stratégie OBI + micro-momentum ---
    order_qty: float = Field(0.01, gt=0, description="taille en lots")
    imbalance_levels: int = Field(5, ge=1)
    imbalance_decay: float = Field(0.8, gt=0, le=1)
    tick_flow_window: int = Field(50, ge=5)
    momentum_fast: int = Field(8, ge=2)
    momentum_slow: int = Field(40, ge=3)
    momentum_scale_bps: float = Field(1.0, gt=0)
    vol_halflife: int = Field(100, ge=2)
    warmup_ticks: int = Field(60, ge=1)
    entry_threshold: float = Field(0.45, gt=0, lt=1)
    exit_threshold: float = Field(0.10, ge=0, lt=1)
    aggressive_threshold: float = Field(0.75, gt=0, le=1)
    max_spread_points: float = Field(35.0, gt=0)
    min_vol_bps: float = Field(0.02, ge=0)
    max_vol_bps: float = Field(8.0, gt=0)
    take_profit_points: float = Field(60.0, gt=0)
    stop_loss_points: float = Field(40.0, gt=0)
    max_holding_s: float = Field(90.0, gt=0)
    decision_cooldown_ms: float = Field(250.0, ge=0)

    # --- Sélection des stratégies ---
    strategy: str = Field("auto", pattern="^(auto|momentum|meanrev|obi)$",
                          description="auto = choix selon le régime de marché")
    min_tp_spread_mult: float = Field(2.5, ge=1, description="TP visé >= k × spread, sinon pas d'entrée")
    exit_cooldown_s: float = Field(20.0, ge=0, description="pause après chaque sortie")

    # --- Régime de marché (bougies M1 reconstruites à partir des ticks) ---
    atr_period: int = Field(14, ge=2)
    atr_baseline_bars: int = Field(120, ge=10, description="ATR « normal » de référence")
    shock_velocity_atr: float = Field(2.0, gt=0, description="vitesse > k×ATR -> choc (news)")
    shock_pause_s: float = Field(120.0, ge=0)
    server_utc_offset_hours: float = Field(2.0, description="décalage heure serveur (backtest)")
    momentum_sessions_utc: str = "07:00-11:00,13:30-17:00"
    meanrev_sessions_utc: str = "19:30-21:30,00:30-06:00"
    session_open_blackout_min: float = Field(5.0, ge=0)
    session_opens_utc: str = "07:00,13:30"
    news_times_utc: str = Field("", description="'HH:MM' chaque jour ou 'AAAA-MM-JJ HH:MM', séparés par des virgules")
    news_before_min: float = Field(5.0, ge=0)
    news_after_min: float = Field(15.0, ge=0)

    # --- Stratégie 1 : scalping momentum / cassure ---
    range_window_s: float = Field(60.0, gt=1)
    velocity_window_ms: float = Field(2_000.0, gt=50)
    breakout_buffer_atr: float = Field(0.10, ge=0)
    velocity_entry_atr: float = Field(0.45, gt=0)
    mom_tp_atr: float = Field(0.8, gt=0)
    mom_sl_atr: float = Field(0.5, gt=0)
    mom_max_hold_s: float = Field(60.0, gt=0)
    mom_min_vol_ratio: float = Field(0.6, ge=0)
    mom_max_vol_ratio: float = Field(2.5, gt=0)

    # --- Stratégie 2 : micro mean-reversion ---
    mr_anchor: str = Field("ema", pattern="^(vwap|ema)$", description="ema = moyenne courte (défaut)")
    mr_ema_ticks: int = Field(300, ge=10)
    mr_entry_dev_atr: float = Field(1.2, gt=0)
    mr_exit_dev_atr: float = Field(0.2, ge=0)
    mr_sl_atr: float = Field(1.2, gt=0)
    mr_max_hold_s: float = Field(240.0, gt=0)
    mr_max_vol_ratio: float = Field(1.1, gt=0, description="au-delà : stratégie désactivée")
    mr_max_atr_points: float = Field(250.0, gt=0, description="ATR M1 max (points)")
    mr_stall_velocity_atr: float = Field(0.25, gt=0, description="l'extension doit ralentir")

    # --- Risque ---
    starting_equity: float = Field(10_000.0, gt=0, description="utilisé en DRY_RUN/BACKTEST")
    max_daily_drawdown: float = Field(300.0, gt=0, description="devise du compte")
    max_net_inventory: float = Field(0.05, gt=0, description="lots")
    max_orders_per_second: int = Field(8, ge=1)
    max_latency_ms: float = Field(150.0, gt=0)
    latency_breaches_to_kill: int = Field(3, ge=1)
    max_data_staleness_ms: float = Field(5_000.0, gt=0)
    flatten_on_kill: bool = True
    max_daily_loss_pct: float = Field(3.0, ge=0, description="% du capital du jour (0 = off)")
    max_consecutive_rejects: int = Field(5, ge=1)
    max_feed_silence_s: float = Field(30.0, gt=0, description="pas de tick -> pause")
    max_tick_lag_s: float = Field(10.0, gt=0, description="heure du tick vs heure système")
    server_sl_mult: float = Field(1.5, ge=0, description="SL de secours côté serveur = k × SL (0 = off)")

    # --- Coûts (paper / backtest) ---
    maker_fee_bps: float = 0.0
    taker_fee_bps: float = 0.0
    commission_per_lot: float = Field(7.0, ge=0, description="aller-retour / 2 par exécution")
    sim_latency_ms: float = Field(40.0, ge=0)
    sim_slippage_points: float = Field(2.0, ge=0)

    # --- Spécifications par défaut du symbole (écrasées par MT5 en DRY_RUN/LIVE) ---
    point: float = 0.01
    contract_size: float = 100.0
    volume_step: float = 0.01
    volume_min: float = 0.01
    stops_level_points: float = 0.0
    freeze_level_points: float = 0.0

    # --- Apprentissage continu ---
    model_file: str = "data/zeus_hft_model_{symbol}.json"   # mémoire de l'IA entre sessions
    journal_file: str = "data/zeus_hft_trades.csv"          # journal des aller-retours
    verdict_min_trades: int = Field(50, ge=10)

    log_level: str = "INFO"
    log_file: str = "data/zeus_hft.log"
    confirm_live: bool = False

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if self.momentum_fast >= self.momentum_slow:
            raise ValueError("momentum_fast doit être < momentum_slow")
        if self.exit_threshold >= self.entry_threshold:
            raise ValueError("exit_threshold doit être < entry_threshold")
        if self.mr_exit_dev_atr >= self.mr_entry_dev_atr:
            raise ValueError("mr_exit_dev_atr doit être < mr_entry_dev_atr")
        if self.order_qty > self.max_net_inventory:
            raise ValueError("order_qty ne peut pas dépasser max_net_inventory")
        return self

    def resolved_model_file(self) -> str:
        return self.model_file.format(symbol=self.symbol, mode=self.mode.value)

    @property
    def is_live(self) -> bool:
        return self.mode is Mode.LIVE
