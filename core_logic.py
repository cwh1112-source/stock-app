import json
import urllib.request
import webbrowser
import os
import sys
import threading
import socket
import time
import ssl
import csv
import io
from http.server import BaseHTTPRequestHandler, HTTPServer

# ==========================================
# 部分台灣官方/半官方網域（twse / tpex / taifex）的 SSL 憑證鏈
# 缺少 Subject Key Identifier，會導致標準驗證失敗（CERTIFICATE_VERIFY_FAILED）。
# 這裡建立一個專用的「不驗證憑證」context，只用在這幾個已知有此問題的網域，
# 其餘請求（如 Yahoo Finance）仍走預設的正常憑證驗證，不影響整體安全性。
# ==========================================
_UNVERIFIED_SSL_CONTEXT = ssl.create_default_context()
_UNVERIFIED_SSL_CONTEXT.check_hostname = False
_UNVERIFIED_SSL_CONTEXT.verify_mode = ssl.CERT_NONE

def _urlopen_relaxed(req, timeout=5):
    """專供 twse/tpex/taifex 等已知憑證有問題的網域使用"""
    return urllib.request.urlopen(req, timeout=timeout, context=_UNVERIFIED_SSL_CONTEXT)

# ==========================================
# 核心修復：記憶體直衝網頁，完成後自動銷毀
# ==========================================
class MemoryHTMLHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(self.server.html_content.encode('utf-8'))
        
    def log_message(self, format, *args):
        pass # 隱藏日誌

def find_free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port

# ==========================================
# 共用工具：附過期時間的簡易快取
# 避免程式長時間執行（例如改為常駐服務）時，記憶體快取的資料
# 永遠不更新，導致抓到過期的舊資料。
# CACHE_TTL_SECONDS：快取有效時間，預設 6 小時
# （公司基本資料、股利公告都是低頻更新的資料，不需要太短的 TTL）
# ==========================================
CACHE_TTL_SECONDS = 6 * 60 * 60

