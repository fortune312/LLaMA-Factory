import torch
import inspect
from typing import TYPE_CHECKING, Any, Dict, List
from transformers import PreTrainedModel
from transformers.utils import cached_file
from transformers.trainer import WEIGHTS_NAME, SAFE_WEIGHTS_NAME

from llmtuner.extras.logging import get_logger
from llmtuner.extras.misc import get_current_device

if TYPE_CHECKING:
    from transformers import PretrainedConfig, PreTrainedTokenizer
    from llmtuner.hparams import ModelArguments, DataArguments, FinetuningArguments


logger = get_logger(__name__)


def dispatch_model(model: "PreTrainedModel") -> "PreTrainedModel":
    r"""
    Dispatches a pre-trained model to GPUs with balanced memory when the GPU is available.
    Borrowed from: https://github.com/huggingface/transformers/blob/v4.36.2/src/transformers/modeling_utils.py#L3570
    """
    if getattr(model, "quantization_method", None): # already set on current device
        return model

    if (
        torch.cuda.device_count() > 1
        and isinstance(model, PreTrainedModel)
        and getattr(model.config, "model_type", None) != "chatglm"
    ):
        from accelerate import dispatch_model
        from accelerate.utils import infer_auto_device_map, get_balanced_memory

        if getattr(model, "_no_split_modules", None) is None:
            raise ValueError("The model class needs to implement the `_no_split_modules` attribute.")

        kwargs = {"dtype": model.dtype, "no_split_module_classes": model._get_no_split_modules("auto")}
        max_memory = get_balanced_memory(model, **kwargs)
        # Make sure tied weights are tied before creating the device map.
        model.tie_weights()
        device_map = infer_auto_device_map(model, max_memory=max_memory, **kwargs)
        device_map_kwargs = {"device_map": device_map}
        if "skip_keys" in inspect.signature(dispatch_model).parameters:
            device_map_kwargs["skip_keys"] = model._skip_keys_device_placement
        return dispatch_model(model, **device_map_kwargs)
    else:
        return model.to(device=get_current_device())


def find_all_linear_modules(model: "PreTrainedModel") -> List[str]:
    r"""
    Finds all available modules to apply lora.
    """
    quantization_method = getattr(model, "quantization_method", None)
    if quantization_method is None:
        linear_cls = torch.nn.Linear
    elif quantization_method == "bitsandbytes":
        import bitsandbytes as bnb
        linear_cls = bnb.nn.Linear4bit if getattr(model, "is_loaded_in_4bit", False) else bnb.nn.Linear8bitLt
    else:
        raise ValueError("Finding linear modules for {} models is not supported.".format(quantization_method))

    output_layer_names = ["lm_head"]
    if model.config.model_type == "chatglm":
        output_layer_names.append("output_layer")

    module_names = set()
    for name, module in model.named_modules():
        if (
            isinstance(module, linear_cls)
            and not any([output_layer in name for output_layer in output_layer_names])
        ):
            module_names.add(name.split(".")[-1])

    logger.info("Found linear modules: {}".format(",".join(module_names)))
    return list(module_names)


def get_modelcard_args(
    model_args: "ModelArguments",
    data_args: "DataArguments",
    finetuning_args: "FinetuningArguments"
) -> Dict[str, Any]:
    return {
        "tasks": "text-generation",
        "license": "other",
        "finetuned_from": model_args.model_name_or_path,
        "dataset": [dataset.strip() for dataset in data_args.dataset.split(",")],
        "tags": ["llama-factory"] + (["lora"] if finetuning_args.finetuning_type == "lora" else [])
    }


def load_valuehead_params(path_or_repo_id: str, model_args: "ModelArguments") -> Dict[str, torch.Tensor]:
    r"""
    Loads value head parameters from Hugging Face Hub or local disk.

    Returns: dict with keys `v_head.summary.weight` and `v_head.summary.bias`.
    """
    kwargs = {
        "path_or_repo_id": path_or_repo_id,
        "cache_dir": model_args.cache_dir,
        "token": model_args.hf_hub_token
    }

    try:
        from safetensors import safe_open
        vhead_file = cached_file(filename=SAFE_WEIGHTS_NAME, **kwargs)
        with safe_open(vhead_file, framework="pt", device="cpu") as f:
            return {
                "v_head.summary.weight": f.get_tensor("v_head.summary.weight"),
                "v_head.summary.bias": f.get_tensor("v_head.summary.bias")
            }
    except Exception as err:
        logger.info("Failed to load {}: {}".format(SAFE_WEIGHTS_NAME, str(err)))

    try:
        vhead_file = cached_file(filename=WEIGHTS_NAME, **kwargs)
        return torch.load(vhead_file, map_location="cpu")
    except Exception as err:
        logger.info("Failed to load {}: {}".format(WEIGHTS_NAME, str(err)))

    logger.warning("Provided path ({}) does not contain valuehead weights.".format(path_or_repo_id))
    return None


def register_autoclass(config: "PretrainedConfig", model: "PreTrainedModel", tokenizer: "PreTrainedTokenizer"):
    if "AutoConfig" in getattr(config, "auto_map", {}):
        config.__class__.register_for_auto_class()
    if "AutoModelForCausalLM" in getattr(config, "auto_map", {}):
        model.__class__.register_for_auto_class()
    if "AutoTokenizer" in tokenizer.init_kwargs.get("auto_map", {}):
        tokenizer.__class__.register_for_auto_class()
