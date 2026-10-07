# 投资组合价值看板

展示 **60 余位投资者**的组合价值：每人本金 **100 万人民币**、统一起始日（`2026-09-24`），
每人有一条**持仓路径**（可全仓切换标的），按路径的**分段收益率连乘**折算成 100 万的累计市值。
用一个交互式 Streamlit 看板呈现，并由 GitHub Actions 每日自动更新。

## 口径

- **持仓路径**：每位投资者从初始持仓起步，可切换到**别的标的**或**多标的权重组合**（见 `switches.yaml`）。
- **组合市值**：把路径上各段收益**连乘**（复利）；段内多标的按权重加权（**buy & hold**）：
  `市值(t) = 本金 × ∏(已结束段收益) × Σ wᵢ·Pᵢ(t)/Pᵢ(段起点)`（见 `src/aggregate.py`）。
- **现金腿**：`type: cash` 的一条腿（价格恒为 1，不涨不跌，**无需行情**），用于持现金 / 减仓。
- **切换口径**：调仓日按**收盘价**全仓切换；**当日算旧标的，次交易日起算新标的**；不计费用。
- **未复权**：用**未复权收盘价**，**不含分红 / 除权**，也**不考虑汇率**。
  - ⚠️ 因此跨**除权除息**（A股）或**拆股**（美股）会有跳空，适合近期窗口；长历史数值会失真。
  - **例外 1**：`type: fund`（场外开放式基金）用**累计净值**（含分红再投）——场外基金没有
    "未复权原始价"这个概念。详见下文「场外开放式基金」一节。
  - **例外 2**：`type: crypto`（加密货币）用 **UTC 日线收盘价**，且**周末也有数据**。
    详见下文「加密货币」一节。
- **`type` 取值**：`stock` / `etf` / `bond` / `index` / `fund` / `crypto` / `futures` / `cash`。
  它同时是**数据源路由**与**文件名 key**（`{market}_{type}_{symbol}`）的一部分，所以
  同一标的所有持有人必须写**同一个** `type`，否则会各抓一份、各算一套收益。
- **`expires`（可选）**：标的的**到期日**（期货 = 最后交易日）。规则：**当日仍有价格，
  次日起视为现金**——到期而未调仓则余额冻结不涨不跌。只对"到期日事先可知、到期后不再有价格"
  的标的（期货/期权等衍生品）有意义；股票/ETF/基金/加密**留空**即可。详见「期货合约」一节。
- **只展示起始日及之后**的数据：所有人从 100 万起步。
- 债券 / 部分标的以**代表性 ETF** 表征（如 `511010`、`TLT`、`SOXX`）。

## 多标的权重组合（阶段 1：buy & hold）

每位投资者的持仓从「单个标的」扩展为「多个标的 + 权重」（权重省略 = 等权）：

```yaml
investors:
  - {nickname: "investor01", symbol: "600519", market: cn, type: stock}   # 旧写法：单标的（等价 weight=1）
  - nickname: "investor02"                                                # 新写法：多标的权重
    holdings:
      - {symbol: "600519", market: cn, type: stock, weight: 0.6}
      - {symbol: AAPL,    market: us, type: stock, weight: 0.4}
```

调仓（`switches.yaml`）同理：一条 = 提交**新的目标权重向量**（`holdings: [...]`）；
旧写法（单个 `symbol`）= 全仓切换到该标的。

**现金腿**：把 `type` 写 `cash`（`symbol` 可省略）即持有现金 —— 现金价格恒为 1（不涨不跌）：

```yaml
  - nickname: "investor03"
    holdings:
      - {symbol: "600519", market: cn, type: stock, weight: 0.6}
      - {type: cash, weight: 0.4}                     # 40% 现金
```

现金腿**不需要行情**（不抓价、也不写 `data/prices/cash.csv`）；`holding` 列形如
`cash:0.4|cn_stock_600519:0.6`，看板显示「现金 40%」。权重照常内部归一化（写 `60/40` 与 `6/4` 等价）。

**口径（阶段 1）**：段内 **buy & hold** —— 按起始权重分配本金后**不再平衡**：

