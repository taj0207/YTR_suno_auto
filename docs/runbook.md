# YTR_suno_auto — 使用指南(已驗證部分)

從零安裝到「Suno 收到提交、開始生成歌曲」結束。
Step 5(WAV 下載)、album 組裝、YouTube 文案還沒實跑驗證,暫略。

---

## §1 系統需求

- Windows 10/11
- Python 3.10+
- 你日常用的 Google Chrome
- Suno 帳號(目前驗證到「提交生成」就夠,未驗證 WAV 下載所以暫不要求 Pro)
- Gemini API key + Google Cloud Translation API key

---

## §2 一次性安裝(只做一次)

### 2.1 Python 套件

```powershell
cd D:\github\taj0207\YTR_suno_auto
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2.2 填 .env

```powershell
copy .env.example .env
notepad .env
```

填:
```
GEMINI_API_KEY=AIza...                # https://aistudio.google.com/app/apikey
GOOGLE_TRANSLATE_API_KEY=AIza...      # GCP Console → 啟用 Cloud Translation API → 建 API key
```

### 2.3 載 Chrome extension

1. Chrome → `chrome://extensions/` → 右上開 **Developer mode**
2. **Load unpacked** → 選 `D:\github\taj0207\YTR_suno_auto\chrome-extension`
3. 釘到工具列

---

## §3 一次性 Chrome 環境準備

### 3.1 解 KKBox WAF

開 https://www.kkbox.com/tw/tc/(跳 CAPTCHA 就解)→ 設好 WAF cookie。

### 3.2 登入 Suno

開 https://suno.com/ → 登入(任何方式)→ 進 `/create`。

### 3.3 Prime Suno generate template

**在 suno.com/create 點一次 Create 按鈕**(隨便寫個 prompt)。
- Extension webRequest 攔到 POST → 自動存 template 進 storage.local
- 不需等到 render 完,送出就好
- Pipeline 之後就拿這份 template 替換 prompt/tags 再送

驗證有抓到:點 extension 圖示 → badge 應該變綠 ✓

---

## §4 編 artist_list.yaml

```powershell
copy artist_list.example.yaml artist_list.yaml
notepad artist_list.yaml
```

**第一次先小批試水**:
```yaml
artists:
  - slug: eason_chan
    display_name: "陳奕迅"
    limit: 2
```

可選欄位 `kkbox_url`:如果 KKBox 搜不對人(例如同名歌手),手動指定 artist 頁:
```yaml
  - slug: jay_chou
    display_name: "周杰倫"
    limit: 5
    kkbox_url: "https://www.kkbox.com/tw/tc/artist/<id>"
```

---

## §5 (選擇性)新主題建 workspace

新風格 album 才需要。否則用既有的 `billie_eilish_depressed`。

```powershell
xcopy /E /I workspaces\billie_eilish_depressed workspaces\<新主題slug>
notepad workspaces\<新主題slug>\config.yaml
notepad workspaces\<新主題slug>\prompt_3_2.j2
```

`config.yaml` 改:
- `name`、`display_name`
- `suno.wid`:Suno 開新 workspace,從 URL `?wid=` 抓 UUID

`prompt_3_2.j2` 改 Gemini 風格要求那段。

---

## §6 跑 pipeline

確認:
- ✅ Chrome 開著
- ✅ 有 `suno.com/create` 一個 tab
- ✅ 有 `kkbox.com` 一個 tab(任意 KKBox 頁,Step 1 需要)
- ✅ extension badge 是 ✓(綠色,表示 Bearer + template 都有)

跑:
```powershell
python pipeline\run_all.py --workspace billie_eilish_depressed --artists artist_list.yaml --mode vocal
```

### 預期 log

**Step 0(KKBox 找歌)**:
```
[srch] eason_chan: searching KKBox for '陳奕迅'
        -> https://www.kkbox.com/tw/tc/artist/...
[ok  ] eason_chan: got 10 song(s) from KKBox
```

**Step 1(抓歌詞,extension 開背景 tab)**:
```
[scrp] song_xxx: https://www.kkbox.com/tw/tc/song/...
[ok  ] song_xxx: wrote data\lyrics\raw\song_xxx.txt
```

**Step 2(翻譯)**:
```
[trn ] song_xxx: translating 800 chars...
[ok  ] song_xxx: wrote data\lyrics\en\song_xxx.txt
```

**Step 3(Gemini 生 production note)**:
```
[gen ] song_xxx: calling Gemini (3_2)...
[ok  ] song_xxx: wrote data\prompts\<date>\song_xxx_3_2.txt (XXXX chars)
```

**Step 4(送 Suno)**:
```
[ext] extension connected — Bearer=yes, template=yes
[gen ] song_xxx: vocal (lyrics=4106 styles=675)
[ok  ] song_xxx: song_ids=['abc-uuid', 'def-uuid']
[wait] for previous 2 variant(s) to finish — polling /feed/v3...
        0/2 done — ['submitted', 'submitted']
        1/2 done — ['streaming', 'submitted']
        2/2 done — ['streaming', 'streaming']
[gen ] song_yyy: ...
```

