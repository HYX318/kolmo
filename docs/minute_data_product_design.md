# A 股分钟行情标准产品设计

- 状态：方案确认，实施中
- 版本：0.2
- 日期：2026-09-06
- 范围：A 股 1/5/15/30/60 分钟 OHLCVA 数据
- 不包含：逐笔成交、盘口、Level-2、复权因子生产和策略逻辑

## 1. 决策摘要

本方案将供应商交付物与 Kolmo 正式数据产品分成两层：

1. 原始交付层保持供应商文件原样，不改名、不重压，用于审计、追溯和重新生产。
2. 正式产品层采用版本化 Arrow/Parquet schema，按“频率 × 复权口径 × 交易所 × 交易日”落地。

正式产品以 1 分钟数据为基础事实。5/15/30/60 分钟由 1 分钟按照中国市场交易时段
规则统一派生。供应商提供的 5/15/30/60 分钟数据作为对账来源保留，不直接作为正式产品
的权威值。

建议目录：

```text
$KOLMO_DATA_ROOT/profile/minute/v1/
  1m/raw/{sz,sh,bj}/YYYY/MM/YYYYMMDD.parquet
  5m/raw/{sz,sh,bj}/YYYY/MM/YYYYMMDD.parquet
  15m/raw/{sz,sh,bj}/YYYY/MM/YYYYMMDD.parquet
  30m/raw/{sz,sh,bj}/YYYY/MM/YYYYMMDD.parquet
  60m/raw/{sz,sh,bj}/YYYY/MM/YYYYMMDD.parquet
  manifests/YYYY.json
  validation/YYYY/
```

不采用“股票 × 交易日”分文件，避免形成数百万小文件。

## 2. 背景与现状

当前供应商数据位于：

```text
$KOLMO_DATA_ROOT/raw/vendor_candidate/minute_201001_202609/分钟线数据/
```

截至 2026-09-06 的快速清单结果：

- 主分钟数据约 64.75 GB；
- 125 个正常上市股票 ZIP，加上 10 个退市股票 ZIP；
- 525,677 个 Parquet 成员；
- 2010–2025 为“年份 × 频率”年包；
- 2026 为“月份 × 频率”月包，五个频率均覆盖 1–9 月；
- 2026 每个频率的股票并集为 5,535 只；
- 文件名混用“分钟”和 `min`；
- 退市股票单独交付；
- 正常上市及退市股票的 1/5/15/30/60 分钟交付均已下载完成；
- 最新快速 inventory 为 0 error、1 warning；
- 退市股票包存在一个跨频率成员集合差异：1 分钟缺少 `832317.BJ`，而
  5/15/30/60 分钟缺少 `000787.SZ` 和 `000805.SZ`。

所有交付文件已经下载完成，可以开始 staging 试生产。上述退市成员集合差异需要在全量
发布前结合证券主数据核对；生产器以 1 分钟为权威输入，不会用其他频率静默补写缺失股票。

原始 Parquet 当前字段为：

```text
ts_code:    string
freq:       string
trade_time: timestamp[ms, tz=Asia/Shanghai]
open:       double
close:      double
high:       double
low:        double
vol:        int64
amount:     double
```

供应商物理打包方式不一致不应传播到正式查询接口，也不能仅通过改名或重新压缩解决。
正式生产必须同时解决 schema、主键、时段语义、退市覆盖、派生频率一致性和可追溯性。

## 3. 设计目标

### 3.1 目标

- 为所有年份、频率和交易所提供同一个稳定的数据契约；
- 支持按交易日进行全市场截面读取；
- 支持每日增量、单日修复和年度回放；
- 保证每个正式分区可以追溯到原始 ZIP 和生产代码版本；
- 明确集合竞价、午休、bar 标签、时区、成交量和成交额单位；
- 在发布前完成结构、行情、日历、跨频率和独立日线对账；
- 生产失败时不污染上一版正式产品。

### 3.2 非目标

- 不修改或删除供应商原始文件；
- 不把未复权数据覆盖成前复权或后复权数据；
- 不根据零成交量自行判断停牌状态；
- 不在本产品中引入盘口或逐笔字段；
- 不为追求磁盘极限压缩而使用私有二进制格式。

