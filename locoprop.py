import functools
import math
import typing
import warnings

import torch
import torch.nn as nn



function_mapping = {nn.Sigmoid: lambda x: (x + (1 + (-x).exp()).log()).sum(dim=-1),
                    nn.Tanh: lambda x: (x + (1 + (-x).exp()).log()).sum(dim=-1),
                    nn.ReLU: lambda x: 0.5 * (x * F.relu(x)).sum(dim=-1),
                    nn.Softmax: lambda x: torch.logsumexp(x, dim=-1)
                   }


class LocoLayer(nn.Module):
    def __init__(self, module, activation, implicit=False, eps=1e-5):
        super().__init__()
        self.module = module
        self.activation = activation
        self.implicit = implicit
        self.eps = eps

    def forward(self, x=None, hidden=None):
        if x is None and hidden is None:
            raise ValueError(
                "No argument was given. Provide either input or hidden state."
            )

        if hidden is None:
            hidden = self.module(x)

        if self.implicit:
            return hidden
        return self.activation(hidden)

    def hidden(self, x):
        return self.module(x)

    def _pseudo_inverse(self, target):
        with torch.no_grad():
            # compute pseudo-inverse of y
            if isinstance(self.activation, nn.Sigmoid):
                a = y.clip(self.eps, 1 - self.eps)
                return torch.log(a / (1 - a))
            elif isinstance(self.activation, nn.Tanh):
                a = (y + 1) / 2
                a = a.clip(self.eps, 1 - self.eps)
                return 0.5 * torch.log(a / (1 - a))
            elif isinstance(self.activation, nn.ReLU):
                y = y.clip(0, None)
                return y  # y if y > 0 else 0 => already ReLU
            elif isinstance(self.activation, nn.Softmax):
                return y.log()
        raise ValueError(f"Unsupported activation function: {self.activation}")
        
    
    def bregman_loss(self, x, y):
        pre_act = self.module(x).flatten(start_dim=1)
        a = self._pseudo_inverse(y).flatten(start_dim=1)
        
        F = function_mapping[self.activation]
        return F(pre_act) - F(a) - torch.einsum("bf,bf->b", self.activation(a), pre_act - a)


class LocopropTrainer:
    def __init__(
        self,
        model: nn.Sequential,
        loss_fn: typing.Callable[[torch.Tensor, torch.Tensor], torch.Tensor],
        optimizer_class=torch.optim.RMSprop,
        optimizer_hparams: typing.Dict = dict(
            lr=2e-5, eps=1e-6, momentum=0.999, alpha=0.9
        ),
        learning_rate: float = 10,
        local_iterations: int = 5,
        variant: typing.Literal["LocoPropS", "LocoPropM"] = "LocoPropM",
        momentum: float = 0.0,
        correction: float = 0.1,
    ):
        self.model = model
        self.loss_fn = loss_fn
        self.learning_rate = learning_rate
        self.local_iterations = local_iterations
        self.variant = variant
        self.momentum = momentum
        self.correction = correction

        self.opts = []
        for layer in model:
            trainable_params = sum(
                p.numel() for p in layer.parameters() if p.requires_grad
            )
            if trainable_params > 0 and not isinstance(layer, LocoLayer):
                warnings.warn(
                    f"Layer {layer} is trainable but not a LocoLayer. Its parameters will not be updated."
                )

            if isinstance(layer, LocoLayer) and trainable_params > 0:
                self.opts.append(
                    optimizer_class(layer.parameters(), **optimizer_hparams)
                )
            else:
                self.opts.append(None)
        self.grads = []
        self._step = 0
        
    def step(self, xs, ys):
        self._step += 1
        inps = []
        hiddens = []
        curr = xs

        for layer, opt in zip(self.model, self.opts):
            inps.append(curr.detach())
            if opt is not None and isinstance(layer, LocoLayer):
                hidden = layer.hidden(curr)
                hidden.requires_grad_(True)
                hidden.retain_grad()
                hiddens.append(hidden)
                curr = layer(hidden=hidden)
            else:
                hiddens.append(None)
                curr = layer(curr)

        hidden_loss = self.loss_fn(curr, ys)
        hidden_loss.backward()

        grads = [None if h is None or not hasattr(h, "grad") or h.grad is None else h.grad for h in hiddens]
        if len(self.grads) == 0:
            self.grads = grads
        else:
            debias = (1 - (1 - self.momentum) ** self._step)
            self.grads = [None if m is None else ((1 - self.momentum) * g + self.momentum * m) / debias for g, m in zip(grads, self.grads)]

        for p in self.model.parameters():
            p.requires_grad = True

        for i, (opt, layer, grad) in enumerate(zip(self.opts, self.model, self.grads)):
            if opt is None:
                continue
            if not isinstance(layer, LocoLayer):
                raise ValueError(f"Expected trainable layers to be instance of `LocoLayer` but found `{layer.__class__}`")

            inp = inps[i]
            with torch.no_grad():
                a = layer.hidden(inp)
                y = layer.activation(a)
                post_target = (y - self.learning_rate * grad).detach()

            base_lr = opt.param_groups[0]["lr"]
            for j in range(self.local_iterations):
                opt.param_groups[0]["lr"] = base_lr * max(1.0 - j / self.local_iterations, 0.25)
                opt.zero_grad()

                loss = layer.bregman_loss(inp, post_target).mean()
                loss.backward()
                opt.step()
            opt.param_groups[0]["lr"] = base_lr

            # correct input of next layer
            if self.correction > 0 and i + 1 < len(inps):
                with torch.no_grad():
                    delta = layer(inp) - inps[i + 1]
                    norm = delta.norm() + 1e-5
                    inps[i + 1] += min(norm, self.correction * delta.size(1) ** 0.5) * delta / norm
        return hidden_loss
