// SPDX-License-Identifier: GPL-3.0
// Standalone LRO feedback reader. No ROS, motor objects, CAN sends or link changes.
#include <linux/can.h>
#include <linux/can/raw.h>
#include <net/if.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>

namespace {
volatile std::sig_atomic_t stopping = 0;
void interrupt(int) { stopping = 1; }
using Clock = std::chrono::steady_clock;

bool decode(const canfd_frame& frame, ssize_t bytes, int flags, unsigned id,
            int& motor, int& mos) {
    if ((bytes != CAN_MTU && bytes != CANFD_MTU) ||
        (flags & (MSG_DONTROUTE | MSG_TRUNC | MSG_CTRUNC)) ||
        (frame.can_id & (CAN_EFF_FLAG | CAN_RTR_FLAG | CAN_ERR_FLAG)) ||
        (frame.can_id & CAN_SFF_MASK) != id || frame.len < 8 ||
        frame.len > (bytes == CAN_MTU ? 8 : 64) || (frame.data[0] >> 5) != 1)
        return false;
    motor = static_cast<int>(frame.data[6]) - 25;
    mos = static_cast<int>(frame.data[7]) - 25;
    return true;
}

bool fresh_age(double kernel_time, double realtime, double& age) {
    age = realtime - kernel_time;
    return kernel_time > 0 && age >= 0 && age <= 3;
}

void self_test() {
    const auto require = [](bool value) {
        if (!value) throw std::runtime_error("synthetic self-test failed");
    };
    canfd_frame frame{};
    frame.can_id = 1; frame.len = 8; frame.data[0] = 0x23;
    frame.data[6] = 20; frame.data[7] = 55;
    int motor = 0, mos = 0;
    require(decode(frame, CAN_MTU, 0, 1, motor, mos) && motor == -5 && mos == 30);
    require(decode(frame, CANFD_MTU, 0, 1, motor, mos));
    for (int flags : {MSG_DONTROUTE, MSG_TRUNC, MSG_CTRUNC})
        require(!decode(frame, CANFD_MTU, flags, 1, motor, mos));
    for (canid_t flags : {CAN_EFF_FLAG, CAN_RTR_FLAG, CAN_ERR_FLAG}) {
        frame.can_id = 1 | flags;
        require(!decode(frame, CANFD_MTU, 0, 1, motor, mos));
    }
    frame.can_id = 2; require(!decode(frame, CANFD_MTU, 0, 1, motor, mos));
    frame.can_id = 1; frame.data[0] = 0;
    require(!decode(frame, CANFD_MTU, 0, 1, motor, mos)); // command-shaped frame
    frame.data[0] = 0x20; frame.len = 7;
    require(!decode(frame, CANFD_MTU, 0, 1, motor, mos));
    frame.len = 9; require(!decode(frame, CAN_MTU, 0, 1, motor, mos));
    frame.len = 65; require(!decode(frame, CANFD_MTU, 0, 1, motor, mos));
    frame.len = 8; require(!decode(frame, CAN_MTU - 1, 0, 1, motor, mos));
    double age = 0;
    require(fresh_age(100, 100.5, age) && age == .5);
    require(!fresh_age(100, 104, age));
    require(!fresh_age(101, 100, age));
    require(!fresh_age(0, 0, age));
    std::puts("PASS: simulated frame/time cases only; no CAN socket opened.");
}

void option(int socket, int level, int name, const void* value, socklen_t bytes) {
    if (setsockopt(socket, level, name, value, bytes) < 0)
        throw std::runtime_error(std::strerror(errno));
}

void read_temperature(int socket, const char* interface, unsigned id) {
    int on = 1, off = 0, receive_bytes = 32768;
    option(socket, SOL_CAN_RAW, CAN_RAW_FD_FRAMES, &on, sizeof(on));
    option(socket, SOL_CAN_RAW, CAN_RAW_RECV_OWN_MSGS, &off, sizeof(off));
    option(socket, SOL_SOCKET, SO_TIMESTAMPNS, &on, sizeof(on));
    option(socket, SOL_SOCKET, SO_RCVBUF, &receive_bytes, sizeof(receive_bytes));
    option(socket, SOL_SOCKET, SO_RXQ_OVFL, &on, sizeof(on));
    can_filter filter{id, CAN_SFF_MASK | CAN_EFF_FLAG | CAN_RTR_FLAG};
    option(socket, SOL_CAN_RAW, CAN_RAW_FILTER, &filter, sizeof(filter));
    sockaddr_can address{};
    address.can_family = AF_CAN;
    address.can_ifindex = static_cast<int>(if_nametoindex(interface));
    if (!address.can_ifindex) throw std::runtime_error("CAN interface does not exist");
    if (bind(socket, reinterpret_cast<sockaddr*>(&address), sizeof(address)) < 0)
        throw std::runtime_error(std::strerror(errno));
    int motor = 0, mos = 0;
    bool available = false;
    double received_unix = 0;
    unsigned dropped = 0;
    auto observed = Clock::now(), next_print = observed;
    std::printf("LRO receive-only: %s/id=%u; print 1Hz, stale after 3s; Ctrl+C to exit.\n",
                interface, id);
    while (!stopping) {
        auto now = Clock::now();
        if (now >= next_print) {
            double age = std::chrono::duration<double>(now - observed).count();
            if (available && age <= 3)
                std::printf("%s/id=%u motor=%d C MOS=%d C age=%.0f ms rx_unix=%.6f drops=%u\n",
                            interface, id, motor, mos, age * 1000, received_unix, dropped);
            else
                std::printf("%s/id=%u temperature=UNKNOWN (no fresh feedback) drops=%u\n",
                            interface, id, dropped);
            next_print = now + std::chrono::seconds(1);
        }
        int wait_ms = static_cast<int>(std::chrono::duration_cast<std::chrono::milliseconds>(
            next_print - Clock::now()).count());
        if (wait_ms < 0) wait_ms = 0;
        pollfd descriptor{socket, POLLIN, 0};
        int ready = poll(&descriptor, 1, wait_ms);
        if (ready < 0 && errno == EINTR) continue;
        if (ready < 0) throw std::runtime_error(std::strerror(errno));
        if (descriptor.revents & (POLLERR | POLLHUP | POLLNVAL))
            throw std::runtime_error("CAN socket/interface error; interface left unchanged");
        if (!(descriptor.revents & POLLIN)) continue;
        // Bounded drain: printing and stop checks still run under a full queue.
        for (int n = 0; n < 256 && !stopping; ++n) {
            canfd_frame frame{};
            alignas(cmsghdr) char control[CMSG_SPACE(sizeof(timespec)) + CMSG_SPACE(sizeof(unsigned))]{};
            iovec data{&frame, sizeof(frame)};
            msghdr message{};
            message.msg_iov = &data; message.msg_iovlen = 1;
            message.msg_control = control; message.msg_controllen = sizeof(control);
            ssize_t bytes = recvmsg(socket, &message, MSG_DONTWAIT);
            if (bytes < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) break;
            if (bytes < 0 && errno == EINTR) continue;
            if (bytes < 0) throw std::runtime_error(std::strerror(errno));
            timespec timestamp{};
            for (cmsghdr* c = CMSG_FIRSTHDR(&message); c; c = CMSG_NXTHDR(&message, c)) {
                if (c->cmsg_level != SOL_SOCKET) continue;
                if (c->cmsg_type == SO_TIMESTAMPNS && c->cmsg_len >= CMSG_LEN(sizeof(timestamp)))
                    std::memcpy(&timestamp, CMSG_DATA(c), sizeof(timestamp));
                if (c->cmsg_type == SO_RXQ_OVFL && c->cmsg_len >= CMSG_LEN(sizeof(dropped)))
                    std::memcpy(&dropped, CMSG_DATA(c), sizeof(dropped));
            }
            int next_motor = 0, next_mos = 0;
            if (!decode(frame, bytes, message.msg_flags, id, next_motor, next_mos)) continue;
            timespec realtime{};
            if (clock_gettime(CLOCK_REALTIME, &realtime) != 0)
                throw std::runtime_error(std::strerror(errno));
            double stamp = timestamp.tv_sec + timestamp.tv_nsec / 1e9;
            double clock = realtime.tv_sec + realtime.tv_nsec / 1e9, age = 0;
            if (!fresh_age(stamp, clock, age)) continue;
            motor = next_motor; mos = next_mos; received_unix = stamp; available = true;
            observed = Clock::now() - std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(age));
        }
    }
}
} // namespace

