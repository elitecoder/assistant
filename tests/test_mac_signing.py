"""The Mac app keeps one signing identity across builds once its local certificate exists.

Runs the real mac/make-signing-cert.sh, mac/sign-app.sh, and mac/build-app.sh
against a throwaway keychain; build-app.sh gets a stand-in `swift` so no
release build runs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MAC = REPO / "mac"

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="codesign and keychains need macOS")


def run(*args: str, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(args, capture_output=True, text=True, env=env)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout + done.stderr


def requirement(app: Path) -> str:
    run("codesign", "--verify", "--strict", "--deep", str(app))
    shown = run("codesign", "-d", "-r-", str(app))
    return next(line for line in shown.splitlines() if "designated =>" in line)


@pytest.fixture
def keychain(tmp_path):
    path = tmp_path / "test.keychain-db"
    run("security", "create-keychain", "-p", "", str(path))
    run("security", "unlock-keychain", "-p", "", str(path))
    searched = run("security", "list-keychains", "-d", "user").split()
    if os.environ.get("GITHUB_ACTIONS") == "true":
        run("security", "list-keychains", "-d", "user", "-s", str(path), *[k.strip('"') for k in searched])
    yield path
    if os.environ.get("GITHUB_ACTIONS") == "true":
        run("security", "list-keychains", "-d", "user", "-s", *[k.strip('"') for k in searched])
    subprocess.run(["security", "delete-keychain", str(path)], capture_output=True)


def add_certificate(keychain: Path) -> str:
    made = run(str(MAC / "make-signing-cert.sh"), "--keychain", str(keychain))
    run("security", "set-key-partition-list", "-S", "apple-tool:,apple:", "-s", "-k", "", str(keychain))
    return made


def executable(path: Path, version: int, *flags: str):
    source = path.with_suffix(".c")
    source.write_text(f"int build = {version}; int main(void) {{ return build - {version}; }}\n")
    run("cc", *flags, "-o", str(path), str(source))


def signed_app(tmp_path: Path, version: int, keychain: Path, path: str = "/usr/bin:/bin") -> str:
    app = tmp_path / f"v{version}" / "Assistant.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    executable(app / "Contents/MacOS/AssistantBar", version)
    shutil.copy(MAC / "Info.plist", app / "Contents/Info.plist")
    run(str(MAC / "sign-app.sh"), str(app), env={"PATH": path, "ASSISTANT_SIGN_KEYCHAIN": str(keychain)})
    return requirement(app)


def test_builds_signed_with_the_certificate_keep_one_identity(tmp_path, keychain):
    add_certificate(keychain)
    first = signed_app(tmp_path, 1, keychain)
    assert 'identifier "com.assistant.sessions" and certificate leaf = H"' in first
    assert signed_app(tmp_path, 2, keychain) == first


def test_a_long_identity_list_still_finds_the_certificate(tmp_path, keychain):
    add_certificate(keychain)
    tools = tmp_path / "tools"
    tools.mkdir()
    security = tools / "security"
    security.write_text('#!/bin/bash\n/usr/bin/security "$@"\nyes "  padding" | head -n 200000\n')
    security.chmod(0o755)
    assert "certificate leaf" in signed_app(tmp_path, 1, keychain, path=f"{tools}:/usr/bin:/bin")


def test_without_the_certificate_each_build_gets_a_new_identity(tmp_path, keychain):
    first = signed_app(tmp_path, 1, keychain)
    assert "cdhash" in first
    assert signed_app(tmp_path, 2, keychain) != first


def test_making_the_certificate_twice_keeps_the_first(tmp_path, keychain):
    add_certificate(keychain)
    first = signed_app(tmp_path, 1, keychain)
    assert "already" in add_certificate(keychain)
    assert signed_app(tmp_path, 2, keychain) == first


def test_the_setup_refuses_arguments_it_does_not_understand(tmp_path, keychain):
    for args in (["--keychain=" + str(keychain)], ["--keychian", str(keychain)], ["--keychain"]):
        done = subprocess.run([str(MAC / "make-signing-cert.sh"), *args], capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
        assert done.returncode == 2, args
        assert "Usage" in done.stderr
    assert "Assistant Local Signing" not in run("security", "find-identity", "-p", "codesigning", str(keychain))


def test_build_app_signs_with_the_certificate(tmp_path, keychain):
    add_certificate(keychain)
    project = tmp_path / "mac"
    project.mkdir()
    for name in ("build-app.sh", "sign-app.sh", "Info.plist"):
        shutil.copy2(MAC / name, project / name)
    products = tmp_path / "products"
    products.mkdir()
    executable(products / "AssistantBar", 1, "-Wl,-headerpad_max_install_names")
    executable(products / "libStandIn.dylib", 1, "-dynamiclib")
    (products / "StandIn.bundle").mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    swift = tools / "swift"
    swift.write_text(f'#!/bin/bash\n[[ " $* " == *" --show-bin-path "* ]] && echo "{products}"\nexit 0\n')
    swift.chmod(0o755)
    run(str(project / "build-app.sh"),
        env={"PATH": f"{tools}:/usr/bin:/bin", "ASSISTANT_SIGN_KEYCHAIN": str(keychain), "HOME": str(tmp_path)})
    assert "certificate leaf" in requirement(project / "build/Assistant.app")
