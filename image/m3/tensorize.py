"""Run inside the vLLM container on a GPU machine: save the model in tensorizer
format (no tensorizer encryption; our own encryption is applied afterwards)."""
import sys

from vllm.engine.arg_utils import EngineArgs
from vllm.model_executor.model_loader.tensorizer import TensorizerConfig, tensorize_vllm_model

model = sys.argv[1]
tensorize_vllm_model(EngineArgs(model=model), TensorizerConfig(tensorizer_uri="/out/model.tensors"))
print("TENSORIZED", model)
