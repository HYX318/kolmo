#include <arrow/api.h>
#include <arrow/io/api.h>
#include <arrow/util/thread_pool.h>
#include <parquet/arrow/reader.h>
#include <parquet/file_reader.h>
#include <parquet/statistics.h>

#include <algorithm>
#include <chrono>
#include <cctype>
#include <cstdlib>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <regex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <tuple>
#include <vector>

namespace fs = std::filesystem;
template <class T> T unwrap(arrow::Result<T> result) {
  if (!result.ok()) throw std::runtime_error(result.status().ToString());
  return std::move(result).ValueOrDie();
}
void check(arrow::Status status) {
  if (!status.ok()) throw std::runtime_error(status.ToString());
}
const std::vector<std::string> columns = {
    "symbol", "trade_time", "open", "high", "low", "close", "volume", "amount"};

std::string date(std::string value) {
  if (std::regex_match(value, std::regex("[0-9]{4}-[0-9]{2}-[0-9]{2}"))) {
    value.erase(7, 1); value.erase(4, 1);
  }
  if (!std::regex_match(value, std::regex("[0-9]{8}")))
    throw std::runtime_error("Date must be YYYY-MM-DD or YYYYMMDD");
  int y = std::stoi(value.substr(0, 4)), m = std::stoi(value.substr(4, 2));
  int d = std::stoi(value.substr(6, 2));
  int days[] = {0,31,28,31,30,31,30,31,31,30,31,30,31};
  if (y % 4 == 0 && (y % 100 != 0 || y % 400 == 0)) days[2] = 29;
  if (y < 1970 || y > 9999 || m < 1 || m > 12 || d < 1 || d > days[m])
    throw std::runtime_error("Invalid calendar date: " + value);
  return value;
}
int clock_time(const std::string& value) {
  if (!std::regex_match(value, std::regex("[0-9]{2}:[0-9]{2}(:[0-9]{2})?")))
    throw std::runtime_error("Time must be HH:MM or HH:MM:SS");
  int h = std::stoi(value.substr(0,2)), m = std::stoi(value.substr(3,2));
  int s = value.size() == 8 ? std::stoi(value.substr(6,2)) : 0;
  if (h > 23 || m > 59 || s > 59) throw std::runtime_error("Invalid time: " + value);
  return h * 3600 + m * 60 + s;
}
long long number(const std::string& value) {
  if (!std::regex_match(value, std::regex("[0-9]+")))
    throw std::runtime_error("Expected a nonnegative integer: " + value);
  return std::stoll(value);
}
struct Options {
  fs::path root;
  std::string start, end;
  std::set<std::string> symbols, exchanges;
  int frequency = 1, from = 0, to = 86399, threads = 4;
  long long limit = 100;
  bool csv = false;
};
void help() {
  std::cout << R"(Read Kolmo production minute Parquet; print to console.
Usage: kolmo_minute_read --date 2026-09-04 --symbol 600519.SH [options]
  --root PATH                 history-v1 directory (default: $KOLMO_DATA_ROOT/staging/minute/history-v1
                              or $HOME/dat/all/staging/minute/history-v1)
  --date YYYY-MM-DD            Single date (also accepts YYYYMMDD)
  --start-date DATE --end-date DATE   Inclusive date range; required unless --date
  --symbol CODE[,CODE...]      Full symbols, e.g. 600519.SH,000001.SZ (repeatable)
                              Omit to read all stocks in selected exchanges
  --exchange sh|sz|bj          Repeatable; default derived from symbols, otherwise all
  --frequency 1|5|15|30|60     Minutes, default 1
  --start-time HH:MM[:SS]      Inclusive local bar end time, applied every day
  --end-time HH:MM[:SS]        Inclusive local bar end time, applied every day
  --limit N                   Maximum printed rows, default 100; 0 prints all
  --threads N                 Arrow decoding threads, default 4
  --csv                       Print CSV instead of aligned table to stdout
  --help                      Show help
Times use Asia/Shanghai; prices are raw; volume=shares, amount=CNY.
Rows are ordered by date, exchange, then stored symbol/time order.
Absent dates are skipped (no trading-calendar completeness check).
)";
}
Options parse(int argc, char** argv) {
  Options o;
  const char* data = std::getenv("KOLMO_DATA_ROOT");
  const char* home = std::getenv("HOME");
  o.root = (data ? fs::path(data) : fs::path(home ? home : ".") / "dat/all") /
      "staging/minute/history-v1";
  std::string single;
  for (int i = 1; i < argc; ++i) {
    std::string key = argv[i];
    if (key == "--help") { help(); std::exit(0); }
    if (key == "--csv") { o.csv = true; continue; }
    if (i + 1 >= argc) throw std::runtime_error("Missing value for " + key);
    std::string value = argv[++i];
    if (key == "--root") o.root = value;
    else if (key == "--date") single = date(value);
    else if (key == "--start-date") o.start = date(value);
    else if (key == "--end-date") o.end = date(value);
    else if (key == "--start-time") o.from = clock_time(value);
    else if (key == "--end-time") o.to = clock_time(value);
    else if (key == "--limit") o.limit = number(value);
    else if (key == "--threads") {
      auto n = number(value);
      if (n < 1 || n > 256) throw std::runtime_error("threads must be 1..256");
      o.threads = static_cast<int>(n);
    } else if (key == "--frequency") {
      auto n = number(value);
      if (n != 1 && n != 5 && n != 15 && n != 30 && n != 60)
        throw std::runtime_error("frequency must be 1,5,15,30,60");
      o.frequency = static_cast<int>(n);
    } else if (key == "--exchange") {
      if (value != "sh" && value != "sz" && value != "bj")
        throw std::runtime_error("exchange must be sh, sz or bj");
      o.exchanges.insert(value);
    } else if (key == "--symbol") {
      std::stringstream parts(value);
      std::string symbol;
      if (value.empty() || value.back() == ',') throw std::runtime_error("Empty symbol");
      while (std::getline(parts, symbol, ',')) {
        std::transform(symbol.begin(), symbol.end(), symbol.begin(),
                       [](unsigned char c) { return std::toupper(c); });
        if (!std::regex_match(symbol, std::regex("[0-9]{6}\\.(SH|SZ|BJ)")))
          throw std::runtime_error("Use full stock code, e.g. 600519.SH: " + symbol);
        o.symbols.insert(symbol);
      }
    } else throw std::runtime_error("Unknown argument: " + key);
  }
  if (!single.empty()) {
    if (!o.start.empty() || !o.end.empty())
      throw std::runtime_error("Use --date OR --start-date/--end-date");
    o.start = o.end = single;
  }
  if (o.start.empty() || o.end.empty() || o.start > o.end)
    throw std::runtime_error("Supply a valid date or inclusive date range");
  if (o.from > o.to) throw std::runtime_error("start-time must not exceed end-time");
  std::set<std::string> symbol_exchanges;
  for (const auto& symbol : o.symbols) {
    std::string e = symbol.substr(7);
    std::transform(e.begin(), e.end(), e.begin(), [](unsigned char c) { return std::tolower(c); });
    symbol_exchanges.insert(e);
  }
  if (o.exchanges.empty())
    o.exchanges = symbol_exchanges.empty() ? std::set<std::string>{"sh","sz","bj"} : symbol_exchanges;
  for (const auto& e : symbol_exchanges)
    if (!o.exchanges.count(e)) throw std::runtime_error("Symbol conflicts with --exchange");
  if (!fs::is_directory(o.root)) throw std::runtime_error("Missing root: " + o.root.string());
  return o;
}
struct Partition { std::string day, exchange; fs::path path; };
std::vector<Partition> discover(const Options& o) {
  std::vector<Partition> files;
  for (int y = std::stoi(o.start.substr(0,4)); y <= std::stoi(o.end.substr(0,4)); ++y) {
    auto year = std::to_string(y);
    for (const auto& exchange : o.exchanges) {
      auto base = o.root / ("year=" + year) / "profile/minute/v1" /
          (std::to_string(o.frequency) + "m") / "raw" / exchange / year;
      for (int month = 1; month <= 12; ++month) {
        std::string mm = (month < 10 ? "0" : "") + std::to_string(month);
        if (year + mm < o.start.substr(0,6) || year + mm > o.end.substr(0,6)) continue;
        if (!fs::is_directory(base / mm)) continue;
        for (const auto& entry : fs::directory_iterator(base / mm)) {
          if (!entry.is_regular_file() || entry.path().extension() != ".parquet") continue;
          auto day = entry.path().stem().string();
          if (day >= o.start && day <= o.end && day.substr(0,6) == year + mm)
            files.push_back({day, exchange, entry.path()});
        }
      }
    }
  }
  std::sort(files.begin(), files.end(), [](const Partition& a, const Partition& b) {
    return std::tie(a.day,a.exchange) < std::tie(b.day,b.exchange);
  });
  return files;
}
std::string bytes(parquet::ByteArray value) {
  return std::string(reinterpret_cast<const char*>(value.ptr), value.len);
}
void print_row(const std::vector<std::string>& values, bool csv) {
  const int widths[] = {12,21,15,15,15,15,16,22};
  for (size_t i = 0; i < values.size(); ++i) {
    if (csv) { if (i) std::cout << ','; std::cout << values[i]; }
    else std::cout << (i < 2 ? std::left : std::right) << std::setw(widths[i]) << values[i] << ' ';
  }
  std::cout << '\n';
}
int main(int argc, char** argv) {
  try {
    if (argc == 1) { help(); return 0; }
    auto o = parse(argc, argv);
    check(arrow::SetCpuThreadPoolCapacity(o.threads));
    auto started = std::chrono::steady_clock::now();
    auto files = discover(o);
    if (files.empty()) throw std::runtime_error("No production partitions found for the requested range/market/frequency");
    long long printed = 0, scanned = 0, skipped = 0, total_groups = 0, opened = 0;
    bool stopped = false;
    print_row(columns, o.csv);
    for (const auto& file : files) {
      auto input = unwrap(arrow::io::ReadableFile::Open(file.path.string()));
      auto reader = unwrap(parquet::arrow::OpenFile(input, arrow::default_memory_pool()));
      ++opened;
      reader->set_use_threads(o.threads > 1);
      reader->set_batch_size(65536);
      std::shared_ptr<arrow::Schema> schema;
      check(reader->GetSchema(&schema));
      auto expected = arrow::schema({arrow::field("symbol",arrow::utf8(),false),
          arrow::field("trade_time",arrow::timestamp(arrow::TimeUnit::MILLI,"Asia/Shanghai"),false),
          arrow::field("open",arrow::float64(),false), arrow::field("high",arrow::float64(),false),
          arrow::field("low",arrow::float64(),false), arrow::field("close",arrow::float64(),false),
          arrow::field("volume",arrow::int64(),false), arrow::field("amount",arrow::float64(),false)});
      if (!schema->Equals(*expected, false))
        throw std::runtime_error("Unexpected production schema: " + file.path.string());
      auto metadata = reader->parquet_reader()->metadata();
      std::vector<int> groups;
      total_groups += metadata->num_row_groups();
      for (int g = 0; g < metadata->num_row_groups(); ++g) {
        auto stats = std::dynamic_pointer_cast<parquet::ByteArrayStatistics>(
            metadata->RowGroup(g)->ColumnChunk(0)->statistics());
        if (!o.symbols.empty() && stats && stats->HasMinMax()) {
          auto symbol = o.symbols.lower_bound(bytes(stats->min()));
          if (symbol == o.symbols.end() || *symbol > bytes(stats->max())) { ++skipped; continue; }
        }
        groups.push_back(g);
      }
      if (groups.empty()) continue;
      auto batches = unwrap(reader->GetRecordBatchReader(groups));
      while (auto batch = unwrap(batches->Next())) {
        for (const auto& col : batch->columns())
          if (col->null_count()) throw std::runtime_error("Null values in production partition: " + file.path.string());
        auto symbols = std::static_pointer_cast<arrow::StringArray>(batch->column(0));
        auto times = std::static_pointer_cast<arrow::TimestampArray>(batch->column(1));
        for (int64_t i = 0; i < batch->num_rows(); ++i) {
          ++scanned;
          std::string symbol(symbols->GetView(i));
          if (!o.symbols.empty() && !o.symbols.count(symbol)) continue;
          int second = static_cast<int>(((times->Value(i) / 1000 + 8 * 3600) % 86400 + 86400) % 86400);
          if (second < o.from || second > o.to) continue;
          std::ostringstream time;
          time << file.day.substr(0,4) << '-' << file.day.substr(4,2) << '-' << file.day.substr(6,2)
               << ' ' << std::setfill('0') << std::setw(2) << second / 3600 << ':'
               << std::setw(2) << second / 60 % 60 << ':' << std::setw(2) << second % 60;
          std::vector<std::string> values{symbol, time.str()};
          for (int c = 2; c < 8; ++c) {
            std::ostringstream value;
            if (c == 6) value << std::static_pointer_cast<arrow::Int64Array>(batch->column(c))->Value(i);
            else value << std::setprecision(o.csv ? 17 : 12)
                       << std::static_pointer_cast<arrow::DoubleArray>(batch->column(c))->Value(i);
            values.push_back(value.str());
          }
          print_row(values, o.csv);
          if (++printed == o.limit && o.limit > 0) { stopped = true; break; }
        }
        if (stopped) break;
      }
      if (stopped) break;
    }
    std::cout.flush();
    if (!std::cout) throw std::runtime_error("Failed to write stdout");
    double elapsed = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
    std::cerr << "rows=" << printed << " files_opened=" << opened << '/' << files.size()
              << " row_groups_skipped=" << skipped << '/' << total_groups
              << " rows_examined=" << scanned << " seconds=" << std::fixed << std::setprecision(3) << elapsed
              << (stopped ? " limit_reached=true (scan stopped early)" : "") << '\n';
    if (!printed) std::cerr << "No matching rows. Missing dates are not checked against a trading calendar.\n";
    return 0;
  } catch (const std::exception& e) {
    std::cerr << "error: " << e.what() << '\n'; return 1;
  }
}
