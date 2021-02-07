from SourceCodeTools.models.graph.rgcn_sampling import RGCNSampling, RelGraphConvLayer

import torch as th
import torch.nn as nn
import torch.nn.functional as F
import dgl
import dgl.nn as dglnn
# import tqdm
from SourceCodeTools.models.Embedder import Embedder

from SourceCodeTools.nlp import token_hasher

class AttentiveAggregator(nn.Module):
    def __init__(self, emb_dim, num_dst_embeddings=3000, dropout=0.):
        super(AttentiveAggregator, self).__init__()
        self.att = nn.MultiheadAttention(emb_dim, num_heads=1, dropout=dropout)
        self.query_emb = nn.Embedding(num_dst_embeddings, emb_dim)
        self.num_query_buckets = num_dst_embeddings

    def forward(self, list_inputs, dsttype):  # pylint: disable=unused-argument
        if len(list_inputs) == 1:
            return list_inputs[0]
        key = value = th.stack(list_inputs).squeeze(dim=1)
        query = self.query_emb(th.LongTensor([token_hasher(dsttype, self.num_query_buckets)])).unsqueeze(0).repeat(1, key.shape[1], 1)
        att_out, att_w = self.att(query, key, value)
        return att_out.mean(0).unsqueeze(1)


class RGANLayer(RelGraphConvLayer):
    def __init__(self,
                 in_feat,
                 out_feat,
                 rel_names,
                 num_bases,
                 *,
                 weight=True,
                 bias=True,
                 activation=None,
                 self_loop=False,
                 dropout=0.0):
        super(RelGraphConvLayer, self).__init__()
        self.in_feat = in_feat
        self.out_feat = out_feat
        self.rel_names = rel_names
        self.num_bases = num_bases
        self.bias = bias
        self.activation = activation
        self.self_loop = self_loop

        self.attentive_aggregator = AttentiveAggregator(out_feat)

        # TODO
        # think of possibility switching to GAT
        # rel : dglnn.GATConv(in_feat, out_feat, num_heads=4)
        # rel : dglnn.GraphConv(in_feat, out_feat, norm='right', weight=False, bias=False, allow_zero_in_degree=True)
        self.conv = dglnn.HeteroGraphConv({
                rel : dglnn.GATConv(in_feat, out_feat, num_heads=1)
                for rel in rel_names
            }, aggregate=self.attentive_aggregator)

        self.use_weight = weight
        self.use_basis = num_bases < len(self.rel_names) and weight
        if self.use_weight:
            if self.use_basis:
                self.basis = dglnn.WeightBasis((in_feat, out_feat), num_bases, len(self.rel_names))
            else:
                self.weight = nn.Parameter(th.Tensor(len(self.rel_names), in_feat, out_feat))
                # nn.init.xavier_uniform_(self.weight, gain=nn.init.calculate_gain('relu'))
                nn.init.xavier_normal_(self.weight)

        # bias
        if bias:
            self.h_bias = nn.Parameter(th.Tensor(out_feat))
            nn.init.zeros_(self.h_bias)

        # weight for self loop
        if self.self_loop:
            self.loop_weight = nn.Parameter(th.Tensor(in_feat, out_feat))
            # nn.init.xavier_uniform_(self.loop_weight,
            #                         gain=nn.init.calculate_gain('relu'))
            nn.init.xavier_normal_(self.loop_weight)

        self.dropout = nn.Dropout(dropout)


