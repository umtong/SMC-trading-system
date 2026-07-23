#include <algorithm>
#include <array>
#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <queue>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace {

constexpr long long kBoundaryMs = 15LL * 60LL * 1000LL;
constexpr std::array<long long, 3> kWindowsMs{10000LL, 30000LL, 60000LL};

struct Stats {
    long long trades = 0;
    long long buy_trades = 0;
    long long sell_trades = 0;
    double buy_quote = 0.0;
    double sell_quote = 0.0;
    double buy_qty = 0.0;
    double sell_qty = 0.0;
    double sum_price_qty = 0.0;
    double sum_qty = 0.0;
    double first_price = std::numeric_limits<double>::quiet_NaN();
    double last_price = std::numeric_limits<double>::quiet_NaN();
    double high = -std::numeric_limits<double>::infinity();
    double low = std::numeric_limits<double>::infinity();
    double max_trade_quote = 0.0;
    long long first_ts = -1;
    long long last_ts = -1;
    int first_side = 0;
    std::priority_queue<double, std::vector<double>, std::greater<double>> top5;

    void add(long long ts, double price, double qty, bool buyer_maker) {
        if (!(price > 0.0) || !(qty > 0.0)) return;
        const double quote = price * qty;
        const bool taker_buy = !buyer_maker;
        if (trades == 0) {
            first_price = price;
            first_ts = ts;
            first_side = taker_buy ? 1 : -1;
        }
        ++trades;
        if (taker_buy) {
            ++buy_trades;
            buy_quote += quote;
            buy_qty += qty;
        } else {
            ++sell_trades;
            sell_quote += quote;
            sell_qty += qty;
        }
        sum_price_qty += price * qty;
        sum_qty += qty;
        last_price = price;
        last_ts = ts;
        high = std::max(high, price);
        low = std::min(low, price);
        max_trade_quote = std::max(max_trade_quote, quote);
        if (top5.size() < 5) {
            top5.push(quote);
        } else if (quote > top5.top()) {
            top5.pop();
            top5.push(quote);
        }
    }

    double total_quote() const { return buy_quote + sell_quote; }
    double net_quote() const { return buy_quote - sell_quote; }
    double imbalance() const {
        const double total = total_quote();
        return total > 0.0 ? net_quote() / total : std::numeric_limits<double>::quiet_NaN();
    }
    double vwap() const {
        return sum_qty > 0.0 ? sum_price_qty / sum_qty : std::numeric_limits<double>::quiet_NaN();
    }
    double log_return() const {
        return trades > 0 && first_price > 0.0 && last_price > 0.0
            ? std::log(last_price / first_price)
            : std::numeric_limits<double>::quiet_NaN();
    }
    double range_bps() const {
        const double mid = vwap();
        return trades > 0 && mid > 0.0 ? 10000.0 * (high - low) / mid
                                      : std::numeric_limits<double>::quiet_NaN();
    }
    double top5_share() const {
        const double total = total_quote();
        if (!(total > 0.0)) return std::numeric_limits<double>::quiet_NaN();
        auto copy = top5;
        double sum = 0.0;
        while (!copy.empty()) {
            sum += copy.top();
            copy.pop();
        }
        return sum / total;
    }
};

struct Boundary {
    std::array<Stats, 3> post;
    std::array<Stats, 3> pre;
};

long long utc_epoch_ms(int year, int month, int day = 1) {
    std::tm tm{};
    tm.tm_year = year - 1900;
    tm.tm_mon = month - 1;
    tm.tm_mday = day;
    tm.tm_hour = 0;
    tm.tm_min = 0;
    tm.tm_sec = 0;
    const std::time_t seconds = timegm(&tm);
    if (seconds == static_cast<std::time_t>(-1)) {
        throw std::runtime_error("timegm failed");
    }
    return static_cast<long long>(seconds) * 1000LL;
}

std::vector<std::string_view> split_csv(const std::string& line) {
    std::vector<std::string_view> fields;
    fields.reserve(8);
    std::size_t start = 0;
    for (std::size_t i = 0; i <= line.size(); ++i) {
        if (i == line.size() || line[i] == ',') {
            fields.emplace_back(line.data() + start, i - start);
            start = i + 1;
        }
    }
    return fields;
}

