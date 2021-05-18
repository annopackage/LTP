# Copyright 2021 Samsung Semiconductor Incorporated.

import torch
import torch.nn as nn
import math


class AbstractTokenPruner(nn.Module):
    def __init__(self, **kwargs):
        super().__init__()

    def update_attention_mask(self, attention_mask, attention_probs, sentence_lengths):
        return attention_mask


class CascadeTokenPruner(AbstractTokenPruner):
    """
    implements the layer-by-layer operations for the cascade token pruning method described in:

    Wang et al.,
    SpAtten: Efficient Sparse Attention Architecture with Cascade Token and Head Pruning
    https://arxiv.org/abs/2012.09852
    """
    def __init__(self, module_num, token_keep_rate, num_hidden_layers, **kwargs):
        super().__init__()
        self.keep_rate = self._set_token_keep_rate(module_num, num_hidden_layers, token_keep_rate)
        self.threshold_score = None

    @staticmethod
    def _set_token_keep_rate(i, num_hidden_layers, token_keep_rate):
        """
        Following the SpAtten paper, the rules for token pruning are:
        * the first 3 or 15% of layers, whichever is greater, should not be token pruned
        * for the remaining layers, the fraction of pruned tokens should increase linearly until the desired final
        value is reached.
        This method implements these rules and sets the keep_rate field for each pruner in each PIBertLayer.
        """
        layers_before_pruning = max(3, math.ceil(0.15 * num_hidden_layers))
        layers_with_pruning = num_hidden_layers - layers_before_pruning
        if i < layers_before_pruning:
            return 1.0
        else:
            m = (token_keep_rate - 1) / layers_with_pruning
            temp =  m * (i - layers_before_pruning + 1) + 1
            temp = max(0.01, temp)
            print('tkr:', temp)
            return temp

    def update_attention_mask(self, attention_mask, attention_probs, sentence_lengths):
        keep_tokens = torch.round(sentence_lengths * self.keep_rate).long()
        sz = attention_probs.shape[-1]
        batch_size = attention_mask.shape[0]
        self.threshold_score = torch.zeros((batch_size,))
        if self.keep_rate == 1:
            return attention_mask

        # compute the pruning scores by summing the attention probabilities over all heads
        attention_mask_index = (attention_mask < 0).permute(0, 1, 3, 2).repeat(1, attention_probs.shape[1], 1, sz)
        attention_probs[attention_mask_index] = 0
        pruning_scores = attention_probs.view(batch_size, -1, sz).sum(dim=1)

        # sort the pruning scores using the top-k engine
        top_scores, sorted_indices = torch.sort(-pruning_scores, dim=-1)

        # construct the new attention mask
        new_attention_mask = torch.ones(attention_mask.shape, device=attention_mask.device) * -10000
        # TODO: remove for loop if possible
        for i in range(batch_size):
            new_attention_mask[i, ..., sorted_indices[i, 0:keep_tokens[i]]] = 0
            # if keep_tokens[i] < sz:
            #    self.threshold_score[i] = -top_scores[i, keep_tokens[i]] / torch.max(-top_scores[i, ...])

        return new_attention_mask


class ThresholdTokenPruner(AbstractTokenPruner):
    """
    implements the layer-by-layer operations for threshold token pruning, where tokens are pruned if the importance
    score is strictly less than a given fraction of the maximum token importance score
    """
    def __init__(self, module_num, token_threshold, **kwargs):
        super().__init__()
        self.keep_threshold = token_threshold

    def update_attention_mask(self, attention_mask, attention_probs, sentence_lengths):
        sz = attention_probs.shape[-1]
        batch_size = attention_mask.shape[0]
        if self.keep_threshold == 0:
            return attention_mask

        # compute the pruning scores by summing the attention probabilities over all heads
        attention_mask_index = (attention_mask < 0).permute(0, 1, 3, 2).repeat(1, attention_probs.shape[1], 1, sz)
        attention_probs[attention_mask_index] = 0
        pruning_scores = attention_probs.view(batch_size, -1, sz).sum(dim=1)

        max_pruning_scores, _ = torch.max(pruning_scores, dim=-1, keepdim=True)
        relative_pruning_scores = pruning_scores / max_pruning_scores

        # construct the new attention mask
        new_attention_mask = torch.zeros(attention_mask.shape, device=attention_mask.device)
        new_attention_mask[relative_pruning_scores.unsqueeze(1).unsqueeze(1) < self.keep_threshold] = -10000

        return new_attention_mask


