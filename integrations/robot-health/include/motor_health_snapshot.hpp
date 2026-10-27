// SPDX-License-Identifier: GPL-3.0
#pragma once
#include <chrono>
#include <cstdint>
#include <limits>
#include <mutex>
#include <linux/can.h>

namespace rp1_health {
enum class Protocol { unknown, lro, rp };
struct Snapshot {
    bool valid = false;
    Protocol protocol = Protocol::unknown;
    float motor_temperature = std::numeric_limits<float>::quiet_NaN();
    float mos_temperature = std::numeric_limits<float>::quiet_NaN();
    uint32_t fault_code = 0;
    uint64_t sequence = 0;
    std::chrono::steady_clock::time_point received_at{};
};
inline bool valid_id(const canfd_frame& frame, uint32_t expected) {
    return !(frame.can_id & (CAN_EFF_FLAG | CAN_RTR_FLAG | CAN_ERR_FLAG)) &&
           (frame.can_id & CAN_SFF_MASK) == expected;
}
inline bool decode_lro(const canfd_frame& frame, uint16_t motor_id, Snapshot& out) {
    if (!valid_id(frame, motor_id) || frame.len < 8 || frame.len > 64 ||
        ((frame.data[0] >> 5) & 7) != 1) return false;
    out.protocol = Protocol::lro;
    out.motor_temperature = static_cast<int>(frame.data[6]) - 25;
    out.mos_temperature = static_cast<int>(frame.data[7]) - 25;
    out.fault_code = frame.data[0] & 0x1f;
    return true;
}
inline int16_t signed16(const uint8_t* value) {
    const uint16_t raw = (static_cast<uint16_t>(value[0]) << 8) | value[1];
    return static_cast<int16_t>(raw >= 32768 ? static_cast<int>(raw) - 65536 : raw);
}
inline bool decode_rp(const canfd_frame& frame, uint16_t motor_id, Snapshot& out) {
    if (!valid_id(frame, 0x410 + motor_id) || frame.len < 19 || frame.len > 64 || frame.data[0] != 6) return false;
    out.protocol = Protocol::rp;
    out.fault_code = (static_cast<uint16_t>(frame.data[3]) << 8) | frame.data[4];
    out.motor_temperature = signed16(frame.data + 15) * 0.01f;
    out.mos_temperature = signed16(frame.data + 17) * 0.01f;
    return true;
}
class Cache {
    mutable std::mutex mutex_;
    Snapshot snapshot_;
public:
    // Feedback callback never waits behind a reader. A contended health sample is skipped.
    bool store(Snapshot next) {
        std::unique_lock<std::mutex> lock(mutex_, std::try_to_lock);
        if (!lock.owns_lock()) return false;
        next.valid = true;
        next.sequence = snapshot_.sequence + 1;
        next.received_at = std::chrono::steady_clock::now();
        snapshot_ = next;
        return true;
    }
    Snapshot read() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return snapshot_;
    }
};
}