```
组合收益(t) = Σ wᵢ × Pᵢ(t) / Pᵢ(段起点)
```

调仓日收盘切换；**当日算旧组合、次交易日起算新组合**；不计费用 / 汇率。
单标的投资者走同一套代码，结果与旧实现逐行一致（回归用例：`tests/test_weighted_offline.py`）。

- 计算：`src/aggregate.py`（`weighted_path_nav`；`portfolio_nav(rebalance=True)` 为每日再平衡口径，暂未接入管线）
- 输出：`data/portfolios/{昵称}.csv` 的 `holding` 列 —— 单腿 = 裸 key；多腿 = `key:权重|key:权重`
- 录入：`tools/admin_ops.py` 的 `append_switch_multi` / `append_member_multi` 可写多标的（单标的函数行为不变）
- **换手率手续费（阶段 2）**：`src/analysis.py` 的手续费按 **换手率 = ½ Σ|Δ权重|** 计收 ——
  单标的全仓切换 = 1（与旧口径一致），多标的只调一部分就只按部分收；`switch_days` 的返回也带 `turnover` 列
- **多标的 What-if（阶段 3）**：调仓复盘页的「不调仓」= 继续持有**当时的旧持仓组合**（buy & hold），
  多标的按 Σ wᵢ × Pᵢ(t)/Pᵢ(调仓日) 推算；任一条腿缺价 → 该行显示「—（缺价）」
- **录入台**：`python -m streamlit run tools/admin_app.py` 的「新增投资者 / 追加调仓」均支持多标的
  （表格填多行 + 权重；只填 1 行 = 单标的，行为与以前一致）
- **待办**：可选的每日再平衡口径接入管线 / 汇率折算

## 数据源（免 key 行情端点）

不依赖 Tushare 等数据库 API，直接抓行情站的轻量文本端点，**无需 key**：

- **当日报价**：新浪 `hq.sinajs.cn`（主）+ 腾讯 `qt.gtimg.cn`（备）。一次请求可批量多个代码，
  覆盖 A股个股/ETF/债券/指数 + 美股（`gb_`，指数 `gb_$inx` 等）+ 港股（`hk`）。
- **历史回填**：腾讯 `fqkline`（A股 + 港股，未复权 `day`）+ 新浪 `US_MinKService.getDailyK`（美股，未复权）。
- **场外开放式基金**（`type: fund`）：东方财富 `fund.eastmoney.com/pingzhongdata/{code}.js`，
  一次请求拿**全量历史净值**（无需翻页；备用端点 `api.fund.eastmoney.com/f10/lsjz` 每页上限 20 条）。
  需带 `Referer: https://fund.eastmoney.com/`，否则会被拒。
- **加密货币**（`type: crypto`）：币安官方公开镜像 `data-api.binance.vision/api/v3/klines`（日线，
  单次上限 1000 根，向后翻页取全量）。
  - ⚠️ 实测**主域名 `api.binance.com` 在本机连接超时**（OKX / CoinGecko / Coinbase 同样超时），
    只有 `data-api.binance.vision` 可达。若该镜像也失效，改 `prototype/crypto_fetch.py` 的 `BASE_URL` 即可。
- **期货合约**（`type: futures`）：新浪期货 `stock2.finance.sina.com.cn/.../getDailyKLine`（全量日线，
  一次请求即可，无需翻页；目前仅支持上期能源原油 **SC**）。
  - 实时报价 `hq.sinajs.cn/list=nf_{合约}` 仅作诊断（见 `futures_fetch.quote()`）——它的"最新价"
    与日线的"结算价"**不是一个口径**，落盘统一用日线的 `c`（结算价）。
  - 实测**无需 Referer**、任意 User-Agent 均可返回，GitHub Actions 环境可达。
  - **已到期合约的历史仍可抓取**（如 SC2312 取回 435 条），所以期货历史可以事后重建。

历史与当日**同口径**（都未复权），保证序列连续。

## 场外开放式基金（`type: fund`）

**场外基金**（无交易所前缀）与**场内 ETF/LOF** 是两回事，写错了会各抓一份、各算一套收益：

