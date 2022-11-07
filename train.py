import argparse
import os

import torch
import torch.multiprocessing as mp
import yaml
from torch import nn
from torch.distributed import init_process_group
from torch.nn.parallel import DistributedDataParallel
from torch.utils.data import DataLoader, DistributedSampler
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

import fregan
from dataset import Dataset
from modules.gaussian_upsampling import GaussianUpsampling
from modules.loss import FastSpeech2Loss
from utils.logging import log, LossDict
from utils.mask import make_non_pad_mask
from utils.model import Config, get_model, get_param_num, get_vocoder
from utils.synth import synth_one_sample
from utils.tools import to_device, ReProcessedItemTorch

torch.backends.cudnn.benchmark = True


def evaluate(
    fs2_model: nn.Module,  # FastSpeech2
    step: int,
    config: Config,
    logger: SummaryWriter,
    vocoder: fregan.Generator = None,
):
    # Get dataset
    dataset = Dataset(
        "val.txt", config["preprocess"], config["train"], sort=False, drop_last=False
    )
    batch_size = config["train"]["optimizer"]["batch_size"]
    device = torch.device('cuda:0')
    loader = DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=False,
        collate_fn=dataset.collate_fn,
    )

    # Get loss function
    Loss = FastSpeech2Loss().to(device)

    # Evaluation
    loss_mean_dict: LossDict = {
        "total_loss": 0.0,
        "mel_loss": 0.0,
        "postnet_mel_loss": 0.0,
        "duration_loss": 0.0,
        "pitch_loss": 0.0,
        "alignment_loss": 0.0
    }
    count = 0
    for batchs in loader:
        for _batch in batchs:
            batch: ReProcessedItemTorch = to_device(_batch, device)
            (
                ids,
                speakers,
                phonemes,
                phoneme_lens,
                max_phoneme_len,
                accents,
                mels,
                mel_lens,
                max_mel_len,
                pitches,
            ) = batch
            with torch.no_grad():
                (
                    pitch_outputs,
                    log_duration_outputs,
                    avg_pitches,
                    durations,
                    log_p_attn,
                    bin_loss,
                    outputs,
                    postnet_outputs
                ) = fs2_model(
                    phonemes=phonemes,
                    accents=accents,
                    pitches=pitches,
                    speakers=speakers,
                    phoneme_lens=phoneme_lens,
                    max_phoneme_len=max_phoneme_len,
                    mels=mels,
                    mel_lens=mel_lens,
                )

                # Cal Loss
                total_loss, mel_loss, postnet_mel_loss, duration_loss, pitch_loss, align_loss = Loss(
                    outputs=outputs,
                    postnet_outputs=postnet_outputs,
                    log_duration_outputs=log_duration_outputs,
                    pitch_outputs=pitch_outputs,
                    mel_targets=mels,
                    duration_targets=durations,
                    pitch_targets=avg_pitches,
                    log_p_attn=log_p_attn,
                    input_lens=phoneme_lens,
                    output_lens=mel_lens,
                )
                bin_loss *= 2.0
                align_loss += bin_loss

                # total loss
                total_loss += bin_loss

                loss_dict: LossDict = {
                    "total_loss": total_loss,
                    "mel_loss": mel_loss,
                    "postnet_mel_loss": postnet_mel_loss,
                    "duration_loss": duration_loss,
                    "pitch_loss": pitch_loss,
                    "alignment_loss": align_loss
                }

                for key in loss_dict.keys():
                    loss_value = loss_dict[key]
                    if loss_value is not None:
                        loss_mean_dict[key] += loss_dict[key].item() * len(batch[0])

            if count < 5:
                fig, wav_reconstruction, wav_prediction, tag = synth_one_sample(
                    ids=ids,
                    duration_targets=durations,
                    pitch_targets=avg_pitches,
                    mel_targets=mels,
                    mel_predictions=postnet_outputs,
                    phoneme_lens=phoneme_lens,
                    mel_lens=mel_lens,
                    vocoder=vocoder,
                    config=config,
                    synthesis_target=(step == int(config["train"]["step"]["val_step"]))
                )

                log(
                    logger,
                    fig=fig,
                    tag="Validation/{}".format(tag),
                    step=step,
                )
                sampling_rate = config["preprocess"]["audio"]["sampling_rate"]
                log(
                    logger,
                    audio=wav_reconstruction,
                    sampling_rate=sampling_rate,
                    tag="Validation/{}_gt".format(tag),
                )
                log(
                    logger,
                    audio=wav_prediction,
                    sampling_rate=sampling_rate,
                    tag="Validation/{}_synthesized".format(tag),
                    step=step,
                )
            count += 1

    for key in loss_mean_dict.keys():
        loss_value = loss_mean_dict[key]
        if loss_value is not None:
            loss_mean_dict[key] = loss_mean_dict[key] / len(dataset)

    log(logger, step, loss_dict=loss_mean_dict)

    message1 = "Validation Step {}, ".format(step)
    message2 = (
        "Total Loss: {total_loss:.4f}, "
        "Mel Loss: {mel_loss:.4f}, "
        "Mel PostNet Loss: {postnet_mel_loss:.4f}, "
        "Duration Loss: {duration_loss:.4f}, "
        "Pitch Loss: {pitch_loss:.4f}, "
        "Alignment Loss: {alignment_loss:.4f}"
    ).format(
        **loss_mean_dict
    )

    return message1 + message2


