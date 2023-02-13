import json
import os
from typing import Optional, Union, Tuple, TypedDict, Literal, overload

import torch
from torch import nn, device as TorchDevice

from dataset import TrainConfig
from models.fastspeech2 import PitchAndDurationPredictor, MelSpectrogramDecoder, \
    ModelConfig, FeatureEmbedder, VocoderType, VocoderGenerator
from models.optimizer import ScheduledOptim
from preprocessor import PreProcessConfig


class Config(TypedDict):
    preprocess: PreProcessConfig
    model: ModelConfig
    train: TrainConfig


@overload
def get_model(
    restore_step: int,
    config: Config,
    device: torch.device,
    speaker_num: int,
    train: True,
) -> Tuple[PitchAndDurationPredictor, FeatureEmbedder, MelSpectrogramDecoder, ScheduledOptim, int]:
    pass


@overload
def get_model(
    restore_step: int,
    config: Config,
    device: torch.device,
    speaker_num: int,
    train: False,
) -> Tuple[PitchAndDurationPredictor, FeatureEmbedder, MelSpectrogramDecoder, None, int]:
    pass


def get_model(
    restore_step: int,
    config: Config,
    device: torch.device,
    speaker_num: int,
    train: bool = False,
):
    preprocessed_path = config["preprocess"]["path"]["preprocessed_path"]
    with open(os.path.join(preprocessed_path, "stats.json")) as f:
        stats_text = f.read()
    stats_json = json.loads(stats_text)
    pitch_min, pitch_max = stats_json["pitch"][:2]
    variance_model = PitchAndDurationPredictor(config["model"], speaker_num).to(device)
    embedder_model = FeatureEmbedder(config["model"], speaker_num, pitch_min, pitch_max).to(device)
    decoder_model = MelSpectrogramDecoder(config["model"]).to(device)

    epoch = -1
    if restore_step:
        ckpt_path = os.path.join(
            config["train"]["path"]["ckpt_path"],
            "{}.pth.tar".format(restore_step),
        )
        ckpt = torch.load(ckpt_path, map_location=device)
        variance_model.load_state_dict(ckpt["variance_model"])
        embedder_model.load_state_dict(ckpt["embedder_model"])
        decoder_model.load_state_dict(ckpt["decoder_model"])
        epoch = ckpt["epoch"]

    if train:
        scheduled_optim = ScheduledOptim(
            variance_model, embedder_model, decoder_model, config["train"], config["model"], epoch
        )
        if restore_step:
            scheduled_optim.load_state_dict(ckpt["optimizer"])
        variance_model.train()
        embedder_model.train()
        decoder_model.train()
        return variance_model, embedder_model, decoder_model, scheduled_optim, epoch

    variance_model.eval()
    embedder_model.eval()
    decoder_model.eval()
    variance_model.requires_grad_ = False
    embedder_model.requires_grad_ = False
    decoder_model.requires_grad_ = False
    return variance_model, embedder_model, decoder_model, None, epoch


def get_param_num(model: nn.Module) -> int:
    num_param = sum(param.numel() for param in model.parameters())
    return num_param


def get_vocoder(device: TorchDevice, type: VocoderType = "fregan") -> VocoderGenerator:
    if type == "fregan":
        import fregan
        config = fregan.Config()
        vocoder = fregan.Generator(config)
        ckpt = torch.load(f"fregan/g_0003000.pth.tar", map_location=device)
        vocoder.load_state_dict(ckpt["generator"])
        vocoder.eval()
        vocoder.remove_weight_norm()
    elif type == "hifigan":
        import hifigan
        config = hifigan.Config()
        vocoder = hifigan.Generator(config)
        ckpt = torch.load(f"hifigan/g_00445000", map_location=device)
        vocoder.load_state_dict(ckpt["generator"])
        vocoder.eval()
        vocoder.remove_weight_norm()
    else:
        raise Exception(f"Unsupported vocoder: {type}")
    vocoder.to(device)
    return vocoder