```yaml
investors:
  - {nickname: "investor57", symbol: "002910", market: cn, type: fund}   # 场外开放式基金
  - {nickname: "investor31", symbol: "510300", market: cn, type: etf}    # 场内 ETF
```

- key 形如 `cn_fund_002910`；**同一只基金必须统一写 `fund`**（别有的写 `fund`、有的写 `etf`）。
- 新增基金后直接跑 `python -m prototype.daily_close` 即可，会自动补种全量历史净值。
  全量净值缓存在 `data/fund_cache/{key}.json`（含单位净值/累计净值/分红说明），
  补种优先读缓存，避免重复抓取。
- 单独抓取查看：`python -m prototype.fund_fetch --code 002910 --tail 5`

### ⚠️ 口径：累计净值（与其它品种不同）

场外基金**没有"未复权原始价"这个概念**——单位净值本身就是除权后的价格，每次分红/份额折算都会跳水。
所以 `type: fund` 落盘的是**累计净值**（等价于**分红再投**）：

```
组合收益(t) = Σ wᵢ × 累计净值ᵢ(t) / 累计净值ᵢ(段起点)
```

口径差异极大，选错会算错到离谱（实测）：

| 基金                      | 单位净值口径 | 累计净值口径 | 差异                |
| ------------------------- | ------------ | ------------ | ------------------- |
| 163402 兴全趋势投资       | −17.53%     | +1014.59%    | **+1032.12%** |
| 000001 华夏成长混合       | +22.20%      | +279.50%     | +257.30%            |
| 519066 汇添富蓝筹稳健     | +214.00%     | +346.90%     | +132.90%            |
| 002910 易方达供给改革混合 | +694.74%     | +694.74%     | 0（从未分红）       |

因此 `meta.json` 的 `items[].caliber` 会逐项标注：其它品种是 `price_return`（未复权、不含分红），
基金是 `cumulative_nav`（含分红再投）。**这两者不可直接横向比较**。

### ⚠️ 两个已知特性（按设计，不额外处理）

- **净值滞后一天**：场外基金净值 T 日盘后公布，当日 15:35 抓不到当日值。
  这也意味着 09:10 那班（抓美股隔夜）才是拿前一交易日净值的好时机。
- **净值在非交易日不更新**：基金按交易日发布，节假日没有新行，`_append` 按日期去重天然兼容。

## 加密货币（`type: crypto`）

现货交易对，走币安公开镜像。`symbol` 写 `BTCUSDT` 或简写 `BTC`（自动补 `USDT`）；
也接受 `BTC/USDT`、`btc-usdt`。`market` 填 `us` 即可（只用于分组，不影响取数）。

```yaml
investors:
  - {nickname: "investor58", symbol: "BTC",  market: us, type: crypto}
  - {nickname: "investor59", symbol: "ETHUSDT", market: us, type: crypto}
```

- key 形如 `us_crypto_BTCUSDT`；**同一币种必须统一写 `crypto`**。
- 新增后直接跑 `python -m prototype.daily_close` 会自动补种全量日线。
  全量日线缓存在 `data/crypto_cache/{key}.json`（含 OHLCV），补种优先读缓存。
- 单独抓取查看：`python -m prototype.crypto_fetch --symbol BTCUSDT --tail 5`

### ⚠️ 口径：UTC 日线收盘价

- 币安日线的 `openTime` 是 **UTC 零点**（**与基金不同**，基金是本地零点）。
  代码必须用 `datetime.fromtimestamp(ms/1000, timezone.utc)` 解析；用本地时间会把日期**推后一天**。
- 落盘 `close` = 该 UTC 日的收盘价，`caliber` 标为 `utc_daily_close`（相对其它品种的 `price_return`）。
  **锚定时刻是明确的**：日期标签 `D` 对应的价格 = **UTC `D` 日 23:59:59**
  （= 北京 `D+1` 日 07:59:59）。这是币安 `closeTime` 字段本身的值。
- **丢弃未走完的当日 K 线**：UTC 当天还没结束时，当天那根日线的 close 只是"当前价"，
  不能当收盘价落盘——`crypto_fetch` 会把它剔除，因此序列里全是完整日线。