def main(rank: int, restore_step: int, speaker_num, config: Config, num_gpus: int):
    if rank == 0:
        print("Prepare training ...")
    if num_gpus > 1:
        init_process_group(backend="nccl", init_method="env://",
                           world_size=num_gpus, rank=rank)
    device = torch.device('cuda:{:d}'.format(rank))

    # Get dataset
    dataset = Dataset(
        "train.txt", config["preprocess"], config["train"], sort=True, drop_last=True
    )
    batch_size = config["train"]["optimizer"]["batch_size"]
    if num_gpus > 1:
        group_size = 1
    else:
        group_size = 4  # Set this larger than 1 to enable sorting in Dataset
    assert batch_size * group_size < len(dataset)

    sampler = DistributedSampler(dataset) if num_gpus > 1 else None

    cpu_count = os.cpu_count()
    if cpu_count > 8:
        cpu_count = 8
    loader = DataLoader(
        dataset,
        batch_size=batch_size * group_size,
        shuffle=num_gpus == 1,
        num_workers=cpu_count,
        sampler=sampler,
        pin_memory=True,
        drop_last=True,
        collate_fn=dataset.collate_fn,
    )

    # Prepare model
    fs2_model, optimizer, epoch = get_model(restore_step, config, device, speaker_num, train=True)
    if num_gpus > 1:
        fs2_model = DistributedDataParallel(fs2_model, device_ids=[rank]).to(device)

    num_param = get_param_num(fs2_model)
    Loss = FastSpeech2Loss().to(device)
    if rank == 0:
        print("Number of FastSpeech2 Parameters:", num_param)

    # Load vocoder
    vocoder = get_vocoder(device, config["model"]["vocoder_type"])

    # Init logger
    for p in config["train"]["path"].values():
        os.makedirs(p, exist_ok=True)
    train_log_path = os.path.join(config["train"]["path"]["log_path"], "train")
    val_log_path = os.path.join(config["train"]["path"]["log_path"], "val")
    os.makedirs(train_log_path, exist_ok=True)
    os.makedirs(val_log_path, exist_ok=True)
    train_logger = SummaryWriter(train_log_path)
    val_logger = SummaryWriter(val_log_path)

    # Training
    step = restore_step + 1
    epoch = max(1, epoch)
    total_step = config["train"]["step"]["total_step"]
    log_step = config["train"]["step"]["log_step"]
    save_step = config["train"]["step"]["save_step"]
    synth_step = config["train"]["step"]["synth_step"]
    val_step = config["train"]["step"]["val_step"]
    variance_learn_start = config["train"]["step"]["variance_learn_start"]

    if rank == 0:
        outer_bar = tqdm(total=total_step, desc="Training", position=0)
        outer_bar.n = restore_step
        outer_bar.update()

    while True:
        if rank == 0:
            inner_bar = tqdm(total=len(loader), desc="Epoch {}".format(epoch), position=1)

        if num_gpus > 1:
            sampler.set_epoch(epoch)

        for batchs in loader:
            for _batch in batchs:
                batch: ReProcessedItemTorch = to_device(_batch, device)
                (
                    ids,
                    speakers,
                    phonemes,
                    phoneme_lens,
                    max_phoneme_len,
                    accents,
                    mels,
                    mel_lens,
                    max_mel_len,
                    pitches,
                ) = batch

                # Forward
                (
                    pitch_outputs,
                    log_duration_outputs,
                    avg_pitches,
                    durations,
                    log_p_attn,
                    bin_loss,
                    outputs,
                    postnet_outputs
                ) = fs2_model(
                    phonemes=phonemes,
                    accents=accents,
                    pitches=pitches,
                    speakers=speakers,
                    phoneme_lens=phoneme_lens,
                    max_phoneme_len=max_phoneme_len,
                    mels=mels,
                    mel_lens=mel_lens,
                )

                # Cal Loss
                total_loss, mel_loss, postnet_mel_loss, duration_loss, pitch_loss, align_loss = Loss(
                    outputs=outputs,
                    postnet_outputs=postnet_outputs,
                    log_duration_outputs=log_duration_outputs,
                    pitch_outputs=pitch_outputs,
                    mel_targets=mels,
                    duration_targets=durations,
                    pitch_targets=avg_pitches,
                    log_p_attn=log_p_attn,
                    input_lens=phoneme_lens,
                    output_lens=mel_lens,
                    variance_learn=variance_learn_start < step
                )
                # align loss
                bin_loss = bin_loss * 2.0  # loss scaling
                align_loss += bin_loss

                total_loss = total_loss + bin_loss

                loss_dict: LossDict = {
                    "total_loss": total_loss,
                    "mel_loss": mel_loss,
                    "postnet_mel_loss": postnet_mel_loss,
                    "duration_loss": duration_loss,
                    "pitch_loss": pitch_loss,
                    "alignment_loss": align_loss
                }

                # Backward
                total_loss.backward()

                # Update weights
                optimizer.step_and_update_lr()
                optimizer.zero_grad()

                if rank == 0:
                    if step % log_step == 0:
                        message1 = "Step {}/{}, ".format(step, total_step)
                        message2 = (
                            "Total Loss: {total_loss:.4f}, "
                            "Mel Loss: {mel_loss:.4f}, "
                            "Mel PostNet Loss: {postnet_mel_loss:.4f}, "
                            "Duration Loss: {duration_loss:.4f}, "
                            "Pitch Loss: {pitch_loss:.4f}, "
                            "Alignment Loss: {alignment_loss:.4f}"
                        ).format(
                            **loss_dict
                        )

                        with open(os.path.join(train_log_path, "log.txt"), "a") as f:
                            f.write(message1 + message2 + "\n")

                        outer_bar.write(message1 + message2)

                        log(train_logger, step, loss_dict=loss_dict)

                    if step % synth_step == 0:
                        fig, wav_reconstruction, wav_prediction, tag = synth_one_sample(
                            ids=ids,
                            duration_targets=durations,
                            pitch_targets=avg_pitches,
                            mel_targets=mels,
                            mel_predictions=postnet_outputs,
                            phoneme_lens=phoneme_lens,
                            mel_lens=mel_lens,
                            vocoder=vocoder,
                            config=config
                        )
                        log(
                            train_logger,
                            fig=fig,
                            tag="Training/step_{}_{}".format(step, tag),
                        )
                        sampling_rate = config["preprocess"]["audio"]["sampling_rate"]
                        log(
                            train_logger,
                            audio=wav_reconstruction,
                            sampling_rate=sampling_rate,
                            tag="Training/step_{}_{}_reconstructed".format(step, tag),
                        )
                        log(
                            train_logger,
                            audio=wav_prediction,
                            sampling_rate=sampling_rate,
                            tag="Training/step_{}_{}_synthesized".format(step, tag),
                        )

                    if step % val_step == 0:
                        fs2_model.eval()
                        message = evaluate(
                            fs2_model, step, config, val_logger, vocoder
                        )
                        with open(os.path.join(val_log_path, "log.txt"), "a") as f:
                            f.write(message + "\n")
                        outer_bar.write(message)

                        fs2_model.train()

                    if step % save_step == 0:
                        torch.save(
                            {
                                "model": (fs2_model.module if num_gpus > 1 else fs2_model).state_dict(),
                                "optimizer": optimizer._optimizer.state_dict(),
                                "epoch": epoch - 1,
                            },
                            os.path.join(
                                config["train"]["path"]["ckpt_path"],
                                "{:08d}.pth.tar".format(step),
                            ),
                        )

                if step == total_step:
                    quit()
                step += 1

                if rank == 0:
                    outer_bar.update()

            if rank == 0:
                inner_bar.update()
        epoch += 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=str, help="path to config yaml")
    parser.add_argument("--restore_step", type=int, default=0)
    parser.add_argument("--speaker_num", type=int, default=10)
    args = parser.parse_args()

    config: Config = yaml.load(
        open(args.config, "r"), Loader=yaml.FullLoader
    )

    num_gpus = 0
    if torch.cuda.is_available():
        num_gpus = torch.cuda.device_count()
        batch_size = config["train"]["optimizer"]["batch_size"]
        config["train"]["optimizer"]["batch_size"] = int(batch_size / num_gpus)
        print('Batch size per GPU :', config["train"]["optimizer"]["batch_size"])
    else:
        raise Exception("cuda is not available")

    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '54321'

    if num_gpus > 1:
        mp.spawn(main, nprocs=num_gpus, args=(args.restore_step, args.speaker_num, config, num_gpus,))
    else:
        main(0, args.restore_step, args.speaker_num, config, num_gpus)
