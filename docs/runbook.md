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

### 3.1 解 KKBox WAF challenge(讓 extension 之後能抓歌詞)

**為什麼**:KKBox 歌曲頁有 AWS WAF 擋程式抓取。你親自瀏覽過一次後,WAF 會發 cookie 給你的 Chrome,之後 extension 用同一個 tab navigate 就過得了。

**步驟**:
1. Chrome 開 https://www.kkbox.com/tw/tc/
2. 如果看到「正在驗證您是否為機器人」/ hCaptcha 圖片挑戰之類 → **按指示完成**(通常點一下方塊或拼圖)
3. 看到正常 KKBox 首頁(有歌、有海報)就表示**過關**
4. **驗證**:複製貼上任一首歌的網址,例如:
   ```
   https://www.kkbox.com/tw/tc/song/4mjpw823f387robhP5
   ```
   應該看到歌曲頁面 + partial 歌詞顯示。**沒看到歌詞**就是 WAF 還沒清掉,回 step 2 再走一次。
5. **這個 tab 不要關**(讓它放著就好,pipeline 抓歌詞時 extension 會用)

**WAF cookie 通常有效期幾天**。如果之後 pipeline Step 1 失敗訊息含 `WAF-blocking`,就再做一次這節即可。

---

### 3.2 登入 Suno

**步驟**:
1. Chrome 開 https://suno.com/
2. 沒登入就按右上角 **Sign in** → 用 Email / Google / Discord(任何方式)登入
3. 登入後 URL 進到 `https://suno.com/create`(會看到歌曲創作介面)
4. **這個 tab 不要關**(pipeline 跑 Step 4/5 時 extension 會用這個 tab 提交給 Suno)

**驗證**:點工具列的 YTR Suno Bridge extension 圖示。它的 badge 應該顯示綠色 `✓`(表示 extension 已攔到 Bearer token)。

---

### 3.3 Prime Suno generate template(讓 extension 抓到 POST 模板)

**為什麼**:Suno generate POST body 有 20 多個欄位,含你帳號特有的 `user_tier` 跟 `create_session_token`。Pipeline 沒辦法自己合成這些,所以**你親手點一次 Create,extension 攔到那個 POST 存起來當「template」**,之後 pipeline 拿來改 prompt/tags 就能送。

**步驟**:
1. 確認在 https://suno.com/create
2. 切到 **Custom** 模式(右上角應該有 Simple / Custom 切換)
3. 在 **Song Description / Lyrics** 欄位隨便寫一行,例如 `test`
4. 在 **Style of Music** 欄位隨便寫一行,例如 `pop`
5. **按 Create 按鈕**送出
6. **不需要等它真的跑完**。送出去那一瞬間,extension 就攔到 POST 存了 template
7. 想取消那首歌的 Suno credit:在 Suno UI 找到剛跑的歌按刪除(可選)

**驗證**:
- 工具列的 extension badge 仍是 ✓(Bearer 還在)
- 之後 pipeline Step 4 印 `[ext] extension connected — Bearer=yes, template=yes` 就表示 template 也有了

**這個動作只需要做一次**。Template 存在 `chrome.storage.local`,reload extension 跟重開瀏覽器都不會丟。除非 Suno 哪天把 server-side session 過期(pipeline 收到 422),才需要再點一次。

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

## §5 Workspace 設定(每個風格主題一份 config.yaml)

**每個 workspace 是「一張概念專輯的設定包」**,包含:
- 風格 prompt 模板
- 聲音性別
- Suno 目標 workspace 的 `wid`
- Playlist 命名規則
- YouTube 文案設定

預設專案附了一個 `workspaces/billie_eilish_depressed/`。**用既有的就直接編它,要做不同主題才複製新的**。

### 5.1 編現有的(改聲音 / playlist 命名等)

```powershell
notepad workspaces\billie_eilish_depressed\config.yaml
```

可以改的欄位:

