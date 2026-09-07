import pytest
import yaml

from boss_secretary.core import secrets as S


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, name):
        return self.store.get(name)

    def set_password(self, service, name, value):
        self.store[name] = value

    def delete_password(self, service, name):
        self.store.pop(name, None)


@pytest.fixture()
def fake_kr(monkeypatch):
    kr = FakeKeyring()
    monkeypatch.setattr(S, "_keyring", lambda: kr)
    return kr


def test_set_get_roundtrip(fake_kr):
    assert S.set_secret("feishu.app_secret", "abc") is True
    assert S.get_secret("feishu.app_secret") == "abc"
    S.delete_secret("feishu.app_secret")
    assert S.get_secret("feishu.app_secret") is None


def test_merge_overrides_yaml(fake_kr):
    fake_kr.set_password(S.SERVICE, "feishu.app_secret", "KC_SECRET")
    settings = {"feishu": {"app_secret": "PLAIN"}}
    merged = S.merge_secret_overrides(settings)
    assert merged["feishu"]["app_secret"] == "KC_SECRET"
    fake_kr.store.clear()
    merged2 = S.merge_secret_overrides({"feishu": {"app_secret": "PLAIN"}})
    assert merged2["feishu"]["app_secret"] == "PLAIN"


def test_migrate_blanks_yaml_and_stores(fake_kr, tmp_path):
    f = tmp_path / "settings.yaml"
    f.write_text(yaml.safe_dump({
        "feishu": {"app_secret": "REAL_SECRET"},
        "llm": {"cloud": {"api_key": "REAL_KEY"},
                "local": {"api_key": ""}}}), encoding="utf-8")
    moved = S.migrate(str(f))
    assert set(moved) == {"feishu.app_secret", "llm.cloud.api_key"}
    data = yaml.safe_load(f.read_text(encoding="utf-8"))
    assert data["feishu"]["app_secret"] == ""
    assert data["llm"]["cloud"]["api_key"] == ""
    assert fake_kr.store["feishu.app_secret"] == "REAL_SECRET"
    moved2 = S.migrate(str(f))
    assert moved2 == []


def test_llm_load_settings_pulls_keychain(fake_kr, monkeypatch, tmp_path):
    from boss_secretary.core import llm as L
    f = tmp_path / "settings.yaml"
    f.write_text(yaml.safe_dump({"llm": {"provider": "cloud",
                                         "cloud": {"base_url": "http://x",
                                                   "api_key": ""}}}), encoding="utf-8")
    fake_kr.set_password(S.SERVICE, "llm.cloud.api_key", "KC_KEY")
    monkeypatch.setattr(L, "DEFAULT_SETTINGS", str(f))
    s = L.load_settings(str(f))
    assert s["llm"]["cloud"]["api_key"] == "KC_KEY"
    monkeypatch.delattr(L, "DEFAULT_SETTINGS", raising=False)


def test_no_keyring_falls_back_to_yaml(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "_keyring", lambda: None)
    from boss_secretary.core.llm import load_settings as _  # noqa: F401
    f = tmp_path / "settings.yaml"
    f.write_text(yaml.safe_dump({"feishu": {"app_secret": "PLAINTEXT_FALLBACK"}}),
                 encoding="utf-8")
    merged = S.merge_secret_overrides({"feishu": {"app_secret": "PLAINTEXT_FALLBACK"}})
    assert merged["feishu"]["app_secret"] == "PLAINTEXT_FALLBACK"