int main(int argc, char** argv) {
    int socket = -1;
    try {
        if (argc == 2 && std::strcmp(argv[1], "--self-test") == 0) {
            self_test(); return 0;
        }
        if (argc != 3) {
            std::fprintf(stderr, "Usage: %s CAN_INTERFACE MOTOR_ID\nExample: %s can0 1\n", argv[0], argv[0]);
            return 2;
        }
        char* end = nullptr;
        errno = 0;
        long id = std::strtol(argv[2], &end, 0);
        if (errno || !*argv[2] || *end || id < 1 || id > CAN_SFF_MASK ||
            std::strlen(argv[1]) >= IFNAMSIZ)
            throw std::runtime_error("invalid CAN interface or standard motor ID");
        std::signal(SIGINT, interrupt); std::signal(SIGTERM, interrupt);
        setvbuf(stdout, nullptr, _IOLBF, 0);
        socket = ::socket(PF_CAN, SOCK_RAW | SOCK_NONBLOCK | SOCK_CLOEXEC, CAN_RAW);
        if (socket < 0) throw std::runtime_error(std::strerror(errno));
        read_temperature(socket, argv[1], static_cast<unsigned>(id));
        close(socket); return 0;
    } catch (const std::exception& error) {
        if (socket >= 0) close(socket);
        std::fprintf(stderr, "Error: %s\n", error.what()); return 1;
    }
}