class _TTLCache:
    def __init__(self, ttl_seconds=CACHE_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._store = {}  # key -> (value, fetched_at_timestamp)

    def get(self, key):
        item = self._store.get(key)
        if item is None:
            return None
        value, fetched_at = item
        if time.time() - fetched_at > self._ttl:
            # 已過期，視同未快取
            del self._store[key]
            return None
        return value

    def set(self, key, value):
        self._store[key] = (value, time.time())


# ==========================================
# 新增：抓取上市/上櫃公司「股利分派情形」真實資料
# 取得個股最新一筆已公告股利（現金股利＋股票股利）
# ==========================================
_DIVIDEND_CACHE = _TTLCache()

def is_etf_code(stock_no):
    """
    粗略判斷是否為 ETF 代號：
    台股 ETF 代號通常為 00 開頭（如 0050、00981A），
    一般個股代號則是 1~9 開頭的 4 碼數字。
    ETF 沒有「股利分派情形」這種公告資料（ETF 配息走不同的揭露機制），
    判斷出是 ETF 就直接跳過股利查詢，避免無謂的失敗嘗試。
    """
    return str(stock_no).strip().startswith("00")


def _load_dividend_table(market):
    """
    market: "twse"（上市）或 "tpex"（上櫃）
    回傳該市場全部公司的股利分派情形列表（list of dict），失敗回傳 None。
    結果會快取在記憶體中（有效期 CACHE_TTL_SECONDS），避免短時間內重複抓取，
    但也不會永久使用過期資料。
    """
    cached = _DIVIDEND_CACHE.get(market)
    if cached is not None:
        return cached

    # tpex 開放資料平台改版過幾次，路徑曾經變動，這裡依序嘗試多個候選 URL，
    # 第一個成功回傳「JSON 陣列」的就採用，全部失敗才真正回傳 None。
    candidate_urls = {
        "twse": [
            "https://openapi.twse.com.tw/v1/opendata/t187ap45_L",
        ],
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
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"})
            with _urlopen_relaxed(req, timeout=5) as r:
                raw = r.read().decode('utf-8')
            data = json.loads(raw)
            if isinstance(data, list) and data:
                _DIVIDEND_CACHE.set(market, data)
                return data
        except Exception:
            continue

    print(f"⚠️ {market} 股利資料來源暫時無法取得（已嘗試 {len(urls)} 個來源）")
    # 注意：失敗時刻意不寫入快取，讓下一次查詢可以立即重試，
    # 而不是被 TTL 卡住、誤把「暫時連線失敗」當成「快取過的有效空結果」。
    return None


def fetch_dividend_info(stock_no, debug=False):
    """
    回傳 dict：
      cash: 現金股利合計(float) = 該年度所有獨立股利案的現金股利加總
      stock: 股票股利合計(float) = 該年度所有獨立股利案的股票股利加總
      period: 股利所屬期間(str)，若同年度有多筆會列出全部期間
      status: 決議（擬議）進度(str)，取最新一筆的狀態
      cases: 該年度各筆獨立股利案的明細列表（debug/顯示用）
    若抓不到任何公告股利，回傳 None，呼叫端應自行 fallback。

    處理邏輯：
      有些公司一年配息不只一次（例如期中配＋期末配，各自是獨立股利案），
      若只取「出表日期最新」的單一一筆，會漏掉其他次配息、低估全年總額。
      因此先用「股利年度＋期別」當作股利案的唯一識別，相同識別只保留
      出表日期最新的版本（避免重複計算同一案的擬議版與正式通過版），
      不同識別則視為獨立股利案，全部加總。

    ETF（00 開頭代號）不適用「上市公司股利分派情形」這份資料，
    直接跳過查詢，避免無謂的失敗請求。
    """
    if is_etf_code(stock_no):
        return None

    for market in ("twse", "tpex"):
        table = _load_dividend_table(market)
        if not table:
            continue

        rows = [row for row in table if str(row.get("公司代號", "")).strip() == str(stock_no).strip()]
        if not rows:
            continue

        if debug:
            print(f"🔍 偵錯：{market} 找到 {len(rows)} 筆 {stock_no} 的股利紀錄")
            for r in rows:
                print(f"🔍   原始列：年度={r.get('股利年度')}、期別={r.get('期別')}、出表日期={r.get('出表日期')}、"
                      f"期間={r.get('股利所屬期間')}、現金={r.get('股東配發-盈餘分配之現金股利(元/股)')}、"
                      f"進度={r.get('決議（擬議）進度')}")

        # 用「股利所屬期間」當作獨立股利案的唯一識別碼（比股利年度更精確，
        # 因為公司可能在跨年度時才公告上一季的配息，例如 114年Q4 配息在
        # 115年才出表。若只看「股利年度」會誤判成不同年度、漏算最近一次配息）。
        # 同一所屬期間若有多筆（金額調整前後、擬議版、通過版…），
        # 只保留出表日期最新的版本。
        def _period(row):
            return str(row.get("股利所屬期間", "") or "")

        case_map = {}
        for row in rows:
            case_key = _period(row) or (str(row.get("股利年度", "")), str(row.get("期別", "")))
            existing = case_map.get(case_key)
            if existing is None or str(row.get("出表日期", "")) > str(existing.get("出表日期", "")):
                case_map[case_key] = row

        all_cases = list(case_map.values())
        # 依「股利所屬期間」的起始日期排序（而非出表日期），確保季別先後順序正確
        def _period_start(row):
            p = _period(row)
            # 期間格式通常為 "1141001~1141231"，取前半段當排序依據
            return p.split("~")[0] if "~" in p else p

        all_cases.sort(key=_period_start)

        # 只取最近 4 筆獨立股利案（約等於最近 1 年的配息次數，避免把太久遠的歷史資料也納入）
        distinct_cases = all_cases[-4:]

        if debug:
            print(f"🔍 偵錯：{stock_no} 共有 {len(distinct_cases)} 筆獨立股利案（年度 {latest_year}）")

        def _num(row, key):
            v = row.get(key)
            if v in (None, "", "-"):
                return 0.0
            try:
                return float(str(v).replace(",", ""))
            except Exception:
                return 0.0

        def _case_cash(row):
            return (
                _num(row, "股東配發-盈餘分配之現金股利(元/股)")
                + _num(row, "股東配發-法定盈餘公積發放之現金(元/股)")
                + _num(row, "股東配發-資本公積發放之現金(元/股)")
            )

        def _case_stock(row):
            return (
                _num(row, "股東配發-盈餘轉增資配股(元/股)")
                + _num(row, "股東配發-法定盈餘公積轉增資配股(元/股)")
                + _num(row, "股東配發-資本公積轉增資配股(元/股)")
            )

        # 確認過的真實欄位名稱（來自台灣證交所 t187ap45_L 上市公司股利分派情形 API）
        def _case_fallback_cash(row, current):
            if current != 0.0:
                return current
            for k, v in row.items():
                if "現金" in k and "股利" in k and v not in (None, "", "-"):
                    val = _num(row, k)
                    if val:
                        return val
            return current

        def _case_fallback_stock(row, current):
            if current != 0.0:
                return current
            for k, v in row.items():
                if ("股票" in k and "股利" in k and v not in (None, "", "-")) or \
                   ("配股" in k and "元" in k and v not in (None, "", "-")):
                    val = _num(row, k)
                    if val:
                        return val
            return current

        total_cash = 0.0
        total_stock = 0.0
        periods = []
        for row in distinct_cases:
            c = _case_fallback_cash(row, _case_cash(row))
            s = _case_fallback_stock(row, _case_stock(row))
            total_cash += c
            total_stock += s
            p = str(row.get("股利所屬期間", "") or "")
            if p and p not in periods:
                periods.append(p)
            if debug:
                print(f"🔍   股利案（期別 {row.get('期別','')}）：現金 {c}、股票 {s}、期間 {p}、進度 {row.get('決議（擬議）進度','')}")

        # 狀態取最新一筆（最後公告的進度）
        latest_case = distinct_cases[-1] if distinct_cases else None
        status = str(latest_case.get("決議（擬議）進度", "") or "") if latest_case else ""
        # 若同年度有多筆獨立股利案，期間欄位合併顯示（例如「期中配＋期末配」）
        period = "、".join(periods) if periods else ""

        # 另外找出「已生效」的最近一次配息（決議進度為「股東會通過」或等同已定案的版本），
        # 與「最新公告」（可能仍是董事會擬議、尚未除息）區分開來，
        # 因為最新公告不代表已經實際發放，金額也可能在股東會前再調整。
        confirmed_cases = [
            r for r in distinct_cases
            if "股東會" in str(r.get("決議（擬議）進度", "")) and "通過" in str(r.get("決議（擬議）進度", ""))
        ]
        confirmed_latest = confirmed_cases[-1] if confirmed_cases else None

        confirmed_cash = None
        confirmed_period = None
        if confirmed_latest:
            confirmed_cash = round(_case_fallback_cash(confirmed_latest, _case_cash(confirmed_latest)), 2)
            confirmed_period = str(confirmed_latest.get("股利所屬期間", "") or "")

        # 原始資料偶有極小的浮點殘值（例如多個欄位加總後變成 6.00003573），
        # 股利金額通常最多到小數點後 2 位（分），這裡四捨五入避免顯示異常
        cash = round(total_cash, 2)
        stock = round(total_stock, 2)

        # 最新一筆單獨的配息金額（用於卡片顯示，不加總）
        latest_cash = round(_case_fallback_cash(latest_case, _case_cash(latest_case)), 2) if latest_case else cash
        latest_period = str(latest_case.get("股利所屬期間", "") or "") if latest_case else period

        return {
            "cash": cash, "stock": stock, "period": period, "status": status,
            "case_count": len(distinct_cases),
            # 最新一筆公告的單次金額（用於卡片顯示，不加總）
            "latest_cash": latest_cash, "latest_period": latest_period,
            # 已生效（股東會通過）的最近一次配息金額與期間；若尚無已通過版本則為 None
            "confirmed_cash": confirmed_cash, "confirmed_period": confirmed_period,
            # 是否為已生效的數字（latest_case 跟 confirmed_latest 是否同一筆）
            "is_latest_confirmed": (confirmed_latest is not None and confirmed_latest is latest_case),
        }

    return None

# ==========================================
# 新增：自動抓取股票中文名稱（不再只能靠寫死的 db 清單）
#
# 來源優先順序：
#   1. 台灣證交所「上市/上櫃公司基本資料」靜態清單（最權威、最穩定，
#      每日更新、不受開盤時間限制，理論上涵蓋所有上市櫃公司）
#   2. mis.twse 即時資訊 API（涵蓋上市/上櫃，但偶有查無資料的個案）
#   3. Yahoo Finance（最後備援，常只有英文名稱）
# ==========================================
_COMPANY_NAME_CACHE = _TTLCache()

def _load_company_basic_list(market):
    """
    market: "twse"（上市）或 "tpex"（上櫃）
    回傳 dict：{公司代號: 公司簡稱}
    這份資料是公司登記用的靜態基本資料，每天更新，不受開盤時間影響，
    理論上涵蓋所有上市/上櫃公司，是目前最穩定可靠的名稱來源。
    結果會快取在記憶體中（有效期 CACHE_TTL_SECONDS），避免短時間內重複抓取
    這份體積較大的清單，但也不會永久使用過期資料。
    """
    cached = _COMPANY_NAME_CACHE.get(market)
    if cached is not None:
        return cached

    urls = {
        "twse": "https://mopsfin.twse.com.tw/opendata/t187ap03_L.csv",
        "tpex": "https://mopsfin.twse.com.tw/opendata/t187ap03_O.csv",
    }
    url = urls.get(market)
    result = {}

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"})
        with _urlopen_relaxed(req, timeout=5) as r:
            raw = r.read().decode('utf-8-sig')  # 去除可能的 BOM
        reader = csv.DictReader(io.StringIO(raw))
        for row in reader:
            code = str(row.get("公司代號", "")).strip()
            short_name = str(row.get("公司簡稱", "")).strip()
            if code and short_name:
                result[code] = short_name
    except Exception as e:
        print(f"⚠️ {market} 公司基本資料清單抓取失敗：{e}")
        # 失敗時不寫入快取，讓下次查詢可以立即重試
        return {}

    _COMPANY_NAME_CACHE.set(market, result)
    return result


def fetch_pe(stock_no, debug=False):
    """
    本益比抓取順序：
    1. Yahoo Finance（最快，上市上櫃都能抓）
    2. TWSE（上市股票備用）
    3. TWSE OTC（上櫃備用）
    4. TPEx（最後手段）
    """
    # ── 1. Yahoo Finance（最快，一個 API 搞定）──
    for suffix in [".TW", ".TWO"]:
        try:
            url_yf = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock_no}{suffix}?range=1d&interval=1d"
            req = urllib.request.Request(url_yf, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                yf_data = json.loads(r.read().decode())
            result_list = yf_data.get("chart", {}).get("result") or []
            if not result_list:
                continue
            meta = result_list[0].get("meta", {})
            trailing_pe = meta.get("trailingPE")
            if trailing_pe and float(trailing_pe) > 0:
                if debug: print(f"🔍 Yahoo Finance trailingPE {stock_no}{suffix}: {trailing_pe}")
                return round(float(trailing_pe), 1)
            eps = meta.get("epsTrailingTwelveMonths")
            current_price = meta.get("regularMarketPrice")
            if eps and current_price and float(eps) > 0:
                pe_calc = float(current_price) / float(eps)
                if debug: print(f"🔍 Yahoo Finance EPS算PE {stock_no}{suffix}: {pe_calc:.1f}")
                return round(pe_calc, 1)
            if debug: print(f"🔍 Yahoo Finance PE {stock_no}{suffix}: 無有效值")
        except Exception as e:
            if debug: print(f"🔍 Yahoo Finance PE 失敗 {stock_no}{suffix}: {e}")
            continue

    # ── 2. TWSE 上市 API ──
    try:
        url = "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d?response=json&selectType=ALL"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"})
        with _urlopen_relaxed(req, timeout=5) as r:
            data = json.loads(r.read().decode('utf-8'))
        rows = data.get("data", [])
        fields = data.get("fields", [])
        if debug: print(f"🔍 TWSE 本益比 API：共 {len(rows)} 筆，欄位：{fields}")
        for row in rows:
            if row and str(row[0]).strip() == str(stock_no).strip():
                if debug: print(f"🔍 TWSE 找到 {stock_no}，完整列：{row}")
                pe_idx = next((i for i, f in enumerate(fields) if "本益" in str(f)), 4)
                if debug: print(f"🔍 本益比欄位索引：{pe_idx}，值：{row[pe_idx] if pe_idx < len(row) else 'N/A'}")
                pe_val = str(row[pe_idx]).strip() if pe_idx < len(row) else ""
                if pe_val and pe_val not in ("-", "--", ""):
                    return round(float(pe_val.replace(",", "")), 2)
    except Exception as e:
        if debug: print(f"🔍 TWSE 本益比抓取失敗：{e}")

    # ── 3. TWSE OTC 上櫃 API ──
    try:
        url_otc = "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d?response=json&selectType=ALLOTC"
        req = urllib.request.Request(url_otc, headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"})
        with _urlopen_relaxed(req, timeout=5) as r:
            data = json.loads(r.read().decode('utf-8'))
        rows = data.get("data", [])
        fields = data.get("fields", [])
        if debug: print(f"🔍 TWSE OTC 本益比 API：共 {len(rows)} 筆，欄位：{fields}")
        for row in rows:
            if row and str(row[0]).strip() == str(stock_no).strip():
                pe_idx = next((i for i, f in enumerate(fields) if "本益" in str(f)), 4)
                pe_val = str(row[pe_idx]).strip() if pe_idx < len(row) else ""
                if pe_val and pe_val not in ("-", "--", ""):
                    return round(float(pe_val.replace(",", "")), 2)
    except Exception as e:
        if debug: print(f"🔍 TWSE OTC 本益比抓取失敗：{e}")

    # ── 4. TPEx API（最後手段）──
    for pe_url in [
        "https://www.tpex.org.tw/web/stock/aftertrading/peratio_listed/peListed_result.php?l=zh-tw&o=json",
        "https://www.tpex.org.tw/openapi/v1/tpex_peratio_listed",
    ]:
        try:
            req = urllib.request.Request(pe_url, headers={
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
                "Referer": "https://www.tpex.org.tw/",
                "Accept": "application/json, text/plain, */*",
            })
            with _urlopen_relaxed(req, timeout=5) as r:
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
                            return round(float(pe_val.replace(",", "")), 2)
                elif row and str(row[0]).strip() == str(stock_no).strip():
                    pe_val = str(row[4]).strip()
                    if pe_val and pe_val not in ("-", "--", ""):
                        return round(float(pe_val.replace(",", "")), 2)
        except Exception as e:
            if debug: print(f"🔍 TPEx 本益比抓取失敗（{pe_url}）：{e}")
            continue

    if debug: print(f"🔍 本益比查無結果，顯示資料暫缺")
    return None


def fetch_cn_name(stock_no):
    stock_no = str(stock_no).strip()

    # 來源 1：靜態公司基本資料清單（最穩定、優先採用）
    for market in ("twse", "tpex"):
        table = _load_company_basic_list(market)
        if table and stock_no in table:
            return table[stock_no]

    # 來源 2：mis.twse 即時資訊 API
    for attempt in range(2):
        for ex in ("tse", "otc"):
            try:
                url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex}_{stock_no}.tw&json=1&delay=0"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"})
                with _urlopen_relaxed(req, timeout=8) as r:
                    data = json.loads(r.read().decode('utf-8'))
                arr = data.get("msgArray") or []
                if not arr:
                    continue
                name = arr[0].get("n", "")
                if name:
                    return name
            except Exception:
                continue

    # 來源 3：Yahoo Finance（最後備援，常只有英文名稱）
    for suffix in (".TWO", ".TW"):
        try:
            url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={stock_no}{suffix}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read().decode('utf-8'))
            results = data.get("quoteResponse", {}).get("result", [])
            if results:
                name = results[0].get("longName") or results[0].get("shortName")
                if name:
                    return name
        except Exception:
            continue

    return None

