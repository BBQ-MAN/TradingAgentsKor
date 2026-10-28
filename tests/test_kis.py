from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests

from tradingagents.brokers.kis import KISBroker, OrderSubmissionUnknown
from tradingagents.dataflows.vendors import kis
from tradingagents.dataflows.errors import NoMarketDataError, VendorError, VendorUnavailableError


def response(body, status=200, headers=None):
    result = MagicMock(status_code=status, headers=headers or {})
    result.json.return_value = body
    return result


@pytest.fixture
def client(tmp_path):
    client = kis.KISClient("test-key", "test-secret", "demo", tmp_path / "tokens.sqlite3")
    client.session = MagicMock()
    client._interval = 0
    client._throttle_backoff = ()  # tests that want retries opt back in
    client.session.post.return_value = response({"access_token": "token", "expires_in": 86400})
    client.session.get.return_value = response({"rt_cd": "0", "output2": []})
    return client


@pytest.mark.parametrize("symbol", ["005930", "005930.KS", "005930.ks", "005930.KQ"])
def test_symbols(symbol):
    assert kis.normalize_kis_symbol(symbol) == "005930"


@pytest.mark.parametrize("symbol", [5930, "5930", "AAPL", "005930.T", "../005930", "１２３４５６"])
def test_invalid_symbols(symbol):
    with pytest.raises(ValueError):
        kis.normalize_kis_symbol(symbol)


def test_auth_reused_and_refreshes(client):
    client.get("/test", "TEST", {})
    client.get("/test", "TEST", {})
    assert client.session.post.call_count == 1
    assert client.session.get.call_args.kwargs["headers"]["authorization"] == "Bearer token"
    assert client.session.post.call_args.args[0].startswith("https://openapivts.")
    client._expires_at = 0
    client.get("/test", "TEST", {})
    assert client.session.post.call_count == 1  # disk cache survives memory expiry
    import sqlite3

    with sqlite3.connect(client.token_cache_path) as db:
        db.execute("UPDATE tokens SET expires_at=0")
    client._expires_at = 0
    client.get("/test", "TEST", {})
    assert client.session.post.call_count == 2


@pytest.mark.parametrize("body,status,error", [
    ({"msg_cd": "EGW00201"}, 500, VendorUnavailableError),
    ({}, 429, VendorUnavailableError),
    ({"rt_cd": "1", "msg_cd": "BAD"}, 200, VendorError),
    ({}, 200, VendorError),
])
def test_api_errors(client, body, status, error):
    client.session.get.return_value = response(body, status)
    with pytest.raises(error):
        client.get("/test", "TEST", {})


def test_throttled_read_retries_then_succeeds(client):
    client._throttle_backoff = (0, 0)
    client.session.get.side_effect = [
        response({"msg_cd": "EGW00201"}, 500),
        response({}, 429),
        response({"rt_cd": "0", "output2": []}),
    ]
    assert client.get("/test", "TEST", {})["rt_cd"] == "0"
    assert client.session.get.call_count == 3


def test_throttled_read_raises_once_the_budget_is_spent(client):
    client._throttle_backoff = (0, 0)
    client.session.get.return_value = response({"msg_cd": "EGW00201"}, 500)
    with pytest.raises(VendorUnavailableError):
        client.get("/test", "TEST", {})
    assert client.session.get.call_count == 3


def test_throttled_order_is_never_retried(client):
    client.get("/test", "TEST", {})  # authenticate first
    client._throttle_backoff = (0, 0)
    client.session.post.reset_mock()
    client.session.post.return_value = response({"msg_cd": "EGW00201"}, 500)
    with pytest.raises(VendorUnavailableError):
        client.post("/order", "VTTC0012U", {"ORD_QTY": "1"})
    assert client.session.post.call_count == 1


def row(day, close="100"):
    return {"stck_bsop_date": day, "stck_oprc": "90", "stck_hgpr": "110",
            "stck_lwpr": "80", "stck_clpr": close, "acml_vol": "1000"}


def test_chunking_sort_filter_and_adjustment(monkeypatch):
    client = MagicMock()
    client.get.side_effect = [
        {"output2": [row("20250410"), row("20250101"), row("20250410"), row("20250501")]},
        {"output2": [row("20250415"), row("20250411"), row("20250416")]},
    ]
    monkeypatch.setattr(kis, "get_client", lambda: client)
    frame = kis.load_kis_ohlcv("005930.KS", "2025-01-01", "2025-04-15")
    assert frame.Date.dt.strftime("%Y-%m-%d").tolist() == [
        "2025-01-01", "2025-04-10", "2025-04-11", "2025-04-15"]
    assert client.get.call_count == 2
    params = client.get.call_args.args[2]
    assert params["FID_INPUT_DATE_1"] == "20250411"
    assert params["FID_INPUT_DATE_2"] == "20250415"
    assert params["FID_ORG_ADJ_PRC"] == "0"
    assert params["FID_INPUT_ISCD"] == "005930"


