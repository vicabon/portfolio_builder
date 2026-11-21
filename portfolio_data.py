"""資料層：即時報價、匯率、歷史回朔。

環境限制：公司 SSL 代理會讓 yfinance（curl_cffi）握手失敗，但標準 requests
可正常連到 Yahoo chart API 與台灣證交所。因此本模組一律以 requests 實作：

- 台股/台灣 ETF：台灣證券交易所 (TWSE) MIS 即時 API，盤中即時、盤後最後成交價。
- 美股/美股 ETF：Yahoo Finance chart API（regularMarketPrice）取得最即時報價。
- 匯率：Google Finance USD/TWD 優先，失敗改用 Yahoo TWD=X 即時匯率。
- 歷史：Yahoo chart API 歷史收盤價，美股以歷史匯率換算台幣。
"""

import re
import time

import requests

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    )
}
_YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"

# 匯率快取（避免每檔報價都打一次匯率來源）
_fx_cache = {"rate": None, "ts": 0.0}
_FX_TTL_SECONDS = 300

# 名稱快取
_name_cache = {}


def is_taiwan_ticker(ticker):
    """判斷是否為台股/台灣 ETF（yfinance 慣例：.TW / .TWO）。"""
    return ticker.upper().endswith((".TW", ".TWO"))


def is_cash(ticker):
    """判斷是否為現金部位（價格恆為 1 台幣，無報價來源）。"""
    return ticker.upper() in ("TWD-CASH", "CASH", "TWD")


def _to_float(value):
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _twse_channel(ticker):
    """把代號轉成 TWSE MIS 頻道字串。

    2330.TW  -> tse_2330.tw （上市）
    6488.TWO -> otc_6488.tw （上櫃）
    """
    code = ticker.split(".")[0]
    if ticker.upper().endswith(".TWO"):
        return f"otc_{code}.tw"
    return f"tse_{code}.tw"


# ---------------------------------------------------------------- Yahoo chart

def _yahoo_chart(ticker, rng=None, interval="1d", period1=None, period2=None):
    """回傳 Yahoo chart API 的 result[0]，失敗回 None。"""
    params = {"interval": interval}
    if period1 is not None:
        # 指定起訖時間戳可取得每日granularity（range=max 會被降頻成月）
        params["period1"] = period1
        params["period2"] = period2 or int(time.time())
    else:
        params["range"] = rng or "5d"
    try:
        resp = requests.get(
            _YAHOO_CHART.format(sym=ticker), params=params, headers=_HEADERS, timeout=10
        )
        resp.raise_for_status()
        payload = resp.json()
        results = (payload.get("chart") or {}).get("result") or []
        return results[0] if results else None
    except Exception as error:
        print(f"[yahoo] chart 失敗 {ticker}：{error}")
        return None