## 4. 分层架构

### 4.1 原始交付层

原始层是不可变证据，允许存在年包、月包、中文和 `min` 命名差异：

```text
raw/vendor_candidate/minute_201001_202609/
```

原始层规则：

- 文件下载完成前保留供应商临时后缀；
- `.qkdownloading`、`.part`、`.download`、`.crdownload` 和空文件不得进入生产；
- 完成后记录文件大小、mtime、SHA-256 和 ZIP CRC；
- 不通过“规范化文件名”掩盖供应商批次差异；
- 正常上市和退市股票包分别记录来源，直到生产合并完成。

### 4.2 逻辑读取层

读取层将以下两种输入抽象为相同的 `year × frequency` 数据集：

- 一个年度 ZIP；
- 同一年度按月拆分的多个 ZIP。

当同一年度同时存在年包和月包时，读取层默认使用年包，避免重复计数。出现重复月份、
缺月或跨频率月份集合不一致时必须报错。

### 4.3 正式产品层

正式产品按交易日组织，每个文件包含一个交易所当天某频率的全部股票。Parquet 文件本身
使用 ZSTD 压缩，不再套 ZIP 或 gzip。

该布局优化以下主要工作负载：

- 每日全市场截面；
- 因子和选股计算；
- 交易日覆盖率检查；
- 每日增量更新；
- 单日数据修复和回滚。

单股票十几年分钟历史需要读取大量日分区，性能弱于供应商的年度 per-symbol ZIP。第一阶段
继续使用原始年度/月度 ZIP 或 DuckDB/Arrow dataset 承担此类查询；只有实际性能不足时，
再增加年度 symbol cache，不在第一阶段复制一套完整正式数据。

## 5. 标准数据契约

### 5.1 权威 Arrow schema

正式产品以 Arrow schema 为权威契约。Python dataclass、Pandas dtype、C++ struct 和前端
类型均由该契约映射，不能各自定义不同的字段或空值规则。

| 字段 | Arrow 类型 | 是否可空 | 定义 |
|---|---|---:|---|
| `symbol` | `string` | 否 | 大写代码及交易所后缀，如 `600519.SH` |
| `trade_time` | `timestamp[ms, tz=Asia/Shanghai]` | 否 | bar 结束时间 |
| `open` | `float64` | 否 | 未复权开盘价 |
| `high` | `float64` | 否 | 未复权最高价 |
| `low` | `float64` | 否 | 未复权最低价 |
| `close` | `float64` | 否 | 未复权收盘价 |
| `volume` | `int64` | 否 | 成交股数，单位为股 |
| `amount` | `float64` | 否 | 成交额，单位为人民币元 |

主键：

```text
(symbol, trade_time)
```

`frequency`、`trade_date`、`exchange` 和 `adjustment` 已由分区路径确定，不在每行重复。
消费者读取单个文件时仍可从 Parquet metadata 获得这些信息。

### 5.2 Parquet metadata

每个文件至少写入：

| Key | 示例 |
|---|---|
| `kolmo.schema_version` | `1.0.0` |
| `kolmo.product_id` | `ashare_minute_bars` |
| `kolmo.frequency_minutes` | `1` |
| `kolmo.trade_date` | `2026-08-03` |
| `kolmo.exchange` | `SH` |
| `kolmo.adjustment` | `raw` |
| `kolmo.timezone` | `Asia/Shanghai` |
| `kolmo.bar_label` | `end` |
| `kolmo.volume_unit` | `share` |
| `kolmo.amount_unit` | `CNY` |
| `kolmo.source` | 供应商标识 |
| `kolmo.build_id` | 本次生产唯一 ID |

原始 ZIP SHA-256、生产 Git revision、命令参数和全年度统计写入年度 manifest，不在每个
Parquet 文件重复存放大段 provenance。

### 5.3 为什么不采用私有 binary struct

私有定长 struct 可以降低单行解析开销，但会增加版本演进、跨语言读取、时区、空值、
压缩、列裁剪和工具兼容成本。当前数据源已经是 Parquet，使用 Arrow schema 可以直接获得：

