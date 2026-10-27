"""Per-CPU monitoring stays on the host page and clears stale observations."""

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest


class HostCoreMarkup(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.stack = []
        self.locations = {}
        self.scripts = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id", "").startswith("host-core-"):
            self.locations[attrs["id"]] = [item.get("data-page-panel") for _, item in self.stack]
        if tag == "script":
            self.scripts.append(attrs.get("src", ""))
        if tag not in {"meta", "link", "img", "input", "br", "hr", "source"}:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def test_per_core_panel_belongs_to_existing_host_page():
    web = Path(__file__).parents[1] / "src/rp1_monitor/web"
    markup = HostCoreMarkup((web / "index.html").read_text())
    assert {"host-core-title", "host-core-big-list", "host-core-little-list", "host-core-unknown-list"} <= markup.locations.keys()
    assert all("host" in ancestors for ancestors in markup.locations.values())
    assert any("/host-cores.js?" in source for source in markup.scripts)


@pytest.mark.skipif(not shutil.which("node"), reason="Node exercises the shipped per-core renderer")
def test_per_core_values_follow_transport_connection_and_current_robot():
    root = Path(__file__).parents[1]
    subprocess.run(
        ["node", str(root / "reviews/host-cores-web-check.js")],
        check=True,
        capture_output=True,
        text=True,
    )
