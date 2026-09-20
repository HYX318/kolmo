# 生产分钟行情 C++ 查看工具

`kolmo_minute_read` 直接读取 `history-v1` 中已生产的 Parquet，默认在控制台打印
对齐的表格，默认最多显示 100 行。不会读取供应商 ZIP，也不会修改行情。

## 编译

需要 C++17 编译器、CMake，以及项目 `.venv` 中的 PyArrow 21 或更新版本。
工具复用 PyArrow 附带的 Arrow/Parquet C++ 头文件和动态库；Python 仅在 CMake
配置阶段用于定位依赖，运行读取工具时不启动 Python。

在项目根目录执行：

```bash
cmake -S tools/minute_read -B build/minute_read -DCMAKE_BUILD_TYPE=Release
cmake --build build/minute_read -j 4
```

本机默认 Xcode 尚未接受许可，但已安装独立 Command Line Tools，可使用以下已验证命令：

```bash
export DEVELOPER_DIR=/Library/Developer/CommandLineTools
cmake -S tools/minute_read -B build/minute_read \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CXX_COMPILER=/Library/Developer/CommandLineTools/usr/bin/clang++ \
  -DCMAKE_OSX_SYSROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk
cmake --build build/minute_read -j 4
```

使用其他 Python 环境时，配置时增加 `-DKOLMO_PYTHON=/absolute/path/to/python`。
可执行文件依赖该环境中的动态库；移动或升级环境后应重新配置并编译。

## 控制台查看

查看某只股票当天的 1 分钟数据：

```bash
./build/minute_read/kolmo_minute_read \
  --date 2026-09-04 --symbol 600519.SH
```

按日期范围、股票和日内时间筛选，查看全部匹配的 5 分钟行情：

```bash
./build/minute_read/kolmo_minute_read \
  --start-date 2026-09-01 --end-date 2026-09-04 \
  --symbol 600519.SH,000001.SZ \
  --frequency 5 \
  --start-time 09:30 --end-time 10:30 \
  --limit 0
```

查看沪市某天指定时间的全市场行情：

```bash
./build/minute_read/kolmo_minute_read \
  --date 20260904 --exchange sh \
  --start-time 09:31 --end-time 09:31 --limit 20
```

参数说明：

| 参数 | 含义 |
|---|---|
| `--root PATH` | 包含 `year=YYYY` 的 `history-v1` 根目录 |
| `--date DATE` | 单日，与起止日期参数互斥 |
| `--start-date DATE --end-date DATE` | 日期范围，包含两端；接受 `YYYY-MM-DD` 或 `YYYYMMDD` |
| `--symbol CODE[,CODE...]` | 完整股票代码；可重复传参；省略表示所有股票 |
| `--exchange sh/sz/bj` | 可重复；默认根据代码推断，无代码时查询三市 |
| `--frequency N` | `1/5/15/30/60`，默认 1 分钟 |
| `--start-time / --end-time` | 上海时间 `HH:MM` 或 `HH:MM:SS`，包含两端，每天分别应用 |
| `--limit N` | 最多打印 N 行，默认 100；0 表示全部。达到上限立即停止扫描 |
| `--threads N` | Arrow 列解码线程数，默认 4，允许 1–256 |
| `--csv` | 将标准输出改为 CSV，便于重定向或后续处理 |
| `--help` | 显示帮助 |

根目录默认取 `$KOLMO_DATA_ROOT/staging/minute/history-v1`，未设置环境变量时取
`$HOME/dat/all/staging/minute/history-v1`。工具不会自动读取项目 `.env`。

输出字段为 `symbol, trade_time, open, high, low, close, volume, amount`。
时间是 `Asia/Shanghai` 的 bar 结束时间，价格未复权，成交量单位为股，成交额单位为元。
表格数值显示 12 位有效数字；CSV 使用 17 位有效数字以保留 double 精度。

输出顺序为日期、市场、文件内的股票/时间顺序，不是跨股票的全局时间排序。
诊断统计写入 stderr，不混入 stdout 的 CSV。达到 `--limit` 后统计是部分扫描结果，
不能用 `rows` 当作全部匹配行数。

## 性能与边界

- 日期、周期和市场裁剪：仅枚举对应年月目录。
- 股票裁剪：利用 Parquet 行组的 symbol 最小/最大值跳过不相关行组；无统计时正常扫描。
- 每批最多 65,536 行，逐文件流式处理，支持 Arrow 并行列解码。
- 股票和日内时间在读取批次后逐行精确过滤；时间范围目前不会额外裁剪行组。
- 全量终端打印可能比读取更慢；快速查看应保留行数上限。
- 无匹配分区返回退出码 1；有分区但无匹配股票/时间时打印表头并提示，返回 0。
- 日期范围中不存在的分区会跳过；不区分休市和数据缺失。这是读取工具，不代替 MDCheck。
- 只支持当前生产 schema（毫秒精度、上海时区等），不兼容 schema 会明确报错。
- 单股多年查询仍需打开多个日期文件，不提供股票索引或查询缓存。

本机验证：2026-09-03 至 09-04、沪深两只股票、全部五种周期、日内筛选的 CSV
与 PyArrow 直接读取结果逐字段一致；另外检查了行数上限、空结果及非法参数。
