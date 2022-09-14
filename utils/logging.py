from typing import Optional, Sequence, Union, TypedDict

import numpy as np
from torch import Tensor
from torch.utils.tensorboard import SummaryWriter
from matplotlib import pyplot as plt

LossValue = Union[Tensor, np.ndarray, float]


class LossDict(TypedDict):
    total_loss: LossValue
    mel_loss: LossValue
    duration_loss: LossValue
    pitch_loss: LossValue
    alignment_loss: LossValue
    generator_loss: Optional[LossValue]
    discriminator_loss: Optional[LossValue]


def log(
    logger: SummaryWriter,
    step: Optional[int] = None,
    loss_dict: Optional[LossDict] = None,
    fig: Optional[plt.Figure] = None,
    audio: Optional[np.ndarray] = None,
    sampling_rate: int = 48000,
    tag: str = ""
):
    if loss_dict is not None:
        keys = list(loss_dict.keys())
        for key in keys:
            if loss_dict[key] is None:
                del loss_dict[key]

        logger.add_scalars("Loss", loss_dict, step)
    if fig is not None:
        logger.add_figure(tag, fig, step)

    if audio is not None:
        logger.add_audio(
            tag,
            audio / max(abs(audio)),
            step,
            sample_rate=sampling_rate,
        )