@pytest.mark.parametrize("rows,error", [
    ([], NoMarketDataError), ([row("20250101")], NoMarketDataError),
    ([row("20250131", "NaN")], VendorError),
    ([row("20250131", "0")], VendorError),
])
def test_no_data_stale_and_invalid(monkeypatch, rows, error):
    client = MagicMock()
    client.get.return_value = {"output2": rows}
    monkeypatch.setattr(kis, "get_client", lambda: client)
    with pytest.raises(error):
        kis.load_kis_ohlcv("005930", "2025-01-01", "2025-01-31")


def test_indicator_uses_kis_and_requested_window(monkeypatch):
    dates = pd.date_range("2024-01-01", "2025-01-31", freq="B")
    frame = pd.DataFrame({"Date": dates, "Open": 100, "High": 101, "Low": 99,
                          "Close": range(100, 100 + len(dates)), "Volume": 1000})
    loader = MagicMock(return_value=frame)
    monkeypatch.setattr(kis, "load_indicator_ohlcv", loader)
    output = kis.get_indicators("005930", "close_200_sma", "2025-01-31", 2)
    assert "2025-01-31" in output and "2025-01-28" not in output
    loader.assert_called_once_with("005930", "2025-01-31", 2)


def test_routing_and_snapshot(monkeypatch):
    from tradingagents.dataflows.config import set_config
    from tradingagents.dataflows.router import VENDOR_METHODS, route_to_vendor
    from tradingagents.dataflows.vendors.yahoo.snapshot import build_verified_market_snapshot
    set_config({"data_vendors": {"core_stock_apis": "kis", "technical_indicators": "kis"}})
    assert VENDOR_METHODS["get_stock_data"]["kis"] is kis.get_stock_data
    assert VENDOR_METHODS["get_indicators"]["kis"] is kis.get_indicators
    frame = pd.DataFrame({"Date": pd.to_datetime(["2025-01-31"]), "Open": [100],
                          "High": [110], "Low": [90], "Close": [105], "Volume": [1000]})
    monkeypatch.setattr(kis, "load_kis_ohlcv", lambda *a: frame)
    assert "KRW" in route_to_vendor("get_stock_data", "005930", "2025-01-01", "2025-01-31")
    assert "105.00" in build_verified_market_snapshot("005930", "2025-01-31")


@pytest.fixture
def broker(tmp_path):
    client = MagicMock(environment="demo")
    client.post.return_value = {"output": {"ODNO": "123", "KRX_FWDG_ORD_ORGNO": "456"}}
    return KISBroker(client, "12345678", "01", tmp_path / "orders.sqlite3")


def test_preview_never_calls_network_or_writes(broker):
    result = broker.place_order("005930", "buy", 1, 70000, request_id="a")
    assert result["status"] == "preview"
    assert not broker.journal_path.exists()
    broker.client.post.assert_not_called()


@pytest.mark.parametrize("side,kind,price,tr_id", [
    ("buy", "limit", 70000, "VTTC0012U"),
    ("sell", "market", 0, "VTTC0011U"),
])
def test_submission_and_durable_duplicate_guard(broker, side, kind, price, tr_id):
    result = broker.place_order("005930.KS", side, 2, price, kind, request_id="a", execute=True)
    assert result["status"] == "submitted"
    assert result["order_number"] == "123"
    args = broker.client.post.call_args.args
    assert args[1] == tr_id
    assert args[2]["ORD_QTY"] == "2"
    assert args[2]["ORD_UNPR"] == str(price)
    assert args[2]["EXCG_ID_DVSN_CD"] == "KRX"
    second = KISBroker(broker.client, "12345678", "01", broker.journal_path)
    with pytest.raises(ValueError, match="request_id"):
        second.place_order("005930", side, 2, price, kind, request_id="a", execute=True)
    assert broker.client.post.call_count == 1


def test_ambiguous_timeout_not_retried(broker):
    broker.client.post.side_effect = requests.Timeout()
    with pytest.raises(OrderSubmissionUnknown):
        broker.place_order("005930", "buy", 1, 70000, request_id="a", execute=True)
    with pytest.raises(ValueError, match="request_id"):
        broker.place_order("005930", "buy", 1, 70000, request_id="a", execute=True)
    assert broker.client.post.call_count == 1


