import bisect

import torch
from pytorch_lightning.utilities.types import EPOCH_OUTPUT

from opt import get_opts
import os

os.environ['CUDA_LAUNCH_BLOCKING'] = '1'
import glob
import imageio
import numpy as np
import cv2
from einops import rearrange

# data
from torch.utils.data import DataLoader

# models
from kornia.utils.grid import create_meshgrid3d
from models.networks import NGPNetwork

# optimizer, losses
from apex.optimizers import FusedAdam
from torch.optim.lr_scheduler import CosineAnnealingLR

# metrics
from torchmetrics import (
    PeakSignalNoiseRatio,
    StructuralSimilarityIndexMeasure
)

# pytorch-lightning
from pytorch_lightning.plugins import DDPPlugin
from pytorch_lightning import LightningModule, Trainer
from pytorch_lightning.callbacks import TQDMProgressBar, ModelCheckpoint
from pytorch_lightning.loggers import TensorBoardLogger

from datasets.nsvf import NSVFDataset
from datasets.colmap import ColmapDataset
from datasets.rffr import RffrDataset
from utils.ray_utils import get_rays
from utils.render_utils import render, MAX_SAMPLES, render_REN
from utils.custom_utils import VolumeRenderer_REN
from losses import L1Loss, MSELoss, NeRFLoss, EdgePreservingSmoothnessLoss, SmoothnessLoss


def slim_ckpt(ckpt_path, save_poses=False):
    ckpt = torch.load(ckpt_path, map_location='cpu')
    # pop unused parameters
    keys_to_pop = ['directions', 'model.density_grid', 'model.grid_coords']
    if not save_poses: keys_to_pop += ['poses']
    for k in ckpt['state_dict']:
        if k.startswith('val_lpips'):
            keys_to_pop += [k]
    for k in keys_to_pop:
        ckpt['state_dict'].pop(k, None)
    return ckpt['state_dict']


def load_ckpt(model, ckpt_path, model_name='model', prefixes_to_ignore=[]):
    if not ckpt_path: return
    model_dict = model.state_dict()
    checkpoint_ = extract_model_state_dict(ckpt_path, model_name, prefixes_to_ignore)
    model_dict.update(checkpoint_)
    model.load_state_dict(model_dict)


import warnings;

warnings.filterwarnings("ignore")


def extract_model_state_dict(ckpt_path, model_name='model', prefixes_to_ignore=[]):
    checkpoint = torch.load(ckpt_path, map_location='cpu')
    checkpoint_ = {}
    if 'state_dict' in checkpoint:  # if it's a pytorch-lightning checkpoint
        checkpoint = checkpoint['state_dict']
    for k, v in checkpoint.items():
        if not k.startswith(model_name):
            continue
        k = k[len(model_name) + 1:]
        for prefix in prefixes_to_ignore:
            if k.startswith(prefix):
                break
        else:
            checkpoint_[k] = v
    return checkpoint_