class RisingThresholdTokenPruner(ThresholdTokenPruner):
    def __init__(self, module_num, final_token_threshold=None, num_hidden_layers=None, **kwargs):
        super().__init__()
        self.keep_threshold = final_token_threshold * module_num / num_hidden_layers
        print(self.keep_threshold)


class GradientMask(torch.autograd.Function):

    @staticmethod
    def forward(ctx, inputs, threshold, pruning_scores, reg, num):
        ctx.save_for_backward(threshold, pruning_scores)
        ctx.reg = reg
        ctx.num = num #remove this
        return inputs

    @staticmethod
    def backward(ctx, grad_output):
        threshold, pruning_scores = ctx.saved_tensors
        scale = torch.sigmoid((threshold - pruning_scores) * 100) * (pruning_scores != 0)
        reg = ctx.reg
        num = ctx.num

        grad_output_scaled = grad_output * scale.unsqueeze(-1)
        grad_nonzeros = (grad_output_scaled != 0).sum()
        grad_output_scaled = grad_output_scaled.sum()

        reg_scaled = (reg * scale.unsqueeze(-1)).sum()

        grad_threshold = grad_output_scaled + reg_scaled
        if grad_nonzeros != 0:
            grad_threshold /= grad_nonzeros

        return grad_output, -grad_threshold, None, None, None


class AbsoluteThresholdTokenPruner(AbstractTokenPruner):
    """
    implements the layer-by-layer operations for threshold token pruning, where tokens are pruned if the importance
    score is strictly less than a given fraction of the maximum token importance score
    """
    def __init__(self, module_num, final_token_threshold=None, num_hidden_layers=None, scoring_mode='mean', **kwargs):
        super().__init__()
        '''
        example = [0.00051, 0.00082, 0.00089,0.00016, 0.00519, 
                0.00292, 0.00180, 0.00202, 0.00490, 0.00327, 0.00609]
        '''
        self.keep_threshold = nn.Parameter(
                torch.tensor(final_token_threshold * module_num / num_hidden_layers, device='cuda'), requires_grad=True
                #torch.tensor(example[module_num], device='cuda'), requires_grad=False
        )
        self.scoring_mode = scoring_mode
        self.module_num = module_num

        print(float(self.keep_threshold))

    def update_attention_mask(self, attention_mask, attention_probs, sentence_lengths, training=True):
        sz = attention_probs.shape[-1]
        batch_size = attention_mask.shape[0]
        if self.keep_threshold == 0:
            return attention_mask

        # compute the pruning scores by summing the attention probabilities over all heads
        attention_mask_index = (attention_mask < 0).permute(0, 1, 3, 2).repeat(1, attention_probs.shape[1], 1, sz)
        attention_probs[attention_mask_index] = 0 
        if self.scoring_mode == 'mean':
            pruning_scores = attention_probs.view(batch_size, -1, sz).mean(dim=1)
        elif self.scoring_mode == 'mean_norm':
            seqlen = (attention_mask.squeeze(1).squeeze(1) == 0).sum(-1)
            pruning_scores = attention_probs.sum(2) / seqlen.reshape(-1, 1, 1) # BS, H, SL
            pruning_scores = pruning_scores.mean(1)

        new_attention_mask = torch.zeros(attention_mask.shape, device=attention_mask.device)
        new_attention_mask[pruning_scores.unsqueeze(1).unsqueeze(1) < max(1e-5, self.keep_threshold)] = -10000

        return new_attention_mask, self.keep_threshold, pruning_scores


TOKEN_PRUNERS = {'topk': CascadeTokenPruner,
                 'threshold': ThresholdTokenPruner,
                 'rising_threshold': RisingThresholdTokenPruner,
                 'absolute_threshold': AbsoluteThresholdTokenPruner,
                 }

