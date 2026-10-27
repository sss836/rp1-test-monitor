#include "motor_health_diagnostics.hpp"
#include <iostream>
void emit(const diagnostic_msgs::msg::DiagnosticStatus& status) {
    std::cout << "{\"name\":\"" << status.name << "\",\"values\":[";
    bool first = true;
    for (const auto& value : status.values) {
        if (!first) std::cout << ',';
        first = false;
        std::cout << "{\"key\":\"" << value.key << "\",\"value\":\"" << value.value << "\"}";
    }
    std::cout << "]}" << std::endl;
}
int main() {
    using namespace rp1_health;
    Snapshot raw;
    emit(diagnostic(raw, "joint_0", "can0", 1, "replay"));
    canfd_frame rp{}; rp.can_id=0x411; rp.len=19; rp.data[0]=6;
    rp.data[3]=0xab; rp.data[4]=0xcd;
    rp.data[15]=0xff; rp.data[16]=0x9c; rp.data[17]=0x10; rp.data[18]=0x68;
    if (!decode_rp(rp,1,raw)) return 1;
    Cache rp_cache; rp_cache.store(raw);
    emit(diagnostic(rp_cache.read(),"joint_0","can0",1,"replay"));
    emit(diagnostic(rp_cache.read(),"joint_0","can0",1,"replay"));
    canfd_frame lro{}; lro.can_id=1; lro.len=8; lro.data[0]=0x20;
    lro.data[6]=20; lro.data[7]=10;
    if (!decode_lro(lro,1,raw)) return 1;
    Cache lro_cache; lro_cache.store(raw);
    emit(diagnostic(lro_cache.read(),"joint_1","can1",1,"replay"));
}
