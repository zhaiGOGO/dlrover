import functools

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset
from transformers.models.gpt2.configuration_gpt2 import GPT2Config
from transformers.models.gpt2.modeling_gpt2 import GPT2MLP, GPT2Attention, GPT2Block, GPT2Model

from atorch.auto.model_context import ModelContext
from atorch.common.util_func import data_to_device, recursively_apply
from atorch.distributed import seq_all_to_all


def get_gpt2_module_type(module="block"):
    if module == "block":
        return GPT2Block
    elif module == "attn":
        return GPT2Attention
    elif module == "mlp":
        return GPT2MLP
    return None


class ToyCustomModule(nn.Module):
    def __init__(self, in_features=16, out_features=4):
        super().__init__()
        self.linears = torch.nn.ModuleList([nn.Linear(out_features, out_features) for _ in range(8)])

    def forward(self, inputs, test_kwargs=True):
        for op in self.linears:
            inputs = op(inputs)
        return inputs


class ToyModel(nn.Module):
    def __init__(self, in_features=16, out_features=4, use_custom_module=False):
        """
        Args:
            in_feature (int): size of input feature.
            out_feature (int): size of output feature.
        """
        super(ToyModel, self).__init__()
        self.use_custom_module = use_custom_module
        self.linear = torch.nn.Linear(in_features, out_features)
        if use_custom_module:
            self.linears = ToyCustomModule(in_features, out_features)
        else:
            self.linears = torch.nn.ModuleList([nn.Linear(out_features, out_features) for _ in range(8)])

    def forward(self, inputs):
        data = self.linear(inputs[0])
        if self.use_custom_module:
            data = self.linears(data, test_kwargs=True)
        else:
            for op in self.linears:
                data = op(data)
        return data


def optim_func(model_parameters, **kwargs):
    return optim.Adam(model_parameters, **kwargs)


def optim_param_func(model):
    no_decay = "bias"
    parameters = [
        {
            "params": [p for n, p in model.named_parameters() if no_decay not in n],
            "weight_decay": 0.01,
        },
        {
            "params": [p for n, p in model.named_parameters() if no_decay in n],
            "weight_decay": 0.0,
        },
    ]
    return parameters


def loss_func(inputs, output):
    loss = nn.MSELoss()
    return loss(inputs[1], output)


def prepare_input(data, device):
    return data_to_device(data, device)


class ToyDataset(Dataset):
    def __init__(self, size, data_size=(16,), output_size=(4,)):
        """
        Args:
            size (int): the of samples.
            data_size (tuple): the shape of one input, data_size[-1] must match the in_features
                in ToyModule
            output_size (tuple): the shape of output, output_size[-1] must match the out_feautes
                in ToyModule
        """
        self.size = size
        self.data_size = data_size
        self.output_size = output_size

    def __len__(self):
        return self.size

    def __getitem__(self, idx):
        return np.ones(self.data_size, dtype=np.float32) * idx, np.ones(self.output_size, dtype=np.float32)


# copy from pytorch/benchmarks/distributed/pipeline/benchmark_dataset.py with some modification
def collate_sentences_lm(samples):
    if len(samples) == 0:
        return {}

    src_tokens = torch.stack([s["source"] for s in samples], 0)
    tgt_tokens = torch.stack([s["target"] for s in samples], 0)

    batch = {
        "input": src_tokens,
        "target": tgt_tokens,
    }
    return batch


# copy from pytorch/benchmarks/distributed/pipeline/benchmark_dataset.py with some modification
class BenchmarkLMDataset(Dataset):
    def __init__(
        self,
        vocab_size=10000,
        max_source_positions=1024,
        total_samples=10000,
    ):
        self.vocab_size = vocab_size
        self.max_source_positions = max_source_positions
        self.total_samples = total_samples
        self.sizes = [self.max_source_positions] * self.total_samples

    def __getitem__(self, index):
        length = self.sizes[index]
        source = torch.randint(1, self.vocab_size, (length,))
        target = source.clone()
        return {
            "source": source,
            "target": target,
        }

    def __len__(self):
        return self.total_samples


