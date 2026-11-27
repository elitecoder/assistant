"""The Mac app keeps one signing identity across builds once its local certificate exists.

Runs the real mac/make-signing-cert.sh, mac/sign-app.sh, and mac/build-app.sh
against a throwaway keychain. A recording stand-in for `codesign` checks which
identity the scripts ask for on any Mac. The real-signature tests also need a
host whose `codesign` can use a throwaway keychain, and skip where it can't
(GitHub's macOS runners report "no identity found" for it).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MAC = REPO / "mac"
NAME = "Assistant Local Signing"

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
    yield path
    subprocess.run(["security", "delete-keychain", str(path)], capture_output=True)


def add_certificate(keychain: Path) -> str:
    made = run(str(MAC / "make-signing-cert.sh"), "--keychain", str(keychain),
               env={"PATH": "/usr/bin:/bin", "HOME": str(keychain.parent)})
    run("security", "set-key-partition-list", "-S", "apple-tool:,apple:", "-s", "-k", "", str(keychain))
    return made


def certificate_hash(keychain: Path) -> str:
    listed = run("security", "find-identity", "-p", "codesigning", str(keychain))
    found = re.search(rf'([0-9A-F]{{40}}) "{NAME}"', listed)
    assert found, listed
    return found.group(1)


def executable(path: Path, version: int, *flags: str):
    source = path.with_suffix(".c")
    source.write_text(f"int build = {version}; int main(void) {{ return build - {version}; }}\n")
    run("cc", *flags, "-o", str(path), str(source))


def new_app(tmp_path: Path, version: int) -> Path:
    app = tmp_path / f"v{version}" / "Assistant.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    executable(app / "Contents/MacOS/AssistantBar", version)
    shutil.copy(MAC / "Info.plist", app / "Contents/Info.plist")
    return app


def stand_in_codesign(tmp_path: Path, signs: bool) -> tuple[str, Path]:
    """Puts a `codesign` first on PATH that logs each call. Ad hoc calls reach the real one;
    certificate calls succeed without signing when `signs`, else fail like an unusable identity."""
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    log = tmp_path / "codesign.log"
    outcome = "exit 0" if signs else 'echo "no identity found" >&2; exit 1'
    script = tools / "codesign"
    script.write_text(f'#!/bin/bash\necho "$*" >> "{log}"\n'
                      f'[[ " $* " == *" --sign - "* ]] && exec /usr/bin/codesign "$@"\n{outcome}\n')
    script.chmod(0o755)
    return f"{tools}:/usr/bin:/bin", log


def sign(app: Path, keychain: Path, path: str = "/usr/bin:/bin") -> str:
    return run(str(MAC / "sign-app.sh"), str(app), env={"PATH": path, "ASSISTANT_SIGN_KEYCHAIN": str(keychain)})


@pytest.fixture
def real_signing(tmp_path, keychain):
    add_certificate(keychain)
    probe = tmp_path / "probe"
    executable(probe, 0)
    tried = subprocess.run(["codesign", "-f", "-s", certificate_hash(keychain), "--keychain", str(keychain),
                            str(probe)], capture_output=True, text=True)
    if tried.returncode != 0:
        pytest.skip(f"this host's codesign can't use a throwaway keychain: {tried.stderr.strip()}")
    return keychain


def test_sign_app_asks_codesign_for_the_certificate(tmp_path, keychain):
    add_certificate(keychain)
    path, log = stand_in_codesign(tmp_path, signs=True)
    app = new_app(tmp_path, 1)
    assert f'Signed with "{NAME}"' in sign(app, keychain, path)
    assert f"--force --deep --sign {certificate_hash(keychain)} --keychain {keychain} {app}\n" in log.read_text()


def test_without_a_chosen_keychain_sign_app_searches_the_usual_ones(tmp_path):
    path, log = stand_in_codesign(tmp_path, signs=True)
    certificate = "0123456789ABCDEF0123456789ABCDEF01234567"
    security = tmp_path / "tools/security"
    security.write_text(f'#!/bin/bash\n[[ "$*" == "find-identity -p codesigning" ]] || exit 9\n'
                        f'echo \'  1) {certificate} "{NAME}" (CSSMERR_TP_NOT_TRUSTED)\'\n')
    security.chmod(0o755)
    app = new_app(tmp_path, 1)
    run(str(MAC / "sign-app.sh"), str(app), env={"PATH": path})
    assert f"--force --deep --sign {certificate} {app}\n" in log.read_text()


def test_a_long_identity_list_still_finds_the_certificate(tmp_path, keychain):
    add_certificate(keychain)
    path, log = stand_in_codesign(tmp_path, signs=True)
    security = tmp_path / "tools/security"
    security.write_text('#!/bin/bash\n/usr/bin/security "$@"\nyes "  padding" | head -n 200000\n')
    security.chmod(0o755)
    sign(new_app(tmp_path, 1), keychain, path)
    assert f"--sign {certificate_hash(keychain)}" in log.read_text()


def test_a_certificate_that_cannot_sign_falls_back_to_ad_hoc(tmp_path, keychain):
    add_certificate(keychain)
    path, _ = stand_in_codesign(tmp_path, signs=False)
    app = new_app(tmp_path, 1)
    said = sign(app, keychain, path)
    assert "cdhash" in requirement(app)
    assert "couldn't sign" in said


def test_without_the_certificate_each_build_gets_a_new_identity(tmp_path, keychain):
    first, second = new_app(tmp_path, 1), new_app(tmp_path, 2)
    sign(first, keychain)
    sign(second, keychain)
    assert "cdhash" in requirement(first)
    assert requirement(first) != requirement(second)


def test_making_the_certificate_twice_keeps_the_first(keychain):
    add_certificate(keychain)
    first = certificate_hash(keychain)
    assert "already" in add_certificate(keychain)
    assert run("security", "find-certificate", "-a", "-c", NAME, str(keychain)).count('"labl"') == 1
    assert certificate_hash(keychain) == first


def test_the_setup_refuses_arguments_it_does_not_understand(tmp_path, keychain):
    for args in (["--keychain=" + str(keychain)], ["--keychian", str(keychain)], ["--keychain"]):
        done = subprocess.run([str(MAC / "make-signing-cert.sh"), *args], capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
        assert done.returncode == 2, args
        assert "Usage" in done.stderr
    assert NAME not in run("security", "find-identity", "-p", "codesigning", str(keychain))


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
    path, log = stand_in_codesign(tmp_path, signs=True)
    swift = tmp_path / "tools/swift"
    swift.write_text(f'#!/bin/bash\n[[ " $* " == *" --show-bin-path "* ]] && echo "{products}"\nexit 0\n')
    swift.chmod(0o755)
    run(str(project / "build-app.sh"),
        env={"PATH": path, "ASSISTANT_SIGN_KEYCHAIN": str(keychain), "HOME": str(tmp_path)})
    assert f"--sign {certificate_hash(keychain)}" in log.read_text()
    assert (project / "build/Assistant.app/Contents/Frameworks/libStandIn.dylib").exists()


def test_builds_signed_with_the_certificate_keep_one_identity(tmp_path, real_signing):
    first, second = new_app(tmp_path, 1), new_app(tmp_path, 2)
    assert f'Signed with "{NAME}"' in sign(first, real_signing)
    sign(second, real_signing)
    assert 'identifier "com.assistant.sessions" and certificate leaf = H"' in requirement(first)
    assert requirement(first) == requirement(second)