- **USDT 计价**：本项目**不做汇率折算**（与美股同样的已知局限），
  所以 crypto 的美元收益是"美元口径"，不能直接和人民币本金对比。

### ⚠️ 一天跑两次（GitHub Actions 两班次）会不会覆盖 / 重复？

**不会。** 两点保证：

1. **写入是"只增不改"的**：`daily_close._append()` 按日期去重，同一交易日第二次写入
   直接跳过（`appended=False`），连"用错价格再写一次"都不会改写已存在的行。
   币安对**已收盘**的 K 线也不会改值。
2. **两班次看到的其实是同一根日线**。crypto 按 UTC 日切分，UTC 日零点 = **北京 08:00**：

   | 班次            | 北京时间   | = UTC | 当作 15:35/09:10 时                                                                       |
   | --------------- | ---------- | ----- | ----------------------------------------------------------------------------------------- |
   | A（抓 A股收盘） | 15:35      | 07:35 | 当天 UTC 日刚开始 7 小时 →**未走完** → 丢弃，最新可用日 = **前一个 UTC 日** |
   | B（抓美股隔夜） | 次日 09:10 | 01:10 | 前一个 UTC 日已走完 → 最新可用日 =**同一个前一个 UTC 日**                          |

   所以两班次对 crypto 都落在"昨天(UTC)的收盘"，**结果相同、互为幂等重试**（B 班次顺带起到
   兜底作用：若 A 班次因网络失败，B 班次会补上）。真正的日切点是**北京 08:00**，不是北京 15:35。

   由此带来一个使用上的直观影响：**你白天看到的最新 crypto 价格，永远是前一个 UTC 日的收盘**，
   比北京时间滞后约 1 天（跨了 UTC 日边界）。这是 7×24 资产套用"日线"口径的必然结果。

### ⚠️ 7×24：周末也有数据（已按你的选择「保持并集」）

加密每天都有日线（含周六日）。组合日历是"所有持仓标的的日子取并集"，
所以**只要组合里有人持有 crypto，看板就会出现周末点位**：
crypto 各腿正常涨跌，同期股票/基金**前填不动**（显示为平线）。

实测：BTCUSDT 3338 根日线里有 **954 个周末**（477 周六 + 477 周日）。
若你的实际观察窗口是周一~周五，用侧边栏「时间范围」筛选即可，数据本身保留完整的 7×24。

## 期货合约（`type: futures`）

目前仅支持上期能源原油 **SC**（如 `SC2611` = 2026 年 11 月合约）。走新浪期货，**结算价**口径。

```yaml
investors:
  - nickname: "investor60"
    holdings:
      - {symbol: "SC2611", market: cn, type: futures, weight: 0.3, expires: "2026-10-30"}
      - {type: cash, weight: 0.7}
```

- key 形如 `cn_futures_SC2611`；`expires` = **最后交易日**（见下）。
- 新增后直接跑 `python -m prototype.daily_close` 会自动补种全量日线。
  全量日线缓存在 `data/futures_cache/{key}.json`。
- 单独抓取查看：`python -m prototype.futures_fetch --symbol SC2611 --tail 5 --quote`

### 口径与简化假设

- **结算价**：落盘用日线的 `c`（= 当日结算价），`caliber` 标为 `settlement_price`。
  实时报价的"最新价"是另一个口径，**不参与落盘**，只作诊断。
- **不含手数 / 乘数 / 保证金**：按**名义价值比例**记权重——`weight: 0.3` 就是"30% 的钱
  按 SC2611 的价格变动计收益"。框架按 `w × P(t)/P(起点)` 算，不需要知道 1 手 = 1000 桶。
- **不做汇率折算**；SC 以人民币计价，不存在这个问题。

### ⚠️ `expires`：到期日（最后交易日）

**规则**：`expires` **当日仍有价格，次日起视为现金**。也就是"合约到期而未滚仓 → 余额冻结，
不涨不跌，直到下一笔调仓"。

- 只在"到期日事先可知、且到期后不再有价格"的标的上有意义（期货/期权等衍生品）。
  股票/ETF/基金/加密通常**不用填**（留空 = 永不到期）。
