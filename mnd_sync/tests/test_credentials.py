import stat

from app.credentials import CredentialStore


def test_credentials_file_mode_0600(tmp_path):
    p = tmp_path / "c.json"
    CredentialStore(p).save(mealie_token="secret")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_credentials_not_in_repr(tmp_path):
    store = CredentialStore(tmp_path / "c.json")
    store.save(mnd_password="secret")
    assert "secret" not in repr(store)


def test_get_has_clear(tmp_path):
    p = tmp_path / "c.json"
    store = CredentialStore(p)
    assert not store.has("a") and store.get("a") is None
    store.save(a="1", b="2")
    store.save(c="3")
    assert CredentialStore(p).get("a") == "1" and store.has("c")
    store.clear("a")
    assert not CredentialStore(p).has("a") and CredentialStore(p).get("b") == "2"


def test_corrupt_file_does_not_block_saving(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not json")
    store = CredentialStore(p)
    assert store.get("a") is None
    store.save(a="1")
    assert store.get("a") == "1"