class NGPSystem(LightningModule):

    def __init__(self, hparams):
        """
        ???????????????
        :param hparams:
        """
        super().__init__()
        self.current_epochs = 1
        self.save_hyperparameters(hparams)
        # ????????????????????????????????????
        self.warmup_steps = 256
        # ????????????
        self.update_interval = 16
        # ????????????
        self.losses = {
            'mse': MSELoss(),
            'l1': L1Loss(),
            'smoothness': SmoothnessLoss(),
            'edge-preserving-smoothness': EdgePreservingSmoothnessLoss(),
            "ngp_loss": NeRFLoss()
        }
        # ?????? PSNR ???????????????
        self.train_psnr = PeakSignalNoiseRatio(data_range=1)
        # ?????? PSNR ???????????????
        self.val_psnr = PeakSignalNoiseRatio(data_range=1)
        # ??????????????????
        self.val_ssim = StructuralSimilarityIndexMeasure(data_range=1)
        # scale ???????????? 0.5
        self.model = NGPNetwork(scale=self.hparams.scale, dataset_name=self.hparams.dataset_name)
        # ???????????? 128
        G = self.model.grid_size
        # density_grid ???????????? [1, 128*128*128]
        self.model.register_buffer('density_grid',
                                   torch.zeros(self.model.cascades, G ** 3))
        # grid_coords ???????????? [1, 128, 128, 128, 3] ->  [2097152, 3]
        self.model.register_buffer('grid_coords',
                                   create_meshgrid3d(G, G, G, False, dtype=torch.int32).reshape(-1, 3))
        self.lambda_schedulers, self.lambda_schedulers_step = \
            self.parse_lambda_schedulers(self.hparams.lambda_schedulers), \
            self.parse_lambda_schedulers(self.hparams.lambda_schedulers_step)

    def setup(self, stage):
        """
        ????????????
        :param stage:
        :return:
        """
        dataset = None
        # ????????????????????????????????????dataset????????????
        # ???????????? ?????????????????????
        kwargs = {'root_dir': self.hparams.root_dir,
                  'downsample': self.hparams.downsample,
                  "batch_size":self.hparams.batch_size}

        if 'nsvf' == self.hparams.dataset_name:
            dataset = NSVFDataset
        elif "colmap" == self.hparams.dataset_name:
            dataset = ColmapDataset
        elif "rffr" == self.hparams.dataset_name:
            dataset = RffrDataset

        # ???????????? rays [100,640_000,3] 100????????????640_000?????? 3 rgb
        self.train_dataset = dataset(split=self.hparams.split, **kwargs)
        # ???????????? rays [200,640_000,3] 100????????????640_000?????? 3 rgb
        self.test_dataset = dataset(split='test', **kwargs)
        # ???????????????????????????????????? or ?????????????????????????????????
        self.train_dataset.ray_sampling_strategy = self.hparams.ray_sampling_strategy

    def train_dataloader(self):
        """
        ??????????????????
        :return:
        """
        return DataLoader(self.train_dataset,
                          num_workers=0,  # ??????????????????????????????
                          persistent_workers=False,  # true ??????????????????????????????
                          batch_size=None,
                          pin_memory=True)

    def val_dataloader(self):
        """
        ??????????????????
        :return:
        """
        return DataLoader(self.test_dataset,
                          num_workers=0,
                          persistent_workers=False,
                          batch_size=None,
                          pin_memory=True)

        # ????????????????????????

    def forward(self, batch, split):
        """
        ????????????
        :param batch:
        :param split:
        :return:
        """
        if split == 'train':
            poses = self.poses[batch['img_idxs']]
            directions = self.directions[batch['pix_idxs']]
        else:
            poses = batch['pose']
            directions = self.directions
        # ????????????
        rays_o, rays_d = get_rays(directions, poses)
        kwargs = {'test_time': split != 'train',
                  'random_bg': self.hparams.random_bg}
        if self.hparams.scale > 0.5:
            kwargs['exp_step_factor'] = 1 / 256
        if "rffr" == self.hparams.dataset_name:
            return render_REN(self.model, rays_o, rays_d, **kwargs)
        else:
            return render(self.model, rays_o, rays_d, **kwargs)

    def on_train_start(self):
        """
        ????????????  ?????????????????????
        :return:
        """
        self.model.mark_invisible_cells(self.train_dataset.K.to(self.device),
                                        self.poses,
                                        self.train_dataset.img_wh)
        if "rffr" == self.hparams.dataset_name:
            self.update_hyperparameters(self.current_epochs)

    def training_epoch_end(self, outputs: EPOCH_OUTPUT):
        if "rffr" == self.hparams.dataset_name:
            self.current_epochs = self.current_epochs + 1
            self.update_hyperparameters(self.current_epochs)

    def training_step(self, batch, batch_nb, *args):

        """
        ???????????????????????????????????????????????????????????????
        :param batch:
        :param batch_nb:
        :param args:
        :return:
        """
        if self.global_step % self.update_interval == 0:
            self.model.update_density_grid(0.01 * MAX_SAMPLES / 3 ** 0.5,
                                           warmup=self.global_step < self.warmup_steps,
                                           erode=self.hparams.dataset_name == 'colmap')
        loss = 0
        results = self(batch, split='train')
        if "rffr" == self.hparams.dataset_name:
            self.update_hyperparameters_step(self.global_step * self.hparams.num_gpus)
            patch_wh = batch["patch_wh"]

            t_results, r_results = results
            t_opacity, t_depth, t_rgb, t_depth_b2f, beta = t_results['opacity'], t_results['depth'], t_results['rgb'], \
                                                           t_results["depth_b2f"], t_results["beta"]
            r_opacity, r_depth, r_rgb, r_depth_b2f = r_results['opacity'], r_results['depth'], r_results['rgb'], \
                                                     r_results["depth_b2f"]

            com_rgb = t_rgb + beta * r_rgb

            mask = batch["mask"]
            rgb = batch['rgb']
            masks_value = batch["masks_value"]

            # mse_loss
            mse_loss = self.losses["mse"](com_rgb, rgb) * self.hparams.lambda_mse

            # trans depth smoothness loss
            t_depth_smoothness_loss = self.losses["edge-preserving-smoothness"](
                t_depth.view(-1, patch_wh[0], patch_wh[1]),
                rgb.view(-1, patch_wh[0], patch_wh[1], 3)) * self.hparams.lambda_trans_depth_smoothness

            # refl depth smoothness loss
            r_depth_smoothness_loss = self.losses["edge-preserving-smoothness"](
                r_depth.view(-1, patch_wh[0], patch_wh[1]),
                rgb.view(-1, patch_wh[0], patch_wh[1], 3)) * self.hparams.lambda_refl_depth_smoothness

            # beta smoothness loss
            coarse_beta_smoothness = self.losses["smoothness"](
                beta.view(-1, patch_wh[0], patch_wh[1])
            ) * self.hparams.lambda_beta_smoothness

            # beta mask loss
            beta_mask = self.losses['l1'](
                com_rgb, mask, masks_value
            ) * self.hparams.lambda_beta_mask

            # t_lowpass
            t_lowpass = self.losses["mse"](
                t_rgb.view(-1, patch_wh[0], patch_wh[1], 3).mean(dim=-2),
                rgb.view(-1, patch_wh[0], patch_wh[1], 3).mean(dim=-2),
            ) * self.hparams.lambda_trans_lowpass

            # r_bdc
            r_bdc = self.losses['l1'](
                r_depth, r_depth_b2f
            ) * self.hparams.lambda_refl_bdc

            loss += mse_loss
            loss += t_depth_smoothness_loss
            loss += r_depth_smoothness_loss
            loss += coarse_beta_smoothness
            loss += beta_mask
            loss += t_lowpass
            loss += r_bdc

            with torch.no_grad():
                self.train_psnr(com_rgb, batch['rgb'])

            self.log('lr', self.net_opt.param_groups[0]['lr'])
            self.log('train/loss', loss)
            self.log('train/s_per_ray', t_results['total_samples'] / len(batch['rgb']), True)
            self.log('train/psnr', self.train_psnr, True)
        else:
            loss_d = self.losses["ngp_loss"](results, batch)
            loss = sum(lo.mean() for lo in loss_d.values())
            with torch.no_grad():
                self.train_psnr(results['rgb'], batch['rgb'])
            self.log('lr', self.net_opt.param_groups[0]['lr'])
            self.log('train/loss', loss)
            self.log('train/s_per_ray', results['total_samples'] / len(batch['rgb']), True)
            self.log('train/psnr', self.train_psnr, True)
        return loss

    def configure_optimizers(self):
        """
        ???????????????
        :return:
        """
        # define additional parameters ??????????????????
        self.register_buffer('directions', self.train_dataset.directions.to(self.device))
        self.register_buffer('poses', self.train_dataset.poses.to(self.device))

        load_ckpt(self.model, self.hparams.weight_path)

        net_params = []
        for n, p in self.named_parameters():
            if n not in ['dR', 'dT']: net_params += [p]

        opts = []
        self.net_opt = FusedAdam(net_params, self.hparams.lr, eps=1e-15)
        opts += [self.net_opt]
        net_sch = CosineAnnealingLR(self.net_opt,
                                    self.hparams.num_epochs,
                                    self.hparams.lr / 30)

        return opts, [net_sch]

    def on_validation_start(self):
        torch.cuda.empty_cache()
        if not self.hparams.no_save_test:
            self.val_dir = f'results/{self.hparams.dataset_name}/{self.hparams.exp_name}'
            os.makedirs(self.val_dir, exist_ok=True)

    def validation_step(self, batch, batch_nb):
        rgb_gt = batch['rgb']
        results = self(batch, split='test')
        logs = {}
        w, h = self.train_dataset.img_wh
        rgb = None
        depth = None
        if "rffr" == self.hparams.dataset_name:
            t_results, r_results, a = results
            t_opacity, t_depth, t_rgb = t_results['opacity'], t_results['depth'], t_results['rgb']
            r_opacity, r_depth, r_rgb = r_results['opacity'], r_results['depth'], r_results['rgb']
            rgb = t_rgb + a * r_rgb
            depth = t_results["depth"].cpu().numpy()
        else:
            rgb = results['rgb']
            depth = results['depth'].cpu().numpy()

        # compute each metric per image
        self.val_psnr(rgb, rgb_gt)
        logs['psnr'] = self.val_psnr.compute()
        self.val_psnr.reset()

        rgb_pred = rearrange(rgb, '(h w) c -> 1 c h w', h=h)
        rgb_gt = rearrange(rgb_gt, '(h w) c -> 1 c h w', h=h)
        self.val_ssim(rgb_pred, rgb_gt)
        logs['ssim'] = self.val_ssim.compute()
        self.val_ssim.reset()
        if self.hparams.eval_lpips:
            self.val_lpips(torch.clip(rgb_pred * 2 - 1, -1, 1),
                           torch.clip(rgb_gt * 2 - 1, -1, 1))
            logs['lpips'] = self.val_lpips.compute()
            self.val_lpips.reset()

        if not self.hparams.no_save_test:  # save test image to disk
            idx = batch['img_idxs']
            rgb_pred = rearrange(rgb.cpu().numpy(), '(h w) c -> h w c', h=h)
            rgb_pred = (rgb_pred * 255).astype(np.uint8)
            depth = rearrange(depth, '(h w) -> h w', h=h)
            depth = (depth - depth.min()) / (depth.max() - depth.min())
            depth_img = cv2.applyColorMap((depth * 255).astype(np.uint8),
                                          cv2.COLORMAP_TURBO)
            imageio.imsave(os.path.join(self.val_dir, f'{idx:03d}.png'), rgb_pred)
            imageio.imsave(os.path.join(self.val_dir, f'{idx:03d}_d.png'), depth_img)

        return logs

    def get_progress_bar_dict(self):
        # don't show the version number
        items = super().get_progress_bar_dict()
        items.pop("v_num", None)
        return items

    """
        ???????????????
    """

    def parse_lambda_schedulers(self, schedulers_str):
        # [name]@[step/linear/exp]@[epoch1]:[lr1]:[epoch2]:[lr2]:..:[epochN]:[lrN];...
        # step: [epoch1, epoch2)
        schedulers_str = schedulers_str.split(';')
        schedulers = {}
        step_inf = int(1e10)
        for s in filter(None, schedulers_str):
            name, sched_type, params = s.split('@')
            assert sched_type in ['step', 'linear']
            params = params.split(':')
            steps, lr = list(map(int, params[0::2])), list(map(float, params[1::2]))
            schedulers[name] = {
                'type': sched_type,
                'steps': [1] + steps + [step_inf],
                'lr': [getattr(self.hparams, 'lambda_' + name)] + lr + [lr[-1]]
            }
        return schedulers

    def update_hyperparameters(self, epoch):
        for name, s in self.lambda_schedulers.items():
            start = bisect.bisect_right(s['steps'], epoch) - 1
            step_start, step_end = s['steps'][start], s['steps'][start + 1]
            lr_start, lr_end = s['lr'][start], s['lr'][start + 1]
            if s['type'] == 'step':
                lr = lr_start
            elif s['type'] == 'linear':
                lr = lr_start + (epoch - step_start) / (step_end - step_start) * (lr_end - lr_start)
            setattr(self.hparams, 'lambda_' + name, lr)
            print(f"Set hyperparameter lambda_{name} to {lr:.3e}")

    def update_hyperparameters_step(self, step):
        for name, s in self.lambda_schedulers_step.items():
            start = bisect.bisect_right(s['steps'], step) - 1
            step_start, step_end = s['steps'][start], s['steps'][start + 1]
            lr_start, lr_end = s['lr'][start], s['lr'][start + 1]
            if s['type'] == 'step':
                lr = lr_start
            elif s['type'] == 'linear':
                lr = lr_start + (step - step_start) / (step_end - step_start) * (lr_end - lr_start)
            setattr(self.hparams, 'lambda_' + name, lr)