- 到期后仍持仓时，`daily_close` 会打印提醒，让你去 `switches.yaml` 追加滚仓：
  ```
  [提醒] 以下持仓的标的**已到期但未调仓**，余额已按现金处理（持仓冻结不涨不跌）：
          - investor60   cn_futures_SC2611   到期日 2026-10-30
  ```

### 展期（roll）怎么建模

**把每次展期写成 `switches.yaml` 里的一次调仓**（与现有调仓口径天然一致）：

```yaml
switches:
  - {date: 2026-10-28, nickname: "investor60", symbol: "SC2612", market: cn, type: futures}
```

这样做的关键好处是**展期价差会被如实计入收益**。期货总收益 = 价格收益 + 展期收益，
而展期收益恰恰是"卖掉近月、买入远月"这个动作产生的：

- **backwardation**（远月更便宜，如 SC2611 711.5 → SC2612 675.8）：卖出贵的、买入便宜的，
  展期**赚** `(711.5−675.8)/711.5 = +5.02%`
- **contango**（远月更贵）：展期**亏**，是持有成本

> 若改用市面上的"原油连续/主力连续"行情，展期早被隐含处理过，你既控制不了它的滚仓规则，
> 也用不上自己的主观滚仓决定——所以本项目**用具体合约 + 调仓建模**。

一处已知的显示细节：滚仓当天若新合约当日无价（如结算价未出），段基期会顺延到新合约的
**下一根日线**，那段之间组合市值保持平线（不涨不跌）——不会算错，只是少一个变动点。

### ⚠️ 一旦合约到期下架，价格序列就**真的结束了**

股票/基金/加密的历史随时可重取，期货不是——合约到期后交易所不再产生行情
（新浪仍保留历史，但这是它的善意，不保证永久）。所以：

- **不要**把已到期合约从 `switches.yaml` 里删掉，那是唯一记录"当时持有什么"的地方；
- 组合日历已修复为"各腿价格日期 ∪ 全局日历"，所以某腿结束后曲线会**继续延伸成平线**
  而不是断掉消失（详见 `tests/test_futures_offline.py` 的日历回归用例）。

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
- **口径说明**（`app/pages/2_口径说明.py`）：起始日 / 调仓规则 / 收益计算规则，附数字示例。
- **管理者视角**（`app/pages/3_管理者视角.py`）：hypothetical——每次调仓按「当日组合市值 × 万分之一 × **换手率**」
  收手续费，费用投入 SP500 的累积。换手率 = ½ Σ|Δ权重|（单标的全仓切换 = 1）。
- **调仓复盘**（`app/pages/4_调仓复盘.py`）：逐次 What-if——「若某次不调仓、**继续持有当时的旧持仓组合**到今天」
  的市值对比（多标的按 buy & hold 加权推算：Σ wᵢ × Pᵢ(t)/Pᵢ(调仓日)）。

## 目录

