"""config.load runs at every startup; a bad file should fail clearly, not crash obscurely."""

import pytest

from strata import config


def test_defaults_without_file(tmp_path):
    cfg = config.load(tmp_path / "absent.toml")
    assert cfg.port == 8080 and cfg.network == "strata"
    assert cfg.default_quota.max_vms == 3
    assert cfg.base_url.startswith("http://") and "/mcp" not in cfg.base_url


def test_reads_values_and_quota(tmp_path):
    p = tmp_path / "strata.toml"
    p.write_text('public_host = "10.1.2.3"\nport = 9000\n'
                 'forward_ports = [40000, 40100]\n[quota]\nmax_vms = 7\nmax_ram_mb = 2048\n')
    cfg = config.load(p)
    assert cfg.public_host == "10.1.2.3" and cfg.port == 9000
    assert cfg.forward_ports == (40000, 40100)
    assert cfg.default_quota.max_vms == 7 and cfg.default_quota.max_ram_mb == 2048
    assert cfg.base_url == "http://10.1.2.3:9000"
    assert cfg.db_path == cfg.data_dir / "strata.db"


def test_paths_become_path_objects(tmp_path):
    from pathlib import Path
    p = tmp_path / "strata.toml"
    p.write_text('data_dir = "/var/lib/elsewhere"\n')
    cfg = config.load(p)
    assert isinstance(cfg.data_dir, Path) and str(cfg.data_dir) == "/var/lib/elsewhere"


def test_unknown_setting_is_rejected(tmp_path):
    p = tmp_path / "strata.toml"
    p.write_text('port = 8080\nnonsense = true\n')
    with pytest.raises(ValueError, match="nonsense"):
        config.load(p)


def test_unknown_quota_key_is_rejected(tmp_path):
    p = tmp_path / "strata.toml"
    p.write_text('[quota]\nmax_potatoes = 5\n')
    with pytest.raises(ValueError, match="max_potatoes"):
        config.load(p)
