from pathlib import Path

import yaml


def test_config_yaml_is_ingress_only():
    cfg = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text())
    assert cfg["name"] == "MyNetDiary Sync"
    assert cfg["ingress"] is True
    assert "ports" not in cfg
    assert cfg["homeassistant_api"] is True
    assert cfg["slug"] == "mnd_sync"
    assert cfg["arch"] == ["aarch64", "amd64"]
