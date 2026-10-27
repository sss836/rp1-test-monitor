#include "motor_health_snapshot.hpp"
#include <cassert>
#include <atomic>
#include <thread>
int main() {
    using namespace rp1_health;
    Cache cache;
    assert(!cache.read().valid);
    canfd_frame frame{}; frame.can_id = 1; frame.len = 8; frame.data[0] = 0x23;
    frame.data[6] = 20; frame.data[7] = 10;
    Snapshot snapshot;
    assert(decode_lro(frame, 1, snapshot));
    assert(snapshot.motor_temperature == -5 && snapshot.mos_temperature == -15 && snapshot.fault_code == 3);
    frame.can_id = 2; assert(!decode_lro(frame, 1, snapshot));
    frame.can_id = 1; frame.data[0] = 0x40; assert(!decode_lro(frame, 1, snapshot));
    frame.can_id = 0x411; frame.len = 19; frame.data[0] = 6;
    frame.data[3] = 0xab; frame.data[4] = 0xcd;
    frame.data[15] = 0xff; frame.data[16] = 0x9c;
    assert(decode_rp(frame, 1, snapshot));
    assert(snapshot.fault_code == 0xabcd && snapshot.motor_temperature == -1);
    frame.len = 18; assert(!decode_rp(frame, 1, snapshot));
    std::atomic<bool> done{false};
    std::thread writer([&] { for (int i=1; i<100000; ++i) {
        Snapshot next; next.motor_temperature = i; next.mos_temperature = i;
        next.fault_code = i; cache.store(next);
    } done = true; });
    uint64_t previous = 0;
    do { auto value = cache.read(); if (value.valid) {
        assert(value.motor_temperature == value.mos_temperature);
        assert(value.motor_temperature == value.fault_code);
        assert(value.sequence >= previous); previous = value.sequence;
    }} while (!done);
    writer.join(); assert(cache.read().valid);
}
