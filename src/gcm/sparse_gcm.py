import torch
import torch_geometric
from gcm import util
from typing import Union, Tuple, List

from torchtyping import TensorType, patch_typeguard
from typeguard import typechecked
patch_typeguard()


class SparseGCM(torch.nn.Module):
    """Graph Associative Memory using sparse-graph representations"""

    did_warn = False

    def __init__(
        self,
        # Graph neural network, see torch_geometric.nn.Sequential
        # for some examples
        gnn: torch.nn.Module,
        # Preprocessor for each feat vec before it's placed in graph
        preprocessor: torch.nn.Module = None,
        # an edge selector from gcm.edge_selectors
        # you can chain multiple selectors together using
        # torch_geometric.nn.Sequential
        edge_selectors: torch.nn.Module = None,
        # Auxiliary edge selectors are called
        # after the positional encoding and reprojection
        # this should only be used for non-human (learned) priors
        aux_edge_selectors: torch.nn.Module = None,
        # Maximum number of nodes in the graph
        graph_size: int = 128,
        # Whether to add sin/cos positional encoding like in transformer
        # to the nodes
        # Creates an ordering in the graph
        positional_encoder: torch.nn.Module = None,
    ):
        super().__init__()

        self.preprocessor = preprocessor
        self.gnn = gnn
        self.graph_size = graph_size
        self.edge_selectors = edge_selectors
        self.aux_edge_selectors = aux_edge_selectors
        self.positional_encoder = positional_encoder

    def get_initial_hidden_state(self, x):
        """Given a dummy x of shape [B, feats], construct
        the hidden state for the base case (adj matrix, weights, etc)"""
        """Returns the initial hidden state h (e.g. h, output = gcm(input, h)),
        for a given batch size (B). Feats denotes the feature size (# dims of each
        node in the graph)."""

        assert x.dim() == 3
        B, _, feats = x.shape
        edges = torch.zeros(B, 2, 0, device=x.device, dtype=torch.long)
        nodes = torch.zeros(B, self.graph_size, feats, device=x.device)
        weights = torch.zeros(B, 1, 0, device=x.device)
        T = torch.zeros(B, dtype=torch.long, device=x.device)

        return nodes, edges, weights, T

    @typechecked
    def forward(
        self, 
        x: TensorType["B","t","feat", float],         # input observations
        taus: TensorType["B", int],                   # sequence_lengths
        hidden: Union[
            None, 
            Tuple[
                TensorType["B", "N", "feats", float], # Nodes
                TensorType[2, "E", int],              # Edges
                TensorType["E", float],               # Weights
                TensorType["B", int],                 # T
            ]
        ]
    ) -> Tuple[
        torch.Tensor, 
            Tuple[
                TensorType["B", "N", "feats", float],  # Nodes
                TensorType[2, "NE", int],              # Edges
                TensorType["NE", float],               # Weights
                TensorType["B", int]                   # T
            ]
        ]:
        """Add a memory x with temporal size tau to the graph, and query the memory for it.
        B = batch size
        N = maximum graph size
        T = number of timesteps in graph before input
        taus = number of timesteps in each input batch
        t = the zero-padded time dimension (i.e. max(taus))
        E = number of edge pairs
        """
        # Base case
        if hidden == None:
            hidden = self.get_initial_hidden_state(x)

        nodes, edges, weights, T = hidden

        N = nodes.shape[1]
        B = x.shape[0]

        self.node_cleanup(nodes)

        # Batch and time idxs for nodes we intend to add
        B_idxs, tau_idxs = util.get_new_node_idxs(T, taus, B)
        dense_B_idxs, dense_tau_idxs = util.get_nonpadded_idxs(T, taus, B)

        nodes = nodes.clone()
        # Add new nodes to the current graph
        assert torch.all(edges[0] < edges[1]), 'Edges violate causality'
        # TODO: Wrap around instead of terminating
        if tau_idxs.max() >= N:
            raise Exception('Overflow')

        nodes[B_idxs, tau_idxs] = x[dense_B_idxs, dense_tau_idxs]

        # We do not want to modify graph nodes in the GCM
        # Do all mutation operations on dirty_nodes, 
        # then use clean nodes in the graph state
        dirty_nodes = nodes.clone()

        if self.edge_selectors:
            edges, weights = self.edge_selectors(
                dirty_nodes, edges, weights, T, taus, B
            )

        # Thru network
        if self.preprocessor:
            dirty_nodes = self.preprocessor(dirty_nodes)
        if self.positional_encoder:
            dirty_nodes = self.positional_encoder(dirty_nodes)
        if self.aux_edge_selectors:
            edges, weights, edge_B_idxs = self.edge_selectors(
                dirty_nodes, edges, weights, T, taus, B
            )

        # Convert to GNN input format
        # TODO coalesce before GNN
        flat_nodes, output_node_idxs = util.flatten_nodes(dirty_nodes, T, taus, B)
        node_feats = self.gnn(flat_nodes, edges, weights)
        # Extract the hidden repr at the new nodes
        # Each mx is variable in temporal dim, so return 2D tensor of [B*tau, feat]
        mx = node_feats[output_node_idxs]

        assert torch.all(
            torch.isfinite(mx)
        ), "Got NaN in returned memory, try using tanh activation"

        # Input obs were dense and padded, so output should be dense and padded
        dense_B_idxs, dense_tau_idxs = util.get_nonpadded_idxs(T, taus, B)
        mx_dense = torch.zeros_like(x)
        mx_dense[dense_B_idxs, dense_tau_idxs] = mx

        '''
        # Fix for ray, edges must be constant size
        if self.pad_edges:
            self.edge_cleanup(edges)
            unpadded_edges = edges
            # Pad edges with -1 (-1 is treated as invalid in GCM)
            edge_padding = torch.ones(edges.shape[0], edges.shape[1], self.max_edges) * -1


            edges[:,:, :edges.shape[-1]] = edges
        '''
        T = T + taus
        return mx_dense, (nodes, edges, weights, T)

    # TODO: implement
    def node_cleanup(self, nodes):
        """Call this when the node matrix if full, so we can erase old
        nodes to add new nodes"""
        return
        overflow_mask = T + taus > graph_size
        overflowing_batches = overflow_mask.nonzero().squeeze()
        nodes[overflowing_batches, 0] = 0

    # TODO: implement
    def edge_cleanup(self, edges):
        """Call this when the edge matrix is full, so we can erase old
        edges to add new edges"""
        # Remove edges over the limit
        return
        overflow_mask = edges.shape[-1]
        if torch.any(unpadded_edges.shape[-1] > self.max_edges):
            overflow = unpadded_edges.shape[-1] - self.max_edges
            print('Warning: edge overflow, truncating {overflow} edge pairs from front')
            unpadded_edges = unpadded_edges[:,:,overflow:]
        

    def wrap_overflow(self, nodes, adj, weights, num_nodes):
        """Call this when the node/adj matrices are full. Deletes the zeroth element
        of the matrices and shifts all the elements up by one, producing a free row
        at the end. You will likely want to call .clone() on the arguments that require
        gradient computation.

        Returns new nodes, adj, weights, and num_nodes matrices"""
        N = nodes.shape[1]
        overflow_mask = num_nodes + 1 > N
        # Shift node matrix into the past
        # by one and forget the zeroth node
        overflowing_batches = overflow_mask.nonzero().squeeze()
        #nodes = nodes.clone()
        #adj = adj.clone()
        # Zero entries before shifting
        nodes[overflowing_batches, 0] = 0
        adj[overflowing_batches, 0, :] = 0
        adj[overflowing_batches, :, 0] = 0
        # Roll newly zeroed zeroth entry to final entry
        nodes[overflowing_batches] = torch.roll(nodes[overflowing_batches], -1, -2)
        adj[overflowing_batches] = torch.roll(
            adj[overflowing_batches], (-1, -1), (-1, -2)
        )
        if weights.numel() != 0:
            #weights = weights.clone()
            weights[overflowing_batches, 0, :] = 0
            weights[overflowing_batches, :, 0] = 0
            weights[overflowing_batches] = torch.roll(
                weights[overflowing_batches], (-1, -1), (-1, -2)
            )

        num_nodes[overflow_mask] = num_nodes[overflow_mask] - 1
        return nodes, adj, weights, num_nodes
