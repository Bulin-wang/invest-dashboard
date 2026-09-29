# 投资组合价值看板

展示 **60 余位投资者**的组合价值：每人本金 **100 万人民币**、统一起始日（`2026-09-24`），
每人有一条**持仓路径**（可全仓切换标的），按路径的**分段收益率连乘**折算成 100 万的累计市值。
用一个交互式 Streamlit 看板呈现，并由 GitHub Actions 每日自动更新。

## 口径

- **持仓路径**：每位投资者从初始标的起步，可**全仓切换**到别的标的（见 `switches.yaml`）。
- **组合市值**：把路径上各段标的收益**连乘**（复利）：
  `市值(t) = 本金 × ∏(已结束段收益) × 当前段 close(t)/close(段起点)`（见 `src/portfolio.py`）。
- **切换口径**：调仓日按**收盘价**全仓切换；**当日算旧标的，次交易日起算新标的**；不计费用。
- **未复权**：用**未复权收盘价**，**不含分红 / 除权**，也**不考虑汇率**。
  - ⚠️ 因此跨**除权除息**（A股）或**拆股**（美股）会有跳空，适合近期窗口；长历史数值会失真。
- **只展示起始日及之后**的数据：所有人从 100 万起步。
- 债券 / 部分标的以**代表性 ETF** 表征（如 `511010`、`TLT`、`SOXX`）。

## 数据源（免 key 行情端点）

不依赖 Tushare 等数据库 API，直接抓行情站的轻量文本端点，**无需 key**：

- **当日报价**：新浪 `hq.sinajs.cn`（主）+ 腾讯 `qt.gtimg.cn`（备）。一次请求可批量多个代码，
  覆盖 A股个股/ETF/债券/指数 + 美股（`gb_`，指数 `gb_$inx` 等）+ 港股（`hk`）。
- **历史回填**：腾讯 `fqkline`（A股 + 港股，未复权 `day`）+ 新浪 `US_MinKService.getDailyK`（美股，未复权）。

历史与当日**同口径**（都未复权），保证序列连续。

## 群体平均指数

把**所有投资者的组合收益按人平均**合成一条指数（`prototype/index_build.py`；`daily_close` 顺带重建）：

- **口径**：读 `data/portfolios/{Investor}.csv` 的 nav，**按人等权**平均；
- **基准日**取 `investors.yaml` 顶层的 `start_date`，该日 = **100**（对应 100 万）；
- **只输出基准日及之后**的点；
- 输出 `data/index/equal_weight.csv`（date, index），**看板自动叠加**为 ★ 群体平均。

## 看板页面

- **总览**（`app/streamlit_app.py`）：所有投资者的组合收益曲线 + 明细表（当前持仓 / 调仓次数）。
- **投资者明细**（`app/pages/1_投资者明细.py`）：选一位投资者，看其自起始日**每个交易日**
  持有什么标的、**当日收益**、**截至当日的累计收益**（时间序列），并列出调仓记录。

## 目录

```
investors.yaml               # 公开清单：60 余位投资者的昵称 + **初始**标的（不含真实姓名）
switches.yaml                # 调仓流水（append-only）：每条 = 某投资者某日全仓切换到某标的
private/                     # 本地私密：真实姓名 ↔ 昵称 映射（已 gitignore，绝不上传）
tools/make_investors.py      # 本地匿名化：真实名单 -> investors.yaml
prototype/
  quote_fetch.py             # 报价端点抓取（新浪/腾讯；A股/美股/港股）
  daily_close.py             # 每日：取当日收盘价 → append → 算组合收益 / 群体平均
  backfill.py                # 首次：回填历史日线（未复权）
  index_build.py             # 群体平均指数（对投资者组合收益按人平均，基准日 = 100 = 100 万）
app/streamlit_app.py         # 看板「总览」页
app/common.py                # 看板共用：数据加载 / 常量
app/pages/1_投资者明细.py    # 看板「投资者明细」页（某投资者逐日持仓 / 收益）
data/{prices,returns}/*.csv, data/portfolios/*.csv, data/index/equal_weight.csv, meta.json  # 生成物
tests/                       # 计算层单测（离线）
src/                         # 配置/计算（portfolio.py 组合收益）+【备用后端】
```

## 隐私（重要）

- `investors.yaml` 是**公开**清单，**只有昵称**（Alice / Bob / …）和标的，**不含真实姓名**。
- 真实姓名只在本地 `private/investors.private.yaml`，该目录被 `.gitignore` 忽略，**不会上传**。
- 映射规则与昵称分配都**在本地完成**：`python -m tools.make_investors` 会读私有名单、
  分配昵称、写出公开的 `investors.yaml`，并把反向映射写到 `private/nickname_map.csv`（私密）。