def _yahoo_quote(ticker):
    """Yahoo 即時報價 + 名稱。回傳 (price, name)。"""
    result = _yahoo_chart(ticker, rng="5d")
    if not result:
        return None, None
    meta = result.get("meta") or {}
    price = meta.get("regularMarketPrice")
    if price is None:
        closes = (((result.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
        closes = [c for c in closes if c is not None]
        price = closes[-1] if closes else None
    name = meta.get("shortName") or meta.get("longName")
    if name:
        _name_cache[ticker.upper()] = name
    return (_to_float(price), name)


def _yahoo_history(ticker, start_ts):
    """回傳 {date_str: close_float}，以日為單位。"""
    result = _yahoo_chart(ticker, period1=start_ts, period2=int(time.time()), interval="1d")
    if not result:
        return {}
    timestamps = result.get("timestamp") or []
    closes = (((result.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    out = {}
    upper_ticker = ticker.strip().upper()
    for ts, close in zip(timestamps, closes):
        if close is None or ts < start_ts:
            continue
        date_str = time.strftime("%Y-%m-%d", time.gmtime(ts))
        val = float(close)
        # Yahoo Finance 對 0052.TW（富邦科技 2025-11-17 1拆7）分割前歷史價未自動除權還原，手動除以 7
        if upper_ticker == "0052.TW" and date_str < "2025-11-17" and val > 60:
            val /= 7.0
        # Yahoo Finance 對 00662.TW（富邦NASDAQ 2026-11-17 1拆5）分割前歷史價未自動除權還原，手動除以 5
        if upper_ticker == "00662.TW" and date_str < "2026-11-17" and val > 25.8:
            val /= 5.0
        out[date_str] = val
    return out


# ------------------------------------------------------------------ 匯率

def get_usd_twd_rate():
    """取得 USD -> TWD 即時匯率（Google Finance 優先，Yahoo TWD=X 備援）。"""
    now = time.time()
    if _fx_cache["rate"] and (now - _fx_cache["ts"]) < _FX_TTL_SECONDS:
        return _fx_cache["rate"]

    rate = _google_finance_usd_twd()
    if rate is None:
        rate = _yahoo_usd_twd()
    if rate is None:
        rate = 32.0  # 最終保底，避免整頁掛掉

    _fx_cache["rate"] = rate
    _fx_cache["ts"] = now
    return rate


def _google_finance_usd_twd():
    try:
        resp = requests.get(
            "https://www.google.com/finance/quote/USD-TWD",
            headers=_HEADERS,
            timeout=8,
        )
        resp.raise_for_status()
        for pattern in (
            r'data-last-price="([\d.]+)"',
            r'"USD / TWD"[^}]*?\[(3[0-9]\.\d+)',
            r'class="YMlKec fxKbKc">([\d.]+)',
        ):
            match = re.search(pattern, resp.text)
            if match:
                return float(match.group(1))
    except Exception as error:
        print(f"[fx] Google Finance 匯率取得失敗：{error}")
    return None


def _yahoo_usd_twd():
    price, _ = _yahoo_quote("TWD=X")
    return price


# ------------------------------------------------------------------ 報價

def _get_tw_quote(ticker):
    """台股即時報價（盤中即時價，盤後最後成交價）。"""
    channel = _twse_channel(ticker)
    name = None
    price = None
    try:
        resp = requests.get(
            "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
            params={
                "ex_ch": channel,
                "json": "1",
                "delay": "0",
                "_": int(time.time() * 1000),
            },
            headers={**_HEADERS, "Referer": "https://mis.twse.com.tw/stock/index.jsp"},
            timeout=8,
        )
        resp.raise_for_status()
        rows = resp.json().get("msgArray") or []
        if rows:
            row = rows[0]
            name = row.get("n") or None
            price = _to_float(row.get("z"))  # 最近成交價
            if price is None and row.get("b"):  # 盤後改用最佳買價
                price = _to_float(row.get("b", "").split("_")[0])
            if price is None:
                price = _to_float(row.get("y"))  # 昨收保底
    except Exception as error:
        print(f"[tw] TWSE 即時報價失敗 {ticker}：{error}")

    if price is None:  # 最終備援：Yahoo
        price, yahoo_name = _yahoo_quote(ticker)
        name = name or yahoo_name

    if name:
        _name_cache[ticker.upper()] = name
    return {"name": name, "price": price, "currency": "TWD"}


def _get_us_quote(ticker):
    """美股即時報價（USD），回傳含美元原價與換算後台幣。"""
    price, name = _yahoo_quote(ticker)
    rate = get_usd_twd_rate()
    twd = round(price * rate, 2) if price is not None else None
    return {
        "name": name,
        "price": twd,
        "priceUsd": price,
        "currency": "TWD",
        "fxRate": rate,
    }


def get_quote(ticker):
    """單檔即時報價，價格一律回傳台幣。"""
    ticker = ticker.strip().upper()
    if is_cash(ticker):
        return {"name": "台幣現金", "price": 1.0, "currency": "TWD"}
    if is_taiwan_ticker(ticker):
        return _get_tw_quote(ticker)
    return _get_us_quote(ticker)


def get_quotes(tickers):
    """多檔報價（逐檔抓取）。"""
    return {ticker.upper(): get_quote(ticker) for ticker in tickers}


# ------------------------------------------------------------------ 歷史回朔

def _extract_shares_map(holdings):
    shares = {}
    for holding in holdings or []:
        qty = _to_float(holding.get("shares"))
        if qty:
            key = holding["ticker"].strip().upper()
            # 同 ticker 可能出現多筆（例如美股 QQQM + 台股複委託 QQQM），
            # 對「總市值走勢」而言報價來源相同，股數應合併加總，不能覆蓋。
            shares[key] = shares.get(key, 0.0) + qty
    return shares


def get_history(holdings, start="2020-11-01", holdings_2025=None):
    """歷史回朔：以「目前持股固定不變」為假設，往回推算（預設涵蓋完整 5 年以上交易日）。

    holdings: [{"ticker": ..., "shares": ...}, ...]
    holdings_2025: 2025portfolio.json 持倉清單（選擇性，用於同步計算 2025 總資產曲線）
    回傳：
      dates:          日期序列
      totalValue:     每日總市值（台幣）
      totalValue2025: 2025portfolio.json 每日總市值（台幣）
      holdings2025:   [{"ticker", "shares"}] 供前端即時刷新時同步更新 2025 曲線最後一點
      perTicker:      {ticker: {"value": [...], "weight": [...]}}
      dailyReturns:   [{"date", "value", "ret"}]  總市值每日報酬率
    """
    shares = _extract_shares_map(holdings)
    shares_2025 = _extract_shares_map(holdings_2025)
    if not shares:
        return _empty_history()

    start_ts = int(time.mktime(time.strptime(start, "%Y-%m-%d")))
    all_tickers = sorted(set(shares.keys()) | set(shares_2025.keys()))

    close_frames = {}
    for ticker in all_tickers:
        if is_cash(ticker):
            continue  # 現金無需抓歷史，稍後以固定值加入
        hist = _yahoo_history(ticker, start_ts)
        if hist:
            close_frames[ticker] = hist
        else:
            print(f"[history] 無歷史資料：{ticker}")

    cash_total = sum(shares[t] for t in shares if is_cash(t))

    if not close_frames:
        if cash_total <= 0:
            return _empty_history()

    # 美股需要歷史匯率換算
    need_fx = any(not is_taiwan_ticker(t) for t in close_frames)
    fx_hist = _yahoo_history("TWD=X", start_ts) if need_fx else {}

    all_dates = sorted({d for h in close_frames.values() for d in h.keys()})
    if not all_dates:
        return _empty_history()

    combined = {}
    for ticker, hist in close_frames.items():
        series = {}
        last_val = None
        for d in all_dates:
            if d in hist and hist[d] is not None:
                last_val = float(hist[d])
            series[d] = last_val
        combined[ticker] = series

    fx_aligned = None
    if need_fx and fx_hist:
        fx_aligned = {}
        last_fx = None
        for d in all_dates:
            if d in fx_hist and fx_hist[d] is not None:
                last_fx = float(fx_hist[d])
            fx_aligned[d] = last_fx
        first_fx = next((fx_aligned[d] for d in all_dates if fx_aligned[d] is not None), None)
        if first_fx is not None:
            for d in all_dates:
                if fx_aligned[d] is None:
                    fx_aligned[d] = first_fx
                else:
                    break

    columns = [t for t in shares if not is_cash(t) and t in combined]
    if cash_total > 0:
        columns.extend([t for t in shares if is_cash(t)])

    raw_values = {t: {} for t in columns}
    for ticker in shares:
        if is_cash(ticker) or ticker not in combined:
            continue
        qty = shares[ticker]
        is_tw = is_taiwan_ticker(ticker)
        for d in all_dates:
            p = combined[ticker][d]
            if p is None:
                raw_values[ticker][d] = None
            else:
                if not is_tw and fx_aligned is not None and fx_aligned.get(d) is not None:
                    p = p * fx_aligned[d]
                raw_values[ticker][d] = p * qty

    if cash_total > 0:
        for ticker in shares:
            if is_cash(ticker):
                c_val = float(shares[ticker])
                for d in all_dates:
                    raw_values[ticker][d] = c_val

    valid_dates = []
    totals = []
    for d in all_dates:
        if all(raw_values[t][d] is None for t in columns):
            continue
        day_total = sum((raw_values[t][d] or 0.0) for t in columns)
        if day_total > 0:
            valid_dates.append(d)
            totals.append(day_total)

    if not valid_dates:
        return _empty_history()

    total_value_2025 = []
    if shares_2025:
        for d in valid_dates:
            day_2025 = 0.0
            for ticker, qty in shares_2025.items():
                if is_cash(ticker):
                    day_2025 += float(qty)
                elif ticker in combined:
                    p = combined[ticker][d]
                    if p is not None:
                        if not is_taiwan_ticker(ticker) and fx_aligned is not None and fx_aligned.get(d) is not None:
                            p = p * fx_aligned[d]
                        day_2025 += p * qty
            total_value_2025.append(round(float(day_2025), 2))

    per_ticker = {}
    for ticker in columns:
        vals = [raw_values[ticker][d] or 0.0 for d in valid_dates]
        per_ticker[ticker] = {
            "value": [round(float(v), 2) for v in vals],
            "weight": [round(float(v / tot * 100.0), 2) for v, tot in zip(vals, totals)],
        }

    daily_returns = []
    for i, (d, tot) in enumerate(zip(valid_dates, totals)):
        ret = 0.0 if i == 0 else (tot - totals[i - 1]) / totals[i - 1] * 100.0
        daily_returns.append(
            {"date": d, "value": round(float(tot), 2), "ret": round(float(ret), 3)}
        )

    return {
        "dates": valid_dates,
        "totalValue": [round(float(v), 2) for v in totals],
        "totalValue2025": total_value_2025,
        "holdings2025": [{"ticker": k, "shares": v} for k, v in shares_2025.items()],
        "perTicker": per_ticker,
        "dailyReturns": daily_returns,
    }


def _empty_history():
    return {
        "dates": [],
        "totalValue": [],
        "totalValue2025": [],
        "holdings2025": [],
        "perTicker": {},
        "dailyReturns": [],
    }


# ------------------------------------------------------------ 樂活五線譜

_LOHAS_WINDOW = 875  # 約 3.5 年交易日（回歸與標準差計算窗口）
_LOHAS_DISPLAY_WINDOW = 875  # 約 3.5 年交易日（供前端 3Y 瀏覽）


def get_lohas(ticker, start="2019-01-01"):
    """樂活五線譜：線性回歸中軸 TL 與 ±1SD/±2SD 及 -3SD/-4SD 通道。

    回傳：
      currencySymbol, lastDate, sentimentDescription,
      chartData: [{date, close, tl, p1sd, p2sd, m1sd, m2sd, m3sd, m4sd}]
    """
    import math

    ticker = ticker.strip().upper()
    start_ts = int(time.mktime(time.strptime(start, "%Y-%m-%d")))

    if is_cash(ticker):
        return {"error": "現金無五線譜資料", "chartData": []}

    hist = _yahoo_history(ticker, start_ts)
    if not hist:
        return {"error": f"找不到 {ticker} 的歷史數據", "chartData": []}

    items = sorted(hist.items())[-_LOHAS_DISPLAY_WINDOW:]
    dates = [d for d, _ in items]
    closes = [float(c) for _, c in items]

    # 以最近 _LOHAS_WINDOW（約 3.5 年）進行線性回歸與標準差計算，並將軌道延伸涵蓋整個顯示區間（5Y）
    reg_closes = closes[-_LOHAS_WINDOW:]
    n = len(reg_closes)
    x_mean = (n - 1) / 2.0
    y_mean = sum(reg_closes) / n
    num = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(reg_closes))
    den = sum((i - x_mean) ** 2 for i in range(n))
    slope = num / den if den else 0.0
    intercept = y_mean - slope * x_mean
    residuals = [y - (slope * i + intercept) for i, y in enumerate(reg_closes)]
    r_mean = sum(residuals) / n
    sd = math.sqrt(sum((r - r_mean) ** 2 for r in residuals) / n)

    offset = len(closes) - len(reg_closes)
    tl = [slope * (i - offset) + intercept for i in range(len(closes))]

    chart_data = []
    for i, date_str in enumerate(dates):
        chart_data.append(
            {
                "date": date_str,
                "close": round(float(closes[i]), 2),
                "tl": round(float(tl[i]), 2),
                "p1sd": round(float(tl[i] + sd), 2),
                "p2sd": round(float(tl[i] + 2 * sd), 2),
                "m1sd": round(float(tl[i] - sd), 2),
                "m2sd": round(float(tl[i] - 2 * sd), 2),
                "m3sd": round(float(tl[i] - 3 * sd), 2),
                "m4sd": round(float(tl[i] - 4 * sd), 2),
            }
        )

    is_tw = is_taiwan_ticker(ticker)
    return {
        "ticker": ticker,
        "currencySymbol": "NT$" if is_tw else "$",
        "lastDate": dates[-1] if dates else None,
        "sentimentDescription": (
            "統計學價格軌道與 CNN 美股市場情緒參考"
            if is_tw
            else "統計學價格軌道與美股即時情緒指標"
        ),
        "chartData": chart_data,
    }


def get_fear_greed():
    """CNN Fear & Greed 即時分數與歷史比較（透過 requests，避免額外套件）。"""
    try:
        resp = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers=_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        fg = resp.json().get("fear_and_greed") or {}
        score = float(fg.get("score"))
        rating = str(fg.get("rating") or "").upper()
        prev_close = _to_float(fg.get("previous_close"))
        prev_week = _to_float(fg.get("previous_1_week"))
        prev_month = _to_float(fg.get("previous_1_month"))
        prev_year = _to_float(fg.get("previous_1_year"))
        return {
            "score": round(score, 1),
            "rating": rating,
            "timestamp": fg.get("timestamp"),
            "previousClose": round(prev_close, 1) if prev_close is not None else None,
            "previous1Week": round(prev_week, 1) if prev_week is not None else None,
            "previous1Month": round(prev_month, 1) if prev_month is not None else None,
            "previous1Year": round(prev_year, 1) if prev_year is not None else None,
        }
    except Exception as error:
        print(f"[fg] CNN Fear & Greed 取得失敗：{error}")
        return {"score": 50.0, "rating": "資料暫缺"}


def gauge_color(score):
    if score < 25:
        return "#cc0000"
    if score < 45:
        return "#ff9900"
    if score < 55:
        return "#888888"
    if score < 75:
        return "#66cc00"
    return "#008800"


# ------------------------------------------------------------ 市場指標 (VIX & Nasdaq100 Forward PE)

_DOLLARLIQUIDITY_URL = "https://dollarliquidity.com/en/valuation/nasdaq-100-forward-pe"
_DOLLARLIQUIDITY_OVERVIEW_URL = "https://dollarliquidity.com/en/valuation"


def get_vix():
    """取得 CBOE VIX 波動率指數最新報價與前一個交易日收盤價（Yahoo Finance Chart API）。"""
    result = _yahoo_chart("%5EVIX", rng="5d")
    if not result:
        return {"value": None, "previousValue": None, "date": None}
    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    raw_closes = (((result.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    bars = [
        (time.strftime("%Y-%m-%d", time.gmtime(ts)), _to_float(c))
        for ts, c in zip(timestamps, raw_closes)
        if _to_float(c) is not None
    ]
    price = _to_float(meta.get("regularMarketPrice"))
    if price is None and bars:
        price = bars[-1][1]
    ts = meta.get("regularMarketTime")
    date_str = time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts else (bars[-1][0] if bars else None)

    # meta.chartPreviousClose 在 range=5d 時為 5 日前收盤價，優先取 previousClose 或前一日日線收盤價
    prev = _to_float(meta.get("previousClose"))
    if prev is None and bars:
        if date_str and bars[-1][0] == date_str:
            prev = bars[-2][1] if len(bars) >= 2 else None
        else:
            prev = bars[-1][1]
    if prev is None:
        prev = _to_float(meta.get("chartPreviousClose"))

    return {
        "value": round(price, 2) if price is not None else None,
        "previousValue": round(prev, 2) if prev is not None else None,
        "date": date_str,
    }


def _parse_dollarliquidity_overview(html_text):
    """從 DollarLiquidity /en/valuation 總覽頁（較輕量）解析 Nasdaq-100 Forward P/E。"""
    from datetime import datetime

    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_text))
    match = re.search(
        r"Nasdaq-100 Forward P/E\s+(\d+(?:\.\d+)?)\s*[×x].{0,400}?"
        r"As of\s+([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})\s*([+−-]?\d+(?:\.\d+)?)",
        plain,
    )
    if not match:
        return None
    val = round(float(match.group(1)), 2)
    chg = round(float(match.group(3).replace("−", "-")), 2)
    date_str = datetime.strptime(match.group(2), "%b %d, %Y").strftime("%Y-%m-%d")
    return {
        "value": val,
        "date": date_str,
        "previousValue": round(val - chg, 2),
        "previousDate": None,
        "change": chg,
        "url": _DOLLARLIQUIDITY_URL,
        "source": "DollarLiquidity",
    }


def get_nasdaq100_forward_pe_dollarliquidity():
    """從 DollarLiquidity.com 取得 Nasdaq-100 Forward P/E Ratio 最新統計數值與前值。

    優先抓取較輕量的 /en/valuation 總覽頁（約少 30% 傳輸量）並帶時間戳參數避開
    Cloudflare CDN 6 小時邊緣快取；若總覽頁版面變動則自動退回明細頁解析。
    """
    cache_headers = {**_HEADERS, "Cache-Control": "no-cache", "Pragma": "no-cache"}
    try:
        resp_ov = requests.get(
            _DOLLARLIQUIDITY_OVERVIEW_URL,
            params={"_": int(time.time())},
            headers=cache_headers,
            timeout=15,
        )
        resp_ov.raise_for_status()
        parsed = _parse_dollarliquidity_overview(resp_ov.text)
        if parsed:
            return parsed
    except Exception as error:
        print(f"[pe] DollarLiquidity 總覽頁取得失敗，改用明細頁：{error}")

    try:
        # 備援：明細頁（同樣帶時間戳避開 Cloudflare CDN 快取）
        resp = requests.get(
            _DOLLARLIQUIDITY_URL,
            params={"_": int(time.time())},
            headers=cache_headers,
            timeout=15,
        )
        resp.raise_for_status()
        text = resp.text

        prev_val = None
        prev_date = None
        hp_idx = text.find("historyPoints")
        if hp_idx > 0:
            snippet = text[max(0, hp_idx - 1200):hp_idx]
            pairs = re.findall(
                r'\\?"date\\?"\s*:\s*\\?"(\d{4}-\d{2}-\d{2})\\?"\s*,\s*\\?"pe\\?"\s*:\s*([\d.]+)',
                snippet,
            )
            if len(pairs) >= 2:
                prev_date, prev_pe_str = pairs[-2]
                prev_val = round(float(prev_pe_str), 2)

        match = re.search(
            r"Forward P/E Ratio Today:\s*([\d.]+)\s*[×x]?\s*\((\d{4}-\d{2}-\d{2})\)",
            text,
            re.I,
        )
        if match:
            val = round(float(match.group(1)), 2)
            return {
                "value": val,
                "date": match.group(2),
                "previousValue": prev_val,
                "previousDate": prev_date,
                "change": round(val - prev_val, 2) if prev_val is not None else None,
                "url": _DOLLARLIQUIDITY_URL,
                "source": "DollarLiquidity",
            }
        match_val = re.search(
            r'"name"\s*:\s*"Forward P/E"\s*,\s*"unitText"\s*:\s*"ratio"\s*,\s*"value"\s*:\s*([\d.]+)',
            text,
        )
        if match_val:
            val = round(float(match_val.group(1)), 2)
            date_match = re.search(r'"latestDate"\s*:\s*"(\d{4}-\d{2}-\d{2})"', text)
            return {
                "value": val,
                "date": date_match.group(1) if date_match else None,
                "previousValue": prev_val,
                "previousDate": prev_date,
                "change": round(val - prev_val, 2) if prev_val is not None else None,
                "url": _DOLLARLIQUIDITY_URL,
                "source": "DollarLiquidity",
            }
    except Exception as error:
        print(f"[pe] DollarLiquidity Nasdaq100 Forward PE 取得失敗：{error}")
    return {
        "value": None,
        "date": None,
        "previousValue": None,
        "previousDate": None,
        "change": None,
        "url": _DOLLARLIQUIDITY_URL,
        "source": "DollarLiquidity",
    }


def get_market_indicators():
    """並行抓取 VIX 與 DollarLiquidity 的 Nasdaq 100 Forward P/E Ratio，縮短等待時間。"""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_vix = pool.submit(get_vix)
        f_pe = pool.submit(get_nasdaq100_forward_pe_dollarliquidity)
        return {
            "vix": f_vix.result(),
            "nasdaq100ForwardPe": {
                "dollarliquidity": f_pe.result(),
            },
        }
