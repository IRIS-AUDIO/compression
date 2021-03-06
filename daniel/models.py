import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from utils import *


class Grid(nn.Module):
    def __init__(self, grid_resolution, bitwidth=6, code_size=4,
                 cubic_interpolation=False):
        super().__init__()
        self.grid_resolution = grid_resolution
        self.bitwidth = bitwidth
        self.code_size = code_size
        self.cubic_interpolation = cubic_interpolation

        self.codebook = nn.Parameter(
            0.01 * torch.randn(2**bitwidth, code_size), requires_grad=True)
        self.indices = nn.Parameter(
            0.01 * torch.randn(grid_resolution, 2**bitwidth),
            requires_grad=True)

    def forward(self, coords):
        # coords: values in between [-1, 1]
        # grid: [grid_resolution, code_size]
        soft_grid = F.softmax(self.indices, dim=-1) @ self.codebook
        max_grid = torch.index_select(self.codebook, 0,
                                      torch.argmax(self.indices, dim=1))
        grid = (max_grid - soft_grid).detach() + soft_grid

        coords = (coords + 1) / 2 * (self.grid_resolution - 1)

        left = torch.floor(coords).clamp(max=self.grid_resolution-2).int()
        right = torch.ceil(coords).clamp(min=1).int()
        weight = (coords - left).unsqueeze(-1)

        left_value = torch.index_select(grid, 0, left)
        right_value = torch.index_select(grid, 0, right)

        if not self.cubic_interpolation:
            return (1-weight) * left_value + weight * right_value

        l_left = torch.floor(coords-1) \
                      .clamp(min=0, max=self.grid_resolution-3).int()
        r_right = torch.ceil(coords+1) \
                       .clamp(min=2, max=self.grid_resolution-1).int()

        l_left_value = torch.index_select(grid, 0, l_left)
        r_right_value = torch.index_select(grid, 0, r_right)

        return left_value + 0.5 * weight * (right_value - l_left_value + weight * (2 * l_left_value - 5 * left_value + 4 * right_value - r_right_value + weight * (3 * (left_value - right_value) + r_right_value - l_left_value)))

    def get_bit_size(self):
        return 16 * self.codebook.numel() + self.grid_resolution * self.bitwidth


class GridVINR(nn.Module):
    def __init__(self, in_dim, out_dim, n_hidden_layers=3,
                 hidden_dim=64, activation='gelu', n_bits=8, grid_reduce='sum'):
        super().__init__()
        self.n_hidden_layers = n_hidden_layers
        self.grid_reduce = grid_reduce

        self.grids = nn.ModuleList([Grid(2**i, code_size=c)
                                    for i, c in [[7, 4], [9, 4], [11, 4]]])

        if grid_reduce == 'sum':
            first_size = in_dim + self.grids[0].code_size
        elif grid_reduce == 'cat':
            first_size = in_dim + sum([g.code_size for g in self.grids])
        else:
            raise ValueError(f'invalid grid_reduce: {grid_reduce}')

        net = [QALinear(first_size, hidden_dim, n_bits=n_bits)]

        for i in range(n_hidden_layers):
            net.append(get_activation_fn(activation))
            net.append(QALinear(hidden_dim, hidden_dim, n_bits=n_bits))

        net.extend([get_activation_fn(activation),
                    QALinear(hidden_dim, out_dim, n_bits=n_bits)])

        self.net = nn.Sequential(*net)

    def forward(self, inputs):
        if self.grid_reduce == 'sum':
            inputs = torch.cat([sum([g(inputs[..., -1]) for g in self.grids]),
                                inputs[..., :-1]], -1)
        else:
            inputs = torch.cat([torch.cat([g(inputs[..., -1])
                                           for g in self.grids], -1),
                                inputs[..., :-1]], -1)
        return torch.tanh(self.net(inputs))

    def get_bit_size(self):
        return sum([0 if not hasattr(l, 'get_bit_size') else l.get_bit_size()
                    for l in self.net]) \
               + sum([g.get_bit_size() for g in self.grids])


