# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import logging
import math
import types

import torch
import torch.optim
import torch.distributed as dist

from fairseq.optim import FairseqOptimizer, register_optimizer
from fairseq.optim.fused_adam import get_fused_adam_class
from fairseq import utils

logger = logging.getLogger(__name__)


@register_optimizer('adam_encdec')
class FairseqAdamEncDec(FairseqOptimizer):
    """Adam optimizer for fairseq.

    Important note: this optimizer corresponds to the "AdamW" variant of
    Adam in its weight decay behavior. As such, it is most closely
    analogous to torch.optim.AdamW from PyTorch.
    """

    def __init__(self, args, params):
        super().__init__(args)
        #self.args = args
        fused_adam_cls = get_fused_adam_class()
        use_fused_adam = (
            not getattr(args, 'use_old_adam', False)
            and fused_adam_cls is not None
            and torch.cuda.is_available()
        )
        if getattr(args, 'tpu', False):
            # on TPUs we use the Adam defined here, since it
            # automatically casts gradients to FP32
            self._optimizer_enc = Adam(params[0], **self.optimizer_config_enc)
            self._optimizer_dec = Adam(params[1], **self.optimizer_config_dec)
        elif use_fused_adam:
            logger.info('using FusedAdam')
            self._optimizer_enc = fused_adam_cls(params[0], **self.optimizer_config_enc)
            self._optimizer_dec = fused_adam_cls(params[1], **self.optimizer_config_dec)
        else:
            self._optimizer_enc = Adam(params[0], **self.optimizer_config_enc)
            self._optimizer_dec = Adam(params[1], **self.optimizer_config_dec)

    @staticmethod
    def add_args(parser):
        """Add optimizer-specific arguments to the parser."""
        # fmt: off
        parser.add_argument('--adam-betas', default='(0.9, 0.999)', metavar='B',
                            help='betas for Adam optimizer')
        parser.add_argument('--adam-eps', type=float, default=1e-8, metavar='D',
                            help='epsilon for Adam optimizer')
        parser.add_argument('--weight-decay', '--wd', default=0.0, type=float, metavar='WD',
                            help='weight decay')
        parser.add_argument('--lr-enc', default=0.0, type=float)
        parser.add_argument('--lr-dec', default=0.0, type=float)
        # Maintain backward compatibility with old checkpoints that have stored
        # optimizer state as fairseq.optim.adam.Adam.
        parser.add_argument(
            "--use-old-adam",
            action='store_true',
            default=False,
            help="Use fairseq.optim.adam.Adam",
        )
        # fmt: on

    @property
    def optimizer_config_enc(self):
        """
        Return a kwarg dictionary that will be used to override optimizer
        args stored in checkpoints. This allows us to load a checkpoint and
        resume training using a different set of optimizer args, e.g., with a
        different learning rate.
        """
        return {
            'lr': self.args.lr_enc,
            'betas': eval(self.args.adam_betas),
            'eps': self.args.adam_eps,
            'weight_decay': self.args.weight_decay,
        }

    @property
    def optimizer_config_dec(self):
        """
        Return a kwarg dictionary that will be used to override optimizer
        args stored in checkpoints. This allows us to load a checkpoint and
        resume training using a different set of optimizer args, e.g., with a
        different learning rate.
        """
        return {
            'lr': self.args.lr_dec,
            'betas': eval(self.args.adam_betas),
            'eps': self.args.adam_eps,
            'weight_decay': self.args.weight_decay,
        }

    '''
    def average_params(self):
        """Reduce Params is only used during BMUF distributed training."""
        state_dict = self.optimizer.state_dict()
        total_gpus = float(dist.get_world_size())

        for _, value in state_dict["state"].items():
            value["exp_avg"] /= total_gpus
            value["exp_avg_sq"] /= total_gpus
            dist.all_reduce(value["exp_avg"], op=dist.ReduceOp.SUM)
            dist.all_reduce(value["exp_avg_sq"], op=dist.ReduceOp.SUM)'''

    @property
    def optimizer_enc(self):
        """Return a torch.optim.optimizer.Optimizer instance."""
        if not hasattr(self, '_optimizer_enc'):
            raise NotImplementedError
        if not isinstance(self._optimizer_enc, torch.optim.Optimizer):
            raise ValueError('_optimizer_enc must be an instance of torch.optim.Optimizer')
        return self._optimizer_enc

    @property
    def optimizer_dec(self):
        """Return a torch.optim.optimizer.Optimizer instance."""
        if not hasattr(self, '_optimizer_dec'):
            raise NotImplementedError
        if not isinstance(self._optimizer_dec, torch.optim.Optimizer):
            raise ValueError('_optimizer_dec must be an instance of torch.optim.Optimizer')
        return self._optimizer_dec

    @property
    def params(self):
        """Return an iterable of the parameters held by the optimizer."""
        for param_group in self.param_groups_enc:
            for p in param_group['params']:
                yield p
        for param_group in self.param_groups_dec:
            for p in param_group['params']:
                yield p

    @property
    def param_groups_enc(self):
        return self.optimizer_enc.param_groups

    @property
    def param_groups_dec(self):
        return self.optimizer_dec.param_groups

    '''def __getstate__(self):
        return self._optimizer.__getstate__()'''

    def get_lr_enc(self):
        """Return the current learning rate."""
        return self.param_groups_enc[0]['lr']
    
    def get_lr_dec(self):
        """Return the current learning rate."""
        return self.param_groups_dec[0]['lr']

    def set_lr_enc(self, lr):
        """Set the learning rate."""
        for param_group in self.param_groups_enc:
            param_group['lr'] = lr

    def set_lr_dec(self, lr):
        """Set the learning rate."""
        for param_group in self.param_groups_dec:
            param_group['lr'] = lr

    def state_dict(self):
        """Return the optimizer's state dict."""
        return {'enc': self.optimizer_enc.state_dict(), 'dec': self.optimizer_dec.state_dict()}

    def load_state_dict(self, state_dict, optimizer_overrides=None):
        """Load an optimizer state dict.

        In general we should prefer the configuration of the existing optimizer
        instance (e.g., learning rate) over that found in the state_dict. This
        allows us to resume training from a checkpoint using a new set of
        optimizer args.
        """
        self.optimizer_enc.load_state_dict(state_dict['enc'])
        self.optimizer_dec.load_state_dict(state_dict['dec'])

        if optimizer_overrides is not None and len(optimizer_overrides) > 0:
            # override learning rate, momentum, etc. with latest values
            for group in self.param_groups_enc:
                group.update(optimizer_overrides)
            for group in self.param_groups_dec:
                group.update(optimizer_overrides)

    def backward(self, loss):
        """Computes the sum of gradients of the given tensor w.r.t. graph leaves."""
        loss.backward()

    def multiply_grads(self, c):
        """Multiplies grads by a constant *c*."""
        for p in self.params:
            if p.grad is not None:
                p.grad.data.mul_(c)

    def clip_grad_norm(self, max_norm, aggregate_norm_fn=None):
        """Clips gradient norm."""
        return utils.clip_grad_norm_(self.params, max_norm, aggregate_norm_fn)

    def step(self, closure=None, scale=1.):
        """Performs a single optimization step."""
        if self.supports_step_with_scale:
            self.optimizer_enc.step(closure, scale=scale)
            self.optimizer_dec.step(closure, scale=scale)
        else:
            self.optimizer_enc.step(closure)
            self.optimizer_dec.step(closure)

    def zero_grad(self):
        """Clears the gradients of all optimized parameters."""
        for p in self.params:
            p.grad = None
        self.optimizer_enc.zero_grad()
        self.optimizer_dec.zero_grad()

    @property
    def supports_memory_efficient_fp16(self):
        if hasattr(self.optimizer_enc, 'supports_memory_efficient_fp16') and hasattr(self.optimizer_dec, 'supports_memory_efficient_fp16'):
            return self.optimizer_enc.supports_memory_efficient_fp16 and self.optimizer_dec.supports_memory_efficient_fp16
        return False

    @property
    def supports_step_with_scale(self):
        if hasattr(self.optimizer_enc, 'supports_step_with_scale') and hasattr(self.optimizer_dec, 'supports_step_with_scale'):
            return self.optimizer_enc.supports_step_with_scale and self.optimizer_dec.supports_step_with_scale
        return False

    @property
    def supports_flat_params(self):
        """
        Whether the optimizer supports collapsing of the model
        parameters/gradients into a single contiguous Tensor.
        """
        if hasattr(self.optimizer_enc, 'supports_flat_params') and hasattr(self.optimizer_dec, 'supports_flat_params'):
            return self.optimizer_enc.supports_flat_params and self.optimizer_dec.supports_flat_params
        return False



