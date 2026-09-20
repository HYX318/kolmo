# A 股分钟数据：字段、路径、生产与读取

本文是当前实现的使用手册，更新于 2026-09-20。文件名沿用历史名称；正文描述已经实现的行为，未实现功能单独列出。

- [生产数据 MDCheck 检查清单](minute_daily_mdcheck.md)
- [原始供应商数据检查](minute_mdcheck.md)
- [C++ 控制台读取工具及编译说明](../tools/minute_read/README.md)

## 1. 数据来源和当前状态

供应商的 1 分钟数据是生产输入，正常上市和退市股票默认均纳入。生产程序执行标准化、已登记修正和整日无效数据排除，然后从 1 分钟派生 5/15/30/60 分钟。供应商自带的其他周期用于原始数据对账，不直接写成生产周期。

本机已经构建的历史位于 `$HOME/dat/all/staging/minute/history-v1`。2026-09-08 的报告记录覆盖 2010-01-04 至 2026-09-04，1 分钟共 3,441,924,942 行。这是该次报告的快照，不代表持续更新到今天。

“已生产”指已写成标准 Parquet；当前仍保存在 staging。没有已经完成的 `CURRENT`/catalog 正式发布与回滚流程。历史清单的 `validated` 仅表示对应校验流程通过，不表示已完成独立行情准确性验收。

## 2. 数据根目录与路径

Shell 包装脚本加载项目 `.env` 中的 `KOLMO_DATA_ROOT`，默认 `$HOME/dat/all`。直接运行 Python 模块或 C++ 工具时应显式导出环境变量，或使用各入口的路径参数；不要假设它们自动加载 `.env`。

```bash
cd /Users/galoishuang/Development/kolmo
export KOLMO_DATA_ROOT="$HOME/dat/all"
```

| 数据 | 路径（相对 `$KOLMO_DATA_ROOT`） | 用途 |
|---|---|---|
| 原始供应商包 | `raw/vendor_candidate/minute_201001_202609/分钟线数据/` | 年包/月包 ZIP，退市数据位于其下 `退市股票/`；用于追溯和重建 |
| 历史生产数据 | `staging/minute/history-v1/` | C++ 读取、生产 MDCheck 的根目录 |
| 独立构建批次 | `staging/minute/<build_id>/` | 单年或日期范围构建的默认输出 |
| 日线参考 | `profile/daily_raw/{sh,sz,bj}/YYYY/MM/YYYYMMDD.csv[.gz]` | 原始分钟检查的未复权日线参考；是否存在、是否覆盖须另行确认 |

历史生产目录：

```text
staging/minute/history-v1/
  history-manifest.json
  validation/
    daily-mdcheck.json
    daily-mdcheck.findings.csv
  year=2026/
    manifest.json
    quality/
      corrections.json
      exclusions.json
      observations.json
      unresolved.json
    validation/
      report.json
      findings.csv
    profile/minute/v1/
      1m/raw/sh/2026/09/20260904.parquet
      5m/raw/sh/2026/09/20260904.parquet
      15m/raw/sh/2026/09/20260904.parquet
      30m/raw/sh/2026/09/20260904.parquet
      60m/raw/sh/2026/09/20260904.parquet
```

上述 `sh` 还可为 `sz`、`bj`；历史年份的 quality 文件可能不全，校验器对 manifest 声明的报告进行核对。每个 Parquet 包含一个市场、一天、一个周期的所有入库股票，并非单只股票文件。

## 3. 字段含义

权威定义：[minute_product.py](../kolmo/ashare/minute_product.py) 的 `MINUTE_BAR_SCHEMA`。

| 生产字段 | Arrow 类型 | 含义 |
|---|---|---|
| `symbol` | string，非空 | 完整股票代码，如 `600519.SH`、`000001.SZ`、`920000.BJ` |
| `trade_time` | timestamp[ms, tz=Asia/Shanghai]，非空 | 当前契约使用的 bar 结束标签，上海时区 |
| `open` | float64，非空 | **这一根 bar** 的起始价格；通常对应区间首笔成交价，不是每行重复的全天开盘价 |
| `high` | float64，非空 | 这一根 bar 的最高价，不是全天累计最高价 |
| `low` | float64，非空 | 这一根 bar 的最低价，不是全天累计最低价 |
| `close` | float64，非空 | 这一根 bar 的结束价格；通常对应区间末笔成交价 |
| `volume` | int64，非空 | 这一根 bar 的成交量，单位：股，不是手 |
| `amount` | float64，非空 | 这一根 bar 的成交额，单位：人民币元 |

OHLC 均为未复权价。原始字段 `ts_code` 转为 `symbol`、`vol` 转为 `volume`；原始 `freq` 不保留为逐行字段。周期、市场、日期、复权口径由路径和 Parquet metadata 表示。