class RGAN(RGCNSampling):
    def __init__(self,
                 g,
                 h_dim, num_classes,
                 num_bases,
                 num_hidden_layers=1,
                 dropout=0,
                 use_self_loop=False,
                 activation=F.relu):
        super(RGCNSampling, self).__init__()
        self.g = g
        self.h_dim = h_dim
        self.out_dim = num_classes
        self.activation = activation

        self.rel_names = list(set(g.etypes))
        self.rel_names.sort()
        if num_bases < 0 or num_bases > len(self.rel_names):
            self.num_bases = len(self.rel_names)
        else:
            self.num_bases = num_bases
        self.num_hidden_layers = num_hidden_layers
        self.dropout = dropout
        self.use_self_loop = use_self_loop

        self.layers = nn.ModuleList()
        # i2h
        self.layers.append(RGANLayer(
            self.h_dim, self.h_dim, self.rel_names,
            self.num_bases, activation=self.activation, self_loop=self.use_self_loop,
            dropout=self.dropout, weight=False))
        # h2h
        for i in range(self.num_hidden_layers):
            self.layers.append(RGANLayer(
                self.h_dim, self.h_dim, self.rel_names,
                self.num_bases, activation=self.activation, self_loop=self.use_self_loop,
                dropout=self.dropout, weight=False))  # changed weight for GATConv
            # TODO
            # think of possibility switching to GAT
            # weight=False
        # h2o
        self.layers.append(RGANLayer(
            self.h_dim, self.out_dim, self.rel_names,
            self.num_bases, activation=None,
            self_loop=self.use_self_loop, weight=False))  # changed weight for GATConv
        # TODO
        # think of possibility switching to GAT
        # weight=False

        self.emb_size = num_classes
        self.num_layers = len(self.layers)

    def node_embed(self):
        return None

    def forward(self, h=None, blocks=None,
                return_all=False):  # added this as an experimental feature for intermediate supervision

        all_layers = []  # added this as an experimental feature for intermediate supervision

        if blocks is None:
            raise NotImplemented()
            # full graph training
            for layer in self.layers:
                h = layer(self.g, h)
                all_layers.append(h)  # added this as an experimental feature for intermediate supervision
        else:
            # minibatch training
            for layer, block in zip(self.layers, blocks):
                h = layer(block, h)
                all_layers.append(h)  # added this as an experimental feature for intermediate supervision

        if return_all:  # added this as an experimental feature for intermediate supervision
            return all_layers
        else:
            return h

    def inference(self, g, batch_size, device, num_workers, x=None):
        """Minibatch inference of final representation over all node types.

        ***NOTE***
        For node classification, the model is trained to predict on only one node type's
        label.  Therefore, only that type's final representation is meaningful.
        """
        raise NotImplemented()

        with th.set_grad_enabled(False):

            if x is None:
                x = self.embed_layer()

            for l, layer in enumerate(self.layers):
                y = {
                    k: th.zeros(
                        g.number_of_nodes(k),
                        self.h_dim if l != len(self.layers) - 1 else self.out_dim)
                    for k in g.ntypes}

                sampler = dgl.dataloading.MultiLayerFullNeighborSampler(1)
                dataloader = dgl.dataloading.NodeDataLoader(
                    g,
                    {k: th.arange(g.number_of_nodes(k)) for k in g.ntypes},
                    sampler,
                    batch_size=batch_size,
                    shuffle=True,
                    drop_last=False,
                    num_workers=num_workers)

                for input_nodes, output_nodes, blocks in dataloader:  # tqdm.tqdm(dataloader):
                    block = blocks[0].to(device)

                    if not isinstance(input_nodes, dict):
                        key = next(iter(g.ntypes))
                        input_nodes = {key: input_nodes}
                        output_nodes = {key: output_nodes}

                    h = {k: x[k][input_nodes[k]].to(device) for k in input_nodes.keys()}
                    h = layer(block, h)

                    for k in h.keys():
                        y[k][output_nodes[k]] = h[k].cpu()

                x = y
            return y


class OneStepGRU(nn.Module):
    def __init__(self, dim):
        super(OneStepGRU, self).__init__()
        self.gru_rx = nn.Linear(dim, dim)
        self.gru_rh = nn.Linear(dim, dim)
        self.gru_zx = nn.Linear(dim, dim)
        self.gru_zh = nn.Linear(dim, dim)
        self.gru_nx = nn.Linear(dim, dim)
        self.gru_nh = nn.Linear(dim, dim)
        self.act_r = nn.Sigmoid()
        self.act_z = nn.Sigmoid()
        self.act_n = nn.Tanh()

    def forward(self, x, h):
        r = self.act_r(self.gru_rx(x) + self.gru_rh(h))
        z = self.act_z(self.gru_zx(x) + self.gru_zh(h))
        n = self.act_n(self.gru_nx(x) + self.gru_nh(r * h))
        return (1 - z) * n + z * h