# ==========================================
# 新增：抓取期交所「三大法人－總表－依日期」真實資料
# 取得最新一筆「外資及陸資」在台股期貨（小台＋大台）的未平倉淨口數
# ==========================================
_FUTURES_CACHE = _TTLCache(ttl_seconds=90 * 60)  # 1.5 小時快取

def fetch_foreign_futures_net_position(debug=False):
    """
    回傳 (net_position, date_str)。
    net_position: 外資台指期淨部位（口數，整數，負值代表淨空單）
    date_str: 資料日期 (YYYY/MM/DD)
    若抓取失敗回傳 (None, None)，呼叫端應自行處理顯示「資料暫缺」。

    debug=True 時，會在每個可能失敗的步驟印出詳細診斷訊息，
    方便定位問題卡在「連線失敗」「資料格式不符預期」還是「欄位名稱不對」。
    """
    url = "https://openapi.taifex.com.tw/v1/MarketDataOfMajorInstitutionalTradersGeneralBytheDate"

    # 先查快取（1.5小時內不重複抓）
    cached = _FUTURES_CACHE.get("futures")
    if cached is not None:
        if debug: print(f"🔍 外資期貨部位：使用快取資料 {cached}")
        return cached

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"})
        with _urlopen_relaxed(req, timeout=5) as r:
            raw = r.read().decode('utf-8')
        data = json.loads(raw)
    except Exception as e:
        print(f"⚠️ 外資期貨部位【步驟1：連線/解析JSON】失敗：{e}")
        return None, None

    if debug:
        print(f"🔍 外資期貨部位偵錯：成功連線，資料筆數 = {len(data) if isinstance(data, list) else 'N/A（非列表）'}")

    if not data:
        print("⚠️ 外資期貨部位【步驟2】：API 回傳空資料")
        return None, None

    if not isinstance(data, list):
        print(f"⚠️ 外資期貨部位【步驟2】：API 回傳格式不是預期的列表，實際型別：{type(data)}")
        if debug:
            print(f"🔍 原始回傳內容（前500字）：{str(data)[:500]}")
        return None, None

    if debug and len(data) > 0:
        print(f"🔍 第一筆資料的所有欄位名稱：{list(data[0].keys())}")
        print(f"🔍 第一筆資料完整內容：{data[0]}")

    # 確認過的真實欄位名稱（英文鍵名，來自期交所 OpenAPI 實際回傳）：
    #   Date            : 日期，格式 YYYYMMDD
    #   Item            : 身份別（自營商 / 投信 / 外資及陸資）
    #   OpenInterest(Net): 多空淨額未平倉口數（整數字串，負值代表淨空單）
    # 這份「總表」本身就是台股期貨整體的三大法人加總，沒有個別契約名稱欄位，
    # 不需要再篩選「臺股期貨」這個條件。
    target_rows = []
    sample_identities = set()
    for row in data:
        identity = str(row.get("Item", ""))
        if identity:
            sample_identities.add(identity)
        if "外資" in identity:
            target_rows.append(row)

    if not target_rows:
        print("⚠️ 外資期貨部位【步驟3】：找不到身份別包含「外資」的資料列")
        if debug:
            print(f"🔍 資料中出現過的身份別（全部）：{list(sample_identities)}")
        return None, None

    if debug:
        print(f"🔍 找到 {len(target_rows)} 筆符合條件的資料")

    # 取日期最新的一筆
    def _row_date(row):
        return str(row.get("Date", ""))

    target_rows.sort(key=_row_date)
    latest = target_rows[-1]

    date_str = _row_date(latest)
    # 將 YYYYMMDD 轉成較易讀的 YYYY/MM/DD
    if len(date_str) == 8 and date_str.isdigit():
        date_str = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"

    if debug:
        print(f"🔍 最新一筆符合條件的資料（日期：{date_str}）：{latest}")

    net_val = latest.get("OpenInterest(Net)")
    matched_key = "OpenInterest(Net)" if net_val not in (None, "", "-") else None

    if net_val in (None, "", "-"):
        print(f"⚠️ 外資期貨部位【步驟4】：找到資料列，但 OpenInterest(Net) 欄位無值")
        if debug:
            print(f"🔍 該筆資料實際擁有的欄位：{list(latest.keys())}")
        return None, date_str

    if debug:
        print(f"🔍 命中欄位「{matched_key}」，原始值：{net_val}")

    try:
        net_val = int(str(net_val).replace(",", ""))
    except Exception as e:
        print(f"⚠️ 外資期貨部位【步驟5】：數值轉換失敗：{e}，原始值：{net_val}")
        return None, date_str

    if debug:
        print(f"✅ 外資期貨部位抓取成功：{net_val} 口（{date_str}）")

    result = (net_val, date_str)
    _FUTURES_CACHE.set("futures", result)
    return result


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
        "8064": {"n": "東捷",   "d": 0.0,  "stk": 0.0, "e": 0.8}
    }
    info = db.get(stock_no, {"n": f"股票{stock_no}", "d": 0.0, "stk": 0.0, "e": 1.0})

    # 自動抓取真實中文公司名稱，抓不到才退回 db 裡寫死的名稱（或預設的「股票xxxx」）
    real_name = fetch_cn_name(stock_no)
    if real_name:
        info['n'] = real_name

    # 抓取真實本益比（直接由交易所計算提供，不用自己猜 EPS）
    real_pe = fetch_pe(stock_no)

    # 優先抓取真實公告股利資料，失敗才退回寫死的 db 數字
    dividend_period = ""
    dividend_status = ""
    confirmed_cash = None
    confirmed_period = None
    is_latest_confirmed = False
    latest_div_cash = None   # 最新一筆單次金額（卡片顯示用）
    latest_div_period = ""
    real_div = fetch_dividend_info(stock_no, debug=False)
    if real_div:
        div_amount = real_div["cash"]          # 加總（保留備用）
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

    # ── 抓取歷史日線（Yahoo Finance）──
    suffixes = [".TW", ".TWO"]
    res = None
    for suffix in suffixes:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock_no}{suffix}?range=1y&interval=1d"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            res = json.loads(urllib.request.urlopen(req, timeout=8).read().decode())["chart"]["result"][0]
            break
        except:
            try:
                url2 = f"https://query2.finance.yahoo.com/v8/finance/chart/{stock_no}{suffix}?range=1y&interval=1d"
                req2 = urllib.request.Request(url2, headers={"User-Agent": "Mozilla/5.0"})
                res = json.loads(urllib.request.urlopen(req2, timeout=8).read().decode())["chart"]["result"][0]
                break
            except:
                continue

    if not res:
        print(f"❌ 錯誤：無法取得代碼 {stock_no} 的數據")
        return None

    try:
        quote = res["indicators"]["quote"][0]
        c     = [float(x) for x in quote["close"]  if x is not None]
        o_raw = [float(x) for x in quote.get("open",   []) if x is not None]
        h_raw = [float(x) for x in quote.get("high",   []) if x is not None]
        l_raw = [float(x) for x in quote.get("low",    []) if x is not None]
        v_raw = [float(x) for x in quote.get("volume", []) if x is not None]
        m     = res.get("meta", {})
        price = float(m.get("regularMarketPrice", c[-1]))

        # 昨收（Yahoo Finance 歷史日線的倒數第二筆）
        prev_close = c[-2] if len(c) >= 2 else price

        ma10, ma20, ma60 = sum(c[-10:])/10, sum(c[-20:])/20, sum(c[-60:])/60
        ma120 = sum(c[-120:])/120 if len(c) >= 120 else sum(c)/len(c)
        bias  = ((price - ma20) / ma20) * 100

        diff = [c[i] - c[i-1] for i in range(1, len(c))]
        gain = sum(d for d in diff[-14:] if d > 0) / 14
        loss = abs(sum(d for d in diff[-14:] if d < 0)) / 14
        rsi  = 100 - (100 / (1 + (gain / (loss if loss != 0 else 1))))

        # 成交量倍數
        if len(v_raw) >= 21:
            avg_vol_20d = sum(v_raw[-21:-1]) / 20
            v_ratio = (v_raw[-1] / avg_vol_20d) if avg_vol_20d > 0 else 1.0
        else:
            avg_vol_20d = sum(v_raw) / len(v_raw) if v_raw else 0
            v_ratio = 1.0

        # ATR(14)
        if len(h_raw) >= 15 and len(l_raw) >= 15 and len(c) >= 15:
            tr = [max(h_raw[i]-l_raw[i], abs(h_raw[i]-c[i-1]), abs(l_raw[i]-c[i-1])) for i in range(1, len(c))]
            atr = sum(tr[-14:]) / 14
        else:
            atr = price * 0.03

        # 近60日最高價
        high_60d = max(h_raw[-60:]) if len(h_raw) >= 60 else (max(h_raw) if h_raw else price)

        # K線形態
        last_open = o_raw[-1] if o_raw else price
        if price > last_open and price > ma20 and v_ratio > 1.2:
            pattern = "🎯 強勢紅K突破"
        elif price > last_open:
            pattern = "📈 紅K攻擊"
        elif price <= last_open and price > ma20:
            pattern = "⚠️ 多頭回檔"
        else:
            pattern = "📉 弱勢盤整"

        # ── 葛蘭碧：計算 20MA 斜率（上彎/走平/下彎）──
        if len(c) >= 25:
            ma20_5d_ago = sum(c[-25:-5]) / 20
            ma20_slope_pct = (ma20 - ma20_5d_ago) / ma20 * 100 if ma20 > 0 else 0.0
        else:
            ma20_slope_pct = 0.0

        if ma20_slope_pct > 0.3:
            ma20_direction = "up"
        elif ma20_slope_pct < -0.3:
            ma20_direction = "down"
        else:
            ma20_direction = "flat"

        # 昨日20MA（判斷今天是否剛穿越均線）
        prev_price = c[-2] if len(c) >= 2 else price
        ma20_yesterday = sum(c[-21:-1]) / 20 if len(c) >= 21 else ma20

        # ── 葛蘭碧八大訊號（修正版）──
        # 買1：均線上彎，股價從下方突破均線（趨勢反轉向上）
        gran_buy1  = (ma20_direction == "up" and prev_price < ma20_yesterday and price >= ma20)
        # 買2：均線上彎，股價在均線上方回測均線附近獲支撐（乖離<=5%）
        gran_buy2  = (price > ma20 and ma20_direction == "up" and bias <= 5 and not gran_buy1)
        # 買3：均線上彎，股價短暫跌破均線後立即站回（假跌破）
        gran_buy3  = (price >= ma20 and ma20_direction == "up" and prev_price < ma20_yesterday and bias > 0)
        # 買4：負乖離過大+RSI超賣，超跌反彈機會
        gran_buy4  = (price < ma20 and bias < -8 and rsi < 35)
        # 賣1：均線下彎，股價從上方跌破均線（趨勢反轉向下）
        gran_sell1 = (ma20_direction == "down" and prev_price > ma20_yesterday and price < ma20)
        # 賣2：均線下彎，股價在均線下方反彈至均線附近受壓（乖離>=-5%）
        gran_sell2 = (price < ma20 and ma20_direction == "down" and bias >= -5 and not gran_sell1)
        # 賣3：均線下彎，股價短暫突破均線後立即跌回（假突破）
        gran_sell3 = (price < ma20 and ma20_direction == "down" and prev_price > ma20_yesterday and bias < 0)
        # 賣4：正乖離過大+RSI超買，超漲回調風險
        gran_sell4 = (price > ma20 and bias > 10 and rsi > 70)

        return {
            "name": info['n'], "price": price, "ma10": ma10, "ma20": ma20, "ma60": ma60, "ma120": ma120,
            "bias": bias, "rsi": rsi, "div": div_amount, "stk": stk_amount, "div_period": dividend_period,
            "div_status": dividend_status, "confirmed_cash": confirmed_cash,
            "confirmed_period": confirmed_period, "is_latest_confirmed": is_latest_confirmed,
            "latest_div_cash": latest_div_cash, "latest_div_period": latest_div_period,
            "pe": real_pe if real_pe is not None else 0,
            "prev_close": prev_close, "v_ratio": v_ratio, "avg_vol_20d": avg_vol_20d,
            "atr": atr, "high_60d": high_60d, "pattern": pattern,
            "ma20_direction": ma20_direction, "ma20_slope_pct": ma20_slope_pct,
            "gran_buy1": gran_buy1, "gran_buy2": gran_buy2, "gran_buy3": gran_buy3, "gran_buy4": gran_buy4,
            "gran_sell1": gran_sell1, "gran_sell2": gran_sell2, "gran_sell3": gran_sell3, "gran_sell4": gran_sell4,
        }
    except Exception as e:
        print(f"數據解析錯誤: {e}")
        return None

