"""Domestic-stock analysis entry point. Orders use python -m cli.kis separately."""
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from tradingagents.default_config import korean_config
from tradingagents.graph.trading_graph import TradingAgentsGraph


def main():
    parser = argparse.ArgumentParser(description="한국 증시 KIS 분석")
    parser.add_argument("--ticker", default="005930.KS", help="KOSPI: 005930.KS / KOSDAQ: 247540.KQ")
    parser.add_argument("--date", default=datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d"))
    args = parser.parse_args()
    from tradingagents.dataflows.vendors.kis import normalize_kis_symbol

    normalize_kis_symbol(args.ticker)
    if not args.ticker.upper().endswith((".KS", ".KQ")):
        parser.error("재무·뉴스 조회용 시장을 명시하세요: KOSPI .KS / KOSDAQ .KQ")
    graph = TradingAgentsGraph(debug=True, config=korean_config())
    _, decision = graph.propagate(args.ticker.upper(), args.date)
    print(decision)


if __name__ == "__main__":
    main()