别人 clone 仓库只能看到「Alice 持有 600519」，看不到真实身份。

## 本地使用

```powershell
cd invest-dashboard
py -m venv .venv
.\.venv\Scripts\Activate.ps1        # 或: py -m pip install -r requirements.txt 直接装到全局
pip install -r requirements.txt

# 0) 改名单后（可选）重新生成公开清单：编辑 private/investors.private.yaml，然后
python -m tools.make_investors

# 1) 首次：回填历史（未复权）→ 生成 data/prices/
python -m prototype.backfill --days 400

# 2) 每天：取当日收盘价 → 增量 append → 算收益率/市值
python -m prototype.daily_close

# 3) 看板
streamlit run app/streamlit_app.py

# 4) 单测（不联网）
python -m pytest tests/ -q
```

编辑 `investors.yaml` 增删投资者（`market: cn|us|hk`，`type: stock|etf|bond|index`），
顶层 `start_date` / `principal` 为统一默认值，可逐条覆盖。**新增标的后直接跑
`python -m prototype.daily_close` 即可**——脚本会自动给历史过短/缺失的标的**补种日线**
（等效于对它跑一次 `backfill`），所以不必手动回填新标的，也不会因为某标只有 1 天数据把
群体平均指数带偏。

> 同一标的多位持有者会**共享同一份** `data/prices/{key}.csv` 与 `data/returns/{key}.csv`，
> 抓取按标的去重；人只是显示标签。文件名的 `key` = `{market}_{type}_{ticker}`
> （如 `cn_stock_600519`、`cn_index_000300`、`us_index_GSPC`）——**含 `type`** 以区分
> 同市场同代码的不同品种（例：`cn_stock_000001` 平安银行 vs `cn_index_000001` 上证指数）。

### 调仓（switches.yaml）

投资者的**初始持仓**写在 `investors.yaml`（`symbol/market/type`）；**调仓**往 `switches.yaml`
**追加一行**即可（append-only，不改历史）：

```yaml
switches:
  - {date: 2026-10-15, nickname: "Bob", symbol: "AAPL", market: us, type: stock}
```

含义：Bob 在 `2026-10-15` 收盘把整仓**全仓切换**到 AAPL。追加后跑 `python -m prototype.daily_close`
即可重算组合收益。每位投资者各自独立，日期 / 标的互不影响。

## 云端部署

1. 把本目录推到 GitHub（public/private 均可）。**注意 `private/` 已被忽略，不会上传。**
2. **定时更新**：`.github/workflows/update.yml` 已配好，Actions 会按 cron 自动抓数、
   算收益/市值/指数，并把 `data/` **commit 回仓库**。**无需任何 Secret**（端点无 key）。
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

`data/` 由云端 bot 维护。**本地只改 `investors.yaml` 和代码，不要提交本地重算的 `data/`**，
否则会和 bot 的提交分叉、产生 merge：

```powershell
git pull                          # 先拉最新（含 bot 的提交）
# 改 investors.yaml / 代码
git add investors.yaml switches.yaml   # 只 add 你改的文件，别 git add data/（也别 add private/）
git commit -m "add XXX" ; git push
# 云端下次运行会自动给新标的补历史、重算收益与指数
```

若本地跑过 `daily_close` 只是为了预览，提交前用 `git checkout -- data/` 丢弃本地数据改动即可。

## 备用后端（`src/`，可选）

`src/` 下另有一套基于 **Tushare MCP（A股个股/指数）+ akshare（A股 ETF）+ yfinance（美股）**
的**全量重取** pipeline，口径为**后复权 / 含分红再投**，由 `python -m src.pipeline` 运行，
读取 `holdings.yaml`（旧的个人持仓清单），写入同一套 `data/`。若要用它：

- Tushare token 从 `TUSHARE_MCP_URL` / `TUSHARE_TOKEN` / 根目录 `.tushare_token` 读取（已 gitignore）；
- 免费 token 对 `adj_factor` / `index_daily` / `us_daily` 限速 **1 次/分钟 + 1 次/小时**，
  且**无基金接口权限**（A股 ETF 取不到）。

它与 `prototype/` 二选一即可；两者写同一个 `data/`，**不要同时跑**（口径也不同）。

## 后续扩展（已预留）

- **汇率折算 / 权重组合**：`src/aggregate.py` 占位（多标的权重、CNY 折算，跨市场统一口径）。
- **更多基准 / 自定义指数**：加到 `investors.yaml` 的 `benchmarks:` 即可。