class RGGANLayer(RelGraphConvLayer):
    def __init__(self,
                 in_feat,
                 out_feat,
                 rel_names,
                 num_bases,
                 *,
                 weight=True,
                 bias=True,
                 activation=None,
                 self_loop=False,
                 dropout=0.0):
        super(RGGANLayer, self).__init__(
            in_feat, out_feat, rel_names, num_bases, weight=weight, bias=bias, activation=activation,
            self_loop=self_loop, dropout=dropout
        )

        self.gru = OneStepGRU(out_feat)

    def forward(self, g, inputs):
        """Forward computation

        Parameters
        ----------
        g : DGLHeteroGraph
            Input graph.
        inputs : dict[str, torch.Tensor]
            Node feature for each node type.

        Returns
        -------
        dict[str, torch.Tensor]
            New node features for each node type.
        """
        g = g.local_var()
        if self.use_weight:
            weight = self.basis() if self.use_basis else self.weight
            wdict = {self.rel_names[i] : {'weight' : w.squeeze(0)}
                     for i, w in enumerate(th.split(weight, 1, dim=0))}
        else:
            wdict = {}

        if g.is_block:
            inputs_src = inputs
            # the begginning of src and dst indexes match, that is why we can simply slice the first
            # nodes to get dst embeddings
            inputs_dst = {k: v[:g.number_of_dst_nodes(k)] for k, v in inputs.items()}
        else:
            inputs_src = inputs_dst = inputs

        hs = self.conv(g, inputs_src, mod_kwargs=wdict)

        def _apply(ntype, h):
            if self.self_loop:
                h = h + th.matmul(inputs_dst[ntype], self.loop_weight)
            if self.bias:
                h = h + self.h_bias
            if self.activation:
                h = self.activation(h)
            return self.dropout(h)
        # TODO
        # think of possibility switching to GAT
        # return {ntype: _apply(ntype, h) for ntype, h in hs.items()}
        h_gru_input = {ntype : _apply(ntype, h) for ntype, h in hs.items()}

        return {dsttype: self.gru(h_dst, inputs_dst[dsttype].unsqueeze(1)).squeeze(dim=1) for dsttype, h_dst in h_gru_input.items()}

class RGGAN(RGAN):
    """A gated recurrent unit (GRU) cell

    .. math::

        \begin{array}{ll}
        r = \sigma(W_{ir} x + b_{ir} + W_{hr} h + b_{hr}) \\
        z = \sigma(W_{iz} x + b_{iz} + W_{hz} h + b_{hz}) \\
        n = \tanh(W_{in} x + b_{in} + r * (W_{hn} h + b_{hn})) \\
        h' = (1 - z) * n + z * h
        \end{array}

    where :math:`\sigma` is the sigmoid function, and :math:`*` is the Hadamard product."""
    def __init__(self,
                 g,
                 h_dim, num_classes,
                 num_bases,
                 num_steps=1,
                 dropout=0,
                 use_self_loop=False,
                 activation=F.relu):
        super(RGCNSampling, self).__init__()
        self.g = g
        self.h_dim = h_dim
        self.out_dim = num_classes
        self.activation = activation

        self.rel_names = list(set(g.etypes))
        self.rel_names.sort()
        if num_bases < 0 or num_bases > len(self.rel_names):
            self.num_bases = len(self.rel_names)
        else:
            self.num_bases = num_bases

        self.dropout = dropout
        self.use_self_loop = use_self_loop

        # i2h
        self.layer = RGGANLayer(
            self.h_dim, self.h_dim, self.rel_names,
            self.num_bases, activation=self.activation, self_loop=self.use_self_loop,
            dropout=self.dropout, weight=False
        )
        # TODO
        # think of possibility switching to GAT
        # weight=False

        self.emb_size = num_classes
        self.num_layers = num_steps

    def forward(self, h=None, blocks=None,
                return_all=False):  # added this as an experimental feature for intermediate supervision

        all_layers = []  # added this as an experimental feature for intermediate supervision

        if blocks is None:
            raise NotImplemented()
            # full graph training
            for l in range(self.steps):
                h = self.layer(self.g, h)
                all_layers.append(h)  # added this as an experimental feature for intermediate supervision
        else:
            # minibatch training
            for l, block in enumerate(blocks):
                h = self.layer(block, h)
                all_layers.append(h)  # added this as an experimental feature for intermediate supervision

        if return_all:  # added this as an experimental feature for intermediate supervision
            return all_layers
        else:
            return h

    def inference(self, g, batch_size, device, num_workers, x=None):
        raise NotImplemented()