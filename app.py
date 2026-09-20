# -*- coding: utf-8 -*-
"""
app.py — 台股分析工具（網頁版）
用途：把 stock_phone_PC_gemini.py 的分析邏輯包成 Flask 網頁服務，
      部署在 Render 上，讓不用裝 Python/Pyto 的人也能直接用瀏覽器查詢。

所有數據抓取與技術分析邏輯（本益比、股利、葛蘭碧八大法則、ATR停損等）
完全沿用原始腳本，未做任何邏輯修改，只把輸出方式從「本機開瀏覽器」
改成「回傳 HTML 給 Flask 路由」。
"""
from flask import Flask, request, Response

from core_logic import generate_report_html, _build_not_found_html

app = Flask(__name__)

# 首頁：一個輸入框 + 查詢按鈕，輸入後用 GET 帶 ?code=xxxx 導向 /report
INDEX_HTML = """
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>台股分析</title>
<style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
           background: #f5f7fa; margin: 0; padding: 60px 20px; }
    .box { max-width: 480px; margin: 0 auto; display: flex; gap: 10px; }
    input[type=text] { flex: 1; font-size: 18px; padding: 14px 18px; border-radius: 14px;
           border: 1.5px solid #d7dce3; outline: none; }
    input[type=text]:focus { border-color: #007AFF; }
    button { font-size: 18px; font-weight: 700; padding: 14px 26px; border-radius: 14px;
           border: none; background: #007AFF; color: white; cursor: pointer; }
    button:hover { background: #005fcc; }
</style>
</head><body>
<form class="box" action="/report" method="get">
    <input type="text" name="code" placeholder="輸入股票代碼，例如 2330" autofocus>
    <button type="submit">查詢</button>
</form>
</body></html>
"""


@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/report")
def report():
    stock = (request.args.get("code") or "").strip()
    if not stock:
        return Response(INDEX_HTML, mimetype="text/html")

    try:
        html = generate_report_html(stock)
    except Exception as e:
        # 保底：任何未預期的錯誤都顯示查無此股票頁面，而不是 500 錯誤頁，
        # 避免朋友看到一片空白或伺服器錯誤訊息不知所措。
        print(f"⚠️ 產生報告時發生例外：{e}")
        html = _build_not_found_html(stock)

    if not html:
        html = _build_not_found_html(stock)

    return Response(html, mimetype="text/html")


# 健康檢查用路由，Render 有時會 ping 這個路徑
@app.route("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    # 本機測試用；Render 上實際是用 gunicorn 啟動（見 Procfile / start command）
    app.run(host="0.0.0.0", port=5000, debug=True)
