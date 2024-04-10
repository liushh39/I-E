import torch
import torch.nn as nn
import torch.nn.init as init
import random
import numpy as np
from collections import OrderedDict
from os import path as osp
from tqdm import tqdm
import cv2
import os

from basicsr.archs import build_network
from basicsr.losses import build_loss
from basicsr.metrics import calculate_metric
from basicsr.utils import get_root_logger, imwrite, tensor2img
from basicsr.utils.registry import MODEL_REGISTRY
from .base_model import BaseModel


# import basicsr.archs.networks as networks

@MODEL_REGISTRY.register()
class LEDNetModel(BaseModel):
    """Base SR model for single image super-resolution."""

    def __init__(self, opt):
        super(LEDNetModel, self).__init__(opt)
        # Set random seed and deterministic
        # define network
        self.net_g = build_network(opt['network_g'])
        self.init_weights = self.opt['train'].get('init_weights', False)
        if self.init_weights:
            self.initialize_weights(self.net_g, 0.1)

        # self.netG = networks.define_G(opt).to(self.device)
        # original
        self.net_g = self.model_to_device(self.net_g)
        self.print_network(self.net_g)

        # load pretrained models
        load_path = self.opt['path'].get('pretrain_network_g', None)
        if load_path is not None:
            param_key = self.opt['path'].get('param_key_g', 'params')
            self.load_network(self.net_g, load_path, self.opt['path'].get('strict_load_g', True), param_key)

        if self.is_train:
            self.init_training_settings()

    def initialize_weights(self, net_l, scale=0.1):
        if not isinstance(net_l, list):
            net_l = [net_l]
        for net in net_l:
            for n, m in net.named_modules():
                if isinstance(m, nn.Conv2d):
                    init.kaiming_normal_(m.weight, a=0, mode='fan_in')
                    m.weight.data *= scale  # for residual block
                    if m.bias is not None:
                        m.bias.data.zero_()

    def init_training_settings(self):
        self.net_g.train()
        train_opt = self.opt['train']

        self.ema_decay = train_opt.get('ema_decay', 0)
        if self.ema_decay > 0:
            logger = get_root_logger()
            logger.info(f'Use Exponential Moving Average with decay: {self.ema_decay}')
            # define network net_g with Exponential Moving Average (EMA)
            # net_g_ema is used only for testing on one GPU and saving
            # There is no need to wrap with DistributedDataParallel
            self.net_g_ema = build_network(self.opt['network_g']).to(self.device)
            # load pretrained model
            load_path = self.opt['path'].get('pretrain_network_g', None)
            if load_path is not None:
                self.load_network(self.net_g_ema, load_path, self.opt['path'].get('strict_load_g', True), 'params_ema')
            else:
                self.model_ema(0)  # copy net_g weight
            self.net_g_ema.eval()

        # define losses
        if train_opt.get('pixel_opt'):
            self.cri_pix = build_loss(train_opt['pixel_opt']).to(self.device)
        else:
            self.cri_pix = None

        if train_opt.get('perceptual_opt'):
            self.cri_perceptual = build_loss(train_opt['perceptual_opt']).to(self.device)
        else:
            self.cri_perceptual = None

        if self.cri_pix is None and self.cri_perceptual is None:
            raise ValueError('Both pixel and perceptual losses are None.')

        self.down_edge_fre_loss = train_opt.get('down_edge_fre_loss', True)
        self.use_side_loss = train_opt.get('use_side_loss', True)
        self.side_loss_weight = train_opt.get('side_loss_weight', 0.8)

        # set up optimizers and schedulers
        self.setup_optimizers()
        self.setup_schedulers()

    def setup_optimizers(self):
        train_opt = self.opt['train']
        optim_params = []
        for k, v in self.net_g.named_parameters():
            if v.requires_grad:
                optim_params.append(v)
            else:
                logger = get_root_logger()
                logger.warning(f'Params {k} will not be optimized.')

        optim_type = train_opt['optim_g'].pop('type')
        self.optimizer_g = self.get_optimizer(optim_type, optim_params, **train_opt['optim_g'])
        self.optimizers.append(self.optimizer_g)

    def feed_data(self, data):
        self.lq = data['lq'].to(self.device)  # low-blurred image
        self.gt = data['gt'].to(self.device)  # ground truth

        self.nf = data['nf'].to(self.device)  # ground truth
        self.gt_edge = data['edge'].to(self.device)  # ground truth
        self.gt_fre = data['gt_fre'].to(self.device)  # ground truth

    def optimize_parameters(self, current_iter):
        self.optimizer_g.zero_grad()

        # SNR-mask calculate
        dark = self.lq
        dark = dark[:, 0:1, :, :] * 0.299 + dark[:, 1:2, :, :] * 0.587 + dark[:, 2:3, :, :] * 0.114  # gray-scale
        light = self.nf
        light = light[:, 0:1, :, :] * 0.299 + light[:, 1:2, :, :] * 0.587 + light[:, 2:3, :, :] * 0.114
        noise = torch.abs(dark - light)  # noise map

        mask = torch.div(light, noise + 0.0001)  # SNR map = clear map / noise map

        batch_size = mask.shape[0]
        height = mask.shape[2]
        width = mask.shape[3]
        x = mask.view(batch_size, -1)
        y = torch.max(x, dim=1)
        mask_max = torch.max(mask.view(batch_size, -1), dim=1)[0]
        mask_max = mask_max.view(batch_size, 1, 1, 1)
        mask_max = mask_max.repeat(1, 1, height, width)
        mask = mask * 1.0 / (mask_max + 0.0001)  # normalize its values to range [0, 1]

        mask = torch.clamp(mask, min=0, max=1.0)
        mask = mask.float()

        # prediction output
        # self.edge_output, self.fre_output, self.output, self.side_output = self.net_g(self.lq, mask, side_loss=self.use_side_loss)
        self.edge_output, self.fre_output, self.output = self.net_g(self.lq, mask, side_loss=self.use_side_loss)
        # edge and frequency down-sample
        if self.down_edge_fre_loss:
            h, w = self.edge_output.shape[2:]
            self.gt_edge = torch.nn.functional.interpolate(self.gt_edge, (h, w), mode='bicubic', align_corners=False)
            self.gt_fre = torch.nn.functional.interpolate(self.gt_fre, (h, w), mode='bicubic', align_corners=False)

        # Intermediate gt
        if self.use_side_loss:
            h, w = self.side_output.shape[2:]
            self.side_gt = torch.nn.functional.interpolate(self.gt, (h, w), mode='bicubic', align_corners=False)

        l_total = 0
        loss_dict = OrderedDict()
        # pixel loss
        if self.cri_pix:
            l_pix = self.cri_pix(self.output, self.gt)
            l_total += l_pix
            loss_dict['l_pix'] = l_pix
            if self.use_side_loss:
                l_side_pix = self.cri_pix(self.side_output, self.side_gt) * self.side_loss_weight
                l_total += l_side_pix
                loss_dict['l_side_pix'] = l_side_pix

        # perceptual loss
        if self.cri_perceptual:
            l_percep, _ = self.cri_perceptual(self.output, self.gt)
            l_total += l_percep
            loss_dict['l_percep'] = l_percep
            if self.use_side_loss:
                l_side_percep, _ = self.cri_perceptual(self.side_output, self.side_gt)
                l_side_percep = l_side_percep * self.side_loss_weight
                l_total += l_side_percep
                loss_dict['l_side_percep'] = l_side_percep

        # edge and high_frequency loss
        if self.cri_pix:
            l_edge = self.cri_pix(self.edge_output, self.gt_edge) * 0.001
            l_total += l_edge
            loss_dict['l_edge'] = l_edge

            l_fre = self.cri_pix(self.fre_output, self.gt_fre) * 0.001
            l_total += l_fre
            loss_dict['l_fre'] = l_fre

        loss_dict['l_total'] = l_total
        l_total.backward()
        self.optimizer_g.step()

        self.log_dict = self.reduce_loss_dict(loss_dict)

        if self.ema_decay > 0:
            self.model_ema(decay=self.ema_decay)

    def test(self):
        if self.ema_decay > 0:
            self.net_g_ema.eval()
            with torch.no_grad():
                self.output = self.net_g_ema(self.lq)
        else:
            self.net_g.eval()
            with torch.no_grad():

                # SNR-mask calculate
                dark = self.lq
                dark = dark[:, 0:1, :, :] * 0.299 + dark[:, 1:2, :, :] * 0.587 + dark[:, 2:3, :,
                                                                                 :] * 0.114  # gray-scale
                light = self.nf
                light = light[:, 0:1, :, :] * 0.299 + light[:, 1:2, :, :] * 0.587 + light[:, 2:3, :, :] * 0.114
                noise = torch.abs(dark - light)  # noise map

                mask = torch.div(light, noise + 0.0001)  # SNR map = clear map / noise map

                batch_size = mask.shape[0]
                height = mask.shape[2]
                width = mask.shape[3]
                x = mask.view(batch_size, -1)
                y = torch.max(x, dim=1)
                mask_max = torch.max(mask.view(batch_size, -1), dim=1)[0]
                mask_max = mask_max.view(batch_size, 1, 1, 1)
                mask_max = mask_max.repeat(1, 1, height, width)
                mask = mask * 1.0 / (mask_max + 0.0001)  # normalize its values to range [0, 1]

                mask = torch.clamp(mask, min=0, max=1.0)
                mask = mask.float()

                self.edge_output, self.fre_output, self.output = self.net_g(self.lq, mask)
            self.net_g.train()

    def dist_validation(self, dataloader, current_iter, tb_logger, save_img):
        if self.opt['rank'] == 0:
            self.nondist_validation(dataloader, current_iter, tb_logger, save_img)

    def nondist_validation(self, dataloader, current_iter, tb_logger, save_img):
        dataset_name = dataloader.dataset.opt['name']
        with_metrics = self.opt['val'].get('metrics') is not None
        if with_metrics:
            self.metric_results = {metric: 0 for metric in self.opt['val']['metrics'].keys()}
        pbar = tqdm(total=len(dataloader), unit='image')

        for idx, val_data in enumerate(dataloader):
            img_name = osp.splitext(osp.basename(val_data['lq_path'][0]))[0]  # image name

            # extract sub_folder name
            folder2 = os.path.splitext(val_data['lq_path'][0])[0]  # sub_folder name
            folder1 = os.path.split(folder2)[0]
            folder = os.path.split(folder1)[1]

            self.feed_data(val_data)
            self.test()

            visuals = self.get_current_visuals()

            # no normalize
            sr_img = tensor2img([visuals['result']])
            # normalize
            # sr_img = tensor2img([visuals['result']], rgb2bgr=True, min_max=(-1, 1))
            if 'gt' in visuals:
                # no normalize
                gt_img = tensor2img([visuals['gt']])
                # normalize
                # gt_img = tensor2img([visuals['gt']], rgb2bgr=True, min_max=(-1, 1))
                del self.gt

            # visual edge and high_frequency
            # sr_gt_edge = tensor2img([visuals['gt_edge']])
            # sr_gt_fre = tensor2img([visuals['gt_fre']])
            sr_edge_output = tensor2img([visuals['edge_output']])
            sr_fre_output = tensor2img([visuals['fre_output']])

            # tentative for out of GPU memory
            del self.lq
            del self.output
            torch.cuda.empty_cache()

            if save_img:
                if self.opt['is_train']:
                    # save prediction
                    save_img_name = osp.join(self.opt['path']['visualization'], '%01d' % current_iter, f'{folder}',
                                             f'{img_name}.png')
                    imwrite(sr_img, save_img_name)

                    # # save  edge and high_frequency
                    # save_img_gt_edge = osp.join(self.opt['path']['visualization_gt_edge'], '%01d' % current_iter, f'{folder}',f'{img_name}.png')
                    # imwrite(sr_gt_edge, save_img_gt_edge)
                    #
                    # save_img_gt_fre = osp.join(self.opt['path']['visualization_gt_fre'], '%01d' % current_iter, f'{folder}', f'{img_name}.png')
                    # imwrite(sr_gt_fre, save_img_gt_fre)
                    #
                    save_img_edge_output = osp.join(self.opt['path']['visualization_edge_output'], '%01d' % current_iter, f'{folder}', f'{img_name}.png')
                    imwrite(sr_edge_output, save_img_edge_output)

                    save_img_fre_output = osp.join(self.opt['path']['visualization_fre_output'], '%01d' % current_iter, f'{folder}', f'{img_name}.png')
                    imwrite(sr_fre_output, save_img_fre_output)

                    # save_img_path = osp.join(self.opt['path']['visualization'], '%01d' % current_iter, f'{folder}')
                    # os.makedirs(save_img_path, exist_ok=True)
                    # save_img_name = osp.join(self.opt['path']['visualization'], '%01d' % current_iter, f'{folder}', f'{img_name}.png')
                    # cv2.imwrite(save_img_name, sr_img.astype(np.uint8))

            # if save_img:
            #     if self.opt['is_train']:
            #         save_img_path = osp.join(self.opt['path']['visualization'], img_name,
            #                                  f'{img_name}_{current_iter}.png')
            #     else:
            #         if self.opt['val']['suffix']:
            #             save_img_path = osp.join(self.opt['path']['visualization'], dataset_name,
            #                                      f'{img_name}_{self.opt["val"]["suffix"]}.png')
            #         else:
            #             save_img_path = osp.join(self.opt['path']['visualization'], dataset_name,
            #                                      f'{img_name}_{self.opt["name"]}.png')
            #     imwrite(sr_img, save_img_path)

            if with_metrics:
                # calculate metrics
                for name, opt_ in self.opt['val']['metrics'].items():
                    metric_data = dict(img1=sr_img, img2=gt_img)
                    self.metric_results[name] += calculate_metric(metric_data, opt_)
            pbar.update(1)
            pbar.set_description(f'Test {img_name}')
        pbar.close()

        if with_metrics:
            for metric in self.metric_results.keys():
                self.metric_results[metric] /= (idx + 1)

            self._log_validation_metric_values(current_iter, dataset_name, tb_logger)

    def _log_validation_metric_values(self, current_iter, dataset_name, tb_logger):
        log_str = f'Validation {dataset_name}\n'
        for metric, value in self.metric_results.items():
            log_str += f'\t # {metric}: {value:.4f}\n'
        logger = get_root_logger()
        logger.info(log_str)
        if tb_logger:
            for metric, value in self.metric_results.items():
                tb_logger.add_scalar(f'metrics/{metric}', value, current_iter)

    def get_current_visuals(self):
        out_dict = OrderedDict()
        out_dict['lq'] = self.lq.detach().cpu()
        out_dict['result'] = self.output.detach().cpu()

        # out_dict['gt_edge'] = self.gt_edge.detach().cpu()
        # out_dict['gt_fre'] = self.gt_fre.detach().cpu()
        out_dict['edge_output'] = self.edge_output.detach().cpu()
        out_dict['fre_output'] = self.fre_output.detach().cpu()

        if hasattr(self, 'gt'):
            out_dict['gt'] = self.gt.detach().cpu()
        return out_dict

    def save(self, epoch, current_iter):
        if self.ema_decay > 0:
            self.save_network([self.net_g, self.net_g_ema], 'net_g', current_iter, param_key=['params', 'params_ema'])
        else:
            self.save_network(self.net_g, 'net_g', current_iter)
        self.save_training_state(epoch, current_iter)

