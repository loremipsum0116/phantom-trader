"""Strategy factory — maps config to strategy instances."""
from __future__ import annotations

from config import StrategyConfig, STRATEGIES
from indicators.hub import IndicatorHub
from strategies.base_strategy import BaseStrategy
from strategies.trailing_breakout import TrailingBreakoutStrategy
from strategies.keltner_breakout import KeltnerBreakoutStrategy
from strategies.fixed_breakout import FixedTPBreakoutStrategy
from strategies.ma_crossover import MACrossoverStrategy
from strategies.multi_coin import MultiCoinStrategy
from strategies.composite import CompositeStrategy
from utils.logger import log

# strategy_id prefix → strategy class mapping
_STRATEGY_MAP = {
    "S1_trail":    TrailingBreakoutStrategy,  # #1
    "S2_keltner":  KeltnerBreakoutStrategy,   # #2
    "S3_trail":    TrailingBreakoutStrategy,   # #3
    "S4_trail":    TrailingBreakoutStrategy,   # #4
    "S5_fixed":    FixedTPBreakoutStrategy,    # #5
    "S6_ma":       MACrossoverStrategy,        # #6
    "S7_trail":    TrailingBreakoutStrategy,   # #7
    "S8_multi":    MultiCoinStrategy,          # #8
    "S9_ma":       MACrossoverStrategy,        # #9
    "S10_composite": CompositeStrategy,        # #10
}


def _match_strategy_class(strategy_id: str) -> type[BaseStrategy] | None:
    """Match a strategy class from strategy_id."""
    for prefix, cls in _STRATEGY_MAP.items():
        if strategy_id.startswith(prefix):
            return cls
    return None


def create_strategies(hub: IndicatorHub) -> dict[str, BaseStrategy]:
    """
    Create all strategy instances from config.STRATEGIES.
    
    Returns:
        {strategy_id: BaseStrategy}
    """
    strategies: dict[str, BaseStrategy] = {}
    for cfg in STRATEGIES:
        cls = _match_strategy_class(cfg.strategy_id)
        if cls is None:
            log.error("전략 클래스 매핑 실패: %s", cfg.strategy_id)
            continue
        strategy = cls(cfg, hub)
        strategies[cfg.strategy_id] = strategy
        log.info(
            "✅ 전략 생성: %s (%s) [%s, %dx, %s]",
            cfg.strategy_name, cls.__name__,
            cfg.timeframe, cfg.leverage, cfg.direction,
        )
    log.info("총 %d개 전략 인스턴스 생성 완료", len(strategies))
    return strategies