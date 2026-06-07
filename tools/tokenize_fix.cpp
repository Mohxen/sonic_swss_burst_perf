// Fix for swss::tokenize() in sonic-swss-common/common/tokenize.cpp
//
// Root cause: the original implementation constructs std::istringstream on
// every call. istringstream construction initialises std::locale, which
// acquires a mutex and performs multiple heap allocations. With 10+ tokenize
// calls per route in RouteOrch::doTask(), this becomes the dominant CPU cost:
//   10 calls/route × 50 000 routes = 500 000 locale initialisations
//   measured impact: 50k-route add drain time 190 s → <1 s after fix
//
// Original (tokenize.cpp):
//   istringstream iss(str);
//   while (getline(iss, tmp, token))
//       ret.push_back(tmp);
//
// Replacement: plain find/substr loop, pre-reserved vector, no locale touch.
//
// Apply via LD_PRELOAD (no rebuild of the full library needed):
//   g++ -O2 -fPIC -std=c++17 -shared -o fast_tokenize.so tokenize_fix.cpp
//   docker cp fast_tokenize.so swss:/usr/local/lib/fast_tokenize.so
//   # edit orchagent.sh to export LD_PRELOAD=/usr/local/lib/fast_tokenize.so
//   # restart orchagent inside swss container

#include <cstddef>
#include <string>
#include <vector>

namespace swss {

std::vector<std::string> tokenize(const std::string &str, const char token)
{
    std::vector<std::string> ret;
    if (str.empty()) return ret;
    ret.reserve(4);
    size_t start = 0, pos;
    while ((pos = str.find(token, start)) != std::string::npos) {
        ret.push_back(str.substr(start, pos - start));
        start = pos + 1;
    }
    ret.push_back(str.substr(start));
    return ret;
}

// The firstN overload already uses find/substr in the original — kept identical.
std::vector<std::string> tokenize(const std::string &str, const char token,
                                  const size_t firstN)
{
    std::vector<std::string> ret;
    std::string tmp = str;
    size_t i = 0;
    auto pos = tmp.find(token);
    while (pos != std::string::npos && i++ < firstN) {
        ret.push_back(tmp.substr(0, pos));
        tmp = tmp.substr(pos + 1);
        pos = tmp.find(token);
    }
    ret.push_back(tmp);
    return ret;
}

} // namespace swss