主键为 `(symbol, trade_time)`，文件内按这两个字段升序。Parquet metadata 包含 schema/product 版本、frequency、trade_date、exchange、adjustment、timezone、bar_label、volume/amount 单位、source 和 build_id。生产校验核对其中的契约字段，但不核验 source 的真实性。

对有成交的常规分钟，上述首末成交价解释是预期语义；零量填充和供应商特殊标签需要另行验证，不能只凭字段名断定真实成交语义。部分历史 OHLC 经过推断修复，也不保证恢复原始成交真值。

### 09:30 的已知语义疑点

当前代码保留 `09:30` 为独立 bar，并在派生周期中不将其并入 `09:31–09:35` 等窗口。旧文档把它直接称为“集合竞价 bar”，但供应商语义尚未证实。

已读到 `600519.SH / 2026-09-04 09:30` 的 open 为 1295.88、close 为 1297.86。如果它仅表示一次开盘集合竞价撮合，OHLC 应相同；该样本与这种解释不符。现阶段只能确认标签和数值确实存在，不能确认它就是集合竞价成交价。生产 MDCheck 尚不检查这一语义。

## 4. 时间网格和聚合规则

| 周期 | 沪深完整网格根数 | 北交所带完整盘后调整网格根数 |
|---|---:|---:|
| 1m | 241 | 271 |
| 5m | 49 | 55 |
| 15m | 17 | 19 |
| 30m | 9 | 10 |
| 60m | 5 | 6 |

1 分钟标签为 `09:30`、`09:31–11:30`、`13:01–15:00`；北交所另允许 `15:01–15:30` 的供应商盘后调整记录。生产 MDCheck 将北交所 241 或 271 根均视为日内根数完整。

派生周期的上午、下午、盘后分别分桶，不跨午休；`09:30` 独立保留。北交所 60 分钟的盘后桶标为 `15:30`，实际覆盖 30 分钟。北交所盘后段允许负量/负额，代码将其作为调整记录处理，这不等价于证明每条记录都是普通成交。

```text
派生 open   = 窗口中第一根 1m open
派生 high   = max(窗口内 1m high)
派生 low    = min(窗口内 1m low)
派生 close  = 窗口中最后一根 1m close
派生 volume = sum(窗口内 1m volume)
派生 amount = sum(窗口内 1m amount)
```

在交易时段、复权口径、行情覆盖一致的条件下，全天 `max(minute.high)` 和 `min(minute.low)` 应分别与日线 high/low 一致。**生产校验目前没有执行这项独立日线对账。**

## 5. 生产脚本与使用方式

| 入口 | 实现 | 行为 |
|---|---|---|
| `scripts/build_vendor_minute_profile.sh` | [build_vendor_minute_profile.py](../kolmo/ashare/build_vendor_minute_profile.py) | 构建一个年份内指定范围，默认写新批次；不自动完成独立验证 |
| `scripts/validate_vendor_minute_profile.sh` | [minute_profile.py](../kolmo/validation/minute_profile.py) | 检查一个构建目录，写 validation 报告 |
| `scripts/build_vendor_minute_history.sh` | [build_vendor_minute_history.py](../kolmo/ashare/build_vendor_minute_history.py) | 按年构建并调用单批验证，可恢复；默认写 history-v1 |

单批构建及验证示例（输出目录须不存在或为空）：

```bash
./scripts/build_vendor_minute_profile.sh \
  --year 2026 --start-date 2026-08-01 --end-date 2026-08-31 \
  --output-dir /tmp/kolmo-minute-202608-build --workers 4

./scripts/validate_vendor_minute_profile.sh \
  --build-root /tmp/kolmo-minute-202608-build
```

历史构建示例：

```bash
./scripts/build_vendor_minute_history.sh \
  --start-year 2010 --end-year 2026 --end-date 2026-09-04 \
  --workers 4
```

主要参数：

| 参数 | 单批/历史 | 含义 |
|---|---|---|
| `--year` | 单批 | 构建年份，必填 |
| `--start-year / --end-year` | 历史 | 构建年份范围 |
| `--start-date / --end-date` | 两者 | 单批限制在该年；历史作用于首尾年份 |
| `--source-root` | 两者 | 原始供应商目录 |
| `--output-dir` / `--output-root` | 单批 / 历史 | 单批输出目录 / 含 year=YYYY 的历史根目录 |
| `--include-delisted / --no-include-delisted` | 两者 | 默认纳入退市数据 |
| `--workers` | 两者 | 每阶段进程数，默认至多 4 |
| `--batch-symbols` | 两者 | 默认 50 |
| `--row-group-size` | 两者 | 默认 65,536 行 |
| `--compression-level` | 两者 | 正式 Parquet 使用 ZSTD，默认级别 3；临时分片使用 Snappy |
| `--limit` | 单批 | 股票数限制，默认 0（不限），仅用于小规模试跑 |

