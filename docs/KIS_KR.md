# 한국 증시 분석 및 한국투자증권 주문

## 설정

```bash
uv sync --extra dev
cp .env.example .env
```

`.env`에 LLM API 키와 다음 KIS 설정을 입력합니다. 키는 저장소에 커밋하지 마세요.

```dotenv
TRADINGAGENTS_MARKET=KR
KIS_ENV=real
KIS_APP_KEY=발급받은앱키
KIS_APP_SECRET=발급받은앱시크리트
KIS_CANO=계좌앞8자리
KIS_ACNT_PRDT_CD=01
KIS_ENABLE_LIVE_TRADING=false
```

`real`은 실전, `demo`는 모의 API입니다. 환경에 맞는 앱키와 계좌를 사용합니다.
시세 조회에는 계좌번호가 필요하지 않습니다. 토큰은 기본 `~/.tradingagents/kis-tokens.sqlite3`에 소유자 전용 권한으로
저장하고 만료 전까지 프로세스 간 재사용합니다. `KIS_TOKEN_CACHE`로 경로를 지정할 수 있습니다.
계좌 환경·앱키별로 분리하고 동시에 실행된 프로세스의 토큰 발급은 직렬화합니다. 실행 중 호출 간격은 실전 0.12초, 모의 0.6초입니다.
다른 프로세스의 호출량까지 합산하여 제한하지는 않습니다.

## 국내주식 분석

```bash
uv run python main.py --ticker 005930.KS --date 2026-09-15
uv run python main.py --ticker 247540.KQ --date 2026-09-15
# 대화형 CLI: .env의 TRADINGAGENTS_MARKET=KR 적용
uv run python -m cli.main
```

날짜를 생략하면 한국 시간 기준 오늘입니다. KIS는 종목의 6자리 코드를 사용하지만,
재무·뉴스·기업 식별 등 Yahoo 자료를 함께 사용하기 위해 분석 진입점에는 `.KS`/`.KQ`를
명시합니다. 시장을 임의 추정하지 않습니다.

KIS 일봉은 시작·종료일 포함, 수정주가, KRX 기준입니다. 한 번에 최대 100건이므로
100일 이하 구간으로 나누어 요청합니다. 기술지표는 같은 KIS 일봉으로 계산하며
과거 분석 시 기준일 이후 가격을 제외합니다. 오늘 봉은 장중에 변경될 수 있습니다.
통화는 원화(KRW), 날짜 기준은 Asia/Seoul입니다.

시세·기술지표·검증 스냅샷은 KIS를 사용합니다. 재무제표, 뉴스, 기업명 확인,
회고의 수익률 계산은 기존 공급자 경로를 유지합니다. 한국 관련 뉴스 검색어와
KOSPI/KOSDAQ 벤치마크를 추가했으나 모든 한국 기업의 재무·뉴스 자료가 보장되지는 않습니다.
현재 KIS 연동 범위는 6자리 국내 주식/ETF 현금 거래입니다. 해외주식, 신용·공매도,
NXT/SOR, 실시간 WebSocket, 정정·취소 자동화는 포함하지 않습니다. 주문 취소는 HTS/MTS를 사용합니다.

기존 해외시장 설정은 `TRADINGAGENTS_MARKET`을 비우고 기존 CLI 또는 `DEFAULT_CONFIG`를
사용하면 됩니다. `main.py`는 국내주식 전용 예제입니다.

## 주문 미리보기

주문별로 재사용하지 않을 `request-id`를 지정합니다. 아래 명령은 전송하지 않습니다.
가격은 예시이며 현재 시세가 아닙니다.

```bash
uv run python -m cli.kis order 005930 buy 1 --price 70000 --request-id samsung-buy-001
```

계좌·키 형식이 설정되어 있어야 하지만 미리보기는 네트워크 호출이나 주문 기록을 만들지 않습니다.
지정가는 양의 정수 원화 가격, 수량은 양의 정수입니다. 시장가는 `--type market`을 사용하며
가격을 지정하지 않습니다. 호가 단위, 주문 가능 금액·보유 수량 등 최종 수락 여부는 KIS가 판단합니다.

## 실전 주문 전송

`.env`에서 `KIS_ENV=real`, `KIS_ENABLE_LIVE_TRADING=true`를 설정한 후
명시적으로 `--execute`를 추가합니다. **아래 명령을 실행하면 실제 주문이 전송됩니다.**

```bash
uv run python -m cli.kis order 005930 buy 1 --price 70000 --request-id samsung-buy-001 --execute
uv run python -m cli.kis order 005930 sell 1 --type market --request-id samsung-sell-001 --execute
```

모의 주문은 `KIS_ENV=demo`와 모의 계좌/앱키를 사용하고 `--execute`를 지정합니다.
분석 결과는 자동으로 주문에 전달되지 않습니다. LLM 분석의 수량·가격을 실행하려면
이 주문 인터페이스에 명시적으로 전달해야 합니다.

응답의 `submitted`는 주문 접수이며 체결 완료가 아닙니다. `order_number`로 체결을 확인합니다.

```bash
uv run python -m cli.kis orders 2026-09-15
uv run python -m cli.kis orders 2026-09-15 --order-number 0000123456
```

최근 89일 이내 하루 단위 조회와 연속 페이지 조회를 지원합니다. 출력은 KIS 원본 주문/체결 필드입니다.
로컬 중복 전송 방지 기록은 기본 `~/.tradingagents/kis-orders.sqlite3`에 저장됩니다.
필요하면 `KIS_ORDER_JOURNAL`에 고정된 절대 경로를 지정하세요. 주문 요청은 네트워크 호출 전에
기록하고 동일 환경·계좌의 같은 요청 ID는 프로세스를 재실행해도 전송하지 않습니다.
통신 오류·응답 파싱 실패는 `unknown`으로 남고 자동 재전송하지 않습니다. 새 ID로 다시 주문하기 전
HTS/MTS 또는 주문체결 조회로 접수 여부를 확인하세요. 이 기록은 브로커의 멱등성 보장이 아니며,
다른 기록 파일이나 다른 ID로 전송하면 중복 주문이 될 수 있습니다.

## 검증과 공식 규격

```bash
uv run pytest -q tests/test_kis.py
```

테스트는 HTTP 및 주문 응답을 대체합니다. 실제 계좌 인증·주문·체결은 테스트하지 않습니다.

- [공식 국내주식 일봉 예제](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_itemchartprice/inquire_daily_itemchartprice.py)
- [공식 현금 주문 예제](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/order_cash/order_cash.py)
- [공식 주문체결 조회 예제](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_ccld/inquire_daily_ccld.py)

공식 예제 확인일: 2026-09-16. 실전 매수/매도 TR은 `TTTC0012U`/`TTTC0011U`,
모의는 `VTTC0012U`/`VTTC0011U`이며 거래소 필드는 `KRX`입니다.
