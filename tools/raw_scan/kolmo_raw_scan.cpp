#include <zlib.h>

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace fs = std::filesystem;

namespace {

struct Stats {
    std::uint64_t files = 0;
    std::uint64_t rows = 0;
    std::uint64_t bytes = 0;
    std::string first_date;
    std::string last_date;
};

bool ends_with(std::string_view value, std::string_view suffix) {
    return value.size() >= suffix.size() &&
           value.substr(value.size() - suffix.size()) == suffix;
}

bool is_raw_csv_path(const fs::path& path) {
    const std::string name = path.filename().string();
    return ends_with(name, ".csv") || ends_with(name, ".csv.gz");
}

std::string first_field(const std::string& line) {
    const auto pos = line.find(',');
    return pos == std::string::npos ? line : line.substr(0, pos);
}

void update_date_range(Stats& stats, const std::string& date) {
    if (date.empty() || date == "date") {
        return;
    }
    if (stats.first_date.empty() || date < stats.first_date) {
        stats.first_date = date;
    }
    if (stats.last_date.empty() || date > stats.last_date) {
        stats.last_date = date;
    }
}

void scan_plain_file(const fs::path& path, Stats& stats) {
    std::ifstream input(path);
    if (!input.is_open()) {
        throw std::runtime_error("could not open " + path.string());
    }

    std::string line;
    bool header = true;
    while (std::getline(input, line)) {
        stats.bytes += line.size() + 1;
        if (header) {
            header = false;
            continue;
        }
        if (line.empty()) {
            continue;
        }
        ++stats.rows;
        update_date_range(stats, first_field(line));
    }
}

void scan_gzip_file(const fs::path& path, Stats& stats) {
    gzFile file = gzopen(path.string().c_str(), "rb");
    if (file == nullptr) {
        throw std::runtime_error("could not open gzip " + path.string());
    }

    std::string line;
    line.reserve(512);
    bool header = true;
    char buffer[1 << 15];

    while (gzgets(file, buffer, static_cast<int>(sizeof(buffer))) != nullptr) {
        line.assign(buffer);
        while (!line.empty() && line.back() != '\n' && !gzeof(file)) {
            if (gzgets(file, buffer, static_cast<int>(sizeof(buffer))) == nullptr) {
                break;
            }
            line += buffer;
        }

        stats.bytes += line.size();
        if (!line.empty() && line.back() == '\n') {
            line.pop_back();
        }
        if (!line.empty() && line.back() == '\r') {
            line.pop_back();
        }
        if (header) {
            header = false;
            continue;
        }
        if (line.empty()) {
            continue;
        }
        ++stats.rows;
        update_date_range(stats, first_field(line));
    }

    const int close_result = gzclose(file);
    if (close_result != Z_OK) {
        throw std::runtime_error("gzip close failed for " + path.string());
    }
}

std::vector<fs::path> collect_files(const fs::path& root) {
    std::vector<fs::path> files;
    for (const auto& entry : fs::recursive_directory_iterator(root)) {
        if (!entry.is_regular_file()) {
            continue;
        }
        if (is_raw_csv_path(entry.path())) {
            files.push_back(entry.path());
        }
    }
    std::sort(files.begin(), files.end());
    return files;
}

void usage() {
    std::cerr << "Usage: kolmo_raw_scan <raw-cache-dir> [--limit N]\n";
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        usage();
        return 1;
    }

    fs::path root = argv[1];
    std::uint64_t limit = 0;
    for (int i = 2; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--limit" && i + 1 < argc) {
            limit = std::stoull(argv[++i]);
        } else {
            usage();
            return 1;
        }
    }

    try {
        Stats stats;
        auto files = collect_files(root);
        if (limit > 0 && files.size() > limit) {
            files.resize(static_cast<std::size_t>(limit));
        }

        for (const auto& path : files) {
            if (ends_with(path.filename().string(), ".gz")) {
                scan_gzip_file(path, stats);
            } else {
                scan_plain_file(path, stats);
            }
            ++stats.files;
        }

        std::cout << "files=" << stats.files << "\n";
        std::cout << "rows=" << stats.rows << "\n";
        std::cout << "bytes_uncompressed_approx=" << stats.bytes << "\n";
        std::cout << "first_date=" << stats.first_date << "\n";
        std::cout << "last_date=" << stats.last_date << "\n";
    } catch (const std::exception& ex) {
        std::cerr << "Error: " << ex.what() << "\n";
        return 1;
    }

    return 0;
}

