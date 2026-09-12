# 按日分钟产品 MDCheck

`kolmo.validation.daily_mdcheck` 对已经生产到 `history-v1/year=YYYY` 的按日 Parquet
产品做独立、只读的全历史复检。它不读取供应商 ZIP，也不会修改生产数据。

## 检查范围

- history manifest、年度 manifest、质量审计文件的一致性；
- 年份连续性以及年度目录完整性；
- Parquet schema、Kolmo metadata、路径中的频率/市场/日期；
- 空分区、空值、重复主键、行排序、日期和市场归属；
- OHLC 包络、正价格、成交量/成交额以及交易时段；
- 每个股票每日应有的交易时段格点数；
- 5/15/30/60 分钟分区集合与 1 分钟是否一致；
- 从 1 分钟重新聚合出的 OHLCV 是否与各高频率文件精确一致；
- manifest 声明的分区数、行数、字节数和日期范围是否与磁盘一致。

日内格点不完整记为 `warning`，用于识别上市首日、停牌或供应商缺口。北交所允许
标准 241 根，或带完整 15:01–15:30 供应商盘后调整网格的 271 根；部分出现的盘后
网格仍会报警。其余会破坏产品契约或数值正确性的项目记为 `error`。

## 全历史执行

```bash
caffeinate -i ./scripts/check_vendor_minute_daily.sh \
  --start-year 2010 \
  --end-year 2026 \
  --workers 4 \
  --output /tmp/minute-daily-mdcheck.json
```

默认根目录是 `$KOLMO_DATA_ROOT/staging/minute/history-v1`。指定其他位置：

```bash
python3 -m kolmo.validation.daily_mdcheck \
  --root /path/to/history-v1 \
  --workers 4 \
  --output /tmp/minute-daily-mdcheck.json
```

不传年份时自动检查磁盘上最早到最晚年份组成的连续区间。默认 error 导致退出码 1；
如需将 warning 也作为发布门禁，增加 `--fail-on-warning`。如只需产品契约检查而不做
逐股票日内完整性分析，可增加 `--no-check-symbol-sessions`。

主报告为 JSON；问题明细默认写到同目录下的
`minute-daily-mdcheck.findings.csv`。

## 并行与资源模型

并行单位是年份。每个 worker 在年份内依次读取日分区，因此内存中只保留少量单日
DataFrame；同时避免为数万个小文件创建独立进程任务。SSD 本机建议从 `--workers 4`
开始，若磁盘吞吐已满，继续增加 worker 通常不会更快。
