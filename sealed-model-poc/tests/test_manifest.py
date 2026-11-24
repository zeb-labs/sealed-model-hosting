import copy

import pytest

from sealbox import manifest

GOOD = {
    "bucket": "builder-model-bucket",
    "key_arn": "arn:aws:kms:us-east-1:123456789012:key/1234abcd-12ab-34cd-56ef-1234567890ab",
    "images": [
        {"name": "vllm", "digest": "sha256:" + "a" * 64},
        {"name": "backend", "digest": "sha256:" + "b" * 64},
    ],
}


def test_fingerprint_is_stable_and_96_hex_characters():
    first = manifest.digest(GOOD)
    assert first == manifest.digest(copy.deepcopy(GOOD))
    assert len(first) == 96
    int(first, 16)


def test_image_order_does_not_change_the_fingerprint():
    flipped = copy.deepcopy(GOOD)
    flipped["images"].reverse()
    assert manifest.digest(flipped) == manifest.digest(GOOD)


@pytest.mark.parametrize(
    "path,value",
    [
        (("bucket",), "another-bucket"),
        (("key_arn",), GOOD["key_arn"].replace("1234abcd", "ffffabcd")),
        (("images", 0, "digest"), "sha256:" + "c" * 64),
    ],
)
def test_any_change_changes_the_fingerprint(path, value):
    changed = copy.deepcopy(GOOD)
    target = changed
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    assert manifest.digest(changed) != manifest.digest(GOOD)


def test_unknown_and_missing_fields_are_refused():
    bad = {"bucket": GOOD["bucket"], "extra": 1}
    with pytest.raises(manifest.ManifestError) as caught:
        manifest.validate(bad)
    text = " | ".join(caught.value.problems)
    assert "unknown fields: extra" in text
    assert "missing field: images" in text and "missing field: key_arn" in text


@pytest.mark.parametrize(
    "digest", ["latest", "sha256:abc", "sha256:" + "A" * 64, "md5:" + "a" * 64]
)
def test_images_must_be_pinned_by_full_digest(digest):
    bad = copy.deepcopy(GOOD)
    bad["images"][0]["digest"] = digest
    with pytest.raises(manifest.ManifestError):
        manifest.validate(bad)


def test_repeated_image_names_are_refused():
    bad = copy.deepcopy(GOOD)
    bad["images"][1]["name"] = "vllm"
    with pytest.raises(manifest.ManifestError):
        manifest.validate(bad)


def test_key_alias_is_refused():
    bad = copy.deepcopy(GOOD)
    bad["key_arn"] = "alias/GuardianEncryptionKey"
    with pytest.raises(manifest.ManifestError):
        manifest.validate(bad)
