"""
@article{yun2022FastGTN,
title = {Graph Transformer Networks: Learning meta-path graphs to improve GNNs},
journal = {Neural Networks},
volume = {153},
pages = {104-119},
year = {2022},
}
github: https://github.com/seongjunyun/Graph_Transformer_Networks/blob/master/model_fastgtn.py
"""

import torch
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import math
import torch_sparse
from torch_geometric.utils import softmax
from utils import _norm, generate_non_local_graph
from torch.nn import Parameter
from torch_scatter import scatter_add
from torch_geometric.nn.conv.message_passing import MessagePassing
from torch_geometric.utils import add_self_loops
from inits import glorot, zeros

device = f'cuda' if torch.cuda.is_available() else 'cpu'


class FastGTNs(nn.Module):
    def __init__(self, num_edge_type, w_in, num_class, num_nodes, args=None):
        super(FastGTNs, self).__init__()
        self.args = args
        self.num_nodes = num_nodes
        self.fastGTN = FastGTN(num_edge_type, w_in, num_class, num_nodes, args)

    def forward(self, A, X, num_nodes=None, eval=False, args=None, n_id=None, node_labels=None,
                epoch=None):
        if num_nodes == None:
            num_nodes = self.num_nodes
        H_, Ws = self.fastGTN(A, X, num_nodes=num_nodes, epoch=epoch)
        return H_


class FastGTN(nn.Module):
    def __init__(self, num_edge_type, w_in, num_class, num_nodes, args=None, pre_trained=None):
        super(FastGTN, self).__init__()
        if args.non_local:
            num_edge_type += 1
        self.num_edge_type = num_edge_type
        self.num_channels = args.num_channels
        self.num_nodes = num_nodes
        self.w_in = w_in
        args.w_in = w_in
        self.w_out = args.node_dim
        self.num_class = num_class
        self.num_layers = args.num_layers

        if pre_trained is None:
            layers = []
            for i in range(self.num_layers):
                if i == 0:
                    layers.append(FastGTLayer(num_edge_type, self.num_channels, num_nodes, first=True, args=args))
                else:
                    layers.append(FastGTLayer(num_edge_type, self.num_channels, num_nodes, first=False, args=args))
            layers.append(FastGTLayer(num_edge_type, self.num_channels, num_nodes, first=True, args=args))
            self.layers = nn.ModuleList(layers)

        self.Ws = []
        for i in range(self.num_channels):
            self.Ws.append(GCNConv(in_channels=self.w_in, out_channels=self.w_out).weight)
        self.Ws = nn.ParameterList(self.Ws)

        self.linear1 = nn.Linear(self.w_out * self.num_channels, self.w_out)

        feat_trans_layers = []
        for i in range(self.num_layers + 1):
            feat_trans_layers.append(nn.Sequential(nn.Linear(self.w_out, 128),
                                                   nn.ReLU(),
                                                   nn.Linear(128, 64)))
        self.feat_trans_layers = nn.ModuleList(feat_trans_layers)

        self.args = args

        self.out_norm = nn.LayerNorm(self.w_out)
        self.relu = torch.nn.ReLU()

    def forward(self, A, X, num_nodes, eval=False, node_labels=None, epoch=None):
        Ws = []
        X_ = [X @ W.to(X.dtype) for W in self.Ws]   # GCN
        H = [X @ W.to(X.dtype) for W in self.Ws]   # GCN
        init_H = H
        concat_h = []

        for i in range(self.num_layers):
            if self.args.non_local:
                g = generate_non_local_graph(self.args, self.feat_trans_layers[i], torch.stack(H).mean(dim=0), A, self.num_edge_type, num_nodes)
                deg_inv_sqrt, deg_row, deg_col = _norm(g[0].detach(), num_nodes, g[1])
                g[1] = softmax(g[1],deg_row)
                if len(A) < self.num_edge_type:
                    A.append(g)
                else:
                    A[-1] = g

            H, W = self.layers[i](H, A, num_nodes, epoch=epoch, layer=i + 1)
            H = H + init_H
            Ws.append(W)   # FastLayer

        for i in range(self.num_channels):
            if i == 0:
                H_ = F.relu(self.args.beta * (X_[i]) + (1 - self.args.beta) * H[i])
            else:
                if self.args.channel_agg == 'concat':
                    H_ = torch.cat((H_,F.relu(self.args.beta * (X_[i]) + (1-self.args.beta) * H[i])), dim=1)
                elif self.args.channel_agg == 'mean':
                    H_ = H_ + F.relu(self.args.beta * (X_[i]) + (1-self.args.beta) * H[i])
        if self.args.channel_agg == 'concat':
            H_ = self.linear1(H_)
        elif self.args.channel_agg == 'mean':
            H_ = H_ / self.args.num_channels
        return H_, Ws