def sp_data_process_fn(batch, sp_size, sp_rank):
    # partition "input" and "target"
    seq_length = batch["input"].shape[-1]
    sub_seq_length = seq_length // sp_size
    sub_start = sp_rank * sub_seq_length
    batch["input"] = batch["input"][:, sub_start : sub_start + sub_seq_length]
    batch["target"] = batch["target"][:, sub_start : sub_start + sub_seq_length].contiguous()
    return batch


class ToyGPT2Model(GPT2Model):
    def __init__(self, hidden_size=256, head_num=4, layer_num=3, seq_length=512):
        config = GPT2Config()
        c_s = f"n_embd={hidden_size},n_head={head_num},n_layer={layer_num},n_positions={seq_length}"
        config.update_from_string(c_s)
        super().__init__(config)
        self.config = config
        self.lm_head = torch.nn.Linear(config.n_embd, config.vocab_size, bias=False)
        self.sp_size = 1
        self.sp_rank = 0

    def set_sp(self, sp_size, sp_rank, sp_group):
        self.sp_size = sp_size
        self.sp_rank = sp_rank

        #  patch attn for all2all q/k/v.
        # This is a very hacky way to add all2all in attn module, which is not recommended. It is better
        # to change the codes in the original codes, or create a new Attention module to use in the model.

        if hasattr(GPT2Attention, "__ori_attn_func"):
            ori_attn = GPT2Attention.__ori_attn_func
        else:
            ori_attn = GPT2Attention._attn
            GPT2Attention.__ori_attn_func = ori_attn

        def _sp_attn(self, query, key, value, attention_mask=None, head_mask=None):
            # query, key, value shapes are (batch, head, sub_seq_length, head_features)
            # 1. all2all to shape (batch, sub_head, seq_length, head_features), scatter head dim, gather seq dim.
            query = seq_all_to_all(query, scatter_idx=1, gather_idx=2, group=sp_group, group_size=sp_size)
            key = seq_all_to_all(key, scatter_idx=1, gather_idx=2, group=sp_group, group_size=sp_size)
            value = seq_all_to_all(value, scatter_idx=1, gather_idx=2, group=sp_group, group_size=sp_size)

            attn_output, attn_weights = self.__ori_attn_func(query, key, value, attention_mask, head_mask)

            # attn_output shape is (batch, sub_head, seq_length, head_features)
            # 2. all2all to shape (batch, head, sub_seq_length, head_features), scatter seq dim, gather head dim.
            attn_output = seq_all_to_all(attn_output, scatter_idx=2, gather_idx=1, group=sp_group, group_size=sp_size)

            return attn_output, attn_weights

        GPT2Attention._attn = _sp_attn

    def forward(
        self,
        input_ids=None,
        past_key_values=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        labels=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        # inputs requirement:
        # If sequence parallel (sp_size > 1), input_ids/inputs_embeds/token_type_ids/position_ids should already
        # be partitioned to rank-th sub-sequence [seq_length//sp_size*rank:seq_length//sp_size*(rank+1)].
        # Note that sequence parallel for all computations outside attention module.
        # Inside attention, only head dimension is parallized, not the sequence dimension.
        # Thus, attention_mask should not be partitioned.

        if self.sp_size > 1:
            # 1. change head_mask if required
            # head_mask shape is [num_heads] or [num_hidden_layers x num_heads]
            # when sp, it should be converted to [num_hidden_layers x num_sub_heads]
            if head_mask is not None:
                if head_mask.dim() == 1:
                    head_mask = head_mask.expand(self.config.n_layer, -1)
                # now split to get corresponding sub_head_mask
                heads_per_subseq = self.config.num_attention_heads // self.sp_size
                head_mask = head_mask[self.sp_rank * heads_per_subseq : (self.sp_rank + 1) * heads_per_subseq]

            # 2. generate sub_seq position_ids if position_ids not provided
            if position_ids is None:
                if input_ids is not None:
                    seq_length = input_ids.size()[-1] * self.sp_size
                    device = input_ids.device
                else:
                    seq_length = inputs_embeds.size[-1] * self.sp_size
                    device = inputs_embeds.device
                past_length = 0 if past_key_values is None else past_key_values[0][0].size(-2)
                sub_seq_length = seq_length // self.sp_size
                start_pos = self.sp_rank * sub_seq_length + past_length
                position_ids = torch.arange(start_pos, start_pos + sub_seq_length, dtype=torch.long, device=device)
                position_ids = position_ids.unsqueeze(0)

        transformer_outputs = super().forward(
            input_ids,
            past_key_values=past_key_values,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            encoder_hidden_states=encoder_hidden_states,
            encoder_attention_mask=encoder_attention_mask,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        hidden_states = transformer_outputs[0]
        lm_logits = self.lm_head(hidden_states)
        return lm_logits

    def vocab_size(self):
        return self.config.vocab_size


def gpt2_loss_func(inputs, output, vocab_size):
    criterion = torch.nn.CrossEntropyLoss()
    return criterion(output.view(-1, vocab_size), inputs["target"].view(-1))


def create_model_context(
    data_size=16,
    batch_size=2,
    use_optim_param_func=False,
    dataset=None,
    distributed_sampler_cls=None,
    use_custom_module=False,
    use_gpt2=False,
    hidden_size=256,
    head_num=4,
    layer_num=3,
    seq_length=512,
    extra_args=None,
):
    user_defined_optim_param_func = optim_param_func if use_optim_param_func else None
    dataloader_args = {"batch_size": batch_size, "drop_last": True, "shuffle": True, "num_workers": 1}
    if use_gpt2:
        model = ToyGPT2Model(hidden_size=hidden_size, head_num=head_num, layer_num=layer_num, seq_length=seq_length)
        dataset = BenchmarkLMDataset(
            vocab_size=model.vocab_size(), max_source_positions=seq_length, total_samples=data_size
        )
        dataloader_args["collate_fn"] = collate_sentences_lm
        model_loss_func = functools.partial(gpt2_loss_func, vocab_size=model.vocab_size())
    else:
        model = ToyModel(use_custom_module=use_custom_module)
        dataset = ToyDataset(data_size) if dataset is None else dataset
        model_loss_func = loss_func
    model_context = ModelContext(
        model=model,
        optim_func=optim_func,
        dataset=dataset,
        loss_func=model_loss_func,
        prepare_input=prepare_input,
        optim_args={"lr": 0.001},
        optim_param_func=user_defined_optim_param_func,
        dataloader_args=dataloader_args,
        distributed_sampler_cls=distributed_sampler_cls,
        extra_args=extra_args,
    )
    return model_context


def change_dtype(data, dtype, fp32_only=True):
    if data.dtype == torch.float32 or not fp32_only:
        return data.to(dtype)
    else:
        return data


def run_train(
    model,
    dataloader,
    optim,
    prepare_input,
    loss_func,
    device="cpu",
    input_dtype=torch.float32,
    gpt2_model=False,
    use_optim_backward=False,
):
    for idx, data in enumerate(dataloader):
        pdata = prepare_input(data, device)
        if input_dtype != torch.float32:
            pdata = recursively_apply(change_dtype, pdata, input_dtype)
        optim.zero_grad()
        if gpt2_model:
            output = model(pdata["input"])
        else:
            output = model(pdata)
        loss = ModelContext.get_loss_from_loss_func_output(loss_func(pdata, output))
        if use_optim_backward:
            optim.backward(loss)
        else:
            loss.backward()
        optim.step()
    return idx + 1