- 列式压缩和谓词下推；
- Python、C++、DuckDB 和前端服务一致的类型语义；
- schema metadata 和版本控制；
- 无需维护私有编码器和解码器。

因此“自定义标准 struct”的正确落点是标准数据契约，而不是自定义磁盘格式。

## 6. 时间与交易时段语义

`trade_time` 统一解释为 `Asia/Shanghai` 时区的 bar 结束时间。

### 6.1 1 分钟标准网格

- 集合竞价：`09:30`，单独一根；
- 上午连续竞价：`09:31`–`11:30`；
- 下午连续竞价：`13:01`–`15:00`；
- 北交所盘后交易：`15:01`–`15:30`，作为独立 session 分桶，不并入 `15:00`；
- 沪深完整交易日正常为 241 根，带盘后网格的北交所完整日为 271 根。

### 6.2 高频率派生

集合竞价 bar 不与连续竞价合并。连续竞价分别以 `09:30` 和 `13:00` 为分桶基准，
向上取整到目标频率的 bar 结束时间；上午和下午独立分桶，禁止跨午休 resample。

目标频率完整交易日正常根数：

| 频率 | 沪深 | 北交所含盘后 |
|---:|---:|---:|
| 1m | 241 | 271 |
| 5m | 49 | 55 |
| 15m | 17 | 19 |
| 30m | 9 | 10 |
| 60m | 5 | 6 |

北交所盘后只有 30 分钟，因此 60m 的最后一根是标记为 `15:30` 的 30 分钟部分 bar。
供应商同时包含零成交占位和少量真实盘后成交；二者都保留，以免丢失实际成交或改变
网格语义。消费者需要常规竞价样本时，应显式过滤 `trade_time <= 15:00`。

派生 OHLCVA：

```text
open   = 第一根 1m open
high   = max(1m high)
low    = min(1m low)
close  = 最后一根 1m close
volume = sum(1m volume)
amount = sum(1m amount)
```

禁止使用会跨午休或把 `09:30` 集合竞价混入 `09:31–09:35` 的默认 resample。

## 7. 1 分钟作为正式基准的依据

供应商不同频率并非完全一致。2010 年 `600519.SH` 抽检发现：

- 5 分钟数据缺少 1 个交易日、共 49 根 bar；
- 1 分钟聚合与供应商 5/15/30/60 分钟的 high/low 存在大量差异；
- 2010-01-12 的 1 分钟日内最高价为 `166.17`；
- 同一供应商的独立日线最高价也是 `166.17`；
- 供应商 5/15/30/60 分钟最高价均为 `166.15`。

因此当前证据支持以 1 分钟作为基础事实，并从其确定性派生其他频率。供应商其他频率
仍需保留并生成差异报告，以便识别 1 分钟自身可能存在的问题，但不应覆盖派生结果。

若后续全量日线对账证明某些年份的 1 分钟质量不达标，应按年份隔离并重新决策，不能
在生产程序中按字段静默混用不同频率来源。

## 8. 文件组织与写入参数

### 8.1 路径

```text
profile/minute/v1/{frequency}/raw/{exchange}/YYYY/MM/YYYYMMDD.parquet
```

示例：

```text
profile/minute/v1/1m/raw/sh/2026/08/20260803.parquet
```

### 8.2 文件内部组织

- 行顺序：`symbol ASC, trade_time ASC`；
- 压缩：ZSTD；
- 建议初始 row group：64K–128K 行；
- 写入前必须完成显式 Arrow cast；
- 不使用 CSV 作为正式分钟产品；
- 不在 Parquet 外层增加 ZIP/gzip；
- 单个交易所单日数据正常只写一个文件；
- 只有文件明显超过目标大小时才允许 `part-NNN.parquet`，并在 manifest 记录。

row group 最终参数由试生产基准决定。目标是兼顾全市场顺序扫描和按股票过滤时的统计裁剪。

### 8.3 文件数量评估

按 17 年、每年约 242 个交易日估算：