class VINR(nn.Module):
    '''
    Assumptions
        1. mono audio
    '''
    def __init__(self, in_dim, out_dim, n_hidden_layers=3,
                 hidden_dim=64, activation='gelu', n_bits=8):
        super().__init__()
        self.n_hidden_layers = n_hidden_layers

        net = [QALinear(in_dim, hidden_dim, n_bits=n_bits)]

        for i in range(n_hidden_layers):
            net.append(get_activation_fn(activation))
            net.append(QALinear(hidden_dim, hidden_dim, n_bits=n_bits))

        net.extend([get_activation_fn(activation),
                    QALinear(hidden_dim, out_dim, n_bits=n_bits)])

        self.net = nn.Sequential(*net)

    def forward(self, inputs):
        return torch.tanh(self.net(inputs))

    def get_bit_size(self):
        return sum([0 if not hasattr(l, 'get_bit_size') else l.get_bit_size()
                    for l in self.net])


class QALinear(nn.Module):
    def __init__(self, in_dim, out_dim, n_bits=8, quant_axis=(-2, -1)):
        super().__init__()
        self.in_dim = in_dim
        self.out_dim = out_dim
        self.n_bits = n_bits
        self.quant_axis = quant_axis

        self.weight = nn.Parameter(
            (2 * torch.rand(in_dim, out_dim) - 1) / (in_dim**0.5),
            requires_grad=True)
        self.bias = nn.Parameter(
            (2 * torch.rand(out_dim) - 1) / (in_dim**0.5),
            requires_grad=True)

    def forward(self, inputs):
        # quantize
        if self.n_bits != 16:
            r_weight = self.rounding(self.weight, self.quant_axis)
            weight = (r_weight - self.weight).detach() + self.weight

            bias = (self.rounding(self.bias) - self.bias).detach() + self.bias
        else:
            weight = self.weight
            bias = self.bias

        return inputs @ weight + bias

    def rounding(self, inputs, axis=-1, minvalue=1e-8):
        min_value = torch.amin(inputs, axis, keepdims=True)
        max_value = torch.amax(inputs, axis, keepdims=True)
        scale = (max_value - min_value).clamp(min=minvalue) / (self.n_bits**2 - 1)

        return torch.round((inputs - min_value) / scale) * scale + min_value

    def get_bit_size(self):
        if self.n_bits == 16:
            return 16 * (self.weight.numel() + self.bias.numel())
        return 16 * 2 * self.weight.amax(self.quant_axis).numel() \
            + self.weight.numel() * self.n_bits \
            + 16 * 2 + self.bias.numel() * self.n_bits


class SE(nn.Module):
    def __init__(self, channel, reduction=4):
        super().__init__()
        self.fc = nn.Sequential(
            nn.Linear(channel, channel // reduction, bias=False),
            nn.ReLU(inplace=True),
            nn.Linear(channel // reduction, channel, bias=False),
            nn.Sigmoid())

    def forward(self, inputs):
        return inputs * self.fc(inputs)


class Siren(nn.Module):
    def __init__(self, in_features, out_features, n_hidden_layers, hidden_dim,
                 first_omega_0=30, hidden_omega_0=30):
        super().__init__()
        net = [SineLayer(in_features, hidden_dim, is_first=True,
                         omega_0=first_omega_0)]
        for i in range(n_hidden_layers):
            net.append(SineLayer(hidden_dim, hidden_dim,
                                 omega_0=hidden_omega_0))

        # output layer
        net.append(nn.Linear(hidden_dim, out_features))
        with torch.no_grad():
            net[-1].weight.uniform_(-np.sqrt(6/hidden_dim) / hidden_omega_0,
                                     np.sqrt(6/hidden_dim) / hidden_omega_0)
        net.append(nn.Tanh())

        self.net = nn.Sequential(*net)

    def forward(self, inputs):
        return self.net(inputs)


class SineLayer(nn.Module):
    def __init__(self, in_features, out_features, bias=True, is_first=False,
                 omega_0=30):
        super().__init__()
        self.omega_0 = omega_0
        self.is_first = is_first
        self.linear = nn.Linear(in_features, out_features, bias=bias)
        self.init_weights(in_features)

    def init_weights(self, in_features):
        with torch.no_grad():
            if self.is_first:
                self.linear.weight.uniform_(-1 / in_features, 1 / in_features)
            else:
                self.linear.weight.uniform_(
                    -np.sqrt(6/in_features) / self.omega_0,
                     np.sqrt(6/in_features) / self.omega_0)

    def forward(self, inputs):
        return torch.sin(self.omega_0 * self.linear(inputs))