构建流程：读取 1m 正常/退市来源 → 标准化及质量处理 → 临时日分片 → 按日/市场合并排序 → 派生其他频率 → 写 Parquet、manifest 和质量记录。

质量处理见 [minute_corrections.py](../kolmo/ashare/minute_corrections.py)：精确勘误和推断修复写入 `corrections.json`；整日零价格、零活动排除写入 `exclusions.json`；盘后调整观察记录写入 `observations.json`；未解决问题写入 `unresolved.json` 并阻断构建。推断修复只能改善结构约束，不能宣称恢复真实分钟 high/low。

年度 manifest 构建完成后为 `built_not_validated`；单独验证不会将它改成 `validated`。历史编排将验收状态写入 `history-manifest.json`，这两个状态字段用途不同。

历史编排根据同 build_id 的既有干净验证报告跳过已验证年份，不会因本次日期范围扩大或源文件更新自动重建该年。需要重建或延长同一年覆盖时使用新的 `--output-root`。失败构建可被归档至 `failed-attempts` 后重试；验证失败会停止后续年份。

年度生产调用的 `validate_build()` 默认**不开启**个股日内根数检查；完成后如需完整生产复检，应运行下一节的历史 MDCheck。

## 6. 对生产数据执行 MDCheck

```bash
./scripts/check_vendor_minute_daily.sh \
  --root "$HOME/dat/all/staging/minute/history-v1" \
  --start-year 2010 --end-year 2026 --workers 4 \
  --output /tmp/production-minute-mdcheck.json
```

读取已有 Parquet，不修改行情。输出 JSON 和同目录的 `production-minute-mdcheck.findings.csv`。检查项、严重级别、容差、退出码以及未覆盖项均见 [生产 MDCheck 清单](minute_daily_mdcheck.md)。

## 7. C++ 读取生产数据并打印

源码：[tools/minute_read/main.cpp](../tools/minute_read/main.cpp)。依赖 C++17、CMake、项目 `.venv` 内的 PyArrow 21+ 提供的 C++ 动态库；运行时不启动 Python。

```bash
cmake -S tools/minute_read -B build/minute_read -DCMAKE_BUILD_TYPE=Release
cmake --build build/minute_read -j 4
```

本机使用独立 Command Line Tools 的编译方式、完整参数和动态库说明见 [工具 README](../tools/minute_read/README.md)。

```bash
./build/minute_read/kolmo_minute_read \
  --date 2026-09-04 --symbol 600519.SH \
  --start-time 09:30 --end-time 10:00 --limit 20

./build/minute_read/kolmo_minute_read \
  --start-date 2026-09-01 --end-date 2026-09-04 \
  --symbol 600519.SH,000001.SZ --frequency 5 \
  --start-time 09:30 --end-time 10:30 --limit 0
```

默认控制台表格显示最多 100 行；`--limit 0` 打印全部；`--csv` 改为 CSV。
`--root` 指向 history-v1，`--exchange sh/sz/bj` 可筛选市场，省略 symbol 时查询该市场全部股票。
日期及每日时间范围均包含两端；日期接受 `YYYY-MM-DD`/`YYYYMMDD`；周期支持 1/5/15/30/60。

工具裁剪日期/市场分区，并用 symbol 行组统计跳过无关数据，分批解码；时间过滤在批次内执行。
输出按日期、市场、文件内股票/时间顺序。诊断信息写 stderr，显示行数上限触发后立即停止，统计不是全量匹配数量。
不存在的日分区会跳过，不能将“无结果”直接解释为休市或停牌。

## 8. Python 读取生产 Parquet

使用 `.venv/bin/python` 运行：

```python
from pathlib import Path
import pandas as pd

root = Path.home() / "dat/all/staging/minute/history-v1"
path = root / "year=2026/profile/minute/v1/1m/raw/sh/2026/09/20260904.parquet"
df = pd.read_parquet(
    path,
    engine="pyarrow",
    columns=["symbol", "trade_time", "open", "high", "low", "close", "volume", "amount"],
    filters=[("symbol", "=", "600519.SH")],
)
print(df.to_string(index=False))
```

指定列也避免 Arrow 自动发现 `year=2026` 分区时额外带入 `year` 列。去掉 filters 可读取该文件内的全部股票。
原始 ZIP 读取命令 `kolmo.ashare.vendor_minute` 不读取上述生产数据，其用法见 [原始数据文档](minute_mdcheck.md)。