每首歌約 1-2 分鐘(Suno 不允許並行)。

### Pipeline 自動處理

- **422 token_validation_failed** → 自動 reload suno.com tab + 提示「請點 Create」→ 拿新 template 自動 retry
- **429 too_many_running_jobs** → 等前一首跑完才送下一首
- **Bearer 過期** → extension 自動捕獲新的

### Ctrl+C 安全嗎?

**安全**。每個 step idempotent:
- 抓過的歌詞、翻譯、prompt 全在 disk,重跑 skip
- 已送 Suno 的歌記在 `data\.cache\suno_submissions.jsonl`,不會重燒 credit

---

## §7 跑完 Step 4 看結果

```powershell
# 看送過哪些 prompt(每行一個提交)
type data\.cache\suno_submissions.jsonl

# 看本次 job 的 generation_log
type data\jobs\<date>_<workspace>\generation_log.json
```

`generation_log.json` 每首歌有 `song_id` 跟 `status`。Suno UI 上(suno.com/me 或 workspace 頁)也能看到歌已在播放清單裡。

---

## §8 已知狀況 & 解法

### `[fail] xxx: KKBox still WAF-blocking (status=202)`
WAF cookie 過期。Chrome 開 kkbox.com 一次。

### `no kkbox.com tab open`
Chrome 開一個 kkbox 頁面,任何 url 都可以。

### `[suno] 422 from /generate — likely session token expired.`
Pipeline 會自動 reload tab。重整後到 suno.com 點一次 Create,pipeline 自動繼續。

### `0/2 done — ['streaming', 'streaming']` 卡住
Status `streaming` 算 done(audio 可播了)。如果一直在 streaming 沒進 done,可能 Suno 真的需要等到 complete。把實際 log 貼給作者修。

### `429 too_many_running_jobs`
等待邏輯有問題。Ctrl+C 重跑會接續處理,但要先確認 `streaming` 算 done 的邏輯。

### Gemini quota 429
Free tier 每日有限。隔天再跑,或在 `_lib/gemini.py` 改 model 到更便宜的 `gemini-2.5-flash`。

---

## §9 環境變數可調

| 變數 | 預設 | 作用 |
|---|---|---|
| `SUNO_TEMPLATE_WAIT` | 180 | 等用戶點 Create 重 prime template 的秒數 |
| `SUNO_JOB_WAIT_MAX_S` | 300 | 等一首歌生成完的最大秒數 |
| `SUNO_JOB_POLL_S` | 10 | 輪詢 /feed/v3 的間隔(秒) |
| `SUNO_SUBMIT_DELAY_S` | 5 | 沒有 in-flight 時送下一首之前的延遲 |
| `SUNO_AUTH_WAIT` | 180 | 等 Bearer 第一次到達的秒數 |
| `SUNO_API_BASE` | (auto) | 強制覆寫 Suno API base URL |

---

## §10 下一輪

直接重跑 §6。Step 0 自動跳過已送 Suno 的歌:
```powershell
python pipeline\run_all.py --workspace X --artists artist_list.yaml --mode vocal
```

要叫 Step 0 重新去 KKBox 抓 artist 的歌(加新歌進 catalog):
```powershell
python pipeline\run_all.py ... --refresh-catalog
```

---

## §11 流程圖(已驗證範圍)

```
artist_list.yaml
   ↓
Step 0  KKBox 找 artist 頁 + 抓 songs                   ← requests + BS4
   ↓
Step 1  Extension 開背景 tab navigate 抓歌詞 HTML        ← chrome.tabs.create
   ↓
Step 2  Google Translate API                            ← requests + key
   ↓
Step 3  Gemini 生 production note (lyrics + 風格要求)    ← google.generativeai
   ↓
Step 4  Extension proxy POST /api/generate/v2-web/      ← bridge.fetch via suno tab
   ↓ ← 等 streaming/complete 才送下一首
   完成後:song_ids 寫入 generation_log.json
═══════════════════════════════════════════════════════════
(以下未驗證,文件略)
   ↓
Step 5  WAV 下載
   ↓
人工挑歌組 album
   ↓
Gemini 生 YouTube 文案
```

---

## §12 出狀況 debug

1. **Pipeline console** 印的錯
2. **Extension service worker console**(chrome://extensions → Service worker 連結)
3. **`data\.debug\kkbox_song_html\*.html`** — KKBox HTML dump
4. **`data\jobs\<job>\generation_log.json`** — Suno 提交記錄
5. **`data\.cache\suno_submissions.jsonl`** — 全域 Suno dedup ledger

不能解決時把 console output 整段貼出來。
