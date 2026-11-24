from sealbox import gatekeys

K1, K2 = "a" * 24, "Zx9_-" * 8


def test_valid_keys_become_gate_entries():
    text, n, ignored = gatekeys.render(f"# my keys\n{K1}\n\n  {K2}  \n")
    assert text == f'"Bearer {K1}" 1;\n"Bearer {K2}" 1;\n' and (n, ignored) == (2, 0)


def test_no_file_or_no_valid_key_means_nobody_gets_in():
    assert gatekeys.render("") == ("", 0, 0)
    assert gatekeys.render("short\n")[0] == ""


def test_lines_that_could_break_the_gate_file_are_ignored():
    bad = ['x" 1; } server { listen 80; #' + "a" * 24, "a" * 23, "a" * 129, "key with spaces " + "a" * 24,
           "a" * 24 + ";", "a" * 24 + '"', "$http_host" + "a" * 24, "é" * 24, "a" * 12 + "\t" + "a" * 12]
    text, n, ignored = gatekeys.render("\n".join(bad + [K1]))
    assert text == f'"Bearer {K1}" 1;\n' and n == 1 and ignored == len(bad)
    assert all(c not in text.replace('"Bearer ', "").replace('" 1;\n', "") for c in '";{}$ ')


def test_duplicates_and_floods_are_capped():
    keys = [f"{i:024d}" for i in range(80)]
    text, n, ignored = gatekeys.render("\n".join(keys + keys[:5]))
    assert n == 50 and text.count("\n") == 50 and ignored == 35
    assert gatekeys.render("x" * (1 << 20))[1] == 0            # a huge file is cut, not read whole


def test_reason_never_shows_a_key():
    text, _, _ = gatekeys.render(K1 + "\n" + K2)
    out = gatekeys.reason(f'nginx: [emerg] bad "{K1}" and Bearer {K2}\n in keys.map:1\n', text)
    assert K1 not in out and K2 not in out and "emerg" in out and len(out) <= 200


def test_longest_key_fits_the_gate_bucket():
    # nginx needs 8 + round_up_to_8(len + 2) bytes per entry, within bucket - 8 (ngx_hash.c)
    import os, re
    conf = open(os.path.join(os.path.dirname(__file__), "..", "..", "image", "root", "etc", "nginx", "nginx.conf")).read()
    bucket = int(re.search(r"map_hash_bucket_size (\d+);", conf)[1])
    longest = len("Bearer ") + 128
    assert 8 + -(-(longest + 2) // 8) * 8 <= bucket - 8
