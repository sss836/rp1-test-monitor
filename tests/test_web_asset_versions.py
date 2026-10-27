"""Dashboard and administrator entry points must use the same current UI assets."""

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


class LocalAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script":
            value = attributes.get("src", "")
        elif tag == "link" and attributes.get("rel") == "stylesheet":
            value = attributes.get("href", "")
        else:
            return
        if value.startswith("./assets/"):
            self.assets.append(value)


def test_web_entry_points_request_one_version_of_existing_scripts_and_styles():
    root = Path(__file__).parents[1] / "src/rp1_monitor/web"
    observed = {}
    for name in ("index.html", "admin.html"):
        parser = LocalAssets()
        parser.feed((root / name).read_text())
        assert parser.assets
        for value in parser.assets:
            url = urlsplit(value)
            version = parse_qs(url.query)
            assert set(version) == {"v"}
            assert len(version["v"]) == 1 and version["v"][0]
            assert (root / url.path.removeprefix("./assets/")).is_file()
            observed[Path(url.path).name] = url.query
    assert {"operations.js", "thermal.js", "admin.js", "app.css", "admin.css"} <= observed.keys()
    assert len(set(observed.values())) == 1
