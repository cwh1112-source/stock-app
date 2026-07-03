import json
import urllib.request
import ssl
import time
import datetime
from flask import Flask, render_template, jsonify

app = Flask(__name__)

# ── SSL（台灣官方網站用）──
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

def _get(url, timeout=12):
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.twse.com.tw/",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
        return r.read().decode("utf-8", errors="replace")

# ── 簡易 TTL 快取 ──
class _Cache:
    def __init__(self, ttl=3600):
        self._ttl = ttl
        self._d = {}
    def get(self, k):
        v = self._d.get(k)
        if v and time.time() - v[1] < self._ttl:
            return v[0]
        return None
    def set(self, k, v):
        self._d[k] = (v, time.time())

_cache = _Cache(ttl=3600)

# ════════════════════════════════════════
# 1. 抓歷史日線（TWSE / TPEx 官方 API）
#    回傳最近 N 個月的 {date, close, high, low, volume} list
# ════════════════════════════════════════
def _fetch_twse_history(stock_no, months=7):
    """上市股票：TWSE afterTrading/STOCK_DAY"""
    rows = []
    today = datetime.date.today()
    for i in range(months):
        d = today.replace(day=1) - datetime.timedelta(days=i*28)
        ym = d.strftime("%Y%m01")
        try:
            url = f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?stockNo={stock_no}&date={ym}&response=json"
            data = json.loads(_get(url))
            if data.get("stat") != "OK":
                continue
            for row in data.get("data", []):
                # row: [日期, 成交股數, 成交金額, 開盤價, 最高價, 最低價, 收盤價, 漲跌價差, 成交筆數]
                try:
                    rows.append({
                        "close": float(row[6].replace(",", "")),
                        "high":  float(row[4].replace(",", "")),
                        "low":   float(row[5].replace(",", "")),
                        "open":  float(row[3].replace(",", "")),
                        "vol":   float(row[1].replace(",", "")),
                    })
                except:
                    continue
        except Exception as e:
            print(f"TWSE history error {ym}: {e}")
    return rows  # 舊→新順序

def _fetch_tpex_history(stock_no, months=7):
    """上櫃股票：TPEx aftertrading/daily_close_quotes"""
    rows = []
    today = datetime.date.today()
    for i in range(months):
        d = today.replace(day=1) - datetime.timedelta(days=i*28)
        # TPEx 用民國年
        roc_year = d.year - 1911
        ym = f"{roc_year}/{d.month:02d}"
        try:
            url = f"https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php?l=zh-tw&d={ym}&stkno={stock_no}&o=json"
            data = json.loads(_get(url))
            for row in data.get("aaData", []):
                # row: [日期, 收盤, 漲跌, 開盤, 最高, 最低, 成交量(張), ...]
                try:
                    rows.append({
                        "close": float(str(row[2]).replace(",", "")),
                        "high":  float(str(row[5]).replace(",", "")),
                        "low":   float(str(row[6]).replace(",", "")),
                        "open":  float(str(row[3]).replace(",", "")),
                        "vol":   float(str(row[7]).replace(",", "")) * 1000,
                    })
                except:
                    continue
        except Exception as e:
            print(f"TPEx history error {ym}: {e}")
    return rows

def _fetch_realtime_price(stock_no):
    """即時現價：TWSE mis API"""
    # 先試上市
    for ex, prefix in [("tse", "tse"), ("otc", "otc")]:
        try:
            url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={prefix}_{stock_no}.tw&json=1&delay=0"
            data = json.loads(_get(url, timeout=8))
            items = data.get("msgArray", [])
            if items:
                p = items[0].get("z") or items[0].get("y")  # z=即時, y=昨收(收盤後)
                if p and p != "-":
                    return float(p), items[0].get("y", p)
        except Exception as e:
            print(f"realtime price error {prefix}: {e}")
    return None, None

# ════════════════════════════════════════
# 2. ETF 判斷
# ════════════════════════════════════════
def is_etf(stock_no):
    return len(stock_no) == 6 and stock_no.startswith("0")

# ════════════════════════════════════════
# 3. 公司名稱
# ════════════════════════════════════════
def fetch_name(stock_no):
    cached = _cache.get(f"name_{stock_no}")
    if cached:
        return cached
    for url in [
        "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
        "https://openapi.twse.com.tw/v1/opendata/t187ap03_O",
    ]:
        try:
            data = json.loads(_get(url))
            for item in data:
                code = str(item.get("公司代號", item.get("SecuritiesCompanyCode", ""))).strip()
                if code == str(stock_no).strip():
                    name = item.get("公司簡稱", item.get("CompanyAbbreviation", "")).strip()
                    if name:
                        _cache.set(f"name_{stock_no}", name)
                        return name
        except:
            continue
    return f"股票{stock_no}"