- 单频率、全市场约 4,114 个交易日分区；
- 五个频率约 20,570 个交易日分区；
- 按 SH/SZ/BJ 分开后上限约 61,710 个文件，早期年份实际更少。

该规模对本地文件系统可控。若拆成“股票 × 交易日”，文件数量将达到千万级，明确禁止。

## 9. 生产流程

### 9.1 状态机

```text
discovered
  -> inventory_passed
  -> normalized
  -> partitioned
  -> validated
  -> published

任一步失败 -> rejected
```

只有 `validated` 可以进入 `published`。失败批次保留 manifest 和报告，但不能出现在正式
产品指针中。

### 9.2 阶段 A：交付清单

快速清单检查：

- 期望年份、频率和月份是否齐全；
- ZIP 中是否存在 Parquet；
- 同一包内股票成员是否重复；
- 五个频率的月份集合是否一致；
- 是否存在未完成文件；
- 退市股票包是否齐全。

正式生产前执行深度清单：

- ZIP CRC；
- 每个原始文件 SHA-256；
- 文件大小和交付批次记录。

### 9.3 阶段 B：标准化 1 分钟

逐个输入 symbol member 读取，不整体解压 ZIP：

1. 校验 schema；
2. 将字段重命名为标准字段；
3. 显式转换 Arrow 类型；
4. 校验股票代码、频率和时间范围；
5. 合并正常上市与退市来源；
6. 按交易日和交易所写入 staging 分区；
7. 排序并检查主键唯一性。

同一主键来自多个原始包时不得使用 `drop_duplicates(keep=...)` 静默解决。若数值完全一致，
记录重复来源后保留一行；若数值不同，批次失败并输出冲突证据。

### 9.4 阶段 C：派生其他频率

只读取已通过 L1/L2 的正式候选 1 分钟分区，按照第 6 节规则派生 5/15/30/60 分钟。
派生结果带相同 build ID，并记录父分区 checksum。

### 9.5 阶段 D：原子发布

生产写入独立 staging 目录：

```text
$KOLMO_DATA_ROOT/staging/minute/{build_id}/
```

验证通过后：

1. 生成年度 manifest；
2. 将完整年度目录原子移动到带版本的正式目录；
3. 原子更新 `CURRENT` 指针或 catalog 记录；
4. 保留上一版产品，支持快速回滚。

不得逐文件覆盖当前正式年度，否则消费者可能同时看到新旧批次。

## 10. 验收门禁

### 10.1 L0：交付完整性，阻断

- 所有期望 ZIP 存在且非下载临时文件；
- ZIP CRC 全部通过；
- SHA-256 已记录；
- 年份、月份和频率无缺口；
- 正常上市及退市包均已纳入；
- 包内股票成员集合差异已解释。

### 10.2 L1：schema 与主键，阻断

- 字段与 Arrow schema 完全一致；
- 非空字段不存在 null/NaN；
- `(symbol, trade_time)` 唯一；
- 文件内按 `symbol, trade_time` 升序；
- symbol 后缀与输出交易所一致；
- 所有 `trade_time` 属于输出交易日；
- 时区为 `Asia/Shanghai`。

### 10.3 L2：行情约束，阻断

- `low <= open <= high`；
- `low <= close <= high`；
- OHLC 全部大于零；
- `volume >= 0`；
- `amount >= 0`；
- 时间戳属于合法交易网格；
- 完整日正常 bar 数符合频率定义。

零成交量 K 线可以存在，但必须进入质量统计。不能仅凭零成交量删除 bar。

### 10.4 L3：生产守恒，阻断

对每个交易日、股票执行：

- 输入 1 分钟行数 = 输出 1 分钟行数，加上有明确证据的完全相同重复行去重数；
- 输入/输出 OHLCVA checksum 或聚合统计一致；
- 分区 min/max 日期和 symbol 集合一致；
- 重新读取输出 Parquet 后 schema 与统计一致。

### 10.5 L4：独立日线对账，核心门禁

1 分钟聚合日线与独立未复权日线比较：

- OHLC 价格容差：默认 `0.005`；
- 成交量容差：默认 10 股；
- 成交额容差：默认 200 元或 `1e-8` 相对误差，取较大者。