if __name__ == '__main__':
    # ????????????
    hparams = get_opts()
    # ????????????????????????
    if hparams.val_only and (not hparams.ckpt_path):
        raise ValueError('You need to provide a @ckpt_path for validation!')
    # ??????????????????
    system = NGPSystem(hparams)
    # ?????????????????????metric?????????????????????LightningModule ????????? log() ??? log_dict() ???????????????metric??????????????????????????????
    ckpt_cb = ModelCheckpoint(dirpath=f'ckpts/{hparams.dataset_name}/{hparams.exp_name}',  # ???????????????????????????
                              filename='{epoch:d}',  # checkpoint?????????
                              save_weights_only=True,  # ????????????True???????????????????????????????????????????????????????????????????????????????????????????????????
                              every_n_epochs=hparams.num_epochs,  # checkpoints???????????????????????????epoch???
                              save_on_train_epoch_end=True,  # ?????????????????????????????????????????????
                              save_top_k=-1)  # ??????????????????????????????????????????k?????????????????????
    callbacks = [ckpt_cb, TQDMProgressBar(refresh_rate=1)]
    logger = TensorBoardLogger(save_dir=f"logs/{hparams.dataset_name}",
                               name=hparams.exp_name,
                               default_hp_metric=False)
    # ????????????
    trainer = Trainer(max_epochs=hparams.num_epochs,  # ?????????????????? 30
                      check_val_every_n_epoch=hparams.num_epochs,  # ??????n???epoch???????????????
                      callbacks=callbacks,
                      logger=logger,
                      enable_model_summary=False,
                      gpus=[6, 7],
                      accelerator='gpu',
                      devices=hparams.num_gpus,  # gup??????
                      strategy=DDPPlugin(find_unused_parameters=False)
                      if hparams.num_gpus > 1 else None,
                      num_sanity_val_steps=-1 if hparams.val_only else 0,
                      precision=16)
    trainer.fit(system, ckpt_path=hparams.ckpt_path)

    # ?????????????????????
    if not hparams.val_only:  # save slimmed ckpt for the last epoch
        ckpt_ = \
            slim_ckpt(f'ckpts/{hparams.dataset_name}/{hparams.exp_name}/epoch={hparams.num_epochs - 1}.ckpt',
                      save_poses=False)
        torch.save(ckpt_, f'ckpts/{hparams.dataset_name}/{hparams.exp_name}/epoch={hparams.num_epochs - 1}_slim.ckpt')

    # ????????????
    if not hparams.no_save_test:  # save video
        imgs = sorted(glob.glob(os.path.join(system.val_dir, '*.png')))
        imageio.mimsave(os.path.join(system.val_dir, 'rgb.mp4'),
                        [imageio.imread(img) for img in imgs[::2]],
                        fps=30, macro_block_size=1)
        imageio.mimsave(os.path.join(system.val_dir, 'depth.mp4'),
                        [imageio.imread(img) for img in imgs[1::2]],
                        fps=30, macro_block_size=1)
