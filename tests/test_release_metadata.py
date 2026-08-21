import ast
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]
RELEASE_VERSION = "1.1.0"
RELEASE_TAG = f"v{RELEASE_VERSION}"
RELEASE_DATE = "2026-08-22"
FORK_URL = "https://github.com/Arthur-D/NativMix"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _metadata_assignments() -> dict[str, str]:
    tree = ast.parse(_read("lib/nativmix/metadata.py"))
    assignments = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            assignments[node.targets[0].id] = ast.literal_eval(node.value)
    return assignments


def test_application_version_and_fork_identity_are_consistent():
    with (ROOT / "pyproject.toml").open("rb") as source:
        project = tomllib.load(source)["project"]
    metadata = _metadata_assignments()

    assert project["version"] == RELEASE_VERSION
    assert metadata["__version__"] == RELEASE_VERSION
    assert metadata["__github_url__"] == FORK_URL
    assert set(project["urls"].values()) == {FORK_URL, f"{FORK_URL}/issues"}


def test_distribution_release_metadata_is_consistent():
    service = ET.parse(ROOT / "packaging/OSC/_service").getroot()
    service_params = {node.attrib["name"]: node.text for node in service.iter("param")}
    metainfo = ET.parse(ROOT / "flatpak/io.github.ArthurD.NativMix.metainfo.xml").getroot()
    release = metainfo.find("./releases/release")

    assert service_params["revision"] == RELEASE_TAG
    assert service_params["versionformat"] == RELEASE_VERSION
    assert re.search(rf"^Version:\s+{re.escape(RELEASE_VERSION)}$", _read("packaging/OSC/nativmix.spec"), re.MULTILINE)
    assert _read("packaging/OSC/debian.changelog").startswith(f"nativmix ({RELEASE_VERSION}) ")
    assert _read("packaging/debian/changelog").startswith(f"nativmix ({RELEASE_VERSION}) ")
    assert release is not None
    assert release.attrib == {"version": RELEASE_VERSION, "date": RELEASE_DATE}


def test_local_rpm_templates_derive_the_version_from_pyproject():
    fedora_build = _read("packaging/fedora/build_local.sh")
    suse_build = _read("packaging/suse/build_local.sh")

    assert re.search(r"^Version:\s+0$", _read("packaging/fedora/nativmix.spec"), re.MULTILINE)
    assert re.search(r"^Version:\s+0$", _read("packaging/suse/nativmix.spec"), re.MULTILINE)
    assert "pyproject.toml" in fedora_build
    assert "pyproject.toml" in suse_build
    assert 's/^Version:.*/Version:        ${APP_VERSION}/' in fedora_build
    assert 's/^Version:.*/Version:        ${APP_VERSION}/' in suse_build


def test_release_docs_and_artifact_names_use_current_version():
    expected_bundle = f"io.github.ArthurD.NativMix-{RELEASE_TAG}.flatpak"

    assert f"**v{RELEASE_VERSION} - Arthur-D fork release**" in _read("README.md")
    assert expected_bundle in _read("README.md")
    assert expected_bundle in _read("packaging/FLATPAK.md")
    assert f"## v{RELEASE_VERSION} - Arthur-D fork release ({RELEASE_DATE})" in _read("CHANGELOG.md")
    assert "NativMix-{#MyAppVersion}-Setup" in _read("packaging/win/nativmix.iss")


def test_release_workflows_have_one_release_owner_and_tag_version_guards():
    windows = _read(".github/workflows/build-windows.yml")
    flatpak = _read(".github/workflows/build-flatpak.yml")

    assert windows.count("softprops/action-gh-release@") == 1
    assert "Tag version $v does not match project version" in windows
    assert "softprops/action-gh-release@" not in flatpak
    assert 'gh release upload "${TAG}" "${BUNDLE_NAME}"' in flatpak
    assert 'bundle_name="io.github.ArthurD.NativMix-${safe_label}.flatpak"' in flatpak


def test_aur_checksum_is_generated_only_after_the_release_tag_exists():
    workflow = _read(".github/workflows/aur-deploy.yml")
    pkgbuild = _read("packaging/aur/PKGBUILD")
    srcinfo = _read("packaging/aur/.SRCINFO")

    assert "tomllib.load(source)[\"project\"][\"version\"]" in workflow
    assert 'refs/tags/${RELEASE_TAG}^{commit}' in workflow
    assert "updpkgsums && makepkg --printsrcinfo" in workflow
    assert "github.com/Arthur-D/NativMix/archive" in workflow
    assert "'python-packaging'" in pkgbuild
    assert "depends = python-packaging" in srcinfo
    assert "SKIP" not in pkgbuild
    assert "SKIP" not in srcinfo
