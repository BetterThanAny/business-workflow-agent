import json
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).parents[2]
SITE = ROOT / "site"


class ShowcaseParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.local_assets: set[str] = set()
        self.scenario_ids: set[str] = set()

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if element_id := attributes.get("id"):
            self.ids.add(element_id)
        if scenario_id := attributes.get("data-scenario"):
            self.scenario_ids.add(scenario_id)
        asset = attributes.get("href") if tag == "link" else attributes.get("src")
        if asset and not asset.startswith(("http://", "https://", "#")):
            self.local_assets.add(asset)


def test_showcase_assets_and_interaction_targets_exist() -> None:
    parser = ShowcaseParser()
    parser.feed((SITE / "index.html").read_text(encoding="utf-8"))

    assert {"demo", "architecture", "evidence", "step-list", "step-detail"} <= parser.ids
    assert parser.local_assets == {"app.js", "favicon.svg", "styles.css"}
    assert all((SITE / asset).is_file() for asset in parser.local_assets)
    assert parser.scenario_ids == {"approval", "injection", "replay"}


def test_showcase_scenarios_are_complete_and_truthfully_labelled() -> None:
    payload = json.loads((SITE / "demo-data.json").read_text(encoding="utf-8"))

    assert payload["version"] == "showcase-v1"
    assert payload["source"] == "deterministic test scenarios"
    assert {scenario["id"] for scenario in payload["scenarios"]} == {
        "approval",
        "injection",
        "replay",
    }
    for scenario in payload["scenarios"]:
        assert len(scenario["steps"]) >= 4
        assert scenario["title"]
        assert scenario["outcome"]
        for step in scenario["steps"]:
            assert set(step) == {"label", "state", "tone", "summary", "detail", "code"}
            assert all(step.values())
