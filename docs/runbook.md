# YTR_suno_auto — 完整使用指南

從零安裝到生產一張 album 的完整流程。

---

## §1 系統需求

- Windows 10/11
- Python 3.10+(建議 3.12 或 3.14)
- 你日常用的 Google Chrome
- Suno **Pro 訂閱**(WAV 下載需要)
- Gemini API key + Google Cloud Translation API key

---

## §2 一次性安裝(只做一次)

### 2.1 安裝 Python 套件

```powershell
cd D:\github\taj0207\YTR_suno_auto
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

驗證:
```powershell
python -c "import requests, bs4, jinja2, yaml, docx, google.generativeai, websockets; print('ok')"
```

看到 `ok` 就過。

### 2.2 拿 API key 填 .env

```powershell
copy .env.example .env
notepad .env
```

填:
```
GEMINI_API_KEY=AIza...                # 從 https://aistudio.google.com/app/apikey
GOOGLE_TRANSLATE_API_KEY=AIza...      # 從 GCP Console 啟用 Cloud Translation API → 建 API key
```

### 2.3 載 Chrome extension

1. Chrome → `chrome://extensions/`
2. 右上角開 **Developer mode**
3. **Load unpacked** → 選 `D:\github\taj0207\YTR_suno_auto\chrome-extension` 資料夾
4. 把 extension 釘到工具列(按 puzzle icon 找它)

### 2.4 解 KKBox WAF + 設定 Suno 環境

1. Chrome 開 `https://www.kkbox.com/tw/tc/` →(如果跳 CAPTCHA 解一下)→ 設好 WAF cookie
2. Chrome 開 `https://suno.com/` → 登入(任何方式)→ 進 `/create`
3. **在 suno.com/create 點一次 "Create" 按鈕**(任意 prompt,例如 "test")
   - Extension 攔到 POST → 自動存 generate template
   - 不需要等它真的 render 完,點下去就行

---

## §3 編你這次要做的 artist

```powershell
copy artist_list.example.yaml artist_list.yaml
notepad artist_list.yaml
```

範例(**第一次先放一個 artist + limit: 2** 試水):
```yaml
artists:
  - slug: eason_chan
    display_name: "陳奕迅"
    limit: 5
```

**選項**:多 artist:
```yaml
artists:
  - slug: eason_chan
    display_name: "陳奕迅"
    limit: 10
  - slug: jay_chou
    display_name: "周杰倫"
    limit: 10
    kkbox_url: "https://www.kkbox.com/tw/tc/artist/<id>"   # 選填,搜不對人就硬指定
```

---

## §4 (選擇性)新主題就建 workspace

如果這批是新風格 / 新概念 album:

```powershell
xcopy /E /I workspaces\billie_eilish_depressed workspaces\<新主題slug>
notepad workspaces\<新主題slug>\config.yaml
notepad workspaces\<新主題slug>\prompt_3_2.j2
```

`config.yaml` 改:
- `name`、`display_name`
- `suno.wid`:Suno 開新 workspace → 從 URL 抓 `?wid=` 後面那串 UUID
- `youtube.album_name_hint`、`hashtags` 等

`prompt_3_2.j2` 改裡面對 Gemini 的風格要求(整個 1-6 點)。

---

## §5 跑 pipeline(每次)

確認:
- ✅ Chrome 開著
- ✅ 有 `suno.com/create` 一個 tab(extension 用來 proxy fetch)
- ✅ 有 `kkbox.com` 一個 tab(extension 用來 proxy fetch)
- ✅ extension 圖示 badge 是 ✓(綠色)或 ·(藍色)

```powershell
python pipeline\run_all.py --workspace <主題> --artists artist_list.yaml --mode vocal
```

### 預期 log

```
[Step 0 discover]
[srch] eason_chan: searching KKBox for '陳奕迅'
        -> https://www.kkbox.com/tw/tc/artist/...
[ok  ] eason_chan: got 10 song(s) from KKBox

[Step 1 fetch_lyrics]
[scrp] song_xxx: https://www.kkbox.com/tw/tc/song/...
[ok  ] song_xxx: wrote data\lyrics\raw\song_xxx.txt

[Step 2 translate]
[trn ] song_xxx: translating...
[ok  ] song_xxx: wrote data\lyrics\en\song_xxx.txt

[Step 3 gen_prompts]
[gen ] song_xxx: calling Gemini (3_2)...
[ok  ] song_xxx: wrote data\prompts\<date>\song_xxx_3_2.txt

[Step 4 suno_generate]
[ext] extension connected — Bearer=yes, template=yes
[gen ] song_xxx: vocal (lyrics=4106 styles=675)
[ok  ] song_xxx: song_ids=['abc-uuid', 'def-uuid']
[wait] for previous 2 variant(s) to finish — polling /feed/v3...
        2/2 done — ['streaming', 'streaming']
[gen ] song_yyy: ...

[Step 5 suno_download]
[wav ] abc-uuid: trigger convert_wav
[poll] abc-uuid: wav_file
[dl  ] abc-uuid: -> abc-uuid_v1.wav
[ok  ] abc-uuid: 40.2 MB
```

每首歌約 **1–2 分鐘**(Suno 不允許並行,要等前一首 streaming 才送下一首)。

### Pipeline 自動處理的事

- **422 token_validation_failed** → 自動 reload suno.com tab + 提示你點 Create → 拿新 template 自動 retry
- **429 too_many_running_jobs** → 等前一首 streaming 才送下一首,不會碰到
- **Bearer 過期** → extension 自動攔到新的
- **WAF 過期** → 你只需在 Chrome 開一次 kkbox.com 解 CAPTCHA

