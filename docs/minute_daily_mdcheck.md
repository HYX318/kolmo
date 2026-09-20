# 生产分钟 Parquet：MDCheck 实际检查清单

更新于 2026-09-20，按当前代码实现编写。字段、目录、生产与读取命令见 [分钟数据手册](minute_data_product_design.md)。

**本入口检查生产数据的内部契约与聚合一致性。目前不执行生产分钟与独立日线的 OHLC/量额对账，不能将通过解释为完整行情准确性验收。**

## 1. 入口和输入

```text
scripts/check_vendor_minute_daily.sh
  → kolmo.validation.daily_mdcheck.run_daily_mdcheck
    → validate_year
      → kolmo.validation.minute_profile.validate_build
        → read_and_validate_partition_frame / compare_derived
          → kolmo.ashare.minute_product.validate_canonical_bars / aggregate_bars
```

源码：[daily_mdcheck.py](../kolmo/validation/daily_mdcheck.py)、[minute_profile.py](../kolmo/validation/minute_profile.py)、[minute_product.py](../kolmo/ashare/minute_product.py)。

默认根目录：`$KOLMO_DATA_ROOT/staging/minute/history-v1`。输入是该目录的 history manifest、年度 manifest、quality 报告和 `year=YYYY/profile/minute/v1/` 下所有 Parquet。不会读取供应商 ZIP，也不会修改生产数据。

## 2. 实际检查项

除单独标注 warning 外，以下失败均计为 error。

| 分类 | 实际检查 | 报告中的 check 名称 |
|---|---|---|
| 历史清单 | JSON 可读；history 状态为 validated；请求年份条目状态为 validated | `history_manifest_unreadable`, `history_status_mismatch`, `history_years_invalid`, `history_year_status_mismatch` |
| 年度范围 | 请求年份目录齐全；未指定年份时检查磁盘最早到最晚年份的连续区间 | `missing_year_directory` |
| 年度清单 | 年份等于目标年，date_range 首尾字符串以该年份开头；清单可读 | `manifest_year_mismatch`, `manifest_date_range_mismatch`, `year_manifest_unreadable` |
| 产品契约 | product_id/schema_version 正确；年度构建状态为 built_not_validated | `manifest_contract_mismatch`, `manifest_status_mismatch` |
| 修正报告 | manifest 声明修正报告时：可读，策略等于当前代码版本，计数与 manifest 一致，records 长度等于 applied_rows | `correction_report_unreadable`, `correction_report_mismatch` |
| 未解决问题 | manifest 声明 unresolved 时：报告可读，清单与报告 issues 都为 0，records 为空列表 | `unresolved_report_unreadable`, `unresolved_quality_issues` |
| 排除与观察报告 | 声明时检查策略版本、汇总计数及明细计数；排除按 records 内 excluded_rows 求和，观察按 records 长度 | `exclusion_report_*`, `observation_report_*` |
| 文件路径 | 6 层相对路径、raw、合法周期/市场、年月目录与文件名日期一致；无重复“周期/市场/日期” | `partition_validation_failed`, `duplicate_partition` |
| Arrow schema | 字段、顺序、类型、nullable 与 MINUTE_BAR_SCHEMA 完全一致 | `schema_mismatch` |
| Parquet metadata | schema/product、周期、日期、市场、raw、上海时区、end 标签、股/元单位和 build_id 一致 | `metadata_mismatch` |
| 分区非空 | 每个文件至少一行 | `empty_partition` |
| 必需数据 | 必需字段存在，无 null/NaN，(symbol, trade_time) 不重复 | `partition_validation_failed`，detail 中给出原因 |
| 排序与归属 | 按 symbol/time 升序；全部时间属于文件日期；symbol 后缀符合文件市场 | `row_order_mismatch`, `row_date_mismatch`, `row_exchange_mismatch` |
| OHLC | 全部价格 > 0；low ≤ min(open,close)，high ≥ max(open,close)，low ≤ high | `partition_validation_failed` |
| 量额 | volume、amount 非负；**BJ 15:01–15:30 例外，允许负量/负额调整记录** | `partition_validation_failed` |
| 时间标签 | 属于该周期、市场允许的小时/分钟格点，秒数为 0 | `partition_validation_failed` |
| 个股日内根数 | 默认开启，仅检查已出现股票的 1m 行数；沪深 241；BJ 接受 241 或 271，其他计数告警 | `incomplete_symbol_session`，**warning** |
| 跨周期覆盖 | 5/15/30/60m 的市场/日期分区集合与 1m 完全一致 | `frequency_partition_set_mismatch` |
| 派生主键 | 从 1m 重新聚合，目标周期的股票和时间逐行匹配 | `derived_primary_key_mismatch` |
| 派生数值 | 重新聚合的 open/high/low/close/volume/amount 与落盘值比较 | `derived_<field>_mismatch` |
| manifest 总量 | 各周期分区数、行数、字节数、首尾日期与清单相等 | `manifest_output_stats_mismatch` |
| 执行异常 | 文件读取/校验异常形成分区错误；年度异常形成年度错误 | `partition_validation_failed`, `year_validation_failed` |

清洗审计仅核对报告一致性，不重新执行全部修正，也不验证修正证据的真实性。旧年份 manifest 未声明的 quality 报告不会被强制要求存在。

