# Copyright (c) NXAI GmbH.
# This software may be used and distributed according to the terms of the NXAI Community License Agreement.


import os
from dataclasses import dataclass, field

import torch
from torch import nn
from xlstm.blocks.slstm.layer import sLSTMLayer, sLSTMLayerConfig
from xlstm.xlstm_large import xLSTMLargeConfig
from xlstm.xlstm_large.components import RMSNorm
from xlstm.xlstm_large.model import FeedForward, mLSTMBlock, mLSTMStateType


def skip_cuda():
    return os.getenv("TIREX_NO_CUDA", "False").lower() in ("true", "1", "t")


def init_cell(config: xLSTMLargeConfig, block_idx, num_blocks):
    return sLSTMLayer(
        sLSTMLayerConfig(
            embedding_dim=config.embedding_dim,
            num_heads=config.num_heads,
            conv1d_kernel_size=0,  # 0 means no convolution included
            group_norm_weight=True,
            dropout=0,
            # CellConfig
            backend="vanilla" if skip_cuda() else "cuda",
            bias_init="powerlaw_blockdependent",
            recurrent_weight_init="zeros",
            num_gates=4,
            gradient_recurrent_cut=False,
            gradient_recurrent_clipval=None,
            forward_clipval=None,
            batch_size=8,  # needed?
            _block_idx=block_idx,
            _num_blocks=num_blocks,
        )
    )


sLSTMLayerStateType = tuple[torch.Tensor, torch.Tensor]
sLSTMStateType = dict[int, sLSTMLayerStateType]


class sLSTMBlock(nn.Module):
    def __init__(self, config: xLSTMLargeConfig, block_idx: int, num_blocks: int):
        super().__init__()
        self.config = config
        self.norm_slstm = RMSNorm(
            num_features=config.embedding_dim,
            eps=config.norm_eps,
            use_weight=True,
            use_bias=config.use_bias,
            force_float32_reductions=config.norm_reduction_force_float32,
        )
        self.slstm_layer = init_cell(config, block_idx, num_blocks)

        self.norm_ffn = RMSNorm(
            num_features=config.embedding_dim,
            eps=config.norm_eps,
            use_weight=True,
            use_bias=config.use_bias,
            force_float32_reductions=config.norm_reduction_force_float32,
        )
        self.ffn = FeedForward(config)

    def forward(
        self, x: torch.Tensor, state: sLSTMLayerStateType | None = None
    ) -> tuple[torch.Tensor, sLSTMLayerStateType]:
        x_slstm = self.norm_slstm(x)
        if state is None:
            conv_state, slstm_state = None, None
        else:
            conv_state, slstm_state = state
        x_slstm, state = self.slstm_layer(x_slstm, conv_state, slstm_state, return_last_state=True)
        x = x + x_slstm

        x_ffn = self.norm_ffn(x)
        x_ffn = self.ffn(x_ffn)
        x = x + x_ffn

        return x, (state["conv_state"], state["slstm_state"])


@dataclass
class xLSTMMixedLargeConfig(xLSTMLargeConfig):
    slstm_at: list[int] = field(default_factory=list)
    all_slstm: bool = True

    @property
    def block_types(self):
        return ["s" if i in self.slstm_at or self.all_slstm else "m" for i in range(self.num_blocks)]


class xLSTMMixedLargeBlockStack(nn.Module):
    config_class = xLSTMMixedLargeConfig

    def __init__(self, config: xLSTMMixedLargeConfig):
        super().__init__()
        self.config = config

        self.blocks = nn.ModuleList(
            [
                sLSTMBlock(config, block_idx=i, num_blocks=config.num_blocks) if t == "s" else mLSTMBlock(config)
                for i, t in enumerate(config.block_types)
            ]
        )

        if self.config.add_out_norm:
            self.out_norm = RMSNorm(
                num_features=config.embedding_dim,
                eps=config.norm_eps,
                use_weight=True,
                use_bias=config.use_bias,
                force_float32_reductions=config.norm_reduction_force_float32,
            )
        else:
            self.out_norm = nn.Identity()

    def forward(
        self, x: torch.Tensor, state: mLSTMStateType | sLSTMStateType | None = None
    ) -> tuple[torch.Tensor, mLSTMStateType]:
        if state is None:
            state = {i: None for i in range(len(self.blocks))}

        for i, block in enumerate(self.blocks):
            block_state = state[i]
            x, block_state_new = block(x, block_state)

            if block_state is None:
                state[i] = block_state_new
            else:
                pass
                ## layer state is a tuple of three tensors: c, n, m
                ## we update the state in place in order to avoid creating new tensors
                # for state_idx in range(len(block_state)):
                #    state[i][state_idx].copy_(block_state_new[state_idx])

        x = self.out_norm(x)

        return x, state
