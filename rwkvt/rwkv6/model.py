########################################################################################################
# The RWKV Language Model - https://github.com/BlinkDL/RWKV-LM
########################################################################################################
from torch.utils.checkpoint import checkpoint as torch_checkpoint
from torch.profiler import profile, record_function, ProfilerActivity

import os, math, gc, importlib
import torch
import torch.nn as nn
from torch.nn import functional as F
import deepspeed
from rwkvt.infctx_module import BlockStateList
from .block import Block

class RWKV6(nn.Module):
    def __init__(self, args):
        super().__init__()
        self.args = args

        self.emb = nn.Embedding(args.vocab_size, args.n_embd)

        self.blocks = nn.ModuleList([Block(args, i) for i in range(args.n_layer)])

        self.ln_out = nn.LayerNorm(args.n_embd)
        self.head = nn.Linear(args.n_embd, args.vocab_size, bias=False)

    @property
    def _use_infctx(self):
        """判断是否使用无限上下文模式"""
        return os.environ.get("RWKV_TRAIN_TYPE") == 'infctx'

    def forward(self, *args, **kwargs):
        if self._use_infctx:
            return self.forward_infctx(*args, **kwargs)
        return self.forward_normal(*args, **kwargs)

    def forward_normal(self, idx):
        args = self.args
        B, T = idx.size()
        assert T <= args.ctx_len, "Cannot forward, model ctx_len is exhausted."

        x = self.emb(idx)

        for block in self.blocks:
            if args.grad_cp == 1:
                if args.train_type == 'state' or args.peft !='none':
                    x = torch_checkpoint(block, x, use_reentrant=False)
                else:
                    x = deepspeed.checkpointing.checkpoint(block, x)
            else:
                x = block(x)

        x = self.ln_out(x)
        x = self.head(x)

        return x

    def forward_infctx(self, idx,  last_shift_states: torch.Tensor,
            last_wkv_states: torch.Tensor):
        args = self.args
        B, T = idx.size()
        assert T <= args.chunk_ctx, "Cannot forward, model ctx_len is exhausted."
        C = args.n_embd
        H =  args.dim_att // args.head_size_a
        assert C==H*args.head_size_a
        
        x = self.emb(idx)
        new_states = BlockStateList.empty(args.n_layer, B, args.n_embd, H,
                                        x.device, x.dtype)

        
        for i, (block, block_state) in enumerate(zip(self.blocks,
            BlockStateList(last_shift_states, last_wkv_states))):
            if args.grad_cp == 1 and i > 0:# and i < len(self.blocks)-1 :
                x, new_block_state = torch_checkpoint(block, x, block_state, use_reentrant=False)
            else:
                x, new_block_state = block(x, block_state)    
            new_states[i] = new_block_state 

        x = self.ln_out(x)
        x = self.head(x)

        return x, new_states.shift_states, new_states.wkv_states

