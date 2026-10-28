"""Korea Investment & Securities domestic KRX REST client and price provider."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import pandas as pd
import requests
from stockstats import wrap

from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.vendors.yahoo.ohlcv import _assert_ohlcv_not_stale

logger = logging.getLogger(__name__)

_BASE_URLS = {
    "real": "https://openapi.koreainvestment.com:9443",
    "demo": "https://openapivts.koreainvestment.com:29443",
}
INDICATORS = {
    "close_50_sma", "close_200_sma", "close_10_ema", "macd", "macds", "macdh",
    "rsi", "boll", "boll_ub", "boll_lb", "atr", "vwma", "mfi",
}


def normalize_kis_symbol(symbol: str) -> str:
    """Accept six-digit domestic codes, optionally with Yahoo's KS/KQ suffix."""
    if not isinstance(symbol, str):
        raise ValueError("KIS 종목코드는 문자열이어야 합니다 (예: '005930').")
    match = re.fullmatch(r"([0-9]{6})(?:\.(?:KS|KQ))?", symbol.strip().upper())
    if not match:
        raise ValueError("KIS 국내주식 종목코드는 6자리입니다 (005930, 005930.KS, 247540.KQ).")
    return match[1]


class KISClient:
    """Token reuse across processes and paced requests; throttled reads retry, mutations never do."""

    # KIS caps traffic per second per app key and the cap clears within a
    # second, so a throttle is worth riding out rather than aborting on.
    THROTTLE_BACKOFF = (0.5, 1.5, 3.0)

    def __init__(self, app_key: str, app_secret: str, environment: str = "real",
                 token_cache_path: str | Path | None = None):
        if environment not in _BASE_URLS:
            raise ValueError("KIS_ENV must be real or demo")
        if not app_key or not app_secret:
            raise VendorNotConfiguredError("KIS_APP_KEY와 KIS_APP_SECRET을 설정하세요.")
        self.environment = environment
        self.base_url = _BASE_URLS[environment]
        self.app_key = app_key
        self.app_secret = app_secret
        self.session = requests.Session()
        self.token_cache_path = Path(token_cache_path or os.getenv(
            "KIS_TOKEN_CACHE", str(Path.home() / ".tradingagents" / "kis-tokens.sqlite3")))
        self._token_scope = hashlib.sha256(
            f"{environment}:{app_key}:{app_secret}".encode()).hexdigest()
        self._token = None
        self._expires_at = 0.0
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._interval = 0.6 if environment == "demo" else 0.12
        self._throttle_backoff = self.THROTTLE_BACKOFF

    @staticmethod
    def _body(response):
        if response.status_code == 429:
            raise VendorUnavailableError("KIS request rate limit exceeded")
        # Some KIS throttle responses use HTTP 500 with a structured error code.
        try:
            body = response.json()
        except ValueError as exc:
            raise VendorError("KIS returned a non-JSON response") from exc
        if not isinstance(body, dict):
            raise VendorError("KIS returned an invalid response")
        if body.get("msg_cd") == "EGW00201":
            raise VendorUnavailableError("KIS request rate limit exceeded")
        if response.status_code >= 400 or body.get("rt_cd", "0") != "0":
            # Do not echo response bodies or request headers containing credentials.
            raise VendorError(f"KIS request failed (HTTP {response.status_code}, code {body.get('msg_cd', 'unknown')})")
        return body

    def _authenticate(self):
        if self._token and time.monotonic() < self._expires_at:
            return
        self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.token_cache_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.token_cache_path, 0o600)
        # Serialize token issuance across CLI processes (KIS limits token grants).
        with closing(sqlite3.connect(self.token_cache_path, timeout=35)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS tokens (scope TEXT PRIMARY KEY, "
                       "token TEXT, expires_at REAL)")
            db.execute("BEGIN IMMEDIATE")
            cached = db.execute("SELECT token, expires_at FROM tokens WHERE scope=?",
                                (self._token_scope,)).fetchone()
            if cached and cached[1] > time.time() + 60:
                token, expiry = cached
            else:
                response = self.session.post(
                    self.base_url + "/oauth2/tokenP",
                    json={"grant_type": "client_credentials", "appkey": self.app_key,
                          "appsecret": self.app_secret}, timeout=30, allow_redirects=False,
                )
                # The grant counts against the same per-second cap, so pace from
                # it as well — whatever it returned.
                self._last_request = time.monotonic()
                body = self._body(response)
                token = body.get("access_token")
                try:
                    expires = int(body["expires_in"])
                except (KeyError, ValueError, TypeError) as exc:
                    raise VendorError("KIS token response has no valid expiry") from exc
                if not isinstance(token, str) or not token or expires <= 60:
                    raise VendorError("KIS token response is invalid")
                expiry = time.time() + expires
                db.execute("INSERT OR REPLACE INTO tokens VALUES (?, ?, ?)",
                           (self._token_scope, token, expiry))
            db.commit()
        self._token = token
        self._expires_at = time.monotonic() + expiry - time.time() - 60

    def get(self, path: str, tr_id: str, params: dict, tr_cont: str = "") -> dict:
        return self._request("GET", path, tr_id, params, tr_cont)

    def post(self, path: str, tr_id: str, payload: dict) -> dict:
        return self._request("POST", path, tr_id, payload)

    def _request(self, method: str, path: str, tr_id: str, payload: dict,
                 tr_cont: str = "") -> dict:
        with self._lock:
            self._authenticate()
            headers = {"authorization": f"Bearer {self._token}",
                       "appkey": self.app_key, "appsecret": self.app_secret,
                       "tr_id": tr_id, "custtype": "P", "tr_cont": tr_cont}
            # Reads are idempotent, so a throttle is retried: the KR vendor
            # chain is single-vendor, so the router has nothing to fall back to
            # and an unretried throttle aborts the whole analysis. Mutations
            # stay on one attempt — a resent order could fill twice.
            backoffs = self._throttle_backoff if method == "GET" else ()
            for attempt in range(len(backoffs) + 1):
                delay = self._interval - (time.monotonic() - self._last_request)
                if delay > 0:
                    time.sleep(delay)
                try:
                    return self._send(method, path, headers, payload)
                except VendorUnavailableError:
                    if attempt == len(backoffs):  # budget spent; let the caller see it
                        raise
                finally:
                    self._last_request = time.monotonic()
                # Only reached when the attempt was throttled and retries remain.
                logger.warning("KIS throttled %s; retrying in %.1fs.", tr_id, backoffs[attempt])
                time.sleep(backoffs[attempt])

    def _send(self, method: str, path: str, headers: dict, payload: dict) -> dict:
        if method == "POST":
            response = self.session.post(self.base_url + path, headers=headers,
                                         json=payload, timeout=30, allow_redirects=False)
        else:
            response = self.session.get(self.base_url + path, headers=headers,
                                        params=payload, timeout=30, allow_redirects=False)
        body = self._body(response)
        if 300 <= response.status_code < 400:
            raise VendorError("KIS returned an unexpected redirect")
        if body.get("rt_cd") != "0":
            raise VendorError("KIS response has no success status")
        body["_tr_cont"] = response.headers.get("tr_cont", "")
        return body


@lru_cache(maxsize=4)
def _client(app_key: str, app_secret: str, environment: str) -> KISClient:
    return KISClient(app_key, app_secret, environment)


def get_client() -> KISClient:
    return _client(os.getenv("KIS_APP_KEY", "").strip(),
                   os.getenv("KIS_APP_SECRET", "").strip(),
                   os.getenv("KIS_ENV", "real").strip().lower())


def load_kis_ohlcv(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Inclusive adjusted daily OHLCV, chunked below the API's 100-row limit."""
    code = normalize_kis_symbol(symbol)
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    if start > end:
        raise ValueError("start_date must be on or before end_date")
    client = get_client()
    records = []
    cursor = start
    while cursor <= end:
        # At most 100 calendar days also guarantees at most 100 trading rows.
        chunk_end = min(cursor + timedelta(days=99), end)
        body = client.get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            "FHKST03010100",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
             "FID_INPUT_DATE_1": cursor.strftime("%Y%m%d"),
             "FID_INPUT_DATE_2": chunk_end.strftime("%Y%m%d"),
             "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"},
        )
        rows = body.get("output2")
        if not isinstance(rows, list):
            raise VendorError("KIS daily response has no output2 list")
        for row in rows:
            if not isinstance(row, dict):
                raise VendorError("KIS daily response contains an invalid row")
            if not row.get("stck_bsop_date"):
                continue  # KIS can pad responses with empty rows.
            try:
                day = datetime.strptime(row["stck_bsop_date"], "%Y%m%d")
                if not cursor <= day <= chunk_end:
                    continue
                values = [float(row[key]) for key in (
                    "stck_oprc", "stck_hgpr", "stck_lwpr", "stck_clpr", "acml_vol")]
                if not all(pd.notna(v) and abs(v) != float("inf") for v in values):
                    raise ValueError("non-finite OHLCV")
                if any(v <= 0 for v in values[:4]) or values[4] < 0:
                    raise ValueError("invalid OHLCV")
                records.append([day, *values])
            except (KeyError, TypeError, ValueError) as exc:
                raise VendorError("KIS returned malformed daily OHLCV") from exc
        cursor = chunk_end + timedelta(days=1)
    if not records:
        raise NoMarketDataError(symbol, code, f"KIS has no rows between {start_date} and {end_date}")
    frame = pd.DataFrame(records, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    frame = frame.drop_duplicates("Date").sort_values("Date").reset_index(drop=True)
    _assert_ohlcv_not_stale(frame, end_date, symbol, code)
    return frame


def get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    frame = load_kis_ohlcv(symbol, start_date, end_date)
    return (f"# KIS KRX daily adjusted prices: {symbol}; currency KRW; timezone Asia/Seoul\n"
            "# Today's daily bar may be incomplete during the session.\n"
            + frame.to_csv(index=False))


def load_indicator_ohlcv(symbol: str, curr_date: str, look_back_days: int = 0) -> pd.DataFrame:
    end = datetime.strptime(curr_date, "%Y-%m-%d")
    start = end - timedelta(days=look_back_days + 730)
    return load_kis_ohlcv(symbol, start.strftime("%Y-%m-%d"), curr_date)


def get_indicators(symbol: str, indicator: str, curr_date: str, look_back_days: int) -> str:
    if indicator not in INDICATORS:
        raise ValueError(f"Unsupported KIS indicator: {indicator}")
    if not isinstance(look_back_days, int) or not 0 <= look_back_days <= 3650:
        raise ValueError("look_back_days must be an integer between 0 and 3650")
    data = load_indicator_ohlcv(symbol, curr_date, look_back_days)
    stats = wrap(data.copy())
    values = stats[indicator]
    cutoff = pd.Timestamp(curr_date) - pd.Timedelta(days=look_back_days)
    result = pd.DataFrame({"Date": data["Date"], indicator: values.to_numpy()})
    result = result[result["Date"] >= cutoff]
    return f"# KIS {symbol} {indicator}; prices in KRW\n" + result.to_csv(index=False, na_rep="N/A")