bool parse_i64(std::string_view text, long long& value) {
    if (text.empty()) return false;
    std::string copy(text);
    char* end = nullptr;
    errno = 0;
    const long long parsed = std::strtoll(copy.c_str(), &end, 10);
    if (errno != 0 || end == copy.c_str() || *end != '\0') return false;
    value = parsed;
    return true;
}

bool parse_double(std::string_view text, double& value) {
    if (text.empty()) return false;
    std::string copy(text);
    char* end = nullptr;
    errno = 0;
    const double parsed = std::strtod(copy.c_str(), &end);
    if (errno != 0 || end == copy.c_str() || *end != '\0' || !std::isfinite(parsed)) return false;
    value = parsed;
    return true;
}

bool parse_bool(std::string_view text, bool& value) {
    if (text == "true" || text == "True" || text == "TRUE" || text == "1") {
        value = true;
        return true;
    }
    if (text == "false" || text == "False" || text == "FALSE" || text == "0") {
        value = false;
        return true;
    }
    return false;
}

void write_number(std::ostream& out, double value) {
    if (std::isfinite(value)) out << std::setprecision(17) << value;
}

void write_stats_header(std::ostream& out, const std::string& prefix) {
    out << ',' << prefix << "_trades"
        << ',' << prefix << "_buy_trades"
        << ',' << prefix << "_sell_trades"
        << ',' << prefix << "_total_quote"
        << ',' << prefix << "_buy_quote"
        << ',' << prefix << "_sell_quote"
        << ',' << prefix << "_net_quote"
        << ',' << prefix << "_imbalance"
        << ',' << prefix << "_buy_qty"
        << ',' << prefix << "_sell_qty"
        << ',' << prefix << "_first_price"
        << ',' << prefix << "_last_price"
        << ',' << prefix << "_vwap"
        << ',' << prefix << "_high"
        << ',' << prefix << "_low"
        << ',' << prefix << "_log_return"
        << ',' << prefix << "_range_bps"
        << ',' << prefix << "_first_latency_ms"
        << ',' << prefix << "_last_offset_ms"
        << ',' << prefix << "_first_side"
        << ',' << prefix << "_max_trade_quote"
        << ',' << prefix << "_top5_share";
}

void write_stats(std::ostream& out, const Stats& s, long long boundary_ms, bool pre) {
    out << ',' << s.trades
        << ',' << s.buy_trades
        << ',' << s.sell_trades;
    const std::array<double, 16> values{
        s.total_quote(), s.buy_quote, s.sell_quote, s.net_quote(), s.imbalance(),
        s.buy_qty, s.sell_qty, s.first_price, s.last_price, s.vwap(), s.high, s.low,
        s.log_return(), s.range_bps(), s.max_trade_quote, s.top5_share()
    };
    for (std::size_t i = 0; i < values.size(); ++i) {
        if (i == 14) break;
        out << ',';
        write_number(out, values[i]);
    }
    out << ',';
    if (s.first_ts >= 0) {
        out << (pre ? boundary_ms - s.first_ts : s.first_ts - boundary_ms);
    }
    out << ',';
    if (s.last_ts >= 0) {
        out << (pre ? boundary_ms - s.last_ts : s.last_ts - boundary_ms);
    }
    out << ',' << s.first_side
        << ',';
    write_number(out, s.max_trade_quote);
    out << ',';
    write_number(out, s.top5_share());
}

}  // namespace

