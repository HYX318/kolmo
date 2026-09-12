# A 股分钟行情 MDCheck 方案

正式产品 schema、按日分区和发布流程见
[`minute_data_product_design.md`](minute_data_product_design.md)。

这里称为 **MDCheck（market-data check）**。OB test 通常指 order-book / 盘口逐档数据检查；当前数据只有 K 线，不属于 OB 数据。

## 数据契约

供应商分钟文件按“年份 × 频率”打包为 ZIP，ZIP 内每只股票一个 Parquet。字段为：

`ts_code, freq, trade_time, open, close, high, low, vol, amount`

- `trade_time` 是 `Asia/Shanghai` 时区的 bar 结束时间。
- `09:30` 集合竞价单独成 bar；连续竞价上午从 `09:31` 开始，下午从 `13:01` 开始。
- 北交所另保留 `15:01`–`15:30` 盘后交易时段；它独立分桶，不并入 `15:00` 竞价 bar。
- `vol` 单位为股，`amount` 单位为元。
- OHLC 是未复权价。复权因子应在查询/研究层按指定基准日应用，不能覆盖原始行情。
- 零成交量 K 线可能是供应商填充行，不代表股票当天可交易。

## 分层测试

### L0：交付物完整性（阻断）

- ZIP CRC 校验通过；记录文件大小、SHA-256、交付批次。
- 拒绝 `.qkdownloading` 等未完成文件。
- 年份、频率齐全；文件命名可解析。
- ZIP 内股票集合差异必须解释，不能静默忽略。

### L1：单表契约（阻断）

- 必需字段、类型、时区正确；`(ts_code, trade_time)` 唯一且升序。
- `low <= open/close <= high`，价格为正，成交量和成交额非负。
- 文件内股票代码和频率常量与路径一致。
- 缺失值、重复行、非法交易时刻均为错误。

### L2：日历与交易时段（阻断/告警）

- 沪深完整交易日正常应有：1m=241、5m=49、15m=17、30m=9、60m=5 根。
- 北交所若包含盘后时段，canonical 完整日为：1m=271、5m=55、15m=19、30m=10、
  60m=6 根；60m 的最后一根是标记为 `15:30` 的 30 分钟部分 bar。
- 交易日期与交易日历对齐；跨频率交易日集合一致。
- 全市场同一天缺失属于阻断问题；个股缺失需结合上市、退市、停牌状态判断。
- `全天 volume=0` 记为不可交易日。全年零量且价格恒定的填充网格必须被识别，不能进入可交易样本。

### L3：聚合与独立日线对账（核心）

先按中国市场午休边界显式分桶，不使用跨午休的默认 resample：

- `daily.open = 第一根 open`
- `daily.high = max(minute.high)`
- `daily.low = min(minute.low)`
- `daily.close = 最后一根 close`
- `daily.volume = sum(minute.volume)`
- `daily.amount = sum(minute.amount)`

执行两类对账：

1. 1m 聚合到 5/15/30/60m，与供应商对应频率逐 bar 和逐日比较。
2. 1m 聚合日线与独立未复权日线（当前为 `profile/daily_raw`）比较。

价格容差默认半个最小价位 `0.005`；成交量允许 10 股舍入差；成交额允许 200 元或 `1e-8` 相对误差。阈值用于区分舍入和实质差异，报告仍保留最大绝对差。

### L4：复权因子（接入前阻断）

- 因子必须为正、同日唯一、日期升序，并覆盖有行情的交易日。
- 因子变化日与除权除息事件对齐；检查异常倍增、倒置及未来函数。
- 用因子生成前/后复权序列后，抽样与另一独立来源比较；成交量、成交额不随价格复权误改。

### L5：统计异常与回测安全（告警）

- 零量但 OHLC 变化、正成交量但成交额为零、成交均价落在 `[low, high]` 外。
- 长时间恒价、极端收益、异常跳价、价格越过涨跌停限制。
- IPO/退市/ST/停牌日覆盖，幸存者偏差和幽灵可交易日检查。
- 按年份、交易所、板块和流动性分层抽样；每次交付保存 JSON/CSV 证据。

## 验收建议

- L0/L1 错误必须为零。
- 全市场共同缺日必须修复或取得供应商书面说明。
- 日成交量/成交额与独立日线的实质差异率应接近零。
- OHLC 跨频率差异不能只看平均值；需检查差异率、最大值和具体日期。
- 通过前只进入隔离的 `vendor_candidate` 区，不覆盖正式 profile。

## 生产布局

原始交付层保持供应商原样，月包、年包和下载中的临时文件均不改名。这一层用于追溯，
不能作为正式查询接口。清单和 MDCheck 把一个年包或同年的多个月包抽象成相同的
`year × frequency` 逻辑数据集，因此生产代码不应依赖供应商目录深度或中文/`min` 命名。

正式产品统一为“年份 × 频率”的不可变年度快照：

```text
$KOLMO_DATA_ROOT/profile/minute/vendor/raw/annual/YYYY/
  A股{1,5,15,30,60}分钟历史行情_YYYY.zip
  manifest.json
```

每个 ZIP 内每只股票一个 Parquet，主键是 `(ts_code, trade_time)`。年度快照必须合并
正常上市与退市股票；同一股票在多个输入分片出现时先按时间拼接，再检查重复主键，不能
静默覆盖。2026 月包只有在目标月份全部到齐、L0/L1 通过后才合并成年包。历史年包也先
验收后原样晋级或重建，不能仅因文件名规范就直接发布。

建议门禁顺序：

1. 快速 inventory：包数、月份、成员集合、未完成文件。
2. 深度 inventory：对所有 ZIP 做 CRC 和 SHA-256，保存交付 manifest。
3. MDCheck 抽样：跨板块、交易所、流动性和年份。
4. MDCheck 全量：L0/L1 error 必须为零，跨频率和日线差异留证。
5. 合并退市股票，重新跑唯一性、覆盖率和日历检查。
6. 原子写入年度快照和 manifest；只有验收报告通过才更新正式产品指针。

## 命令

先做快速交付清单（只读 ZIP 中央目录，不扫描 63GB payload）：

```bash
kolmo-inventory-minute --start-year 2010 --end-year 2026 \
  --through-month 9 --output /tmp/minute-inventory.json
```

正式验收时增加 `--deep`，逐字节执行 ZIP CRC 并记录每个包的 SHA-256。
快速清单通过后再运行下方逐行 MDCheck。读取层将一个年包和一组月包都视为同一个
`年份 × 频率` 逻辑数据集；原始交付物不改名、不重压。

读取一个股票而不解压全年 ZIP：

```bash
kolmo-read-vendor-minute --year 2010 --frequency 5 --symbol 600519.SH
```

抽样检查：

```bash
kolmo-mdcheck-minute --year 2010 \
  --symbols 000001.SZ 300001.SZ 600000.SH 600519.SH \
  --skip-daily-reference \
  --output /tmp/mdcheck-2010-sample.json
```

2017 年以后可去掉 `--skip-daily-reference`，与已有 `profile/daily_raw` 自动对账。全量扫描时不传 `--symbols`，结果会同时写 JSON 和 findings CSV。
