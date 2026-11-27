"""Réglages du market making crypto (préfixe ZEUS_MM_). Aucune clé dans le code."""
from __future__ import annotations

from enum import Enum

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class MMMode(str, Enum):
    DRY_RUN = "DRY_RUN"     # flux réel, ordres simulés (file d'attente modélisée)
    LIVE = "LIVE"


class MMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ZEUS_MM_", env_file=".env",
                                      env_file_encoding="utf-8", extra="ignore")

    mode: MMMode = MMMode.DRY_RUN
    exchange: str = "mexc"
    symbol: str = "SOL_USDT"                    # contrat perpétuel MEXC (paire à 0 % maker)
    api_key: SecretStr = Field(SecretStr(""), validation_alias=AliasChoices("ZEUS_MM_API_KEY", "MEXC_API_KEY"))
    api_secret: SecretStr = Field(SecretStr(""), validation_alias=AliasChoices("ZEUS_MM_API_SECRET", "MEXC_API_SECRET"))
    ws_url: str = "wss://contract.mexc.com/edge"
    rest_url: str = "https://api.mexc.com"
    leverage: int = Field(3, ge=1, le=20)
    confirm_live: bool = False

    # --- Spécifications du contrat (lues sur l'exchange au démarrage) ---
    tick: float = Field(0.01, gt=0, description="pas de prix (priceUnit)")
    contract_size: float = Field(1.0, gt=0, description="quantité d'actif par contrat")
    min_vol: float = Field(1.0, gt=0)
    vol_step: float = Field(1.0, gt=0)
    maker_fee: float = Field(0.0, ge=-0.001, le=0.01, description="0.0 = 0 %, -0.00005 = rebate")
    taker_fee: float = Field(0.0002, ge=0, le=0.01)

    # --- Cotation ---
    quote_size: float = Field(1.0, gt=0, description="contrats par ordre")
    max_inventory: float = Field(5.0, gt=0, description="contrats (valeur absolue)")
    min_half_spread_ticks: float = Field(0.5, gt=0, description="0.5 = au meilleur prix quand le spread vaut 1 tick")
    vol_mult: float = Field(1.5, ge=0, description="demi-spread >= k × volatilité sur l'horizon")
    vol_horizon_s: float = Field(2.0, gt=0)
    skew_ticks: float = Field(2.0, ge=0, description="décalage à inventaire max (ticks)")
    flow_alpha: float = Field(0.5, ge=0, description="poids du flux d'ordres dans le prix juste")
    requote_ticks: float = Field(2.0, gt=0, description="on ne bouge un ordre que si l'écart >= N ticks (garde la file)")
    min_requote_ms: float = Field(150.0, ge=0)

    # --- Toxicité / sélection adverse ---
    flow_window_s: float = Field(3.0, gt=0)
    toxic_flow: float = Field(0.65, gt=0, le=1, description="déséquilibre acheteurs/vendeurs -> retrait")
    sweep_levels: int = Field(3, ge=2, description="trade qui traverse N niveaux -> retrait")
    pull_ms: float = Field(1500.0, ge=0, description="durée du retrait après un signal toxique")
    markout_s: float = Field(5.0, gt=0, description="horizon de mesure de la sélection adverse")
    markout_window: int = Field(50, ge=5)
    max_adverse_bps: float = Field(3.0, gt=0, description="markout moyen < -N bps -> pause")

    # --- Risque ---
    max_daily_loss: float = Field(50.0, gt=0, description="USDT")
    max_orders_per_second: int = Field(8, ge=1)
    max_feed_silence_s: float = Field(5.0, gt=0)
    starting_equity: float = Field(1_000.0, gt=0)

    # --- Simulation (DRY_RUN / backtest) ---
    sim_latency_ms: float = Field(30.0, ge=0)
    sim_latency_jitter_ms: float = Field(20.0, ge=0)
    record_dir: str = "data/mm"
    log_file: str = "data/zeus_mm.log"

    def mexc_symbol(self) -> str:
        return self.symbol.upper().replace("/", "_").replace("-", "_")
