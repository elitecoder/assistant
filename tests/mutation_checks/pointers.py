"""Checks for src/assistant/pointers.py: trace ids and row pointers."""

CHECKS = (
    (
        "a trace id is minted from the system's randomness",
        "src/assistant/pointers.py",
        '    return "tr-" + secrets.token_hex(8)\n',
        '    return "tr-" + "0" * 16\n',
        "tests/test_pointers.py::test_a_new_trace_id_has_one_shape_and_is_never_the_same_twice",
    ),
    (
        "a legacy trace id keeps its kind and key apart",
        "src/assistant/pointers.py",
        'f"{kind}\\0{key}"',
        'f"{kind}{key}"',
        "tests/test_pointers.py::test_a_legacy_trace_id_is_the_same_every_time_and_marked_as_not_minted",
    ),
    (
        "only the whole of a trace id counts as one",
        "src/assistant/pointers.py",
        "_TRACE_ID.fullmatch(value) is not None",
        "_TRACE_ID.match(value) is not None",
        "tests/test_pointers.py::test_anything_else_is_not_a_trace_id",
    ),
    (
        "something that isn't text is never a trace id",
        "src/assistant/pointers.py",
        "    return isinstance(value, str) and _TRACE_ID",
        "    return _TRACE_ID",
        "tests/test_pointers.py::test_anything_else_is_not_a_trace_id",
    ),
    (
        "a row pointer holds a hash of the link, never the link",
        "src/assistant/pointers.py",
        '    return "row:" + hashlib.sha256(str(key).encode("utf-8", "replace")).hexdigest()[:16]\n',
        '    return "row:" + str(key)[:16]\n',
        "tests/test_pointers.py::test_a_row_pointer_is_a_short_id_with_nothing_of_the_link_in_it",
    ),
)
