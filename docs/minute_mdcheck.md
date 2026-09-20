# 原始供应商分钟数据：读取、清单与 MDCheck

更新于 2026-09-20。本文只描述供应商 ZIP 入口；已生产 Parquet 的检查见 [生产 MDCheck](minute_daily_mdcheck.md)，字段、生产流程及 C++ 读取见 [分钟数据手册](minute_data_product_design.md)。

## 1. 三种入口的边界

| 入口 | 输入 | 目的 |
|---|---|---|
| `kolmo.validation.minute_inventory` | 年/月包及默认纳入的退市包 | 交付物清单；deep 模式增加 CRC 和 SHA-256 |
| `kolmo.validation.minute_mdcheck` | 供应商各周期 ZIP，以及可选本地未复权日线 | 原始分钟结构、供应商跨周期及日线对账 |
| `scripts/check_vendor_minute_daily.sh` | 已生产 history-v1 的 Parquet | 内部契约与派生一致性；**没有独立日线对账** |

不要用原始 ZIP 的检查通过替代生产数据验收，也不要把生产聚合自洽解释为原始行情准确。

## 2. 路径与原始字段

默认原始目录：

```text
$KOLMO_DATA_ROOT/raw/vendor_candidate/minute_201001_202609/分钟线数据/
```

2010–2025 采用年包，2026 采用月包；同年/周期的多个包由 `VendorMinuteDataset` 统一读取。同一年同时有年包和月包时优先年包。每只股票的原始 Parquet 位于 ZIP 内，退市股票另有嵌套 ZIP 读取实现。

| 原始字段 | 含义 | 生产字段 |
|---|---|---|
| `ts_code` | 股票代码及市场后缀 | `symbol` |
| `freq` | `1min`、`5min` 等周期标记 | 周期移入路径/metadata |
| `trade_time` | 上海时区时间标签，当前契约按 bar 结束时间处理 | `trade_time` |
| `open/high/low/close` | 当前 bar 的未复权 OHLC，不是每日重复的开盘价或累计高低价 | 同名 |
| `vol` | 当前 bar 成交量，单位股 | `volume` |
| `amount` | 当前 bar 成交额，单位元 | `amount` |

09:30 的供应商业务语义尚未核实，不能直接等同为单次集合竞价撮合；详见主手册的已知疑点。

## 3. 原始数据读取

源码：[vendor_minute.py](../kolmo/ashare/vendor_minute.py)。在项目根目录运行：

```bash
export KOLMO_DATA_ROOT="$HOME/dat/all"

.venv/bin/python -m kolmo.ashare.vendor_minute \
  --year 2026 --frequency 1 --symbol 600519.SH \
  --start-date 2026-09-01 --end-date 2026-09-04 \
  --output /tmp/vendor-600519-1m.parquet
```

支持 `--root` 覆盖原始目录；`--output` 可为 `.csv`、`.csv.gz` 或 `.parquet`。
该命令读取原始 ZIP；查看生产后的控制台表格请使用 [C++ minute_read](../tools/minute_read/README.md)。

## 4. 交付清单

源码：[minute_inventory.py](../kolmo/validation/minute_inventory.py)。

```bash
.venv/bin/python -m kolmo.validation.minute_inventory \
  --start-year 2010 --end-year 2026 --through-month 9 \
  --output /tmp/minute-inventory.json
```

快速模式读取 ZIP 目录，检查包、年份/月份/周期、成员集合及未完成文件等交付信息；默认包括退市交付物。它不扫描全部分钟数值，不等价于行情质量检查。

增加 `--deep` 才逐字节执行 ZIP CRC 并计算 SHA-256：

```bash
.venv/bin/python -m kolmo.validation.minute_inventory \
  --start-year 2010 --end-year 2026 --through-month 9 --deep \
  --output /tmp/minute-inventory-deep.json
```

## 5. 原始分钟 MDCheck 的实际检查项

源码：[minute_mdcheck.py](../kolmo/validation/minute_mdcheck.py)。默认检查 1m 股票集合，可用 `--symbols` 指定样本，或 `--limit` 限制股票数量。各频率并集与缺少成员列入 archives 信息；逐股票缺少某周期形成 error。

