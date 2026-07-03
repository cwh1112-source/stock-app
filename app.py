import json
import urllib.request
import ssl
import time
import csv
import io
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

# ==========================================
# SSL context for Taiwan official sites
# ==========================================
_UNVERIFIED_SSL_CONTEXT = ssl.create_default_context()
_UNVERIFIED_SSL_CONTEXT.check_hostname = False
_UNVERIFIED_SSL_CONTEXT.verify_mode = ssl.CERT_NONE

def _urlopen_relaxed(req, timeout=10):
    return urllib.request.urlopen(req, timeout=timeout, context=_UNVERIFIED_SSL_CONTEXT)

# ==========================================
# TTL Cache
# ==========================================
CACHE_TTL_SECONDS = 6 * 60 * 60

class _TTLCache:
    def __init__(self, ttl_seconds=CACHE_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._store = {}

    def get(self, key):
        item = self._store.get(key)
        if item is None:
            return None
        value, fetched_at = item
        if time.time() - fetched_at > self._ttl:
            del self._store[key]
            return None
        return value

    def set(self, key, value):
        self._store[key] = (value, time.time())

_cache = _TTLCache()

# ==========================================
# ETF 判斷
# ==========================================
def is_etf_code(stock_no):
    try:
        n = int(stock_no)
        return 00000 <= n <= 9999 and len(stock_no) == 6 and stock_no.startswith('0')
    except:
        return len(stock_no) == 6 and stock_no.startswith('0')

# ==========================================
# 抓取中文公司名稱
# ==========================================
def fetch_cn_name(stock_no, debug=False):
    cached = _cache.get(f"name_{stock_no}")
    if cached:
        return cached
    urls = [
        f"https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
        f"https://openapi.twse.com.tw/v1/opendata/t187ap03_O",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with _urlopen_relaxed(req, timeout=8) as r:
                data = json.loads(r.read().decode('utf-8'))
            for item in data:
                code = str(item.get("公司代號", item.get("SecuritiesCompanyCode", ""))).strip()
                if code == str(stock_no).strip():
                    name = item.get("公司簡稱", item.get("CompanyAbbreviation", "")).strip()
                    if name:
                        _cache.set(f"name_{stock_no}", name)
                        return name
        except Exception as e:
            if debug:
                print(f"name fetch error: {e}")
            continue
    return None

# ==========================================
# 抓取本益比
# ==========================================
def fetch_pe(stock_no, debug=False):
    cached = _cache.get(f"pe_{stock_no}")
    if cached is not None:
        return cached

    # 上市 TWSE
    try:
        url = "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d?response=json"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _urlopen_relaxed(req, timeout=10) as r:
            data = json.loads(r.read().decode('utf-8'))
        rows = data.get("data", [])
        for row in rows:
            if row and str(row[0]).strip() == str(stock_no).strip():
                pe_val = str(row[4]).strip()
                if pe_val and pe_val not in ("-", "--", ""):
                    val = round(float(pe_val.replace(",", "")), 2)
                    _cache.set(f"pe_{stock_no}", val)
                    return val
    except Exception as e:
        if debug:
            print(f"TWSE PE error: {e}")

    # 上櫃 TPEx
    tpex_pe_urls = [
        "https://www.tpex.org.tw/web/stock/aftertrading/peratio_listed/peListed_result.php?l=zh-tw&o=json",
        "https://www.tpex.org.tw/openapi/v1/tpex_peratio_listed",
    ]
    for pe_url in tpex_pe_urls:
        try:
            req = urllib.request.Request(pe_url, headers={"User-Agent": "Mozilla/5.0"})
            with _urlopen_relaxed(req, timeout=10) as r:
                raw = r.read().decode('utf-8')
            if not raw.strip():
                continue
            data = json.loads(raw)
            rows = data.get("aaData", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            for row in rows:
                if isinstance(row, dict):
                    if str(row.get("SecuritiesCompanyCode", "")).strip() == str(stock_no).strip():
                        pe_val = str(row.get("PriceEarningRatio", "")).strip()
                        if pe_val and pe_val not in ("-", "--", ""):
                            val = round(float(pe_val.replace(",", "")), 2)
                            _cache.set(f"pe_{stock_no}", val)
                            return val
                elif row and str(row[0]).strip() == str(stock_no).strip():
                    pe_val = str(row[4]).strip()
                    if pe_val and pe_val not in ("-", "--", ""):
                        val = round(float(pe_val.replace(",", "")), 2)
                        _cache.set(f"pe_{stock_no}", val)
                        return val
            break
        except Exception as e:
            if debug:
                print(f"TPEx PE error: {e}")
            continue
    return None

# ==========================================
# 抓取股利資料
# ==========================================
def fetch_dividend_info(stock_no, debug=False):
    cached = _cache.get(f"div_{stock_no}")
    if cached is not None:
        return cached

    market = "tpex" if len(stock_no) == 4 and stock_no.startswith("8") else "twse"

    candidate_urls = {
        "twse": ["https://openapi.twse.com.tw/v1/opendata/t187ap45_L"],
        "tpex": [
            "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_dividend",
            "https://www.tpex.org.tw/openapi/v1/tpex_dividend_announcement",
            "https://www.tpex.org.tw/openapi/v1/tpex_dividend",
            "https://openapi.twse.com.tw/v1/opendata/t187ap45_O",
        ],
    }
    urls = candidate_urls.get(market, [])

    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with _urlopen_relaxed(req, timeout=10) as r:
                raw = r.read().decode('utf-8')
            if not raw.strip():
                continue
            data = json.loads(raw)
            if not isinstance(data, list):
                continue

            matched = [item for item in data if str(item.get("公司代號", item.get("SecuritiesCompanyCode", ""))).strip() == str(stock_no).strip()]
            if not matched:
                continue

            total_cash = 0.0
            total_stock = 0.0
            periods = []
            statuses = []
            confirmed_cash = None
            confirmed_period = None

            for item in matched:
                cash_val = item.get("現金股利", item.get("CashDividend", "0"))
                stock_val = item.get("股票股利", item.get("StockDividend", "0"))
                period = item.get("資料年度", item.get("Year", ""))
                status = item.get("資料來源", item.get("DataSource", ""))
                try:
                    total_cash += float(str(cash_val).replace(",", "") or 0)
                    total_stock += float(str(stock_val).replace(",", "") or 0)
                    if period:
                        periods.append(str(period))
                    if status:
                        statuses.append(str(status))
                    if status and "董事會" not in status and float(str(cash_val).replace(",", "") or 0) > 0:
                        if confirmed_cash is None:
                            confirmed_cash = float(str(cash_val).replace(",", "") or 0)
                            confirmed_period = str(period)
                except:
                    continue

            latest_cash = None
            latest_period = ""
            is_latest_confirmed = False
            if matched:
                first = matched[0]
                try:
                    latest_cash = float(str(first.get("現金股利", first.get("CashDividend", "0"))).replace(",", "") or 0)
                    latest_period = str(first.get("資料年度", first.get("Year", "")))
                    first_status = str(first.get("資料來源", first.get("DataSource", "")))
                    is_latest_confirmed = "董事會" not in first_status
                except:
                    pass

            result = {
                "cash": total_cash,
                "stock": total_stock,
                "period": "~".join(sorted(set(periods))),
                "status": " / ".join(sorted(set(statuses))),
                "confirmed_cash": confirmed_cash,
                "confirmed_period": confirmed_period,
                "is_latest_confirmed": is_latest_confirmed,
                "latest_cash": latest_cash,
                "latest_period": latest_period,
            }
            _cache.set(f"div_{stock_no}", result)
            return result
        except Exception as e:
            if debug:
                print(f"Dividend fetch error [{url}]: {e}")
            continue
    return None

# ==========================================
# 抓取外資台指期淨部位
# ==========================================
def fetch_foreign_futures_net_position(debug=False):
    cached = _cache.get("futures_net")
    if cached is not None:
        return cached

    candidate_urls = [
        "https://openapi.taifex.com.tw/v1/MarketDataOfMajorInstitutionalTradersGeneralBytheDate",
        "https://openapi.taifex.com.tw/v1/MarketDataOfMajorInstitutionalTradersBytheDate",
    ]
    for url in candidate_urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with _urlopen_relaxed(req, timeout=10) as r:
                raw = r.read().decode('utf-8')
            if not raw.strip():
                continue
            data = json.loads(raw)
            if not isinstance(data, list) or not data:
                continue
            latest = data[-1]
            date_str = str(latest.get("Date", latest.get("date", "")))
            net_val = None
            for key in ["ForeignDealersNetOI", "Foreign_Net_OI", "foreignNetOI", "外資淨未平倉"]:
                if key in latest:
                    try:
                        net_val = int(str(latest[key]).replace(",", ""))
                        break
                    except:
                        continue
            if net_val is not None:
                result = (net_val, date_str)
                _cache.set("futures_net", result)
                return result
        except Exception as e:
            if debug:
                print(f"Futures error [{url}]: {e}")
            continue
    return None, ""

# ==========================================
# 主要資料抓取
# ==========================================
def fetch_comprehensive_data(stock_no):
    db = {
        "2330": {"n": "台積電", "d": 16.0, "stk": 0.0, "e": 40.0},
        "3037": {"n": "欣興",   "d": 3.0,  "stk": 0.0, "e": 8.5},
        "2464": {"n": "盟立",   "d": 0.0,  "stk": 0.0, "e": 1.5},
        "6139": {"n": "亞翔",   "d": 23.0, "stk": 0.0, "e": 32.5},
        "6770": {"n": "力積電", "d": 0.0,  "stk": 0.0, "e": -0.5},
        "6446": {"n": "藥華藥", "d": 1.5,  "stk": 1.1, "e": 15.0},
        "2308": {"n": "台達電", "d": 6.43, "stk": 0.0, "e": 14.2},
        "3481": {"n": "群創",   "d": 0.0,  "stk": 0.0, "e": -0.6},
        "8027": {"n": "鈦昇",   "d": 0.0,  "stk": 0.0, "e": 1.2},
        "8064": {"n": "東捷",   "d": 0.0,  "stk": 0.0, "e": 0.8},
    }
    info = db.get(stock_no, {"n": f"股票{stock_no}", "d": 0.0, "stk": 0.0, "e": 1.0})

    real_name = fetch_cn_name(stock_no)
    if real_name:
        info['n'] = real_name

    real_pe = fetch_pe(stock_no)

    dividend_period = ""
    dividend_status = ""
    confirmed_cash = None
    confirmed_period = None
    is_latest_confirmed = False
    latest_div_cash = None
    latest_div_period = ""
    real_div = fetch_dividend_info(stock_no)
    if real_div:
        div_amount = real_div["cash"]
        stk_amount = real_div["stock"]
        dividend_period = real_div["period"]
        dividend_status = real_div.get("status", "")
        confirmed_cash = real_div.get("confirmed_cash")
        confirmed_period = real_div.get("confirmed_period")
        is_latest_confirmed = real_div.get("is_latest_confirmed", False)
        latest_div_cash = real_div.get("latest_cash", real_div["cash"])
        latest_div_period = real_div.get("latest_period", real_div["period"])
    else:
        div_amount = info['d']
        stk_amount = info['stk']
        latest_div_cash = info['d']
        latest_div_period = ""

    # 使用 yfinance 套件抓取歷史資料（內建 cookie/session 管理，可繞過 429）
    import yfinance as yf
    import time

    res = None
    for suffix in [".TW", ".TWO"]:
        try:
            symbol = f"{stock_no}{suffix}"
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="1y", auto_adjust=True)
            if hist.empty:
                continue
            # 轉換成跟原本相同的格式供後續計算使用
            res = {
                "hist": hist,
                "symbol": symbol,
                "meta_price": float(hist["Close"].iloc[-1]),
            }
            break
        except Exception as e:
            print(f"yfinance error [{suffix}]: {e}")
            time.sleep(1)
            continue

    if not res:
        return None

    hist = res["hist"]
    c = list(hist["Close"])
    o_raw = list(hist["Open"])
    h_raw = list(hist["High"])
    l_raw = list(hist["Low"])
    v_raw = list(hist["Volume"])

    try:
        c = [float(x) for x in c if x is not None]
        o_raw = [float(x) for x in o_raw if x is not None]
        h_raw = [float(x) for x in h_raw if x is not None]
        l_raw = [float(x) for x in l_raw if x is not None]
        v_raw = [float(x) for x in v_raw if x is not None]
        price = float(c[-1])

        ma10 = sum(c[-10:]) / 10
        ma20 = sum(c[-20:]) / 20
        ma60 = sum(c[-60:]) / 60
        ma120 = sum(c[-120:]) / 120 if len(c) >= 120 else sum(c) / len(c)
        bias = ((price - ma20) / ma20) * 100

        diff = [c[i] - c[i-1] for i in range(1, len(c))]
        gain = sum(d for d in diff[-14:] if d > 0) / 14
        loss = abs(sum(d for d in diff[-14:] if d < 0)) / 14
        rsi = 100 - (100 / (1 + (gain / (loss if loss != 0 else 1))))

        prev_close = c[-2] if len(c) >= 2 else price

        if len(v_raw) >= 21:
            avg_vol_20d = sum(v_raw[-21:-1]) / 20
            v_ratio = (v_raw[-1] / avg_vol_20d) if avg_vol_20d > 0 else 1.0
        else:
            avg_vol_20d = sum(v_raw) / len(v_raw) if v_raw else 0
            v_ratio = 1.0

        if len(h_raw) >= 15 and len(l_raw) >= 15 and len(c) >= 15:
            tr = [max(h_raw[i]-l_raw[i], abs(h_raw[i]-c[i-1]), abs(l_raw[i]-c[i-1])) for i in range(1, len(c))]
            atr = sum(tr[-14:]) / 14
        else:
            atr = price * 0.03

        high_60d = max(h_raw[-60:]) if len(h_raw) >= 60 else (max(h_raw) if h_raw else price)

        last_open = o_raw[-1] if o_raw else price
        if price > last_open and price > ma20 and v_ratio > 1.2:
            pattern = "🎯 強勢紅K突破"
        elif price > last_open:
            pattern = "📈 紅K攻擊"
        elif price <= last_open and price > ma20:
            pattern = "⚠️ 多頭回檔"
        else:
            pattern = "📉 弱勢盤整"

        futures_net, futures_date = fetch_foreign_futures_net_position()

        return {
            "name": info['n'],
            "price": price,
            "prev_close": prev_close,
            "ma10": ma10, "ma20": ma20, "ma60": ma60, "ma120": ma120,
            "bias": bias, "rsi": rsi,
            "v_ratio": v_ratio, "avg_vol_20d": avg_vol_20d,
            "atr": atr, "high_60d": high_60d, "pattern": pattern,
            "div": div_amount, "stk": stk_amount,
            "div_period": dividend_period, "div_status": dividend_status,
            "confirmed_cash": confirmed_cash, "confirmed_period": confirmed_period,
            "is_latest_confirmed": is_latest_confirmed,
            "latest_div_cash": latest_div_cash, "latest_div_period": latest_div_period,
            "pe": real_pe if real_pe is not None else (price / info['e'] if info['e'] > 0 else 0),
            "futures_net": futures_net,
            "futures_date": futures_date,
        }
    except Exception as e:
        print(f"數據解析錯誤: {e}")
        return None

# ==========================================
# Flask 路由
# ==========================================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/stock/<stock_no>")
def get_stock(stock_no):
    stock_no = stock_no.strip().upper()
    d = fetch_comprehensive_data(stock_no)
    if not d:
        return jsonify({"error": f"查無股票代碼 {stock_no}"}), 404

    # 五級決策邏輯
    price = d['price']
    ma10, ma20, ma60 = d['ma10'], d['ma20'], d['ma60']
    bias_pct = d['bias']
    rsi = d['rsi']
    v_ratio = d['v_ratio']
    atr = d['atr']
    high_60d = d['high_60d']
    avg_vol_20d = d['avg_vol_20d']

    if price > ma20:
        if bias_pct > 6 or rsi > 70:
            action_tag = "overheat"
            decision_text = "過熱觀察，暫勿追高"
            decision_color = "#b08117"
        elif v_ratio > 1.5 and ma10 > ma20 > ma60:
            action_tag = "strong_buy"
            decision_text = "強烈買進/加碼"
            decision_color = "#24936E"
        else:
            action_tag = "hold"
            decision_text = "續抱"
            decision_color = "#007AFF"
    elif price > ma60 and rsi >= 40:
        action_tag = "watch"
        decision_text = "觀察整理"
        decision_color = "#e67e22"
    else:
        action_tag = "reduce"
        decision_text = "警示/建議減碼"
        decision_color = "#d9383a"

    is_overheat = (action_tag == "overheat")
    is_bull = price > ma20

    # 策略計算
    strategy = {}
    if is_overheat:
        strategy["type"] = "overheat"
        strategy["tp"] = round(price * 1.05, 1)
        strategy["ma20"] = round(ma20, 1)
    else:
        strategy["type"] = "normal"
        if price > ma10:
            strategy["short_entry_min"] = round(ma10, 1)
            strategy["short_entry_max"] = round(ma10 * 1.01, 1)
            strategy["short_tp"] = round(price * 1.05, 1)
            strategy["short_sl"] = round(price - atr * 1.5, 1)
        else:
            strategy["short_no_entry"] = round(ma10, 1)

        if price > ma20:
            strategy["swing_entry_min"] = round(ma20, 1)
            strategy["swing_entry_max"] = round(ma20 * 1.015, 1)
            swing_target = high_60d if high_60d > price else price * 1.15
            strategy["swing_target"] = round(swing_target, 1)
            strategy["swing_target_label"] = "前波高點" if high_60d > price else "估算目標(+15%)"
            strategy["swing_sl"] = round(ma20 - atr * 1.5, 1)
        elif price > ma60 and rsi >= 40:
            strategy["swing_watch_ma60"] = round(ma60, 1)
            strategy["swing_watch_ma20"] = round(ma20, 1)
        else:
            strategy["swing_sl_below"] = round(price - atr * 1.5, 1)
            strategy["swing_ma60"] = round(ma60, 1)

        if action_tag in ("hold", "strong_buy"):
            add_trigger = round(max(price, high_60d) * 1.005, 1)
            vol_lots = round((avg_vol_20d * 1.5) / 1000) if avg_vol_20d else 0
            strategy["add_trigger"] = add_trigger
            strategy["vol_lots"] = vol_lots
            strategy["reduce_trigger"] = round(ma20, 1)

    # 風險提醒
    risk_lines = []
    base_tips = {
        "strong_buy": "🟢 多項指標同步轉強，技術結構健康，可依策略分批佈局。",
        "hold": "🔵 趨勢仍在多頭軌道內，建議續抱觀察，留意加碼/減碼觸發價位。",
        "overheat": "🟡 短線漲多乖離已大，建議暫緩追高，等待回測均線後再評估進場。",
        "watch": "🟠 股價已跌破生命線但季線仍在守，屬整理區間，建議耐心觀察季線防守是否成立。",
        "reduce": f"🔴 警告：{d['name']} 已實質跌破 20 日生命線與季線防守，技術面轉弱，任何反彈都屬弱勢整理，請嚴格執行停損紀律！",
    }
    risk_lines.append(base_tips.get(action_tag, ""))
    if bias_pct > 10.0:
        risk_lines.append(f"⚠️ 正乖離過高 ({bias_pct:.1f}%)：隨時有向 20MA ({ma20:.1f}) 修正風險，切勿追高！")
    elif bias_pct < -10.0:
        risk_lines.append(f"⚠️ 負乖離過大 ({bias_pct:.1f}%)：股價短線超跌，趨勢未明前勿貿然接刀。")
    if rsi > 70:
        risk_lines.append(f"⚠️ RSI 超買區 ({rsi:.1f})：買盤過熱，嚴防主力高檔反手出貨。")
    elif rsi < 30:
        risk_lines.append(f"⚠️ RSI 超賣區 ({rsi:.1f})：短線跌深，但趨勢未明，勿貿然接刀。")
    if action_tag in ("strong_buy", "overheat") and v_ratio > 1.5:
        risk_lines.append("🔥 短線波動劇烈，單筆資金請嚴格控制在 6-8%。")
    if action_tag == "reduce" and price < ma60:
        risk_lines.append(f"⚠️ 季線 ({ma60:.1f} 元) 已跌破，原防守位失效，請嚴格控管持股部位風險。")

    # 今日漲跌幅
    prev_close = d.get('prev_close', price)
    day_chg = price - prev_close
    day_chg_pct = (day_chg / prev_close * 100) if prev_close else 0.0

    # 股利顯示
    latest_div = d.get('latest_div_cash') if d.get('latest_div_cash') is not None else d['div']
    yield_rate = (latest_div / price * 100) if price > 0 else 0.0
    div_display = f"{round(latest_div, 2):g}"
    stk_display = f"{round(d['stk'], 2):g} 元" if d['stk'] > 0 else "-"
    pe_display = f"{d['pe']:.1f} 倍" if d['pe'] and d['pe'] > 0 else "虧損/無"

    # 外資期貨
    futures_net = d.get('futures_net')
    futures_date = d.get('futures_date', '')

    return jsonify({
        "stock_no": stock_no,
        "name": d['name'],
        "price": d['price'],
        "prev_close": prev_close,
        "day_chg": round(day_chg, 2),
        "day_chg_pct": round(day_chg_pct, 2),
        "decision_text": decision_text,
        "decision_color": decision_color,
        "action_tag": action_tag,
        "is_bull": is_bull,
        "ma10": round(ma10, 1),
        "ma20": round(ma20, 1),
        "ma60": round(ma60, 1),
        "bias_pct": round(bias_pct, 1),
        "rsi": round(rsi, 1),
        "v_ratio": round(v_ratio, 1),
        "pattern": d['pattern'],
        "div_display": div_display,
        "stk_display": stk_display,
        "yield_rate": round(yield_rate, 2),
        "pe_display": pe_display,
        "div_period": d.get('latest_div_period', ''),
        "div_status": d.get('div_status', ''),
        "is_latest_confirmed": d.get('is_latest_confirmed', False),
        "futures_net": futures_net,
        "futures_date": futures_date,
        "strategy": strategy,
        "risk_lines": risk_lines,
        "is_etf": is_etf_code(stock_no),
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