def _build_not_found_html(stock_no):
    """查無此股票時顯示的明確錯誤頁面，避免使用者誤以為程式沒反應。"""
    return f"""
    <html><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f5f7fa; padding: 25px; margin: 0; color: #111; }}
        .app-container {{ background: white; max-width: 580px; margin: 80px auto; border-radius: 28px; padding: 40px 30px; box-shadow: 0 10px 40px rgba(0,0,0,0.08); text-align: center; }}
        .icon {{ font-size: 56px; margin-bottom: 10px; }}
        .title {{ font-size: 26px; font-weight: 800; color: #1a202c; margin-bottom: 14px; }}
        .desc {{ font-size: 17px; color: #5a6578; line-height: 1.8; }}
        .code {{ display: inline-block; background: #f8f9fb; border: 1.5px solid #edf0f4; border-radius: 8px; padding: 4px 14px; font-weight: 800; color: #d9383a; }}
        .hint {{ margin-top: 24px; font-size: 14.5px; color: #888; line-height: 1.8; }}
    </style></head><body>
    <div class="app-container">
        <div class="icon">🔍</div>
        <div class="title">查無此股票</div>
        <div class="desc">輸入的代碼 <span class="code">{stock_no}</span> 找不到對應的股價資料。</div>
        <div class="hint">
            可能原因：<br>
            ・代碼輸入錯誤或格式有誤<br>
            ・該股票已下市或剛掛牌尚未有足夠交易資料<br>
            ・暫時的網路或資料來源問題，請稍後再試
        </div>
    </div>
    </body></html>
    """