class Adam(torch.optim.Optimizer):
    """Implements Adam algorithm.

    This implementation is modified from torch.optim.Adam based on:
    `Fixed Weight Decay Regularization in Adam`
    (see https://arxiv.org/abs/1711.05101)

    It has been proposed in `Adam: A Method for Stochastic Optimization`_.

    Arguments:
        params (iterable): iterable of parameters to optimize or dicts defining
            parameter groups
        lr (float, optional): learning rate (default: 1e-3)
        betas (Tuple[float, float], optional): coefficients used for computing
            running averages of gradient and its square (default: (0.9, 0.999))
        eps (float, optional): term added to the denominator to improve
            numerical stability (default: 1e-8)
        weight_decay (float, optional): weight decay (L2 penalty) (default: 0)
        amsgrad (boolean, optional): whether to use the AMSGrad variant of this
            algorithm from the paper `On the Convergence of Adam and Beyond`_

    .. _Adam\: A Method for Stochastic Optimization:
        https://arxiv.org/abs/1412.6980
    .. _On the Convergence of Adam and Beyond:
        https://openreview.net/forum?id=ryQu7f-RZ
    """

    def __init__(self, params, lr=1e-3, betas=(0.9, 0.999), eps=1e-8,
                 weight_decay=0, amsgrad=False):
        defaults = dict(lr=lr, betas=betas, eps=eps,
                        weight_decay=weight_decay, amsgrad=amsgrad)
        super(Adam, self).__init__(params, defaults)

    @property
    def supports_memory_efficient_fp16(self):
        return True

    @property
    def supports_flat_params(self):
        return True

    def step(self, closure=None):
        """Performs a single optimization step.

        Arguments:
            closure (callable, optional): A closure that reevaluates the model
                and returns the loss.
        """
        loss = None
        if closure is not None:
            loss = closure()

        for group in self.param_groups:
            for p in group['params']:
                if p.grad is None:
                    continue
                grad = p.grad.data
                if grad.dtype in {torch.float16, torch.bfloat16}:
                    grad = grad.float()
                if grad.is_sparse:
                    raise RuntimeError('Adam does not support sparse gradients, please consider SparseAdam instead')
                amsgrad = group['amsgrad']

                p_data_fp32 = p.data
                if p.data.dtype in {torch.float16, torch.bfloat16}:
                    p_data_fp32 = p_data_fp32.float()

                state = self.state[p]

                # State initialization
                if len(state) == 0:
                    state['step'] = 0
                    # Exponential moving average of gradient values
                    state['exp_avg'] = torch.zeros_like(p_data_fp32)
                    # Exponential moving average of squared gradient values
                    state['exp_avg_sq'] = torch.zeros_like(p_data_fp32)
                    if amsgrad:
                        # Maintains max of all exp. moving avg. of sq. grad. values
                        state['max_exp_avg_sq'] = torch.zeros_like(p_data_fp32)
                else:
                    state['exp_avg'] = state['exp_avg'].to(p_data_fp32)
                    state['exp_avg_sq'] = state['exp_avg_sq'].to(p_data_fp32)
                    if amsgrad:
                        state['max_exp_avg_sq'] = state['max_exp_avg_sq'].to(p_data_fp32)

                exp_avg, exp_avg_sq = state['exp_avg'], state['exp_avg_sq']
                if amsgrad:
                    max_exp_avg_sq = state['max_exp_avg_sq']
                beta1, beta2 = group['betas']

                state['step'] += 1

                # Decay the first and second moment running average coefficient
                exp_avg.mul_(beta1).add_(grad, alpha=1 - beta1)
                exp_avg_sq.mul_(beta2).addcmul_(grad, grad, value=1 - beta2)
                if amsgrad:
                    # Maintains the maximum of all 2nd moment running avg. till now
                    torch.max(max_exp_avg_sq, exp_avg_sq, out=max_exp_avg_sq)
                    # Use the max. for normalizing running avg. of gradient
                    denom = max_exp_avg_sq.sqrt().add_(group['eps'])
                else:
                    denom = exp_avg_sq.sqrt().add_(group['eps'])

                bias_correction1 = 1 - beta1 ** state['step']
                bias_correction2 = 1 - beta2 ** state['step']
                step_size = group['lr'] * math.sqrt(bias_correction2) / bias_correction1

                if group['weight_decay'] != 0:
                    p_data_fp32.add_(p_data_fp32, alpha=-group['weight_decay'] * group['lr'])

                p_data_fp32.addcdiv_(exp_avg, denom, value=-step_size)

                if p.data.dtype in {torch.float16, torch.bfloat16}:
                    p.data.copy_(p_data_fp32)

        return loss