# ════════════════════════════════════════
# 4. 本益比
# ════════════════════════════════════════
def fetch_pe(stock_no):
    cached = _cache.get(f"pe_{stock_no}")
    if cached is not None:
        return cached
    # 上市
    try:
        url = "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d?response=json"
        data = json.loads(_get(url))
        for row in data.get("data", []):
            if row and str(row[0]).strip() == str(stock_no).strip():
                pe_val = str(row[4]).replace(",", "").strip()
                if pe_val and pe_val not in ("-", "--"):
                    val = round(float(pe_val), 2)
                    _cache.set(f"pe_{stock_no}", val)
                    return val
    except Exception as e:
        print(f"PE TWSE error: {e}")
    # 上櫃
    for pe_url in [
        "https://www.tpex.org.tw/web/stock/aftertrading/peratio_listed/peListed_result.php?l=zh-tw&o=json",
        "https://www.tpex.org.tw/openapi/v1/tpex_peratio_listed",
    ]:
        try:
            raw = _get(pe_url)
            if not raw.strip():
                continue
            data = json.loads(raw)
            rows = data.get("aaData", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            for row in rows:
                if isinstance(row, dict):
                    if str(row.get("SecuritiesCompanyCode", "")).strip() == str(stock_no).strip():
                        pe_val = str(row.get("PriceEarningRatio", "")).replace(",", "").strip()
                        if pe_val and pe_val not in ("-", "--"):
                            val = round(float(pe_val), 2)
                            _cache.set(f"pe_{stock_no}", val)
                            return val
                elif row and str(row[0]).strip() == str(stock_no).strip():
                    pe_val = str(row[4]).replace(",", "").strip()
                    if pe_val and pe_val not in ("-", "--"):
                        val = round(float(pe_val), 2)
                        _cache.set(f"pe_{stock_no}", val)
                        return val
            break
        except Exception as e:
            print(f"PE TPEx error: {e}")
    return None

# ════════════════════════════════════════
# 5. 股利
# ════════════════════════════════════════
def fetch_dividend(stock_no):
    cached = _cache.get(f"div_{stock_no}")
    if cached is not None:
        return cached
    is_tpex = len(stock_no) == 4 and (stock_no.startswith("8") or stock_no.startswith("9"))
    urls = (
        ["https://www.tpex.org.tw/openapi/v1/tpex_mainboard_dividend",
         "https://www.tpex.org.tw/openapi/v1/tpex_dividend_announcement",
         "https://openapi.twse.com.tw/v1/opendata/t187ap45_O"]
        if is_tpex else
        ["https://openapi.twse.com.tw/v1/opendata/t187ap45_L"]
    )
    for url in urls:
        try:
            data = json.loads(_get(url))
            matched = [x for x in data if str(x.get("公司代號", x.get("SecuritiesCompanyCode",""))).strip() == str(stock_no).strip()]
            if not matched:
                continue
            first = matched[0]
            cash = float(str(first.get("現金股利", first.get("CashDividend","0"))).replace(",","") or 0)
            stk  = float(str(first.get("股票股利", first.get("StockDividend","0"))).replace(",","") or 0)
            period = str(first.get("資料年度", first.get("Year","")))
            status = str(first.get("資料來源", first.get("DataSource","")))
            result = {"cash": cash, "stk": stk, "period": period, "status": status,
                      "confirmed": "董事會" not in status}
            _cache.set(f"div_{stock_no}", result)
            return result
        except Exception as e:
            print(f"dividend error: {e}")
    return None

# ════════════════════════════════════════
# 6. 外資台指期淨部位
# ════════════════════════════════════════
def fetch_futures():
    cached = _cache.get("futures")
    if cached:
        return cached
    for url in [
        "https://openapi.taifex.com.tw/v1/MarketDataOfMajorInstitutionalTradersGeneralBytheDate",
        "https://openapi.taifex.com.tw/v1/MarketDataOfMajorInstitutionalTradersBytheDate",
    ]:
        try:
            data = json.loads(_get(url))
            if not isinstance(data, list) or not data:
                continue
            latest = data[-1]
            date_str = str(latest.get("Date", latest.get("date", "")))
            for key in ["ForeignDealersNetOI","Foreign_Net_OI","foreignNetOI","外資淨未平倉"]:
                if key in latest:
                    net = int(str(latest[key]).replace(",",""))
                    result = (net, date_str)
                    _cache.set("futures", result)
                    return result
        except Exception as e:
            print(f"futures error: {e}")
    return None, ""

# ════════════════════════════════════════
# 7. 主資料整合
# ════════════════════════════════════════
def fetch_stock(stock_no):
    # 歷史日線（先試上市，失敗試上櫃）
    rows = _fetch_twse_history(stock_no)
    market = "twse"
    if len(rows) < 20:
        rows = _fetch_tpex_history(stock_no)
        market = "tpex"
    if len(rows) < 20:
        return None

    c    = [r["close"] for r in rows]
    h_r  = [r["high"]  for r in rows]
    l_r  = [r["low"]   for r in rows]
    o_r  = [r["open"]  for r in rows]
    v_r  = [r["vol"]   for r in rows]

    # 即時現價
    live_price, prev_close_raw = _fetch_realtime_price(stock_no)
    price      = live_price if live_price else c[-1]
    prev_close = float(prev_close_raw) if prev_close_raw else c[-2] if len(c) >= 2 else price

    # 技術指標
    ma10  = sum(c[-10:]) / 10
    ma20  = sum(c[-20:]) / 20
    ma60  = sum(c[-60:]) / 60 if len(c) >= 60 else sum(c) / len(c)
    bias  = (price - ma20) / ma20 * 100

    diff  = [c[i]-c[i-1] for i in range(1, len(c))]
    gain  = sum(x for x in diff[-14:] if x > 0) / 14
    loss  = abs(sum(x for x in diff[-14:] if x < 0)) / 14
    rsi   = 100 - (100 / (1 + gain / (loss or 1)))

    avg_vol = sum(v_r[-21:-1]) / 20 if len(v_r) >= 21 else (sum(v_r) / len(v_r) if v_r else 1)
    v_ratio = v_r[-1] / avg_vol if avg_vol else 1.0

    tr  = [max(h_r[i]-l_r[i], abs(h_r[i]-c[i-1]), abs(l_r[i]-c[i-1])) for i in range(1, len(c))]
    atr = sum(tr[-14:]) / 14 if len(tr) >= 14 else price * 0.03

    high_60d = max(h_r[-60:]) if len(h_r) >= 60 else max(h_r)
    avg_vol_20d = avg_vol

    last_open = o_r[-1] if o_r else price
    if price > last_open and price > ma20 and v_ratio > 1.2:
        pattern = "🎯 強勢紅K突破"
    elif price > last_open:
        pattern = "📈 紅K攻擊"
    elif price <= last_open and price > ma20:
        pattern = "⚠️ 多頭回檔"
    else:
        pattern = "📉 弱勢盤整"

    name    = fetch_name(stock_no)
    pe      = fetch_pe(stock_no)
    div     = fetch_dividend(stock_no)
    fut_net, fut_date = fetch_futures()

    return {
        "name": name, "price": price, "prev_close": prev_close,
        "ma10": ma10, "ma20": ma20, "ma60": ma60,
        "bias": bias, "rsi": rsi, "v_ratio": v_ratio,
        "avg_vol_20d": avg_vol_20d, "atr": atr,
        "high_60d": high_60d, "pattern": pattern,
        "pe": pe or 0,
        "div_cash": div["cash"] if div else 0,
        "div_stk":  div["stk"]  if div else 0,
        "div_period": div["period"] if div else "",
        "div_confirmed": div["confirmed"] if div else False,
        "futures_net": fut_net,
        "futures_date": fut_date,
        "is_etf": is_etf(stock_no),
    }

# ════════════════════════════════════════
# Flask 路由
# ════════════════════════════════════════
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/stock/<stock_no>")
def get_stock(stock_no):
    stock_no = stock_no.strip()
    d = fetch_stock(stock_no)
    if not d:
        return jsonify({"error": f"查無股票代碼 {stock_no}，請確認代碼是否正確"}), 404

    price  = d["price"]
    ma10, ma20, ma60 = d["ma10"], d["ma20"], d["ma60"]
    bias_pct = d["bias"]
    rsi, v_ratio = d["rsi"], d["v_ratio"]
    atr, high_60d, avg_vol_20d = d["atr"], d["high_60d"], d["avg_vol_20d"]

    # 五級決策
    if price > ma20:
        if bias_pct > 6 or rsi > 70:
            action_tag, decision_text, decision_color = "overheat", "過熱觀察，暫勿追高", "#b08117"
        elif v_ratio > 1.5 and ma10 > ma20 > ma60:
            action_tag, decision_text, decision_color = "strong_buy", "強烈買進/加碼", "#24936E"
        else:
            action_tag, decision_text, decision_color = "hold", "續抱", "#007AFF"
    elif price > ma60 and rsi >= 40:
        action_tag, decision_text, decision_color = "watch", "觀察整理", "#e67e22"
    else:
        action_tag, decision_text, decision_color = "reduce", "警示/建議減碼", "#d9383a"

    # 策略計算
    s = {}
    if action_tag == "overheat":
        s = {"type": "overheat", "tp": round(price*1.05,1), "ma20": round(ma20,1)}
    else:
        s["type"] = "normal"
        if price > ma10:
            s.update({"short_entry_min": round(ma10,1), "short_entry_max": round(ma10*1.01,1),
                       "short_tp": round(price*1.05,1), "short_sl": round(price-atr*1.5,1)})
        else:
            s["short_no_entry"] = round(ma10,1)
        if price > ma20:
            tgt = high_60d if high_60d > price else price*1.15
            s.update({"swing_entry_min": round(ma20,1), "swing_entry_max": round(ma20*1.015,1),
                       "swing_target": round(tgt,1),
                       "swing_target_label": "前波高點" if high_60d > price else "估算目標(+15%)",
                       "swing_sl": round(ma20-atr*1.5,1)})
        elif price > ma60 and rsi >= 40:
            s.update({"swing_watch_ma60": round(ma60,1), "swing_watch_ma20": round(ma20,1)})
        else:
            s.update({"swing_sl_below": round(price-atr*1.5,1), "swing_ma60": round(ma60,1)})
        if action_tag in ("hold","strong_buy"):
            add_t = round(max(price,high_60d)*1.005,1)
            lots  = round((avg_vol_20d*1.5)/1000) if avg_vol_20d else 0
            s.update({"add_trigger": add_t, "vol_lots": lots, "reduce_trigger": round(ma20,1)})

    # 風險提醒
    base = {"strong_buy":"🟢 多項指標同步轉強，技術結構健康，可依策略分批佈局。",
            "hold":"🔵 趨勢仍在多頭軌道內，建議續抱觀察，留意加碼/減碼觸發價位。",
            "overheat":"🟡 短線漲多乖離已大，建議暫緩追高，等待回測均線後再評估進場。",
            "watch":"🟠 股價已跌破生命線但季線仍在守，屬整理區間，建議耐心觀察季線防守是否成立。",
            "reduce":f"🔴 警告：{d['name']} 已實質跌破 20 日生命線與季線防守，技術面轉弱，請嚴格執行停損紀律！"}
    risk_lines = [base[action_tag]]
    if bias_pct > 10:
        risk_lines.append(f"⚠️ 正乖離過高 ({bias_pct:.1f}%)：隨時有向 20MA ({ma20:.1f}) 修正風險，切勿追高！")
    elif bias_pct < -10:
        risk_lines.append(f"⚠️ 負乖離過大 ({bias_pct:.1f}%)：股價短線超跌，趨勢未明前勿貿然接刀。")
    if rsi > 70:
        risk_lines.append(f"⚠️ RSI 超買區 ({rsi:.1f})：買盤過熱，嚴防主力高檔反手出貨。")
    elif rsi < 30:
        risk_lines.append(f"⚠️ RSI 超賣區 ({rsi:.1f})：短線跌深，但趨勢未明，勿貿然接刀。")
    if action_tag in ("strong_buy","overheat") and v_ratio > 1.5:
        risk_lines.append("🔥 短線波動劇烈，單筆資金請嚴格控制在 6-8%。")
    if action_tag == "reduce" and price < ma60:
        risk_lines.append(f"⚠️ 季線 ({ma60:.1f} 元) 已跌破，原防守位失效，請嚴格控管持股部位風險。")

    # 股利顯示
    div_cash = d["div_cash"]
    yield_rate = round(div_cash/price*100, 2) if price > 0 else 0
    day_chg = round(price - d["prev_close"], 2)
    day_chg_pct = round(day_chg / d["prev_close"] * 100, 2) if d["prev_close"] else 0

    return jsonify({
        "stock_no": stock_no, "name": d["name"],
        "price": d["price"], "prev_close": d["prev_close"],
        "day_chg": day_chg, "day_chg_pct": day_chg_pct,
        "decision_text": decision_text, "decision_color": decision_color, "action_tag": action_tag,
        "is_bull": price > ma20,
        "ma10": round(ma10,1), "ma20": round(ma20,1), "ma60": round(ma60,1),
        "bias_pct": round(bias_pct,1), "rsi": round(rsi,1), "v_ratio": round(v_ratio,1),
        "pattern": d["pattern"],
        "div_display": f"{round(div_cash,2):g}",
        "stk_display": f"{round(d['div_stk'],2):g} 元" if d["div_stk"]>0 else "-",
        "yield_rate": yield_rate,
        "pe_display": f"{d['pe']:.1f} 倍" if d["pe"]>0 else "虧損/無",
        "div_period": d["div_period"], "div_confirmed": d["div_confirmed"],
        "futures_net": d["futures_net"], "futures_date": d["futures_date"],
        "is_etf": d["is_etf"],
        "strategy": s, "risk_lines": risk_lines,
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
