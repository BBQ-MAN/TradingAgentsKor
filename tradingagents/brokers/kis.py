"""KIS domestic cash orders with a durable duplicate-submission guard."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tradingagents.dataflows.errors import VendorError, VendorNotConfiguredError
from tradingagents.dataflows.vendors.kis import get_client, normalize_kis_symbol


class OrderSubmissionUnknown(VendorError):
    """An attempted submission could have reached the broker; reconcile first."""


class KISBroker:
    def __init__(self, client=None, account: str | None = None,
                 product: str | None = None, journal_path: str | Path | None = None):
        self.client = client if client is not None else get_client()
        self.account = account if account is not None else os.getenv("KIS_CANO", "")
        self.product = product if product is not None else os.getenv("KIS_ACNT_PRDT_CD", "01")
        if not re.fullmatch(r"[0-9]{8}", self.account) or not re.fullmatch(r"[0-9]{2}", self.product):
            raise VendorNotConfiguredError("KIS_CANO(계좌 앞 8자리), KIS_ACNT_PRDT_CD(뒤 2자리)를 설정하세요.")
        self.journal_path = Path(journal_path or os.getenv(
            "KIS_ORDER_JOURNAL", str(Path.home() / ".tradingagents" / "kis-orders.sqlite3")))
        self.scope = hashlib.sha256(
            f"{self.client.environment}:{self.account}:{self.product}".encode()).hexdigest()

    def preview(self, symbol: str, side: str, quantity: int, price: int = 0,
                order_type: str = "limit") -> dict:
        code = normalize_kis_symbol(symbol)
        if side not in {"buy", "sell"} or order_type not in {"limit", "market"}:
            raise ValueError("side: buy/sell; order_type: limit/market")
        if type(quantity) is not int or quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        if type(price) is not int or price < 0:
            raise ValueError("price must be a non-negative integer KRW amount")
        if (order_type == "limit" and price == 0) or (order_type == "market" and price != 0):
            raise ValueError("지정가는 양수 가격, 시장가는 price=0을 사용하세요.")
        return {"environment": self.client.environment, "symbol": code, "side": side,
                "quantity": quantity, "price_krw": price, "order_type": order_type,
                "exchange": "KRX", "limit_notional_krw": quantity * price if price else None}

    def place_order(self, symbol: str, side: str, quantity: int, price: int = 0,
                    order_type: str = "limit", *, request_id: str, execute: bool = False) -> dict:
        order = self.preview(symbol, side, quantity, price, order_type)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_id):
            raise ValueError("request_id must contain 1-80 letters, digits, hyphens or underscores")
        if not execute:
            return {"status": "preview", "request_id": request_id, **order}
        if self.client.environment == "real" and os.getenv("KIS_ENABLE_LIVE_TRADING", "").lower() != "true":
            raise ValueError("실전 주문 전송은 KIS_ENABLE_LIVE_TRADING=true 설정이 필요합니다.")
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.journal_path, timeout=30)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS orders (scope TEXT, request_id TEXT, "
                       "status TEXT, payload TEXT, result TEXT, created_at TEXT, "
                       "PRIMARY KEY(scope, request_id))")
            # Commit before touching the network, including across CLI restarts.
            try:
                db.execute("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
                           (self.scope, request_id, "submitting", json.dumps(order), "",
                            datetime.now(ZoneInfo("Asia/Seoul")).isoformat()))
                db.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError("이미 사용된 request_id입니다. 주문체결 내역을 확인하세요.") from exc
            payload = {"CANO": self.account, "ACNT_PRDT_CD": self.product,
                       "PDNO": order["symbol"], "ORD_DVSN": "00" if order_type == "limit" else "01",
                       "ORD_QTY": str(quantity), "ORD_UNPR": str(price),
                       "EXCG_ID_DVSN_CD": "KRX", "SLL_TYPE": "01" if side == "sell" else "",
                       "CNDT_PRIC": ""}
            prefix = "TTTC" if self.client.environment == "real" else "VTTC"
            tr_id = prefix + ("0012U" if side == "buy" else "0011U")
            try:
                body = self.client.post("/uapi/domestic-stock/v1/trading/order-cash", tr_id, payload)
                output = body.get("output")
                if not isinstance(output, dict) or not output.get("ODNO"):
                    raise VendorError("KIS order response has no order number")
            except Exception as exc:
                db.execute("UPDATE orders SET status='unknown' WHERE scope=? AND request_id=?",
                           (self.scope, request_id))
                db.commit()
                reason = str(exc) if isinstance(exc, VendorError) else type(exc).__name__
                raise OrderSubmissionUnknown(
                    f"{reason}: 주문 결과를 확정할 수 없습니다. 자동 재전송하지 않습니다. "
                    "KIS 주문체결 조회에서 접수 여부를 확인하세요."
                ) from exc
            result = {"status": "submitted", "request_id": request_id,
                      "order_number": output["ODNO"],
                      "branch_number": output.get("KRX_FWDG_ORD_ORGNO", ""),
                      "order_time": output.get("ORD_TMD", ""), **order}
            # submitted means accepted, not filled.
            db.execute("UPDATE orders SET status='submitted', result=? WHERE scope=? AND request_id=?",
                       (json.dumps(result), self.scope, request_id))
            db.commit()
            return result

    def orders(self, date: str, order_number: str = "") -> list[dict]:
        day = datetime.strptime(date, "%Y-%m-%d").date()
        today = datetime.now(ZoneInfo("Asia/Seoul")).date()
        if not 0 <= (today - day).days <= 89:
            raise ValueError("주문체결 조회는 오늘부터 최근 89일 이내 날짜를 사용하세요.")
        if order_number and not re.fullmatch(r"[0-9]+", order_number):
            raise ValueError("order_number must contain digits only")
        params = {"CANO": self.account, "ACNT_PRDT_CD": self.product,
                  "INQR_STRT_DT": day.strftime("%Y%m%d"), "INQR_END_DT": day.strftime("%Y%m%d"),
                  "SLL_BUY_DVSN_CD": "00", "CCLD_DVSN": "00", "INQR_DVSN": "00",
                  "INQR_DVSN_3": "00", "PDNO": "", "ORD_GNO_BRNO": "", "ODNO": order_number,
                  "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
                  "EXCG_ID_DVSN_CD": "KRX"}
        tr_id = "TTTC0081R" if self.client.environment == "real" else "VTTC0081R"
        rows, seen = [], set()
        for page in range(100):
            body = self.client.get("/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                                   tr_id, params.copy(), tr_cont="N" if page else "")
            if not isinstance(body.get("output1"), list):
                raise VendorError("KIS order query has no output1 list")
            rows.extend(body["output1"])
            if body.get("_tr_cont") not in {"M", "F"}:
                return rows
            cursor = (body.get("ctx_area_fk100", ""), body.get("ctx_area_nk100", ""))
            if cursor in seen or not any(part.strip() for part in cursor):
                raise VendorError("KIS order pagination did not advance; results are incomplete")
            seen.add(cursor)
            params.update(CTX_AREA_FK100=cursor[0], CTX_AREA_NK100=cursor[1])
        raise VendorError("KIS order pagination limit reached; results are incomplete")
