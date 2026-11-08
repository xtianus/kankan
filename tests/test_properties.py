import os
import shutil
from pathlib import Path

from kankan.properties import PropertiesFile

FIXTURE = Path(__file__).parent / "fixtures" / "messy.properties"


def copy_fixture(tmp_path):
    target = tmp_path / "secrets.properties"
    shutil.copy(FIXTURE, target)
    return target


def test_properties_roundtrip_preserves_bytes(tmp_path):
    target = copy_fixture(tmp_path)
    props = PropertiesFile.load(target)
    props.save()
    assert target.read_bytes() == FIXTURE.read_bytes()


def test_properties_parse_messy(tmp_path):
    props = PropertiesFile.load(copy_fixture(tmp_path))
    assert props.get("db.user") == "admin"
    assert props.get("db.password") == "s3cr\u00e9t"
    assert props.get("mail.host") == "smtp.example.com"
    assert props.get("long.value") == "first part second part"
    assert props.get("indented.key") == "x"
    assert props.get("path") == "C:\\work\\dir"
    assert props.get("unicode.raw") == "café"
    assert props.get("last.key") == "no newline"
    assert props.get("missing") is None


def test_properties_update_in_place_and_append(tmp_path):
    target = copy_fixture(tmp_path)
    props = PropertiesFile.load(target)
    props.set("kankan.trello.key", "newkey")
    props.set("kankan.trello.token", "tok")
    props.save()
    original = FIXTURE.read_bytes().split(b"\r\n")
    updated = target.read_bytes().split(b"\r\n")
    assert updated[:8] == original[:8]
    assert updated[8] == b"kankan.trello.key=newkey"
    assert updated[9:12] == original[9:12]
    assert updated[12] == b"last.key=no newline"
    assert updated[13] == b"kankan.trello.token=tok"
    assert PropertiesFile.load(target).get("kankan.trello.token") == "tok"


def test_properties_keeps_mode(tmp_path):
    target = copy_fixture(tmp_path)
    os.chmod(target, 0o640)
    props = PropertiesFile.load(target)
    props.set("a", "b")
    warning = props.save()
    assert (os.stat(target).st_mode & 0o777) == 0o640
    assert warning and "readable" in warning
    os.chmod(target, 0o600)
    props = PropertiesFile.load(target)
    props.set("a", "c")
    assert props.save() is None


def test_properties_new_file(tmp_path):
    target = tmp_path / "new.properties"
    props = PropertiesFile.load(target, missing_ok=True)
    props.set("k", " leading space and \\ backslash")
    props.save()
    assert PropertiesFile.load(target).get("k") == " leading space and \\ backslash"