`incomplete_symbol_session` 的 detail 给出异常股票数量及最多 10 个股票/根数样本，不是完整逐股票明细表。

### 派生数值容差

- `volume`：严格相等。
- OHLC 和 `amount`：`numpy.isclose(left, right, rtol=1e-12, atol=1e-9)`。
- 超出容差：error，记录差异条数和最大绝对差。

这与原始供应商对账使用的价格 0.005、量额业务容差不同。生产复算与生产生成复用同一个 `aggregate_bars()`，因此可发现文件与复算结果不一致，但不能独立发现两者共享的聚合算法错误。

## 3. 明确未覆盖的检查

| 未覆盖项 | 当前限制 |
|---|---|
| 独立日线 OHLC/量额 | **没有检查 max(分钟 high)=日线 high、min(分钟 low)=日线 low，也没有对账日开收和成交量/额** |
| 独立交易日历 | 若整天在全部周期和 manifest 中一起缺失，内部一致性检查可能通过 |
| 个股整日缺失 | 只对已经存在的股票/日期计根数，不结合上市、退市、停牌清单建立应有集合 |
| 全市场/退市覆盖 | 不能仅凭正常网格证明股票全集和退市历史完整 |
| 09:30 业务语义 | 不验证是否仅为集合竞价，也不要求该 bar 的 OHLC 相同 |
| 零量及统计异常 | 不检测全天零量、零量变价、正量零额、长时间恒价、异常收益或涨跌停越界 |
| 成交均价合理性 | 不检查 amount/volume 是否落在 low/high 范围内 |
| 有限值/亚秒 | 无显式 ±inf 检查；检查秒为 0，但未独立检查毫秒为 0 |
| 来源守恒及真实性 | 不重读原始 ZIP 逐行核验生产结果，不执行 ZIP CRC/SHA-256，也不证明修复后的价格是真值 |
| 复权因子 | 当前只检查 raw 产品；不验证因子或复权结果 |

年度目录连贯和日内根数完整不等于独立交易日覆盖完整。manifest 总量一致也不等于与源数据守恒。

## 4. 运行方法

在项目根目录运行。包装脚本加载 `.env` 并使用项目 `.venv/bin/python`。

```bash
caffeinate -i ./scripts/check_vendor_minute_daily.sh \
  --root "$HOME/dat/all/staging/minute/history-v1" \
  --start-year 2010 --end-year 2026 --workers 4 \
  --output /tmp/production-minute-mdcheck.json
```

`caffeinate -i` 是 macOS 防休眠包装，其他系统直接运行脚本。

只检查 2026 年：

```bash
./scripts/check_vendor_minute_daily.sh \
  --start-year 2026 --end-year 2026 --workers 4 \
  --output /tmp/production-minute-mdcheck-2026.json
```

| 参数 | 含义 |
|---|---|
| `--root` | history-v1 根目录，不是 year=YYYY 目录 |
| `--start-year / --end-year` | 检查年份；都省略时自动发现连续范围 |
| `--workers` | 默认至多 4；多个年份按年份并行，单一年份按日/市场分区并行 |
| `--check-symbol-sessions` | 默认开启个股 1m 日内根数检查 |
| `--no-check-symbol-sessions` | 关闭这一项，其余检查继续执行 |
| `--fail-on-warning` | warning 也导致非零退出码 |
| `--output` | JSON 路径；省略时写入 root/validation/daily-mdcheck.json |
| `--details` | CSV 路径；省略时用 JSON 同名 .findings.csv |

该入口当前无股票、日内时间或交易日范围筛选，最小请求单位是年份。

单批构建不具有 history-manifest，可使用：

```bash
./scripts/validate_vendor_minute_profile.sh \
  --build-root /tmp/kolmo-minute-202608-build \
  --output /tmp/single-build-validation.json \
  --details /tmp/single-build-validation.findings.csv
```

单批 CLI 的默认 `check_symbol_sessions=False`，且未暴露开启参数；也不检查顶层历史清单。不要将它的报告与默认开启 session 检查的全历史入口混为一谈。

## 5. 报告和结果解释

JSON 包含 `generated_at`、`requested_range`、`settings`、逐年 `years`、`findings` 和汇总 `summary`。汇总的 errors/warnings 是问题记录数，不是坏行数；`daily_partitions_checked` 是“市场 × 日期”数量，不是独立交易日数。

CSV 字段为 `year,severity,check,scope,detail`；scope 定位文件或分区，detail 说明原因。

退出码：无 error 时为 0；有 error 时为 1；启用 `--fail-on-warning` 后，有 warning 也为 1。参数或运行环境异常也可能非零退出，应同时查看 stderr 和报告是否成功生成。

2026-09-08 留存报告检查 17 年、45,235 个文件、9,047 个市场/日期分区，结果为 2 error、0 warning：2011/2012 的修正报告版本分别为 v1/v2，而检查器要求 v3，计数分别为 12/76 且一致。其余检查没有报告异常，不代表第 3 节列出的项目已经通过。

不要仅为使报告归零修改审计版本号；应核对历史构建所用规则及修正明细，再决定兼容旧策略或重新构建。
