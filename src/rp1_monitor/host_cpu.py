"""Low-cost CPU counters and capacity metadata from Linux read-only interfaces."""

import re


class CPUObserver:
    def __init__(self):
        self.previous = {}

    def sample(self, proc_root, sysfs_root):
        values, metadata, counters, missing = {}, {}, {}, []
        try:
            rows = (proc_root / "stat").read_text().splitlines()
        except OSError:
            rows = []
        for line in rows:
            fields = line.split()
            if not fields or not re.fullmatch(r"cpu(?:[0-9]+)?", fields[0]):
                continue
            name = fields[0]
            if name != "cpu" and int(name[3:]) >= 64:
                continue
            try:
                ticks = [int(value) for value in fields[1:9]]
                if len(ticks) < 4 or min(ticks) < 0:
                    raise ValueError("invalid CPU counters")
                counters[name] = ticks
            except ValueError:
                continue
        for name, ticks in counters.items():
            previous = self.previous.get(name)
            if previous is None or len(previous) != len(ticks) or any(
                value < old for value, old in zip(ticks, previous)
            ):
                continue
            elapsed = sum(ticks) - sum(previous)
            idle_elapsed = ticks[3] - previous[3]
            if len(ticks) > 4:
                idle_elapsed += ticks[4] - previous[4]
            # Guest ticks already belong to user/nice. Each core has its own baseline.
            if elapsed > 0 and 0 <= idle_elapsed <= elapsed:
                key = "host.cpu.usage" if name == "cpu" else f"host.cpu.{int(name[3:])}.usage"
                values[key] = 100 * (elapsed - idle_elapsed) / elapsed
        self.previous = counters
        if "host.cpu.usage" not in values:
            missing.append("CPU首次采样、计数重置或不可读，等待有效差值")

        root = sysfs_root / "devices" / "system" / "cpu"
        paths = {
            int(path.name[3:]): path
            for path in root.glob("cpu[0-9]*")
            if re.fullmatch(r"cpu[0-9]+", path.name) and int(path.name[3:]) < 64
        }
        indices = sorted(set(paths) | {int(name[3:]) for name in counters if name != "cpu"})
        capacities = {}
        for index, path in paths.items():
            try:
                capacity = int((path / "cpu_capacity").read_text())
                if capacity > 0:
                    capacities[index] = capacity
            except (OSError, ValueError):
                pass
        levels = sorted(set(capacities.values()))
        classified = len(levels) == 2 and len(capacities) == len(indices)
        for index in indices:
            key = f"host.cpu.{index}.usage"
            group = (
                "big" if capacities[index] == levels[-1] else "little"
            ) if classified else "unknown"
            # Existing sensor metadata also carries topology/state; no raw-window
            # fields are added, so older cached windows keep their canonical hash.
            metadata[key] = f"CPU {index}"
            metadata[f"host.cpu.{index}.group"] = group
            metadata[f"host.cpu.{index}.state"] = (
                "live" if key in values else "waiting" if f"cpu{index}" in counters else "unavailable"
            )
        if indices:
            metadata["host.cpu.topology"] = (
                "大小核分组依据内核cpu_capacity的两档容量；每核100%表示该核满载。"
                if classified else
                "内核未提供完整的两档cpu_capacity，核心类型未识别；不按编号或当前频率猜测大小核。"
            )
        return values, metadata, missing