是否允许极少量超阈值记录进入正式产品，需要在试生产后确定百分比门槛。任何全市场共同缺日、
系统性倍数差、单位错误或大面积价格差异均直接阻断。

### 10.6 L5：派生频率检查，阻断

- 派生 5/15/30/60 分钟重新聚合到日线后必须与 1 分钟日线一致；
- open/close 应精确一致；
- high/low 应精确一致；
- volume 应精确守恒；
- amount 仅允许浮点求和误差；
- bar 标签和根数必须符合时段规则。

与供应商对应频率的差异作为 evidence 保存。供应商对应频率不一致本身不自动覆盖派生数据。

### 10.7 L6：统计异常，告警

- 零量但 OHLC 变化；
- 正成交量但成交额为零；
- 成交均价明显落在 `[low, high]` 外；
- 长时间恒价；
- 异常跳价或涨跌停越界；
- 全年零量填充网格；
- 股票日期超出上市/退市区间。

L6 默认告警，不直接删除数据。需要结合证券主数据和公司行动信息解释。

## 11. 年度 manifest

建议结构：

```json
{
  "schema_version": "1.0.0",
  "product_id": "ashare_minute_bars",
  "build_id": "20260906T120000Z-<git-sha>",
  "year": 2026,
  "status": "validated",
  "source_archives": [
    {
      "path": ".../A股1分钟历史行情_2026-08.zip",
      "bytes": 463185742,
      "sha256": "...",
      "crc": "ok"
    }
  ],
  "outputs": {
    "1m": {
      "partitions": 0,
      "rows": 0,
      "first_trade_date": "2026-01-05",
      "last_trade_date": "2026-09-04"
    }
  },
  "validation": {
    "errors": 0,
    "warnings": 0,
    "report": "validation/2026/report.json"
  },
  "created_at": "2026-09-06T12:00:00Z"
}
```

实际 `first_trade_date` 由交易日历和输入数据决定，示例值不能硬编码。

## 12. 试生产方案

第一批仅生产 2026-08，不直接写正式目录。

使用项目虚拟环境包装命令启动：

```bash
./scripts/build_vendor_minute_profile.sh \
  --year 2026 --start-date 2026-08-01 --end-date 2026-08-31
```

该入口固定使用 `.venv/bin/python` 并在缺少 PyArrow 时立即给出安装提示，避免误用 macOS
Xcode 自带的 Python。

已知规模：

- 21 个交易日；
- 1 分钟月包约 452 MB；
- 五个供应商频率月包合计约 712 MB；
- 每个频率覆盖 5,535 只股票。

试生产输出：

```text
staging/minute/<build_id>/profile/minute/v1/...
staging/minute/<build_id>/manifest.json
staging/minute/<build_id>/validation/report.json
staging/minute/<build_id>/validation/findings.csv
staging/minute/<build_id>/benchmark.json
```

需要测量：

- 原始输入、1 分钟标准产品及全部派生频率的磁盘大小；
- 单日全市场读取耗时；
- 单股票单月和单股票全年读取耗时；
- row group 为 64K、128K 时的差异；
- ZSTD 压缩级别 3 和 6 的时间/空间差异；
- DuckDB 和 PyArrow 的列裁剪、symbol 过滤性能；
- 全部验收门禁的执行时间与内存峰值。

试生产建议通过条件：

- L0–L3、L5 error 为零；
- 独立日线不存在系统性差异；
- 生产结果可重复，同一输入和代码得到相同业务统计；
- 单日全市场读取和每日增量耗时满足研究及调度需求；
- 单股票历史查询性能退化已被记录并有可接受的读取路径。

### 12.1 2026-08 首次试生产结果

Build：`20260907T061146Z-e39e3be8`

```text
输入范围：2026-08-01 至 2026-08-31
实际交易日：2026-08-03 至 2026-08-31，共 21 日
股票来源：正常上市 5,535，退市 20
交易所：BJ、SH、SZ
每频率分区：63
Parquet 总数：315
构建状态：built_not_validated
构建耗时：约 23 分 40 秒
磁盘占用：约 550 MiB
```

