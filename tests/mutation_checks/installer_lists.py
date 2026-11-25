"""Checks that the installer's lists start empty, so it runs under bash 5 as well as macOS's bash 3.2."""

CHECKS = (
    (
        "the installer's list of changed agents starts empty",
        "install.sh",
        "declare -a CHANGED_LABELS=()\n",
        "declare -a CHANGED_LABELS\n",
        "tests/test_pro_mode.py::test_every_list_the_installer_declares_starts_empty",
    ),
)
