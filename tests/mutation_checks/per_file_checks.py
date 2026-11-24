"""Checks for the mutation harness's own loading of this folder (tests/mutation_smoke.py: checks_in, well_formed)."""

CHECKS = (
    (
        "checks kept one file per change are part of the list the shards run",
        "tests/mutation_smoke.py",
        "CHECKS = well_formed(CHECKS + checks_in(CHECKS_DIR))\n",
        "CHECKS = well_formed(CHECKS)\n",
        "tests/test_mutation_shards.py::test_the_list_the_shards_run_includes_every_file_in_the_folder",
    ),
    (
        "the files of checks join the list in file-name order",
        "tests/mutation_smoke.py",
        '    for path in sorted(folder.glob("*.py")):\n',
        '    for path in sorted(folder.glob("*.py"), reverse=True):\n',
        "tests/test_mutation_shards.py::test_checks_kept_one_file_per_change_join_the_list_in_file_name_order",
    ),
    (
        "a file of checks that doesn't define a tuple of them is refused",
        "tests/mutation_smoke.py",
        "        if not isinstance(checks, tuple):\n",
        "        if False:\n",
        "tests/test_mutation_shards.py::test_a_file_that_doesnt_define_a_tuple_of_checks_is_refused",
    ),
    (
        "two entries run together into one are refused",
        "tests/mutation_smoke.py",
        "len(check) != 5 or not all(isinstance(part, str) for part in check)",
        "len(check) < 5 or not all(isinstance(part, str) for part in check)",
        "tests/test_mutation_shards.py::test_two_entries_run_together_into_one_are_refused",
    ),
    (
        "a check whose parts aren't all text is refused",
        "tests/mutation_smoke.py",
        "len(check) != 5 or not all(isinstance(part, str) for part in check) \\\n",
        "len(check) != 5 \\\n",
        "tests/test_mutation_shards.py::test_a_malformed_check_is_refused[a-part-that-isnt-text]",
    ),
    (
        "a check with no test to fail is refused",
        "tests/mutation_smoke.py",
        "not all(check[i] for i in (0, 1, 2, 4))",
        "not all(check[i] for i in (0, 1, 2))",
        "tests/test_mutation_shards.py::test_a_malformed_check_is_refused[no-test]",
    ),
    (
        "a check that changes nothing is refused",
        "tests/mutation_smoke.py",
        " or check[2] == check[3]:\n",
        ":\n",
        "tests/test_mutation_shards.py::test_a_malformed_check_is_refused[a-broken-that-changes-nothing]",
    ),
    (
        "two checks with one name are refused",
        "tests/mutation_smoke.py",
        "        if check[0] in names:\n",
        "        if False:\n",
        "tests/test_mutation_shards.py::test_two_checks_with_one_name_are_refused",
    ),
)