| 频率 | 行数 | 文件字节数 |
|---:|---:|---:|
| 1m | 28,012,635 | 399,853,665 |
| 5m | 5,695,515 | 100,115,988 |
| 15m | 1,975,995 | 39,201,183 |
| 30m | 1,046,115 | 22,965,642 |
| 60m | 581,175 | 14,164,790 |

正式日分区验证器对 315 个 Parquet 执行了 schema、metadata、主键、排序、日期、交易所、
OHLCVA 和 manifest 守恒检查，并针对 63 个 1m 日/交易所分区重新派生所有高频率逐行比较。
结果为 0 error、0 warning，验证耗时约 29 秒。验证器按单日分区流式执行，不累计整月
DataFrame。

本次结果证明 staging 布局和确定性派生链路可行，但尚未完成以下发布前检查：

- 原始 ZIP 全量 deep inventory（CRC 和 SHA-256）；
- 1m 聚合与独立日线的全月 L4 对账；
- 64K/128K row group 和 ZSTD 3/6 的对照 benchmark；
- catalog/CURRENT 发布与回滚演练。

### 12.2 生产性能优化与全月回归

首次试生产的主要瓶颈不是 Parquet 本身，而是读取器在查询 2026-08 时，对每只股票重复
解码 2026-01 至 2026-09 的所有月包；此外还存在重复排序 ZIP 成员、临时分片使用 ZSTD
后立即解压重写、每个派生频率重复排序等额外开销。

现已完成以下优化：

- 根据请求日期只打开相交的月包，年度包仍在读取后按日期裁剪；
- ZIP 股票成员集合在 archive 生命周期内缓存，成员判断改为常数时间；
- 临时 Parquet 分片使用 Snappy，正式文件仍使用 ZSTD 3；
- 1m 规范排序只做一次，5/15/30/60m 聚合复用该顺序；
- 股票读取/拆分和日/交易所合并/派生两个阶段都使用独立进程并行；
- manifest 记录 worker、压缩、row group 参数和两个阶段的实际耗时。

在同一台机器上使用 `--workers 4 --batch-symbols 200` 重跑完整 2026-08：

```text
读取并写临时分片：11.513 秒
合并、派生并写正式分区：12.295 秒
生产总耗时：23.809 秒（墙钟 24.19 秒）
相对首次试生产：约 59 倍加速
```

新产物与首次产物的每频率行数、日期范围、分区数完全一致。验证器检查 315 个 Parquet、
63 个日/交易所分区并逐行复算派生频率，结果仍为 0 error、0 warning。两次文件字节数的
微小差异来自 build metadata，不影响业务数据。月度基准的 `batch-symbols=200` 不直接作为
年度默认值；年度单股票行数更高，为控制峰值内存继续采用默认 50。

### 12.3 2011 年供应商 low=0 勘误

2011 年首次全量生产被 canonical OHLC 门禁阻断。检查全部临时分片后确认共有 12 条异常，
均为单字段 `low=0`，其他 OHLC、成交量和成交额为正常正值；其中 8 条集中在
2011-12-12 的沪市股票。使用供应商独立交付的 5m 包核对相应窗口后，修正后的 5m OHLCV
与独立包精确一致，成交额最大绝对差为 2 元（约 1.9e-8 相对误差，属于聚合取整差）。

这些记录进入 `vendor-minute-corrections-v1` 勘误表。生产只在 symbol、trade_time、field 和
原始值 `0.0` 全部精确匹配时修改 low，并将原值、修正值和证据写入年度
`quality/corrections.json`；manifest 记录策略版本、命中数量和报告路径，验证器检查报告与
manifest 守恒。此机制不是通用清洗规则，未登记的 OHLC 异常仍然立即阻断。

2012 年继续发现 65 条同类 `low=0`，覆盖 60 只股票。独立 5m 只能唯一反推出其中 27 条；
其余 38 条只能确认 5m 窗口最低价，无法定位到精确分钟。受影响窗口中另有 16 个 open/high
与独立 5m 不一致，说明不能把跨频率交付描述成分钟级真值恢复。

