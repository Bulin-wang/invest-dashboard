# 持仓价格看板

每日更新股票 / 债券 / ETF / 指数（A股 + 美股 + 港股）的收盘价，展示各标的**自投资起始日**
的**价格变化**，用一个交互式 Streamlit 看板呈现。

## 口径

- **价格变化**：`cum(t) = close(t) / close(start) - 1`，起点取 `holdings.yaml` 的 `start_date`
  （若无则取首个记录日）；起始日若非交易日自动顺延到下一交易日。
- **未复权**：用**未复权收盘价**，**不含分红 / 除权**（不做分红再投、也不做除权修正）。
  - ⚠️ 因此跨**除权除息**（A股）或**拆股**（美股）会有跳空，适合近期窗口；长历史数值会失真。
- 债券 / 部分标的以**代表性 ETF** 表征（如 `511010`、`TLT`、`SOXX`）。

## 数据源（免 key 行情端点）

不依赖 Tushare 等数据库 API，直接抓行情站的轻量文本端点，**无需 key**：

- **当日报价**：新浪 `hq.sinajs.cn`（主）+ 腾讯 `qt.gtimg.cn`（备）。一次请求可批量多个代码，
  覆盖 A股个股/ETF/债券/指数 + 美股（`gb_`，指数 `gb_$inx` 等）+ 港股（`hk`）。
- **历史回填**：腾讯 `fqkline`（A股，未复权 `day`）+ 新浪 `US_MinKService.getDailyK`（美股，未复权）。

历史与当日**同口径**（都未复权），保证序列连续。

## 自定义等权指数

把持仓**等权**合成一条指数（`prototype/index_build.py`；`daily_close` 会顺带重建，无需单独跑）：

- **口径**：价格变化（未复权），**买入持有**（基准日等权买入，之后权重随涨跌漂移），**忽略汇率**；
- **基准日**默认取持仓最早的 `start_date`，该日 = **100**；保留基准日之前的历史（回推）；
- 输出 `data/index/equal_weight.csv`（date, index），**看板自动叠加**为 ★ 等权指数。

## 目录

```
holdings.yaml                # 你唯一需要维护的清单（标的、类型、起始日）
prototype/
  quote_fetch.py             # 报价端点抓取（新浪/腾讯；A股/美股/港股）
  daily_close.py             # 每日：取当日收盘价 → append → 算价格变化（并重建指数）
  backfill.py                # 首次：回填历史日线（未复权）
  index_build.py             # 自定义等权指数（基准日 = 100）
app/streamlit_app.py         # 看板
data/{prices,returns}/*.csv, data/index/equal_weight.csv, meta.json   # 生成物
tests/                       # 计算层单测（离线）
src/                         # 【备用后端】Tushare / akshare / yfinance 版 pipeline
```

## 本地使用

```powershell
cd invest-dashboard
py -m venv .venv
.\.venv\Scripts\Activate.ps1        # 或: py -m pip install -r requirements.txt 直接装到全局
pip install -r requirements.txt

# 1) 首次：回填历史（未复权）→ 生成 data/prices/
python -m prototype.backfill --days 400

# 2) 每天：取当日收盘价 → 增量 append → 算价格变化
python -m prototype.daily_close

# 3) 看板
streamlit run app/streamlit_app.py

# 4) 单测（不联网）
python -m pytest tests/ -q
```

编辑 `holdings.yaml` 增删标的（`market: cn|us|hk`，`type: stock|etf|bond|index`，
`start_date: YYYY-MM-DD`）。**新增标的后直接跑 `python -m prototype.daily_close` 即可**——
脚本会自动给历史过短/缺失的标的**补种日线**（等效于对它跑一次 `backfill`），
所以不必手动回填新标的，也不会因为某标只有 1 天数据把自定义指数带偏。

## 云端部署

1. 把本目录推到 GitHub（public/private 均可）。
2. **定时更新**：`.github/workflows/update.yml` 已配好，Actions 会按 cron 自动抓数、
   算收益/指数，并把 `data/` **commit 回仓库**。**无需任何 Secret**（端点无 key）。
3. **看板托管**：到 [share.streamlit.io](https://share.streamlit.io) 连接该仓库，主文件填
   `app/streamlit_app.py`。Actions commit 新数据后自动 redeploy。

> Streamlit Cloud 只读仓库里的 `data/`；抓数由 Actions 负责，两者职责分离。

### 自动更新的节奏（cron 用的是 UTC！）

| 北京 星期      | 运行时间         | 抓取内容     |
| -------------- | ---------------- | ------------ |
| 周一 ~ 周五    | **15:35**  | A股当日收盘  |
| 周二 ~ 周六    | **09:10**  | 美股隔夜收盘 |
| **周日** | **不运行** | ——         |

- **只在交易日抓**：周日 / 节假日**不运行**（休市本来也没有新数据，属正常）。
- 想立刻更新一次：Actions 页 → `update-data` → **Run workflow**（手动触发）。

### 日常维护（关键：别和云端 bot 撞车）

`data/` 由云端 bot 维护。**本地只改 `holdings.yaml` 和代码，不要提交本地重算的 `data/`**，
否则会和 bot 的提交分叉、产生 merge：

```powershell
git pull                          # 先拉最新（含 bot 的提交）
# 改 holdings.yaml / 代码
git add holdings.yaml             # 只 add 你改的文件，别 git add data/
git commit -m "add XXX" ; git push
# 云端下次运行会自动给新标的补历史、重算收益与指数
```

若本地跑过 `daily_close` 只是为了预览，提交前用 `git checkout -- data/` 丢弃本地数据改动即可。

## 备用后端（`src/`，可选）

`src/` 下另有一套基于 **Tushare MCP（A股个股/指数）+ akshare（A股 ETF）+ yfinance（美股）**
的**全量重取** pipeline，口径为**后复权 / 含分红再投**，由 `python -m src.pipeline` 运行，
写入同一套 `data/`。若要用它：

- Tushare token 从 `TUSHARE_MCP_URL` / `TUSHARE_TOKEN` / 根目录 `.tushare_token` 读取（已 gitignore）；
- 免费 token 对 `adj_factor` / `index_daily` / `us_daily` 限速 **1 次/分钟 + 1 次/小时**，
  且**无基金接口权限**（A股 ETF 取不到）。

它与 `prototype/` 二选一即可；两者写同一个 `data/`，**不要同时跑**。

## 后续扩展（已预留）

- **组合聚合**：`holdings.yaml` 的 `weight`（含汇率折算）。
- **更多基准 / 自定义指数**：加到 `benchmarks:` 即可。