def test_live_gate_and_real_transaction_id(broker, monkeypatch):
    broker.client.environment = "real"
    monkeypatch.delenv("KIS_ENABLE_LIVE_TRADING", raising=False)
    with pytest.raises(ValueError, match="KIS_ENABLE"):
        broker.place_order("005930", "buy", 1, 70000, request_id="a", execute=True)
    broker.client.post.assert_not_called()
    monkeypatch.setenv("KIS_ENABLE_LIVE_TRADING", "true")
    broker.place_order("005930", "buy", 1, 70000, request_id="a", execute=True)
    assert broker.client.post.call_args.args[1] == "TTTC0012U"


@pytest.mark.parametrize("quantity,price,kind", [
    (0, 100, "limit"), (1.5, 100, "limit"), (True, 100, "limit"),
    (1, 0, "limit"), (1, -1, "market"), (1, 100, "market"),
])
def test_bad_order_never_sent(broker, quantity, price, kind):
    with pytest.raises(ValueError):
        broker.place_order("005930", "buy", quantity, price, kind, request_id="a", execute=True)
    broker.client.post.assert_not_called()


def test_orders_pagination(broker):
    broker.client.get.side_effect = [
        {"output1": [{"odno": "1"}], "_tr_cont": "M", "ctx_area_fk100": "a", "ctx_area_nk100": "b"},
        {"output1": [{"odno": "2"}], "_tr_cont": "D"},
    ]
    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")
    assert len(broker.orders(today)) == 2
    args = broker.client.get.call_args
    assert args.kwargs["tr_cont"] == "N"
    assert args.args[1] == "VTTC0081R"
    assert args.args[2]["CTX_AREA_NK100"] == "b"


def test_korean_config_does_not_mutate_defaults():
    from copy import deepcopy

    from tradingagents.default_config import DEFAULT_CONFIG, korean_config
    before = deepcopy(DEFAULT_CONFIG)
    config = korean_config()
    assert config["data_vendors"]["core_stock_apis"] == "kis"
    assert config["benchmark_map"][".KQ"] == "^KQ11"
    assert before == DEFAULT_CONFIG


def test_token_cache_shared_across_client_instances(client):
    client.get("/test", "TEST", {})
    restarted = kis.KISClient("test-key", "test-secret", "demo", client.token_cache_path)
    restarted.session = MagicMock()
    restarted.session.get.return_value = response({"rt_cd": "0"})
    restarted.get("/test", "TEST", {})
    restarted.session.post.assert_not_called()
    assert restarted.session.get.call_args.kwargs["headers"]["authorization"] == "Bearer token"
    assert client.token_cache_path.stat().st_mode & 0o777 == 0o600


def test_token_cache_separates_environments(client):
    client.get("/test", "TEST", {})
    real = kis.KISClient("test-key", "test-secret", "real", client.token_cache_path)
    real.session = MagicMock()
    real.session.post.return_value = response({"access_token": "real-token", "expires_in": 86400})
    real.session.get.return_value = response({"rt_cd": "0"})
    real.get("/test", "TEST", {})
    real.session.post.assert_called_once()
    assert real.session.get.call_args.kwargs["headers"]["authorization"] == "Bearer real-token"


def test_http_order_post_not_retried(client):
    client.get("/test", "TEST", {})  # authenticate first
    client.session.post.reset_mock()
    client.session.post.side_effect = requests.Timeout()
    with pytest.raises(requests.Timeout):
        client.post("/order", "VTTC0012U", {"ORD_QTY": "1"})
    assert client.session.post.call_count == 1
    assert client.session.post.call_args.kwargs["allow_redirects"] is False


def test_order_missing_number_is_unknown(broker):
    broker.client.post.return_value = {"output": {}}
    with pytest.raises(OrderSubmissionUnknown):
        broker.place_order("005930", "buy", 1, 70000, request_id="missing", execute=True)
    with pytest.raises(ValueError, match="request_id"):
        broker.place_order("005930", "buy", 1, 70000, request_id="missing", execute=True)


def test_orders_repeated_cursor_fails(broker):
    broker.client.get.return_value = {
        "output1": [], "_tr_cont": "M", "ctx_area_fk100": "a", "ctx_area_nk100": "b"}
    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")
    with pytest.raises(VendorError, match="pagination"):
        broker.orders(today)
    assert broker.client.get.call_count == 2
