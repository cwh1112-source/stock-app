# 台股分析工具（網頁版）

把 `stock_phone_PC_gemini.py` 的分析邏輯（本益比、股利、葛蘭碧八大法則、
ATR停損、實戰策略建議等）原封不動保留，包成 Flask 網頁服務，部署到 Render。

## 檔案說明
- `core_logic.py` — 你原本的所有資料抓取與技術分析邏輯（未修改任何演算法），
  只把輸出從「產生 HTML 檔案並開瀏覽器」改成「回傳 HTML 字串」。
- `app.py` — Flask 網頁伺服器：首頁是輸入框，`/report?code=2330` 顯示分析結果。
- `requirements.txt` / `Procfile` — Render 部署設定。

## 部署到 Render（跟你現有的 stock-app 一樣的方式）
1. 到 Render Dashboard → 進入你現有的 stock-app 服務（srv-d930la8k1i2s73dbsnig）
2. 用「Manual Deploy」→「Upload files」把這個資料夾的所有檔案上傳（跟你之前部署的方式一樣）
   - 或者：如果你想開一個新的服務，選 "New Web Service" → 一樣選 file upload 或連 GitHub
3. Render 會自動偵測 `requirements.txt` 安裝套件，並依 `Procfile` 用 `gunicorn app:app` 啟動
4. 部署完成後，網址列輸入代碼即可查詢，例如 `你的網址.onrender.com/report?code=2330`

## 本機測試（可選）
```bash
pip install -r requirements.txt
python app.py
# 開瀏覽器 http://127.0.0.1:5000
```

## 沒有改動的部分
所有股價/股利/本益比/外資期貨/葛蘭碧訊號的判斷邏輯、文字、UI 樣式，
都跟你原本的 `stock_phone_PC_gemini.py` 完全一致，只是把「執行方式」
從命令列輸入 + 開本機瀏覽器，改成「網頁路由接收 URL 參數」。