class FastGTLayer(nn.Module):

    def __init__(self, in_channels, out_channels, num_nodes, first=True, args=None, pre_trained=None):
        super(FastGTLayer, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.first = first
        self.num_nodes = num_nodes
        self.conv1 = FastGTConv(in_channels, out_channels, num_nodes, args=args)
        self.args = args
        self.feat_transfrom = nn.Sequential(nn.Linear(args.w_in, 128),
                                            nn.ReLU(),
                                            nn.Linear(128, 64))

    def forward(self, H_, A, num_nodes, epoch=None, layer=None):
        result_A, W1 = self.conv1(A, num_nodes, epoch=epoch, layer=layer)
        W = [W1]
        Hs = []
        for i in range(len(result_A)):
            a_edge, a_value = result_A[i]
            mat_a = torch.sparse_coo_tensor(a_edge, a_value, (num_nodes, num_nodes)).to(a_edge.device)
            H = torch.sparse.mm(mat_a, H_[i])
            Hs.append(H)
        return Hs, W


class FastGTConv(nn.Module):

    def __init__(self, in_channels, out_channels, num_nodes, args=None, pre_trained=None):
        super(FastGTConv, self).__init__()
        self.args = args
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.weight = nn.Parameter(torch.Tensor(out_channels, in_channels))

        self.bias = None
        self.scale = nn.Parameter(torch.Tensor([0.1]), requires_grad=False)
        self.num_nodes = num_nodes

        self.reset_parameters()

    def reset_parameters(self):
        n = self.in_channels
        nn.init.normal_(self.weight, std=0.1)
        if self.args.non_local and self.args.non_local_weight != 0:
            with torch.no_grad():
                self.weight[:,-1] = self.args.non_local_weight
        if self.bias is not None:
            fan_in, _ = nn.init._calculate_fan_in_and_fan_out(self.weight)
            bound = 1 / math.sqrt(fan_in)
            nn.init.uniform_(self.bias, -bound, bound)

    def forward(self, A, num_nodes, epoch=None, layer=None):
        weight = self.weight
        filter = F.softmax(weight, dim=1)
        num_channels = filter.shape[0]
        results = []
        for i in range(num_channels):
            for j, (edge_index, edge_value) in enumerate(A):
                if j == 0:
                    total_edge_index = edge_index
                    total_edge_value = edge_value * filter[i][j]
                else:
                    total_edge_index = torch.cat((total_edge_index, edge_index), dim=1)
                    total_edge_value = torch.cat((total_edge_value, edge_value * filter[i][j]))

            index, value = torch_sparse.coalesce(total_edge_index.detach(), total_edge_value, m=num_nodes, n=num_nodes,
                                                 op='add')
            results.append((index, value))

        return results, filter


class PygFastGTNs(nn.Module):
    def __init__(self, num_edge_type, w_in, num_class, num_nodes, args=None):
        super(PygFastGTNs, self).__init__()
        self.args = args
        self.num_nodes = num_nodes
        self.num_FastGTN_layers = args.num_FastGTN_layers
        fastGTNs = []

        fastGTNs.append(FastGTN(num_edge_type, w_in, num_class, num_nodes, args))
        self.fastGTNs = nn.ModuleList(fastGTNs)
        self.linear = nn.Linear(args.node_dim, num_class)
        self.loss = nn.CrossEntropyLoss()

    def forward(self, A, X, num_nodes=None, eval=False, args=None, n_id=None, node_labels=None,
                epoch=None):
        if num_nodes == None:
            num_nodes = self.num_nodes
        # print(num_nodes)   # 8994
        # print(X.shape)   # 8994 1902
        # print(target_x.shape)   # 600
        # print(target.shape)  # 600 3
        H_, Ws = self.fastGTNs[0](A, X, num_nodes=num_nodes, epoch=epoch)
        return H_

"""
@article{yun2022FastGTN,
title = {Graph Transformer Networks: Learning meta-path graphs to improve GNNs},
journal = {Neural Networks},
volume = {153},
pages = {104-119},
year = {2022},
}
github: https://github.com/seongjunyun/Graph_Transformer_Networks/blob/master/gcn.py
"""

class GCNConv(MessagePassing):
    r"""The graph convolutional operator from the `"Semi-supervised
    Classfication with Graph Convolutional Networks"
    <https://arxiv.org/abs/1609.02907>`_ paper

    .. math::
        \mathbf{X}^{\prime} = \mathbf{\hat{D}}^{-1/2} \mathbf{\hat{A}}
        \mathbf{\hat{D}}^{-1/2} \mathbf{X} \mathbf{\Theta},

    where :math:`\mathbf{\hat{A}} = \mathbf{A} + \mathbf{I}` denotes the
    adjacency matrix with inserted self-loops and
    :math:`\hat{D}_{ii} = \sum_{j=0} \hat{A}_{ij}` its diagonal degree matrix.

    Args:
        in_channels (int): Size of each input sample.
        out_channels (int): Size of each output sample.
        improved (bool, optional): If set to :obj:`True`, the layer computes
            :math:`\mathbf{\hat{A}}` as :math:`\mathbf{A} + 2\mathbf{I}`.
            (default: :obj:`False`)
        cached (bool, optional): If set to :obj:`True`, the layer will cache
            the computation of :math:`{\left(\mathbf{\hat{D}}^{-1/2}
            \mathbf{\hat{A}} \mathbf{\hat{D}}^{-1/2} \right)}`.
            (default: :obj:`False`)
        bias (bool, optional): If set to :obj:`False`, the layer will not learn
            an additive bias. (default: :obj:`True`)
    """

    def __init__(self,
                 in_channels,
                 out_channels,
                 improved=False,
                 cached=False,
                 bias=True,
                 args=None):
        super(GCNConv, self).__init__('add', flow='target_to_source')

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.improved = improved
        self.cached = cached
        self.cached_result = None

        self.weight = Parameter(torch.Tensor(in_channels, out_channels))

        if bias:
            self.bias = Parameter(torch.Tensor(out_channels))
        else:
            self.register_parameter('bias', None)

        self.args = args
        self.reset_parameters()

    def reset_parameters(self):
        glorot(self.weight)
        zeros(self.bias)
        self.cached_result = None

    @staticmethod
    def norm(edge_index, num_nodes, edge_weight, improved=False, dtype=None, args=None):
        if edge_weight is None:
            edge_weight = torch.ones((edge_index.size(1),),
                                     dtype=dtype,
                                     device=edge_index.device)
        edge_weight = edge_weight.view(-1)
        assert edge_weight.size(0) == edge_index.size(1)

        edge_index, _ = add_self_loops(edge_index, num_nodes=num_nodes)

        loop_weight = torch.full((num_nodes,),
                                 1 if not args.remove_self_loops else 0,
                                 dtype=edge_weight.dtype,
                                 device=edge_weight.device)
        edge_weight = torch.cat([edge_weight, loop_weight], dim=0)

        row, col = edge_index

        # deg = scatter_add(edge_weight, col, dim=0, dim_size=num_nodes)
        deg = scatter_add(edge_weight, row, dim=0, dim_size=num_nodes)
        deg_inv_sqrt = deg.pow(-1)
        deg_inv_sqrt[deg_inv_sqrt == float('inf')] = 0

        # return edge_index, (deg_inv_sqrt[col] ** 0.5) * edge_weight * (deg_inv_sqrt[row] ** 0.5)
        return edge_index, deg_inv_sqrt[row] * edge_weight

    def forward(self, x, edge_index, edge_weight=None):
        """"""
        x = torch.matmul(x, self.weight)

        if not self.cached or self.cached_result is None:
            edge_index, norm = self.norm(edge_index, x.size(0), edge_weight,
                                         self.improved, x.dtype, args=self.args)
            self.cached_result = edge_index, norm
        edge_index, norm = self.cached_result

        return self.propagate(edge_index, x=x, norm=norm)

    def message(self, x_j, norm):
        return norm.view(-1, 1) * x_j

    def update(self, aggr_out):
        if self.bias is not None:
            aggr_out = aggr_out + self.bias
        return aggr_out

    def __repr__(self):
        return '{}({}, {})'.format(self.__class__.__name__, self.in_channels,
                                   self.out_channels)