| 检查 | 实际行为 | 严重级别 |
|---|---|---|
| 必需列 | 原始必需字段是否存在；不执行生产 Arrow schema 完全相等检查 | error |
| 空值、标签 | 必需列空值、ts_code 与目标股票、freq 与目标周期是否一致 | error |
| 时间主键 | 重复时间、升序、上海时区 | error |
| OHLC | 包络关系和正价格 | error |
| 量额 | 负量/负额；BJ 15:01–15:30 调整记录例外 | 常规负值 error；允许的盘后负调整 warning |
| 时段 | 分钟格点合法、秒为 0 | error |
| 每日根数 | 对已经出现的日期检查期望根数；BJ 按含盘后完整网格计数 | error |
| 零成交量 | 全天总量为零；所有出现日期都为零量 | 日级 info；全年网格 warning |
| 供应商跨周期 | 1m 聚合到 5/15/30/60m，与对应供应商文件逐 bar 比较；另比较各周期聚合日线 | 缺失/额外键 error；数值超容差 warning |
| 独立日线参考 | 1m 按日聚合 OHLC、vol、amount，与 profile/daily_raw 比较；可关闭 | 参考非空时，缺失键 error；数值差异 warning |

与生产入口不同，原始检查的 BJ 根数规则没有“标准 241 根也接受”的分支；例如只有标准时段的 1m BJ 日会因不满 271 根触发 error。

### 跨周期和日线对账的计算与容差

```text
每日 open   = 第一根 open
每日 high   = max(high)
每日 low    = min(low)
每日 close  = 最后一根 close
每日 vol    = sum(vol)
每日 amount = sum(amount)
```

| 字段 | 允许绝对差 |
|---|---|
| OHLC | 0.005 |
| vol | max(10 股, abs(1m 聚合值) × 1e-8) |
| amount | max(200 元, abs(1m 聚合值) × 1e-8) |

超过容差记录 warning、差异数量、最大绝对差及一个示例键。以上容差是代码判断标准，不是供应商精度已验证的结论。

日线参考默认位置为 `$KOLMO_DATA_ROOT/profile/daily_raw/{exchange}/YYYY/MM/YYYYMMDD.csv[.gz]`，字段为 `symbol,open,high,low,close,volume,amount`。代码只对 1m 已出现的日期查询参考文件。

**当前缺口：某只股票的日线参考完全为空时，代码直接跳过对账，daily_reference 留为空对象，不报缺参考错误。部分参考存在时才会进行比较并报告缺少的日期。因此“不带 --skip-daily-reference”不等于日线对账一定执行或覆盖完整。**

此外，本入口没有独立交易日历、上市/退市/停牌应有集合，也不做完整统计异常检查；不能从正常日内根数推断整日无缺失。退市嵌套包由单独读取实现支持，当前 `minute_mdcheck` 调用的是正常年/月包 `VendorMinuteDataset`，不要据此宣称已对全部退市输入做逐行验收。

## 6. 使用方式与结果

```bash
.venv/bin/python -m kolmo.validation.minute_mdcheck \
  --year 2026 \
  --symbols 000001.SZ 300001.SZ 600000.SH 600519.SH \
  --daily-root "$HOME/dat/all/profile/daily_raw" \
  --output /tmp/vendor-minute-mdcheck-2026.json
```

- 去掉 `--symbols ...`：检查该年份 1m 包中全部股票。
- `--limit N`：仅检查所选股票排序后的前 N 只，0 为不限。
- `--root`：原始供应商目录；与生产检查的 history-v1 根目录不同。
- `--skip-daily-reference`：明确关闭日线参考对账。
- `--details`：自定义 CSV 路径，默认与 JSON 同名 `.findings.csv`。

JSON 包含 archives、symbol_metrics、findings 和 summary。CSV 字段为
`symbol,frequency,check,severity,count,total,max_abs_diff,example`。
summary 中 errors/warnings/info 是 finding 数量，具体受影响行数看 count/total。

**退出码只受 error 影响：error > 0 返回 1，否则返回 0。数值对账差异属于 warning，所以退出码为 0 也可能有行情差异。** 本入口没有 `--fail-on-warning` 参数，验收时必须检查报告内容及日线对账实际覆盖情况。
