"""Run python -m cli.kis --help for explicit domestic-stock broker operations."""

import argparse
import json

from tradingagents.brokers.kis import KISBroker


def main():
    parser = argparse.ArgumentParser(description="한국투자증권 국내주식 현금 주문·체결 조회")
    commands = parser.add_subparsers(dest="command", required=True)
    order = commands.add_parser("order", help="기본은 미리보기; --execute 지정 시 전송")
    order.add_argument("symbol")
    order.add_argument("side", choices=["buy", "sell"])
    order.add_argument("quantity", type=int)
    order.add_argument("--price", type=int, default=0, help="원화 정수 가격")
    order.add_argument("--type", choices=["limit", "market"], default="limit", dest="order_type")
    order.add_argument("--request-id", required=True, help="주문별 고유 ID; 재사용 시 전송 차단")
    order.add_argument("--execute", action="store_true")
    orders = commands.add_parser("orders", help="해당 날짜 주문/체결 내역 전체 조회")
    orders.add_argument("date", help="YYYY-MM-DD, 최근 89일 이내")
    orders.add_argument("--order-number", default="")
    args = parser.parse_args()
    try:
        broker = KISBroker()
        if args.command == "order":
            result = broker.place_order(args.symbol, args.side, args.quantity, args.price,
                                        args.order_type, request_id=args.request_id, execute=args.execute)
        else:
            result = broker.orders(args.date, args.order_number)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        parser.exit(1, f"{type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