因此自 `vendor-minute-corrections-v2` 起增加一个严格限定的结构修复：只有 low 精确为零、
open/high/close 均为正且 high 不低于 open/close 时，才令 `low=min(open, close)`。报告逐条
标记 `inferred=true`；这保证 canonical OHLC 可计算，但不宣称恢复了真实分钟最低价。
精确勘误继续标记 `inferred=false`。任何不满足该窄模式的异常仍然阻断生产。
股票拆分 worker 会先完成本分片内所有股票检查，再把未解决问题汇总到
`quality/unresolved.json`；主进程在进入日分区派生前统一阻断。因此未知异常按年度一次性
报告，而不是由最先完成的日分区逐条暴露。

2013 年汇总发现 11 个 symbol-level 问题：10 条集中在 2013-11-07 收盘分钟，close 比 low
低 0.01–0.03；另 1 条 high 低于 close。`601011.SH` 同一收盘分钟还出现 high=18459.89、
而 open/close 约为 10 元的明显尖峰。独立 5m 支持修正后的 OHLC，但该日部分 5m amount
本身存在数量级错误，因此只用作 OHLC 旁证，不作为整行真值来源。

`vendor-minute-corrections-v3` 将结构修复扩展为：正价格记录的 low/high 必须覆盖
open/close；违反时扩展到相应局部边界。high 超过 `2 * max(open, close)` 时视为明确的
分钟内尖峰并收回局部上界。阈值刻意宽于 A 股普通涨跌停及上市日波动，所有变化均标记
`inferred=true` 并逐字段审计。

2022 年首次生产汇总了 164 只北交所股票的 session timestamp 问题。全量核对确认共有
412,980 条 `15:01`–`15:30` 记录，其中大部分是零成交平价网格，但 591 条有真实成交，
合计成交量 104,808,267 股。因此不能把该区间作为供应商 padding 删除。canonical session
规则改为按交易所区分并完整保留北交所盘后分钟；高频率派生以 `15:00` 为新分桶起点，
确保盘后 bar 不会并入正常竞价 bar。164 只股票真实年度回归为 0 unresolved，五个频率均
成功生产。

2023 年汇总发现两类新的北交所供应商语义：

- 62 条负成交记录全部位于 2023-07-11 15:30，且独立 1/5/15/30/60m 包一致复现；这类
  记录按盘后净冲正/撤销调整保留原值，写入 `quality/observations.json`。canonical 只允许
  BJ 的 `15:01`–`15:30` 出现负 volume/amount，其他市场或时段仍阻断。
- 8 只股票共有 77 个完整零价、零成交、零金额日期，合计 20,867 行；这些无有效行情占位
  日从 canonical 产品排除，并逐股票、逐日期写入 `quality/exclusions.json`。只有整日所有
  行均为零价零活动时才允许排除，真实交易日中的局部零价仍阻断。

## 13. 运维与增量策略

- 日常增量只生产已收盘且交易日历确认的日期；
- 当天数据未完整时写 staging，不创建正式分区；
- 修订历史日时创建新 build，不原地覆盖；
- 每次发布记录新增、修改、删除分区清单；
- 消费者通过 catalog/CURRENT 获取一致版本；
- 定期从正式 Parquet 重新计算年度行数、日期范围和 checksum；
- 原始供应商数据和正式产品使用不同的保留策略。

## 14. 安全与失败处理

- 输出路径不得指向原始交付目录；
- staging 空间不足时在写入前失败；
- 临时文件使用同文件系统并通过原子 rename 发布；
- 进程中断后只留下带 build ID 的 staging，不留下半个正式年度；
- 任何 schema 漂移必须提升 schema version 或显式拒绝；
- 冲突主键、单位变化和时区变化均不得自动猜测修复。
- 已经由独立频率交付验证的供应商坏值可以进入版本化勘误表；应用时必须精确匹配股票、
  时间、字段和原始坏值，并在 `quality/corrections.json` 逐条记录。未登记异常继续阻断生产。

## 15. 决策状态

### 15.1 已确认