```
investors.yaml               # 公开清单：60 余位投资者的昵称 + **初始**标的（不含真实姓名）
switches.yaml                # 调仓流水（append-only）：每条 = 某投资者某日全仓切换到某标的
private/                     # 本地私密：roster.csv(真名↔昵称) + investors.private.yaml（gitignore）
tools/make_investors.py      # 本地匿名化：roster + members -> investors.yaml
tools/admin_app.py           # 本地管理台（Streamlit）：新增投资者 / 追加调仓（支持多标的权重）
tools/admin_ops.py           # 管理台纯文件操作（可单测）
prototype/
  quote_fetch.py             # 报价端点抓取（新浪/腾讯；A股/美股/港股）
  fund_fetch.py              # 场外开放式基金净值抓取（东方财富；累计净值口径 + 本地缓存）
  crypto_fetch.py            # 加密货币日线抓取（币安公开镜像；UTC 日线口径 + 本地缓存）
  futures_fetch.py           # 期货合约日线抓取（新浪期货；结算价口径 + 本地缓存；目前仅 SC）
  daily_close.py             # 每日：取当日收盘价 → append → 算组合收益 / 群体平均
  backfill.py                # 首次：回填历史日线（未复权）；fund/crypto/futures 走各自全量接口补种
  index_build.py             # 群体平均指数（对投资者组合收益按人平均，基准日 = 100 = 100 万）
app/streamlit_app.py         # 看板「总览」页
app/common.py                # 看板共用：数据加载 / 常量
app/pages/1_投资者明细.py    # 看板「投资者明细」页（某投资者逐日持仓 / 收益）
app/pages/2_口径说明.py      # 看板「口径说明」页（规则 + 数字示例）
app/pages/3_管理者视角.py    # 看板「管理者视角」页（手续费 hypothetical）
app/pages/4_调仓复盘.py      # 看板「调仓复盘」页（逐次 What-if）
data/{prices,returns}/*.csv, data/portfolios/*.csv, data/index/equal_weight.csv, meta.json  # 生成物
data/fund_cache/{key}.json   # 生成物：场外基金全量净值缓存（单位净值/累计净值/分红说明）
data/crypto_cache/{key}.json # 生成物：加密货币全量日线缓存（OHLCV）
data/futures_cache/{key}.json # 生成物：期货全量日线缓存（结算价/持仓量）
tests/                       # 计算层与抓取层单测（离线，不联网）
src/                         # 配置/计算（aggregate.py 多标的权重+现金腿、portfolio.py 单标的、analysis.py 附加分析）+【备用后端】
```

## 隐私与昵称映射（重要）

- `investors.yaml` / `switches.yaml` 是**公开**文件，**只出现昵称**（如 `investor01`），**不含真实姓名**。
- 真名 ↔ 昵称 的映射放在本地 **`private/roster.csv`**（`real_name,nickname`）；
  真名 + 初始持仓放在 **`private/investors.private.yaml`**。整个 `private/` 被 `.gitignore` 忽略，**不会上传**。
- 生成：`python -m tools.make_investors` 按 `real_name` 把两者 **JOIN** → 写出公开的 `investors.yaml`（只留昵称 + 标的）。

**维护映射**（`private/roster.csv`，编号昵称、**稳定不重排**）：

```csv
real_name,nickname
张三,investor01
李四,investor02
```

- **加人**：往 `roster.csv` 追加一行（分配一个**没用过**的昵称）+ 往 `investors.private.yaml` 的 `members`
  追加一行（真名 + 初始标的），再跑一次 `python -m tools.make_investors`。
- **改昵称**：改 `roster.csv` 那一行（对外昵称会变，注意历史）。
- **查询 真名 → 昵称**（录调仓时用）：直接查 `roster.csv`。

别人 clone 仓库只能看到「investor01 持有 600519」，看不到真实身份。

## 本地使用

```powershell
cd invest-dashboard
py -m venv .venv
.\.venv\Scripts\Activate.ps1        # 或: py -m pip install -r requirements.txt 直接装到全局
pip install -r requirements.txt

# 0) 改名单后（可选）重新生成公开清单：编辑 private/investors.private.yaml + private/roster.csv，然后
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
  - {date: 2026-10-15, nickname: "investor01", symbol: "AAPL", market: us, type: stock}
```

含义：investor01 在 `2026-10-15` 收盘把整仓**全仓切换**到 AAPL。追加后跑 `python -m prototype.daily_close`
即可重算组合收益。每位投资者各自独立，日期 / 标的互不影响。

## 本地管理台（GUI，辅助录入）

一个**仅本地**的 Streamlit 页面，帮你录入：

```powershell
python -m streamlit run tools/admin_app.py
```

- **➕ 新增投资者**：填「真实姓名 + 初始标的」→ 自动/指定昵称，写入 `private/roster.csv`
  与 `private/investors.private.yaml`，并重生成 `investors.yaml`。
- **🔁 追加调仓**：选投资者 + 目标标的 + 生效日 → 追加到 `switches.yaml`。改完记得跑
  `python -m prototype.daily_close` 重算。

> ⚠️ 它会**写本地文件**（`private/`、`switches.yaml`），**切勿部署到公网 / Streamlit Cloud**
> （所以放在 `tools/` 而非 `app/pages/`）。

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