int main(int argc, char** argv) {
    if (argc != 5) {
        std::cerr << "usage: extract_boundary_aggtrades SYMBOL YYYY-MM ZIP_PATH OUTPUT_CSV\n";
        return 2;
    }
    const std::string symbol = argv[1];
    const std::string month_text = argv[2];
    const std::string zip_path = argv[3];
    const std::string output_path = argv[4];
    if (month_text.size() != 7 || month_text[4] != '-') {
        throw std::runtime_error("month must be YYYY-MM");
    }
    const int year = std::stoi(month_text.substr(0, 4));
    const int month = std::stoi(month_text.substr(5, 2));
    if (month < 1 || month > 12) throw std::runtime_error("invalid month");
    const int next_year = month == 12 ? year + 1 : year;
    const int next_month = month == 12 ? 1 : month + 1;
    const long long start_ms = utc_epoch_ms(year, month);
    const long long end_ms = utc_epoch_ms(next_year, next_month);
    const std::size_t boundary_count = static_cast<std::size_t>((end_ms - start_ms) / kBoundaryMs);
    std::vector<Boundary> boundaries(boundary_count);

    std::string command = "unzip -p '" + zip_path + "'";
    FILE* pipe = popen(command.c_str(), "r");
    if (!pipe) throw std::runtime_error("unable to open unzip pipe");

    char* buffer = nullptr;
    std::size_t capacity = 0;
    long long parsed_rows = 0;
    long long malformed_rows = 0;
    while (true) {
        const ssize_t length = getline(&buffer, &capacity, pipe);
        if (length < 0) break;
        std::string line(buffer, static_cast<std::size_t>(length));
        while (!line.empty() && (line.back() == '\n' || line.back() == '\r')) line.pop_back();
        const auto fields = split_csv(line);
        if (fields.size() < 7) {
            ++malformed_rows;
            continue;
        }
        long long ts = 0;
        double price = 0.0;
        double qty = 0.0;
        bool buyer_maker = false;
        if (!parse_double(fields[1], price) || !parse_double(fields[2], qty)
            || !parse_i64(fields[5], ts) || !parse_bool(fields[6], buyer_maker)) {
            if (parsed_rows == 0) continue;  // optional header
            ++malformed_rows;
            continue;
        }
        if (ts > 100000000000000LL) ts /= 1000LL;  // microseconds to milliseconds
        if (ts < start_ms || ts >= end_ms) continue;
        ++parsed_rows;
        const long long relative = ts - start_ms;
        const std::size_t index = static_cast<std::size_t>(relative / kBoundaryMs);
        const long long offset = relative % kBoundaryMs;
        if (index >= boundaries.size()) continue;
        for (std::size_t w = 0; w < kWindowsMs.size(); ++w) {
            if (offset < kWindowsMs[w]) {
                boundaries[index].post[w].add(ts, price, qty, buyer_maker);
            }
        }
        const long long until_next = kBoundaryMs - offset;
        if (index + 1 < boundaries.size()) {
            for (std::size_t w = 0; w < kWindowsMs.size(); ++w) {
                if (until_next <= kWindowsMs[w]) {
                    boundaries[index + 1].pre[w].add(ts, price, qty, buyer_maker);
                }
            }
        }
        if (parsed_rows % 20000000LL == 0) {
            std::cerr << symbol << ' ' << month_text << " parsed=" << parsed_rows << '\n';
        }
    }
    if (buffer) std::free(buffer);
    const int status = pclose(pipe);
    if (status != 0) throw std::runtime_error("unzip process failed");
    if (parsed_rows == 0) throw std::runtime_error("no aggregate trades parsed");

    std::ofstream out(output_path);
    if (!out) throw std::runtime_error("cannot open output");
    out << "symbol,boundary_time_ms,month,boundary_index,pre_clock_complete";
    for (long long window : kWindowsMs) write_stats_header(out, "post" + std::to_string(window / 1000) + "s");
    for (long long window : kWindowsMs) write_stats_header(out, "pre" + std::to_string(window / 1000) + "s");
    out << '\n';
    for (std::size_t i = 0; i < boundaries.size(); ++i) {
        const long long boundary_ms = start_ms + static_cast<long long>(i) * kBoundaryMs;
        out << symbol << ',' << boundary_ms << ',' << month_text << ',' << i << ',' << (i == 0 ? 0 : 1);
        for (std::size_t w = 0; w < kWindowsMs.size(); ++w) write_stats(out, boundaries[i].post[w], boundary_ms, false);
        for (std::size_t w = 0; w < kWindowsMs.size(); ++w) write_stats(out, boundaries[i].pre[w], boundary_ms, true);
        out << '\n';
    }
    out.close();
    std::cerr << "parsed_rows=" << parsed_rows << " malformed_rows=" << malformed_rows
              << " boundaries=" << boundary_count << '\n';
    return 0;
}