1. 1 分钟为正式基础事实，其他频率统一派生；
2. 正式产品按 `频率/复权/交易所/年月日` 分区；
3. 第一版只发布 `raw` 未复权价格；
4. 价格和成交额采用 `float64`，不引入私有定点格式；
5. 正式产品不按股票分文件；
6. 正常上市与退市股票必须合并，且交付完整是全历史发布门禁；
7. 第一版生产器只输出 staging，验证和发布解耦。

### 15.2 由试生产确定

1. L4 独立日线对账允许的全量失败率和例外审批机制；
2. Parquet row group 最终使用 64K 还是 128K；
3. ZSTD 最终使用压缩级别 3 还是 6；
4. 正式发布采用目录版本 + `CURRENT` 指针，还是接入现有 catalog；
5. 是否需要年度 symbol cache 作为第二阶段优化。

## 16. 现有实现与后续工作

现有组件：

- `kolmo.ashare.vendor_minute`：统一读取年包和月包；
- `kolmo.ashare.vendor_minute_delisted`：读取退市股票嵌套 ZIP；
- `kolmo.ashare.minute_product`：权威 Arrow schema、时段和派生规则；
- `kolmo.ashare.build_vendor_minute_profile`：按批次构建 staging 日分区；
- `kolmo.validation.minute_inventory`：交付清单、CRC 和 SHA-256；
- `kolmo.validation.minute_mdcheck`：结构、时段、跨频率和日线对账；
- `docs/minute_mdcheck.md`：现有 MDCheck 规则。

当前实施顺序：

1. 已完成权威 Arrow schema、年/月包读取、退市嵌套包读取；
2. 已完成 1 分钟按日 staging 及 5/15/30/60 分钟确定性派生骨架；
3. 已通过真实 2026-08-03 两只股票 smoke test；
4. 已将 10 个退市包纳入统一 inventory，并报告跨频率股票集合差异；
5. 已完成正式日分区验证器、manifest 守恒和派生频率逐行复算；
6. 已完成 2026-08 全月首次试生产，315 个文件验证为 0 error、0 warning；
7. 已完成日期裁剪、临时 I/O 和双阶段四进程优化，全月生产由约 23 分 40 秒降至 24 秒；
8. 下一步完成独立日线 L4 对账及压缩参数对照 benchmark；
9. 根据报告确定压缩参数及 L4 阈值；
10. 启动全历史 staging；实现 catalog/CURRENT 和回滚演练后才允许正式发布。

### 16.1 全历史 staging 编排

全历史按年份串行构建，每个年份构建成功后立即执行正式日分区验证。已存在且验证报告为
0 error、0 warning 的年份自动跳过；失败年份保留证据并停止，不继续后续年份。后续可以只
请求新的年份范围，顶层 manifest 会保留既有年份并扩展累计覆盖范围，例如先完成 2010，
再请求 2011–2026，不要求两次命令的起止年份完全相同。

```bash
./scripts/build_vendor_minute_history.sh \
  --start-year 2010 --end-year 2026 --end-date 2026-09-04 \
  --workers 4
```

年度之间保持串行，以便逐年验收和恢复；单个年度内部使用四个进程并行读取股票，并使用
四个进程并行生产互不依赖的日/交易所分区。`--workers 4` 是兼顾 CPU、内存和磁盘吞吐的
默认值。增加 worker 前应先观察一个完整年度的峰值内存，而不是只依据 CPU 核数上调。

包装脚本读取项目 `.env` 中的 `KOLMO_DATA_ROOT`。不要在未确认变量已加载时手工传入
`--output-root "$KOLMO_DATA_ROOT/..."`，否则空变量会被 shell 展开成根目录下的 `/staging`。

输出：

```text
$KOLMO_DATA_ROOT/staging/minute/history-v1/
  history-manifest.json
  year=2010/
    manifest.json
    validation/report.json
    validation/findings.csv
    profile/minute/v1/...
  ...
  year=2026/
```

当前磁盘可用空间约 625 GiB。按 2026-08 实测压缩比例估算，全历史五频率 staging 约
50–60 GiB；年度临时分片在该年度构建成功后清理，因此空间足以进行全量生产。
