from sealbox import logship


def entry(text="hello", container=None, ident="smh-boot", ts=1_700_000_000_000_000):
    e = {"MESSAGE": text, "__REALTIME_TIMESTAMP": str(ts), "SYSLOG_IDENTIFIER": ident}
    if container:
        e["CONTAINER_NAME"] = container
    return e


def shipper(cap=10_000):
    sent, clock = [], [0.0]
    return logship.Shipper(sent.append, lambda: clock[0], cap), sent, clock


def test_a_line_carries_its_source_and_time():
    assert logship.pick(entry("LOADER DONE")) == (1_700_000_000_000, "smh-boot: LOADER DONE")
    assert logship.pick(entry("ready", container="model-server"))[1] == "model-server: ready"


def test_trainer_output_never_leaves():
    s, sent, _ = shipper()
    s.add(entry("step 3 loss 0.1234 grad 0.5", container="trainer"))
    s.flush()
    assert sent == []


def test_binary_empty_and_broken_entries_are_skipped():
    assert logship.pick(entry([104, 105])) is None
    assert logship.pick(entry("   ")) is None
    assert logship.pick({"MESSAGE": "no time"}) is None


def test_long_lines_are_cut():
    assert len(logship.pick(entry("x" * 5000))[1]) == len("smh-boot: ") + logship.MAX_LINE


def test_hourly_cap_drops_and_reports():
    s, sent, clock = shipper(cap=100)
    for _ in range(10):
        s.add(entry("y" * 30))            # 40 bytes each with the source: two fit
    s.flush()
    assert len(sent[0]) == 2 and s.dropped == 8
    clock[0] = 3600
    s.add(entry("after the hour"))
    s.flush()
    assert [text for _, text in sent[1]] == ["smh-logship: 8 line(s) dropped by the hourly cap", "smh-boot: after the hour"]


def test_batches_are_in_time_order():
    s, sent, _ = shipper()
    s.add(entry("second", ts=2_000_000))
    s.add(entry("first", ts=1_000_000))
    s.flush()
    assert [text for _, text in sent[0]] == ["smh-boot: first", "smh-boot: second"]


def test_lines_are_kept_when_sending_fails_and_sent_later():
    sent, fail = [], [True]

    def send(batch):
        if fail[0]:
            raise OSError("no route")
        sent.append(batch)

    s = logship.Shipper(send, lambda: 0.0, 10_000)
    s.add(entry("kept"))
    try:
        s.flush()
    except OSError:
        pass
    fail[0] = False
    s.flush()
    assert [text for _, text in sent[0]] == ["smh-boot: kept"]