```yaml
display_name: "Depressed Billie Eilish (Male Vocal)"

vocal: "male"                 # ← 改 "female" / "androgynous" 改聲音
vocal_style: ""               # ← 選填,"raspy" / "breathy" / "soulful" 等

suno:
  wid: "84cca4a7-..."         # ← 改成你想送進去的 Suno workspace ID
                              #    (從 https://suno.com/create?wid=... 抓)

default_prompt_variant: "3_2" # 3_1 / 3_2 兩種 prompt 順序

# Step 4 自動建 playlist 的名稱 + 描述模板
# placeholders: {date} {batch} {workspace} {display_name} {mode} {job}
playlist_name_template:        "YTR {display_name} · {date}"
playlist_description_template: "YTR_suno_auto · workspace={workspace} · mode={mode} · batch={batch}"

youtube:                      # Step 6 用(暫未驗證)
  album_name_hint: "Depressed Billie Eilish style album"
  hashtags:
    - "#BillieEilishStyle"
    - ...
```

**改完不用 reload 什麼東西,下次 pipeline 跑就生效**(每首歌跑 Step 3 / 4 都重讀 config)。

### 5.2 風格要求(prompt 模板)

如果要動「給 Gemini 的風格要求」內容(例如把「depressed Billie Eilish」改成別的),編:

```powershell
notepad workspaces\billie_eilish_depressed\prompt_3_2.j2
```

模板裡的 `{{ vocal }}` 跟 `{{ vocal_style }}` 會自動帶入 `config.yaml` 的值 —— **改性別只改 config.yaml,不要在模板裡改**。

### 5.3 (選擇性)複製新主題

想做完全不同風格的 album(例如 garage rock),別覆蓋既有的:

```powershell
xcopy /E /I workspaces\billie_eilish_depressed workspaces\<新主題slug>
notepad workspaces\<新主題slug>\config.yaml      # 改 name / display_name / wid / playlist 命名 / youtube
notepad workspaces\<新主題slug>\prompt_3_2.j2    # 改 Gemini 對風格的要求段落
```

跑時切 workspace:
```powershell
python pipeline\run_all.py --workspace <新主題slug> --artists artist_list.yaml --mode vocal
```

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

**Step 4(送 Suno + 自動建 playlist + 加歌)**:
```
[ext] extension connected — Bearer=yes, template=yes
[plst] created Suno playlist 'YTR Depressed Billie Eilish (Male Vocal) · 2026-05-20' id=0ce1df93-...
[gen ] song_xxx: vocal (lyrics=4106 styles=675)
[ok  ] song_xxx: song_ids=['abc-uuid', 'def-uuid', 'ghi-uuid', 'jkl-uuid']
[plst] added 4 clip(s) to playlist 0ce1df93-...
[wait] for previous 4 variant(s) to finish — polling /feed/v3...
        0/4 done — ['submitted', 'submitted', 'queued', 'queued']
        4/4 done — ['streaming', 'streaming', 'streaming', 'streaming']
[gen ] song_yyy: ...
```

每首歌約 1-2 分鐘(Suno 不允許並行)。Suno Pro/Premier 帳號每次給 4 個 variant,Free 給 2 個。

### Pipeline 自動處理

- **建 Suno playlist + 加歌**:第一首之前自動建,每首成功後 add_to_playlist(per job 一個 playlist,sticky)
- **422 token_validation_failed** → 自動 reload suno.com tab + 提示「請點 Create」→ 拿新 template 自動 retry
- **429 too_many_running_jobs** → 等前一首 streaming 才送下一首
- **TypeError: Failed to fetch** → 自動 retry 一次(5 秒後)
- **Bearer 過期** → extension 自動捕獲新的
- **失敗的歌不在 suno_submissions** → 下次重跑只重 submit 那首,成功的全 [cached] skip 不浪費 credit

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
| `SUNO_NO_PLAYLIST` | 0 | 設 `1` 則 Step 4 不自動建 / 不加歌到 playlist |

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
   ↓ (使用 {{ vocal }}、{{ vocal_style }} 等 config.yaml 變數)
Step 4  Extension proxy POST /api/generate/v2-web/      ← bridge.fetch via suno tab
   ├─ 自動建 Suno playlist (config 模板命名)
   ├─ 每首成功 song_ids 加進 playlist
   └─ 等 streaming/complete 才送下一首
   完成後:song_ids + playlist_id 寫入 generation_log.json
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
