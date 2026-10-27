// SPDX-License-Identifier: GPL-3.0
#pragma once
#include "motor_health_snapshot.hpp"
#include <diagnostic_msgs/msg/diagnostic_array.hpp>
#include <string>

namespace rp1_health {
inline diagnostic_msgs::msg::DiagnosticStatus diagnostic(
    const Snapshot& snapshot, const std::string& joint, const std::string& bus,
    uint16_t motor_id, const std::string& epoch) {
    diagnostic_msgs::msg::DiagnosticStatus status;
    status.name = "rp1/motor/" + joint;
    status.hardware_id = bus + ":" + std::to_string(motor_id);
    const double age_ms = snapshot.valid ? std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - snapshot.received_at).count() : -1;
    const bool valid = snapshot.valid && age_ms >= 0 && age_ms <= 5000;
    status.level = !valid ? 3 : snapshot.fault_code ? 2 : 0;
    status.message = !valid ? "No fresh validated feedback" : "Observed raw feedback; health limits not evaluated";
    const auto add = [&](const std::string& key, const std::string& value) {
        diagnostic_msgs::msg::KeyValue field;
        field.key = key; field.value = value;
        status.values.push_back(field);
    };
    add("schema_version", "1"); add("joint_name", joint); add("bus", bus);
    add("motor_id", std::to_string(motor_id)); add("producer_epoch", epoch);
    add("protocol", snapshot.protocol == Protocol::lro ? "lro" : snapshot.protocol == Protocol::rp ? "rp" : "unknown");
    add("valid", valid ? "true" : "false");
    add("feedback_sequence", std::to_string(snapshot.sequence));
    add("feedback_age_ms", std::to_string(age_ms));
    for (const auto* key : {"motor_temperature", "mos_temperature", "fault_code"})
        add(std::string(key) + "_valid", valid ? "true" : "false");
    if (valid) {
        add("motor_temperature", std::to_string(snapshot.motor_temperature));
        add("mos_temperature", std::to_string(snapshot.mos_temperature));
        add("fault_code", std::to_string(snapshot.fault_code));
    }
    return status;
}
}
