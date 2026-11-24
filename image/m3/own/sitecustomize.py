"""Loaded by every Python process of the conversion container when the model is the builder's OWN folder.

vLLM's tensorize step ends by asking Hugging Face for the model's small files (settings, tokenizer) by name
(`serialize_extra_artifacts` -> `HfApi.snapshot_download`). A folder has no name there. So for a folder, hand
those files over from the folder itself. Weights are not touched here, and nothing is sent anywhere.
Used on the builder's conversion machine only; it is not part of the sealed image.
"""
import os
import shutil

try:
    import huggingface_hub

    _original = huggingface_hub.HfApi.snapshot_download
    _SKIP = (".pt", ".safetensors", ".bin", ".cache", ".gitattributes", ".md")

    def _snapshot_download(self, repo_id, *args, **kwargs):
        if isinstance(repo_id, str) and os.path.isdir(repo_id):
            target = kwargs.get("local_dir")
            if target is None:
                return repo_id
            for entry in os.scandir(repo_id):
                if entry.is_file() and not entry.name.endswith(_SKIP):
                    shutil.copy(entry.path, os.path.join(str(target), entry.name))
            return str(target)
        return _original(self, repo_id, *args, **kwargs)

    huggingface_hub.HfApi.snapshot_download = _snapshot_download
except Exception:      # never stop the interpreter from starting
    pass
