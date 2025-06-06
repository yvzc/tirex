# Copyright (c) NXAI GmbH.
# This software may be used and distributed according to the terms of the NXAI Community License Agreement.

import logging
import warnings
from contextlib import redirect_stdout
from dataclasses import dataclass

import lightning as L
import torch
from dacite import Config, from_dict

from ..base import PretrainedModel
from .components import PatchedUniTokenizer, ResidualBlock, StreamToLogger
from .mixed_stack import skip_cuda, xLSTMMixedLargeBlockStack, xLSTMMixedLargeConfig
from .predict_utils import TensorQuantileUniPredictMixin

LOGGER = logging.getLogger()


@dataclass
class TiRexZeroConfig:
    input_patch_size: int
    output_patch_size: int
    quantiles: list[float]
    block_kwargs: dict
    input_ff_dim: int


class TiRexZero(L.LightningModule, PretrainedModel, TensorQuantileUniPredictMixin):
    def __init__(self, model_config: dict, train_ctx_len=None):
        super().__init__()
        self.model_config: TiRexZeroConfig = from_dict(TiRexZeroConfig, model_config, config=Config(strict=True))
        assert self.model_config.input_patch_size == self.model_config.output_patch_size
        self.train_ctx_len = train_ctx_len

        # Block Stack
        self.nan_mask_value = 0
        self.block_stack, resolved_config = self.init_block(self.model_config.block_kwargs)
        self.model_config.block_kwargs = resolved_config

        # Input Layer
        self.input_patch_embedding = ResidualBlock(
            in_dim=self.model_config.input_patch_size * 2,
            h_dim=self.model_config.input_ff_dim,
            out_dim=self.model_config.block_kwargs.embedding_dim,
        )
        self.tokenizer = PatchedUniTokenizer(
            patch_size=self.model_config.input_patch_size,
        )

        # Output Layer
        self.num_quantiles = len(self.model_config.quantiles)
        quantiles = torch.tensor(self.model_config.quantiles)
        self.register_buffer("quantiles", quantiles, persistent=False)

        self.output_patch_embedding = ResidualBlock(
            in_dim=self.model_config.block_kwargs.embedding_dim,
            h_dim=self.model_config.input_ff_dim,
            out_dim=self.num_quantiles * self.model_config.output_patch_size,
        )

        self.save_hyperparameters()

    @classmethod
    def register_name(cls):
        return "TiRex"

    def init_block(self, block_kwargs):
        config = from_dict(xLSTMMixedLargeConfig, block_kwargs)
        log_redirect = StreamToLogger(LOGGER, logging.INFO)
        with redirect_stdout(log_redirect):  # avoid excessive print statements of sLSTM compile
            model = xLSTMMixedLargeBlockStack(config)
        return model, config

    @property
    def quantiles(self):
        return self.model.quantiles

    def _forward_model_tokenized(
        self,
        input_token,
        input_mask=None,
        rollouts=1,
    ):
        input_mask = (
            input_mask.to(input_token.dtype)
            if input_mask is not None
            else torch.isnan(input_token).logical_not().to(input_token.dtype)
        )
        assert rollouts >= 1
        bs, numb_ctx_token, token_dim = input_token.shape
        if rollouts > 1:
            input_token = torch.cat(
                (
                    input_token,
                    torch.full(
                        (bs, rollouts - 1, token_dim),
                        fill_value=torch.nan,
                        device=input_token.device,
                        dtype=input_token.dtype,
                    ),
                ),
                dim=1,
            )
            input_mask = torch.cat(
                (
                    input_mask,
                    torch.full(
                        (bs, rollouts - 1, token_dim),
                        fill_value=False,
                        device=input_mask.device,
                        dtype=input_mask.dtype,
                    ),
                ),
                dim=1,
            )
        input_token = torch.nan_to_num(input_token, nan=self.nan_mask_value)
        input_embeds = self.input_patch_embedding(torch.cat((input_token, input_mask), dim=2))

        # hidden_states = []
        # for rollout in range(rollout):
        x = self.block_stack(input_embeds)
        if isinstance(x, tuple):
            hidden_states = x[0]
        else:
            hidden_states = x

        quantile_preds = self.output_patch_embedding(hidden_states)
        quantile_preds = torch.unflatten(quantile_preds, -1, (self.num_quantiles, self.model_config.output_patch_size))
        quantile_preds = torch.transpose(quantile_preds, 1, 2)  # switch quantile and num_token_dimension
        # quantile_preds: [batch_size, num_quantiles, num_token, output_patch_size]

        return quantile_preds, hidden_states

    @torch.inference_mode()
    def _forecast_tensor(
        self,
        context: torch.Tensor,
        prediction_length: int | None = None,
        max_context: int | None = None,
        max_accelerated_rollout_steps: int = 1,
    ) -> torch.Tensor:
        predictions = []
        if prediction_length is None:
            prediction_length = self.tokenizer.patch_size
        remaining = -(prediction_length // -self.tokenizer.patch_size)
        if max_context is None:
            max_context = self.train_ctx_len
        min_context = max(self.train_ctx_len, max_context)

        context = context.to(
            device=self.device,
            dtype=torch.float32,
        )
        while remaining > 0:
            if context.shape[-1] > max_context:
                context = context[..., -max_context:]
            if context.shape[-1] < min_context:
                pad = torch.full(
                    (context.shape[0], min_context - context.shape[-1]),
                    fill_value=torch.nan,
                    device=context.device,
                    dtype=context.dtype,
                )
                context = torch.concat((pad, context), dim=1)
            tokenized_tensor, tokenizer_state = self.tokenizer.context_input_transform(context)
            fut_rollouts = min(remaining, max_accelerated_rollout_steps)
            with torch.no_grad():
                prediction, _ = self._forward_model_tokenized(input_token=tokenized_tensor, rollouts=fut_rollouts)
                prediction = prediction[:, :, -fut_rollouts:, :].to(tokenized_tensor)  # predicted token
                # [bs, num_quantiles, num_predicted_token, output_patch_size]
            prediction = self.tokenizer.output_transform(prediction, tokenizer_state)
            prediction = prediction.flatten(start_dim=2)

            predictions.append(prediction)
            remaining -= fut_rollouts

            if remaining <= 0:
                break

            context = torch.cat([context, torch.full_like(prediction[:, 0, :], fill_value=torch.nan)], dim=-1)

        return torch.cat(predictions, dim=-1)[..., :prediction_length].to(
            dtype=torch.float32,
        )

    def on_load_checkpoint(self, checkpoint: dict) -> None:
        state_dict = checkpoint["state_dict"]
        load_vanilla_kernel = skip_cuda()
        if load_vanilla_kernel:
            warnings.warn(
                "You use TiRex without sLSTM CUDA kernels! This might slow down the model considerably and might degrade forecasting results!"
                "Set the environment variable TIREX_NO_CUDA to 0 to avoid this!"
            )
            block_kwargs = self.model_config.block_kwargs
            head_dim = block_kwargs.embedding_dim // block_kwargs.num_heads
            num_gates = 4
            new_state_dict = {}
            for k, v in state_dict.items():
                if "slstm_layer.slstm_cell._recurrent_kernel_" in k:
                    new_state_dict[k] = (
                        v.reshape(
                            block_kwargs.num_heads,
                            head_dim,
                            num_gates,
                            head_dim,
                        )
                        .permute(0, 2, 3, 1)
                        .reshape(
                            block_kwargs.num_heads,
                            num_gates * head_dim,
                            head_dim,
                        )
                    )
                    # new_state_dict[k] = v.permute(0, 2, 1)
                elif "slstm_layer.slstm_cell._bias_" in k:
                    new_state_dict[k] = (
                        v.reshape(block_kwargs.num_heads, num_gates, head_dim).permute(1, 0, 2).reshape(-1)
                    )
                else:
                    new_state_dict[k] = v
            checkpoint["state_dict"] = new_state_dict

    def after_load_from_checkpoint(self):
        if not skip_cuda() and self.device.type != "cuda":
            warnings.warn(
                f"You use TiRex with sLSTM CUDA kernels BUT DO NOT LOAD THE DEVICE ON A CUDA DEVICE (device type is {self.device.type})!"
                "This is not supported and calls to the model will likely lead to an error if you dont move your model to a CUDA device!"
                "If you want to run TiRex on CPU you need to disable sLSTM CUDA kernels but be aware of the downsides (see FAQ)"
            )