def generate_report_html(stock):
    import datetime as _dt
    stock = stock.strip()
    if not stock:
        return None

    d = fetch_comprehensive_data(stock)
    if not d:
        return _build_not_found_html(stock)

    is_bull = d['price'] > d['ma20']

    # ── 五級策略建議 ──
    bias_pct    = d['bias']
    rsi         = d['rsi']
    v_ratio     = d.get('v_ratio', 1.0)
    ma10, ma20, ma60 = d['ma10'], d['ma20'], d['ma60']
    price       = d['price']
    atr         = d.get('atr', price * 0.03)
    high_60d    = d.get('high_60d', price)
    avg_vol_20d = d.get('avg_vol_20d', 0)

    if price > ma20:
        if bias_pct > 6 or rsi > 70:
            decision_text, decision_color, action_tag = "過熱觀察，暫勿追高", "#b08117", "overheat"
        elif v_ratio > 1.5 and ma10 > ma20 > ma60:
            decision_text, decision_color, action_tag = "強烈買進/加碼", "#24936E", "strong_buy"
        else:
            decision_text, decision_color, action_tag = "續抱", "#007AFF", "hold"
    elif price > ma60 and rsi >= 40:
        decision_text, decision_color, action_tag = "觀察整理", "#e67e22", "watch"
    else:
        decision_text, decision_color, action_tag = "警示/建議減碼", "#d9383a", "reduce"

    is_overheat = (action_tag == "overheat")

    # ── 葛蘭碧八大法則訊號 ──
    ma20_direction = d.get('ma20_direction', 'flat')
    ma20_slope_pct = d.get('ma20_slope_pct', 0.0)
    gran_buy1  = d.get('gran_buy1',  False)
    gran_buy2  = d.get('gran_buy2',  False)
    gran_buy3  = d.get('gran_buy3',  False)
    gran_buy4  = d.get('gran_buy4',  False)
    gran_sell1 = d.get('gran_sell1', False)
    gran_sell2 = d.get('gran_sell2', False)
    gran_sell3 = d.get('gran_sell3', False)
    gran_sell4 = d.get('gran_sell4', False)

    direction_label = {"up": "📈 上彎（多頭支撐強）", "down": "📉 下彎（空頭壓力大）", "flat": "➡️ 走平（方向不明）"}
    ma20_dir_text = direction_label.get(ma20_direction, "➡️ 走平")

    gran_signals = []
    if gran_buy1:  gran_signals.append(("buy",  "【買1】均線由下彎轉走平或上彎，股價從下方突破均線 → 趨勢反轉買進訊號，可考慮進場"))
    if gran_buy2:  gran_signals.append(("buy",  "【買2】均線持續上彎，股價回測20MA附近獲支撐反彈 → 趨勢續漲，是加碼好時機"))
    if gran_buy3:  gran_signals.append(("buy",  "【買3】均線上彎，股價短暫跌破均線後立刻站回 → 假跌破洗盤，可逢低承接"))
    if gran_buy4:
        buy4_stop = round(price - atr * 1.5, 1)
        if price < ma60:
            # 季線已跌破：要求更高的確認條件
            buy4_confirm1 = round(ma20, 1)    # 第一步：先站回20MA
            buy4_confirm2 = round(ma60, 1)    # 最終確認：站回季線
            gran_signals.append(("buy", f"【買4】超跌反彈警示：負乖離{bias_pct:.1f}%、RSI超賣（{rsi:.1f}）。⚠️ 注意：季線（{ma60:.1f}元）已跌破，技術面嚴重受損，進場條件需更嚴格：\n　① 短線試單（高風險）：若股價站回20MA {buy4_confirm1:.1f} 元且RSI>40，可少量試單，但須嚴格停損\n　② 真正轉多確認：收盤站回季線 {buy4_confirm2:.1f} 元以上，才代表趨勢可能轉強，可正常布局\n　③ 停損設定：進場後若跌破 {buy4_stop:.1f} 元（現價-1.5xATR），立即出場，勿凹單"))
        else:
            # 季線未跌破：只需站回20MA
            buy4_entry   = round(ma20 * 0.97, 1)
            buy4_confirm = round(ma20, 1)
            gran_signals.append(("buy", f"【買4】超跌反彈警示：負乖離{bias_pct:.1f}%、RSI超賣（{rsi:.1f}），技術面可能出現短線反彈。\n　① 短線反彈觀察：若股價反彈至 {buy4_entry:.1f}~{buy4_confirm:.1f} 元（20MA附近）且RSI>40，可考慮試單\n　② 真正轉多確認：收盤站回 {buy4_confirm:.1f} 元（20MA）以上，代表趨勢可能轉強\n　③ 停損設定：進場後若跌破 {buy4_stop:.1f} 元（現價-1.5xATR），立即出場，勿凹單"))

    if gran_sell1: gran_signals.append(("sell", "【賣1】均線由上彎轉走平或下彎，股價從上方跌破均線 → 趨勢反轉賣出訊號，建議減碼"))
    if gran_sell2: gran_signals.append(("sell", "【賣2】均線持續下彎，股價反彈至20MA附近遭壓力回落 → 趨勢續跌，是減碼時機"))
    if gran_sell3: gran_signals.append(("sell", "【賣3】均線下彎，股價短暫突破均線後立刻跌回 → 假突破騙線，切勿追高"))
    if gran_sell4:
        sell4_tp   = round(price * 1.03, 1)   # 再漲3%可考慮分批停利
        sell4_wait = round(ma20 * 1.03, 1)    # 等回測至此區間再評估進場
        sell4_sl   = round(ma20, 1)            # 若已持有：跌破20MA視為風險升高
        gran_signals.append(("sell", f"【賣4】超漲回調警示：正乖離已達{bias_pct:.1f}%、RSI超買（{rsi:.1f}），短線隨時可能回落修正。\n　① 已持有者：可在 {sell4_tp:.1f} 元附近分批停利保護獲利，並將停損上移至 {sell4_sl:.1f} 元（20MA）\n　② 尚未進場者：此時追高風險極大，等股價回測至 {sell4_wait:.1f} 元附近（20MA+3%緩衝區）再重新評估\n　③ 若RSI回落至65以下且股價仍站穩20MA，代表過熱解除，可恢復正常操作"))

    has_buy_signal  = any(s[0] == "buy"  for s in gran_signals)
    has_sell_signal = any(s[0] == "sell" for s in gran_signals)

    if gran_signals:
        sig_items = ""
        for sig_type, sig_text in gran_signals:
            color = "#24936E" if sig_type == "buy" else "#d9383a"
            icon  = "🟢" if sig_type == "buy" else "🔴"
            sig_items += f"<div style=\"margin:10px 0; font-size:17px; line-height:1.7; color:{color};\"><b>{icon} {sig_text}</b></div>"
        gran_bg    = "#eefbf5" if has_buy_signal and not has_sell_signal else ("#fff5f5" if has_sell_signal else "#f8f9fb")
        gran_border = "#24936E" if has_buy_signal and not has_sell_signal else ("#d9383a" if has_sell_signal else "#ccc")
        gran_html = f"""<div style=\"background:{gran_bg}; border:1.5px solid {gran_border}; border-radius:14px; padding:16px 18px; margin:10px 0;\">
            <div style=\"font-weight:800; font-size:18px; margin-bottom:10px;\">🎯 葛蘭碧訊號觸發：</div>
            {sig_items}
        </div>"""
    else:
        gran_html = "<div class=\"check-item\" style=\"color:#888;\">📊 目前無明確葛蘭碧買賣訊號觸發（持續觀察中）</div>"

    # ── 顯示數值準備 ──
    latest_div = d.get('latest_div_cash') if d.get('latest_div_cash') is not None else d['div']
    yield_rate = (latest_div / d['price'] * 100) if d['price'] > 0 else 0.0
    div_display = f"{round(latest_div, 2):g}"
    stk_display = f"{round(d['stk'], 2):g} 元" if d['stk'] > 0 else "-"
    pe_display  = f"{d['pe']:.1f} 倍" if d['pe'] > 0 else "虧損/無"

    prev_close   = d.get('prev_close', d['price'])
    day_chg      = d['price'] - prev_close
    day_chg_pct  = (day_chg / prev_close * 100) if prev_close else 0.0
    day_chg_color = "#d9383a" if day_chg >= 0 else "#24936E"
    day_chg_sign  = "▲" if day_chg >= 0 else "▼"
    day_chg_html  = (
        f"<div style=\"font-size:17px; color:{day_chg_color}; font-weight:700; margin-top:-8px; margin-bottom:18px;\">"
        f"昨收 {prev_close:.1f} 元　{day_chg_sign} {abs(day_chg):.1f} 元（{day_chg_sign}{abs(day_chg_pct):.1f}%）"
        f"</div>"
    )

    div_status = d.get('div_status', '')
    if "股東會" in div_status and "通過" in div_status:
        status_badge_html = f"<span style=\"display:inline-block; background:#eefbf5; color:#1e5236; border:1.5px solid #24936E; border-radius:20px; padding:4px 14px; font-size:13.5px; font-weight:800;\">✅ {div_status}（已定案）</span>"
    elif "擬議" in div_status or "董事會" in div_status:
        status_badge_html = f"<span style=\"display:inline-block; background:#fff8ec; color:#9a6500; border:1.5px solid #f3b94d; border-radius:20px; padding:4px 14px; font-size:13.5px; font-weight:800;\">⚠️ {div_status}（尚未定案，金額可能調整）</span>"
    elif div_status:
        status_badge_html = f"<span style=\"display:inline-block; background:#f8f9fb; color:#5a6578; border:1.5px solid #edf0f4; border-radius:20px; padding:4px 14px; font-size:13.5px; font-weight:800;\">{div_status}</span>"
    else:
        status_badge_html = ""

    confirmed_note_html = ""
    if d.get('confirmed_cash') is not None and not d.get('is_latest_confirmed'):
        confirmed_note_html = (
            f"<div style=\"margin-top:10px; font-size:14.5px; color:#1e5236; background:#eefbf5; "
            f"border:1.5px solid #c6ebd4; border-radius:10px; padding:10px 14px;\">"
            f"✅ 最近一次已除息生效：每股 <b>{d.get('confirmed_cash')}</b> 元"
            f"（期間：{d.get('confirmed_period','')}）"
            f"</div>"
        )

    if is_bull:
        ma20_html = f"✅ <b style=\"color:#24936E;\">已站穩生命線</b> (目前股價高於 20MA 均線 {d['ma20']:.1f} 元)"
    else:
        ma20_html = f"🚨 <b style=\"color:#d9383a;\">已跌破生命線！</b> (警報：目前股價低於 20MA 均線 {d['ma20']:.1f} 元)"

    range_benchmark = "<span style=\"color:#555; font-size:16px; font-weight:normal;\">【基準：-10% ~ +10%】</span>"
    if d['bias'] > 10.0:
        bias_html = f"⚠️ <b style=\"color:#d9383a;\">+{d['bias']:.1f}%</b> {range_benchmark} <b style=\"color:#d9383a; font-size:16px;\">🚨 正乖離過大，慎防過熱回檔！</b>"
    elif d['bias'] < -10.0:
        bias_html = f"⚠️ <b style=\"color:#2b6cb0;\">{d['bias']:.1f}%</b> {range_benchmark} <b style=\"color:#2b6cb0; font-size:16px;\">🚨 負乖離過大，股價超跌易反彈！</b>"
    elif -10.0 <= d['bias'] < 0:
        bias_html = f"📉 <b style=\"color:#e67e22;\">{d['bias']:.1f}%</b> {range_benchmark} <b style=\"color:#e67e22; font-size:16px;\">⚠️ 軌道內弱勢整理 (已跌破均線，切勿盲目加碼)</b>"
    else:
        bias_html = f"✅ <b style=\"color:#24936E;\">+{d['bias']:.1f}%</b> {range_benchmark} <span style=\"color:#24936E; font-size:16px;\">(軌道內溫和偏多，結構健康)</span>"

    rsi_benchmark = "<span style=\"color:#555; font-size:16px; font-weight:normal;\">【基準：30 ~ 70】</span>"
    if d['rsi'] > 70:
        rsi_html = f"<b style=\"color:#d9383a;\">{d['rsi']:.1f}</b> {rsi_benchmark} <b style=\"color:#d9383a; font-size:16px;\">🚨 偏熱，留意超買回檔風險</b>"
    elif d['rsi'] < 30:
        rsi_html = f"<b style=\"color:#2b6cb0;\">{d['rsi']:.1f}</b> {rsi_benchmark} <b style=\"color:#2b6cb0; font-size:16px;\">🚨 偏冷，留意超賣反彈機會</b>"
    else:
        rsi_html = f"<b style=\"color:#24936E;\">{d['rsi']:.1f}</b> {rsi_benchmark} <span style=\"color:#24936E; font-size:16px;\">(區間內正常，無明顯超買超賣)</span>"

    check_ma_align  = (ma10 > ma20 > ma60 and price > ma10)
    check_ma20      = (price > ma20)
    check_rsi_range = (40 <= rsi <= 65)
    check_bias_range = (abs(bias_pct) <= 6)
    check_volume    = (v_ratio > 1.5)

    checklist_html = f"""
        <div class="check-item">🎯 K線形態 | 現: <b>{d.get('pattern', '-')}</b></div>
        <div class="check-item">📐 20MA方向 | 現: <b>{ma20_dir_text}</b></div>
        <div class="check-item">{'✅' if check_ma_align else '❌'} 均線多頭排列向上 | MA10: {ma10:.1f} / MA20: {ma20:.1f} / MA60: {ma60:.1f}</div>
        <div class="check-item">{'✅' if check_ma20 else '❌'} 站穩生命線 20MA ({ma20:.1f} 元)</div>
        <div class="check-item">{'✅' if check_rsi_range else '❌'} RSI(40-65) | 現: {rsi:.1f}</div>
        <div class="check-item">{'✅' if check_bias_range else '❌'} 乖離率&lt;=6% | 現: {bias_pct:.1f}%</div>
        <div class="check-item">{'✅' if check_volume else '❌'} 爆量(&gt;1.5倍) | 現: {v_ratio:.1f}倍</div>
    """

    # ── 實戰策略 ──
    if is_overheat:
        strategy_blocks_html = f"""
        <div class="strategy-group" style="border-left-color: #b08117;">
            <div class="strategy-title" style="color: #b08117;">⏸️ 過熱暫停進場</div>
            <div class="strategy-item">已過熱，暫不提供進場區間，建議等待回測 20MA（<b>{ma20:.1f}</b> 元）附近再評估</div>
            <div class="strategy-item" style="color:#d9383a;">▪ 參考停利：<b>{price * 1.05:.1f}</b> 元</div>
            <div class="strategy-item" style="color:#2b6cb0;">▪ 參考防守：<b>{ma20:.1f}</b> 元</div>
        </div>"""
    else:
        if price > ma10:
            atr_stop_short = price - (atr * 1.5)
            short_html = f"""
            <div class="strategy-item">▪ 短線佈局：<b>{ma10:.1f} ~ {ma10 * 1.01:.1f}</b> 元</div>
            <div class="strategy-item" style="color:#d9383a;">▪ 短線停利：<b>{price * 1.05:.1f}</b> 元</div>
            <div class="strategy-item" style="color:#2b6cb0;">▪ 短線停損：<b>{atr_stop_short:.1f}</b> 元（現價-1.5xATR）</div>"""
        else:
            short_html = f"<div class=\"strategy-item\">▪ 跌破 10MA（<b>{ma10:.1f}</b> 元），暫勿盲目接刀</div>"

        if price > ma20:
            atr_stop_swing = ma20 - (atr * 1.5)
            swing_target   = high_60d if high_60d > price else price * 1.15
            target_label   = "前波高點" if high_60d > price else "估算目標(+15%)"
            swing_html = f"""
            <div class="strategy-item">▪ 波段佈局：<b>{ma20:.1f} ~ {ma20 * 1.015:.1f}</b> 元</div>
            <div class="strategy-item" style="color:#d9383a;">▪ 波段目標：<b>{swing_target:.1f}</b>（{target_label}）</div>
            <div class="strategy-item" style="color:#2b6cb0;">▪ 防守：<b>{atr_stop_swing:.1f}</b> 元（20MA-1.5xATR）</div>"""
        elif price > ma60 and rsi >= 40:
            swing_html = f"""
            <div class="strategy-item">▪ 波段佈局：生命線下方，以季線 <b>{ma60:.1f}</b> 為守</div>
            <div class="strategy-item">💡 若跌破季線 {ma60:.1f} 元，視為轉弱訊號，建議減碼或停損</div>
            <div class="strategy-item">💡 若站回 20MA {ma20:.1f} 元以上，視為轉強訊號，可恢復正常持有</div>"""
        else:
            atr_stop_below = price - (atr * 1.5)
            swing_html = f"""
            <div class="strategy-item" style="color:#d9383a;">⚠️ 季線（{ma60:.1f} 元）已跌破，原防守位失效</div>
            <div class="strategy-item">▪ 下一防守參考：<b>{atr_stop_below:.1f}</b> 元（現價-1.5xATR），請嚴控持股風險</div>
            <div class="strategy-item">💡 若再跌破 {atr_stop_below:.1f} 元，建議執行停損出場，避免虧損擴大</div>
            <div class="strategy-item">💡 若止跌站回 {ma60:.1f} 元（季線）以上且RSI回升至40以上，可重新評估</div>"""

        extra_html = ""
        if action_tag == "hold":
            add_trigger    = max(price, high_60d) * 1.005
            vol_target_lots = (avg_vol_20d * 1.5) / 1000 if avg_vol_20d else 0
            extra_html = f"""
            <div class="strategy-item" style="margin-top:14px;">💡 <b>續抱操作建議：</b></div>
            <div class="strategy-item">加碼點：突破 <b>{add_trigger:.1f}</b> 元（前高附近），且當日成交量 &gt; <b>{vol_target_lots:,.0f}</b> 張（過去20日均量1.5倍），可視為轉強訊號加碼</div>
            <div class="strategy-item">減碼點：跌破 <b>{ma20:.1f}</b> 元（20MA），代表多頭結構轉弱，建議減碼或停利保護獲利</div>"""
        elif action_tag == "strong_buy":
            add_trigger    = max(price, high_60d) * 1.005
            vol_target_lots = (avg_vol_20d * 1.5) / 1000 if avg_vol_20d else 0
            extra_html = f"""
            <div class="strategy-item" style="margin-top:14px;">🟢 <b>強勢加碼操作建議：</b></div>
            <div class="strategy-item">均線多頭排列 + 爆量({v_ratio:.1f}倍)同步出現，趨勢動能強勁，可採分批進場策略</div>
            <div class="strategy-item">加碼點：突破 <b>{add_trigger:.1f}</b> 元（前高附近）且持續爆量，可視為趨勢延續，分批加碼</div>
            <div class="strategy-item">若單日爆量卻收黑K，需留意主力出貨疑慮，宜減碼觀察</div>
            <div class="strategy-item">減碼點：跌破 <b>{ma20:.1f}</b> 元（20MA），代表多頭結構轉弱，建議減碼或停利保護獲利</div>"""

        strategy_blocks_html = f"""
        <div class="strategy-group" style="border-left-color: #007AFF;">
            <div class="strategy-title" style="color: #007AFF;">【短線交易戰術】</div>
            {short_html}
        </div>
        <div class="strategy-group" style="border-left-color: #24936E;">
            <div class="strategy-title" style="color: #24936E;">【波段交易戰術】</div>
            {swing_html}
            {extra_html}
        </div>"""

    # ── 外資台指期 ──
    foreign_net, foreign_date = fetch_foreign_futures_net_position()
    if foreign_net is not None and foreign_date:
        today_str   = _dt.date.today().strftime("%Y/%m/%d")
        date_note   = "" if foreign_date == today_str else "<span style=\"color:#888; font-size:13px;\">(此為最近一個交易日資料，當日資料於收盤後更新)</span>"
        foreign_color = "#d9383a" if foreign_net < 0 else "#24936E"
        foreign_tag = "歷史巨額空單避險" if foreign_net <= -50000 else ("淨空單，偏空避險" if foreign_net < 0 else "淨多單，偏多操作")
        foreign_html = f"""<div class="check-item">👤 外資台指期淨部位：<b>{foreign_net:,} 口</b> <span style=\"color:{foreign_color}; font-weight:bold;\">(⚠️ {foreign_tag})</span> {date_note}</div>"""
        chip_title  = f"🌐 大盤籌碼面 ({foreign_date})"
    else:
        foreign_html = """<div class="check-item">👤 外資台指期淨部位：<b style='color:#888;'>資料暫缺，請稍後再試</b></div>"""
        chip_title  = "🌐 大盤籌碼面（即時抓取中）"

    # ── 風險提醒 ──
    base_tip_map = {
        "strong_buy": "🟢 多項指標同步轉強，技術結構健康，可依策略分批佈局。",
        "hold":       "🔵 趨勢仍在多頭軌道內，建議續抱觀察，留意加碼/減碼觸發價位。",
        "overheat":   "🟡 短線漲多乖離已大，建議暫緩追高，等待回測均線後再評估進場。",
        "watch":      "🟠 股價已跌破生命線但季線仍在守，屬整理區間，建議耐心觀察季線防守是否成立。",
        "reduce":     f"🔴 警告：<b>{d['name']}</b> 已實質跌破 20 日生命線與季線防守，技術面轉弱，任何反彈都屬弱勢整理，請嚴格執行停損紀律！",
    }
    risk_lines = [base_tip_map.get(action_tag, "")]
    if bias_pct > 10.0:
        risk_lines.append(f"⚠️ 正乖離過高 ({bias_pct:.1f}%)：隨時有向 20MA ({ma20:.1f}) 修正風險，切勿追高！")
    elif bias_pct < -10.0:
        risk_lines.append(f"⚠️ 負乖離過大 ({bias_pct:.1f}%)：股價短線超跌，留意是否有反彈機會，但趨勢未明前勿貿然接刀。")
    if rsi > 70:
        risk_lines.append(f"⚠️ RSI 超買區 ({rsi:.1f})：買盤過熱，嚴防主力高檔反手出貨。")
    elif rsi < 30:
        risk_lines.append(f"⚠️ RSI 超賣區 ({rsi:.1f})：短線跌深，但趨勢未明，勿貿然接刀。")
    if action_tag in ("strong_buy", "overheat") and v_ratio > 1.5:
        risk_lines.append("🔥 短線波動劇烈，單筆資金請嚴格控制在 6-8%。")
    if action_tag == "reduce" and price < ma60:
        risk_lines.append(f"⚠️ 季線 ({ma60:.1f} 元) 已跌破，原防守位失效，請嚴格控管持股部位風險。")
    risk_tip_text = "<br>".join(risk_lines)

    html_content = f"""
    <html><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f5f7fa; padding: 25px; margin: 0; color: #111; font-size: 19px; }}
        .app-container {{ background: white; max-width: 580px; margin: auto; border-radius: 28px; padding: 30px; box-shadow: 0 10px 40px rgba(0,0,0,0.08); }}
        .header {{ text-align: center; margin-bottom: 30px; }}
        .stock-title {{ font-size: 36px; font-weight: 800; color: #000; display: flex; align-items: center; justify-content: center; gap: 8px; }}
        .live-price {{ font-size: 52px; font-weight: 900; margin: 15px 0; color: #d9383a; }}
        .decision-badge {{ display: inline-block; background: {decision_color}15; color: {decision_color}; padding: 10px 28px; border-radius: 50px; font-weight: 800; font-size: 21px; border: 1.5px solid {decision_color}; }}
        .metrics-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 30px 0; }}
        .metric-box {{ background: #f8f9fb; border-radius: 14px; padding: 15px 4px; text-align: center; font-size: 15px; border: 1.5px solid #edf0f4; }}
        .metric-box.per {{ border: 1.5px solid #f3d99d; background: #fffdf6; }}
        .metric-label {{ color: #5a6578; font-size: 14px; margin-bottom: 8px; font-weight: 700; }}
        .metric-value {{ font-weight: 900; color: #24936E; font-size: 20px; }}
        .metric-box.per .metric-value {{ color: #b08117; }}
        .hightlight-banner {{ background: #eefbf5; border: 1.5px dashed #24936E; border-radius: 14px; padding: 18px; font-size: 17.5px; font-weight: 700; text-align: center; margin-bottom: 30px; color: #1e5236; line-height: 1.6; }}
        .section-title {{ font-size: 23px; font-weight: 800; color: #1a202c; margin: 35px 0 18px 0; display: flex; align-items: center; gap: 8px; }}
        .section-title::before {{ content: ''; display: inline-block; width: 6px; height: 24px; background: #1a202c; border-radius: 3px; }}
        .check-item {{ font-size: 18.5px; margin: 15px 0; display: flex; align-items: flex-start; flex-wrap: wrap; gap: 6px; color: #1a202c; line-height: 1.6; }}
        .strategy-group {{ border-left: 5px solid #007AFF; padding-left: 16px; margin-bottom: 30px; }}
        .strategy-title {{ font-size: 20px; font-weight: 800; color: #007AFF; margin-bottom: 12px; }}
        .strategy-item {{ font-size: 18.5px; margin: 10px 0; color: #2d3748; }}
        .perfect-tip {{ background: #fff5f5; border-radius: 16px; padding: 20px; font-size: 17.5px; line-height: 1.8; color: #9b2c2c; margin-top: 35px; border: 1.5px solid #fed7d7; }}
        .perfect-tip.bull {{ background: #eefbf5; color: #1e5236; border: 1.5px solid #c6ebd4; }}
    </style></head><body>
    <div class="app-container">
        <div class="header">
            <div class="stock-title">📊 {d['name']} ({stock})</div>
            <div style="font-size:17px; color:#888; font-weight:600; margin-top:4px; margin-bottom:6px;">🕐 報告產生時間：{_dt.datetime.now().strftime("%Y/%m/%d %H:%M")}</div>
            <div class="live-price">{d['price']:.1f} 元</div>
            {day_chg_html}
            <div class="decision-badge">決策建議：{decision_text}</div>
        </div>
        <div class="metrics-grid">
            <div class="metric-box"><div class="metric-label">💰 公告配息</div><div class="metric-value">{div_display} 元</div></div>
            <div class="metric-box"><div class="metric-label">🍇 公告配股</div><div class="metric-value">{stk_display}</div></div>
            <div class="metric-box"><div class="metric-label">📈 單次殖利率</div><div class="metric-value">{yield_rate:.2f} %</div></div>
            <div class="metric-box per"><div class="metric-label">⏳ 本益比</div><div class="metric-value">{pe_display}</div></div>
        </div>
        <div class="hightlight-banner">
            {f"ℹ️ {stock} 為 ETF，不適用一般上市公司股利分派情形資料，配息請參考發行商官方公告與月配/季配時程表。" if is_etf_code(stock) else f"🍼 股利分紅動態：每股配發現金 {div_display} 元 / 配股 {stk_display}，本次殖利率達 {yield_rate:.2f}%！" + (f"<br><span style='font-size:14px; font-weight:normal; color:#5a6578;'>（資料期間：{d.get('latest_div_period','')}　．　殖利率為本次單一公告除以現價，非年化數字）</span>" if d.get('latest_div_period') else "") + (f"<div style='margin-top:10px;'>{status_badge_html}</div>" if status_badge_html and not is_etf_code(stock) else "") + (confirmed_note_html if not is_etf_code(stock) else "")}
        </div>
        <div class="section-title">{chip_title}</div>
        {foreign_html}
        <div class="section-title">🤠 高手決策核心檢核</div>
        <div class="check-item">{'✅' if is_bull else '❌'} 股價在均線之上：<b>{'已達成' if is_bull else '未達成'}</b></div>
        <div class="check-item">🧬 20MA生命線檢視：{ma20_html}</div>
        <div class="check-item">📊 乖離率動態檢視：{bias_html}</div>
        <div class="check-item">🔮 RSI技術指標：{rsi_html}</div>
        <div class="section-title">📋 量化標準檢核</div>
        {checklist_html}
        <div class="section-title">📐 葛蘭碧八大法則分析</div>
        {gran_html}
        <div class="section-title">⚡ 實戰操盤交易策略</div>
        {strategy_blocks_html}
        <div class="perfect-tip {'bull' if action_tag in ('strong_buy', 'hold') else ''}">
            ⚠️ <b>【實戰風控建議】</b><br>
            {risk_tip_text}
        </div>
    </div>
    </body></html>
    """

    return html_content