### 跑到一半 Ctrl+C 安全嗎?

**安全**。每個 step 都 idempotent:
- 已抓到的歌詞、翻譯、prompt 全在 disk,重跑 skip
- 已送 Suno 的歌記在 `data\.cache\suno_submissions.jsonl`,不會重燒 credit
- 已下載的 WAV 不會重抓

---

## §6 (人工)挑歌組 album

聽完 `data\jobs\<date_workspace>\downloads\*.wav`,把你要的歌組成 album:

```powershell
python pipeline\make_album.py --name <album_slug> --workspace <主題> `
    --add 2026-05-20_billie_eilish_depressed/<song_id_1>:1 `
    --add 2026-05-20_billie_eilish_depressed/<song_id_2>:2 `
    --add 2026-05-19_billie_eilish_depressed/<song_id_3>:1
```

格式 `--add <job>/<song_id>:<variant>`:
- `<job>`:`data\jobs\` 底下資料夾名
- `<song_id>`:UUID(從 generation_log.json 找)
- `<variant>`:1 或 2(Suno 每首出 2 個 variant)

可跨多個 job 組同一張 album(挑你最喜歡的)。

---

## §7 生 YouTube 文案

```powershell
python pipeline\07_gen_youtube_desc.py --album <album_slug>
```

輸出 `data\albums\<album_slug>\youtube_description.txt`。

```powershell
notepad data\albums\<album_slug>\youtube_description.txt
```

複製貼到 YouTube post / community / video description。

---

## §8 下一輪

直接重跑 §5。Step 0 會自動跳過已送 Suno 的歌,接續下一批 limit 首。

要叫 Step 0 重新去 KKBox 抓 artist 的歌(加新歌進 catalog):
```powershell
python pipeline\run_all.py --workspace X --artists artist_list.yaml --mode vocal --refresh-catalog
```

---

## §9 常見錯誤 + 解法

### `[fail] xxx: KKBox still WAF-blocking`

KKBox WAF cookie 過期。Chrome 開 https://www.kkbox.com/ 一次解 CAPTCHA。

### `[fail] xxx: no kkbox.com tab open`

Extension 找不到 kkbox.com tab。Chrome 開一個 kkbox 頁面。

### `[suno] 422 from /generate — likely session token expired.`

Pipeline 會自動 reload tab。你只需要在重整完的 suno.com 點一次 Create。

### `[wait] 0/2 done — ['streaming', 'streaming']` 卡很久

正常情況 1-2 分鐘內變 done。如果卡 5 分鐘以上,Suno 那邊可能真的有問題。可:
- Ctrl+C 重跑(已下載完成的 song_ids 在 generation_log,不會重 submit)
- 或調 `$env:SUNO_JOB_POLL_S = "5"` 縮短輪詢間隔

### `429 too_many_running_jobs`

理論上 §5 之後不會出現。如果還有,你的 Suno 帳號可能特別限制 concurrency=1。調:
```powershell
$env:SUNO_JOB_WAIT_MAX_S = "600"   # 等更久
python pipeline\run_all.py ...
```

### Gemini 429 quota

Free tier 每天有限額。隔天再跑,或升級付費 tier。

### `Generate failed: 404`

Suno endpoint 改了。F12 看真實 URL 改 `pipeline/_lib/suno.py:PATHS["generate"]`。

---

## §10 環境變數可調

| 變數 | 預設 | 作用 |
|---|---|---|
| `SUNO_TEMPLATE_WAIT` | 180 | 等用戶點 Create 重 prime template 的秒數 |
| `SUNO_JOB_WAIT_MAX_S` | 300 | 等一首歌生成完的最大秒數 |
| `SUNO_JOB_POLL_S` | 10 | 輪詢 /feed/v3 的間隔(秒) |
| `SUNO_SUBMIT_DELAY_S` | 5 | 沒有 in-flight 時送下一首之前的延遲 |
| `SUNO_AUTH_WAIT` | 180 | 等 Bearer 第一次到達的秒數 |
| `SUNO_API_BASE` | (auto) | 強制覆寫 Suno API base URL |

---

## §11 一張圖總覽

```
artist_list.yaml ──► Step 0 KKBox 找歌
                        ↓
                     Step 1 KKBox 抓歌詞(背景 tab navigate)
                        ↓
                     Step 2 Google Translate
                        ↓
                     Step 3 Gemini 生 production note
                        ↓
                     Step 4 Suno generate(透過 extension)
                        ↓ ← Pipeline 自動等 streaming
                     Step 5 WAV 下載
                        ↓
                ════════════════════════ 人工 ════════════════════════
                        ↓
                     make_album.py(挑歌)
                        ↓
                     07_gen_youtube_desc.py(Gemini 寫文案)
                        ↓
                     貼到 YouTube
```

---

## §12 出狀況 debug 順序

1. **看 pipeline console** 印什麼錯
2. **看 extension SW console**(chrome://extensions → YTR Suno Bridge → service worker 連結):
   - 確認 Bearer / template 有捕獲
   - 確認 SUNO POST 觀察到了
3. **看 `data\.debug\kkbox_song_html\`**:KKBox HTML dump
4. **看 `data\jobs\<job>\generation_log.json`**:每首歌的狀態
5. **看 `data\.cache\suno_submissions.jsonl`**:Suno 送過什麼

不能解決時把 console output 整段貼出來。
