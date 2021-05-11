import torch
import numpy as np
import torch.nn.functional as F
from utils.utils_gcn import get_param, ccorr, rotate, softmax
from torch_scatter import scatter_add, scatter_mean, gather_csr, scatter, segment_csr

from torch_geometric.nn import MessagePassing


class CompGCNConv(MessagePassing):
    """
    Standard Conv Layer for Compgcn
    """
    def __init__(self, in_channels, out_channels, num_rels, act=lambda x:x, params=None):
        # target_to_source -> Edges that flow from x_i to x_j 
        super(self.__class__, self).__init__(flow='target_to_source', aggr='add')

        self.p            = params
        self.in_channels  = in_channels
        self.out_channels = out_channels
        self.num_rels     = num_rels
        self.act          = act
        self.opn          = params['MODEL']['OPN']
        self.device       = None

        # Weight of both comp functions in 'both' aggregate
        self.alpha = params['ALPHA']

        # Three weight matrices for CompGCN 
        # In = Standard / Out = Inverse
        self.w_loop = get_param((in_channels, out_channels))
        self.w_in   = get_param((in_channels, out_channels))
        self.w_out  = get_param((in_channels, out_channels))

        # Weight matrix for relation update
        self.w_rel  = get_param((in_channels, out_channels))

        # TODO: Move out of here?
        # Rel embedding for loop triplets
        self.loop_rel = get_param((1, in_channels))

        self.drop = torch.nn.Dropout(self.p['MODEL']['GCN_DROP'])
        self.bn   = torch.nn.BatchNorm1d(out_channels)



    def __repr__(self):
        return '{}({}, {}, num_rels={})'.format(self.__class__.__name__, self.in_channels, self.out_channels, self.num_rels)


    def forward(self, prop_type, x, edge_index, edge_type, rel_embed, quals=None, aux_ents=None, aux_rels=None): 
        """
        Forward by prop type for each type of aggregation
        """
        if self.device is None:
            self.device = edge_index.device

        num_ent   = x.size(0)
        num_quals = quals.size(1) // 2
        num_edges = edge_index.size(1) // 2

        # Add loop relation in!        
        rel_emb_all = torch.cat([rel_embed, self.loop_rel], dim=0)

        # 2nd half of triplets are inverse so split
        # in_{} = [col0, col1, ... col<num_edges - 1>]
        # out_{} = [col<num_edges>...]
        in_index, out_index = edge_index[:, :num_edges], edge_index[:, num_edges:]
        in_type,  out_type  = edge_type[:num_edges], edge_type [num_edges:]

        # Same as above but for qualifiers
        in_index_qual_ent, out_index_qual_ent = quals[1, :num_quals], quals[1, num_quals:]
        in_index_qual_rel, out_index_qual_rel = quals[0, :num_quals], quals[0, num_quals:]

        # Refers to parent triplet qualifier belongs to
        quals_index_in, quals_index_out = quals[2, :num_quals], quals[2, num_quals:]

        # Same for quals since same entity regardless....
        loop_index  = torch.stack([torch.arange(num_ent), torch.arange(num_ent)]).to(self.device)   # Self edges between all the nodes
        loop_type = torch.full((num_ent,), rel_emb_all.size(0)-1, dtype=torch.long).to(self.device)   # Last dim is for self-loop


        # Hack to ensure correct triplets for qual prop_type for inverse
        # Without this we will be aggregating (o, r^-1, s) instead of (qv, qr^-1, s) 
        if prop_type == "qual":
            out_index, out_type = self.qual_inverse_edges(out_index, quals)
        

        # Hack for fixing triplets for prop_type 'both'
        if prop_type == "both":
            # In -> (qv, s)
            in_index = torch.zeros(2, num_quals, dtype=torch.int64).to(self.device)
            in_index[0] = in_index_qual_ent
            in_index[1] = edge_index[0][quals_index_in]

            # Out -> (s, qv)
            out_index = torch.zeros(2, num_quals, dtype=torch.int64).to(self.device)
            out_index[0] = edge_index[0][quals_index_in]   # `in` is correct! for this and next line
            out_index[1] = in_index_qual_ent

            # The main edge_type here is qr
            in_type  = in_index_qual_rel
            out_type = out_index_qual_rel + self.num_rels

            # Account for triplet object. Comes from original non-inverse triplets
            both_obj_in = edge_index[1][quals_index_in]
            both_obj_out = edge_index[1][quals_index_out]

            # Account for relation in triplet obj
            trip_rel_in = in_type
            trip_rel_out = in_type
        else:
            both_obj_in = both_obj_out = None
            trip_rel_in = trip_rel_out = None


        # Normalized Adj Matrices
        in_norm  = self.compute_norm(in_index,  num_ent)
        out_norm = self.compute_norm(out_index, num_ent)

        in_res = self.propagate(
                        in_index, 
                        x=x, 
                        edge_type=in_type,
                        rel_embed=rel_emb_all, 
                        edge_norm=in_norm,
                        mode='in',
                        ent_embed=x, 
                        qualifier_ent=in_index_qual_ent,
                        qualifier_rel=in_index_qual_rel,
                        qual_index=quals_index_in,
                        prop_type=prop_type,
                        both_obj_index=both_obj_in,
                        trip_rel=trip_rel_in,
                        aux_ent_embs=aux_ents, 
                        aux_rel_embs=aux_rels,
                        entity_index=in_index
                    )

        out_res = self.propagate(
                        out_index, 
                        x=x, 
                        edge_type=out_type,
                        rel_embed=rel_emb_all, 
                        edge_norm=out_norm, 
                        mode='out',
                        ent_embed=x, 
                        qualifier_ent=out_index_qual_ent,
                        qualifier_rel=out_index_qual_rel,
                        qual_index=quals_index_out,
                        prop_type=prop_type,
                        both_obj_index=both_obj_out,
                        trip_rel=trip_rel_out,
                        aux_ent_embs=aux_ents, 
                        aux_rel_embs=aux_rels,
                        entity_index=out_index
                    )

        loop_res = self.propagate(
                        loop_index, 
                        x=x, 
                        edge_type=loop_type, 
                        rel_embed=rel_emb_all, 
                        edge_norm=None, 
                        mode='loop', 
                        prop_type=prop_type
                    )

        out = self.drop(in_res)*(1/3) + self.drop(out_res)*(1/3) + loop_res*(1/3)
        out = self.bn(out)

        # Ignoring the self loop inserted at the end since defined in this layer
        return self.act(out), torch.matmul(rel_emb_all, self.w_rel)[:-1]




    def message(self, x_i, x_j, edge_type, rel_embed, edge_norm, mode, ent_embed=None, 
                qualifier_ent=None, qualifier_rel=None, qual_index=None, prop_type=None, 
                both_obj_index=None, trip_rel=None, aux_ent_embs=None, aux_rel_embs=None, 
                entity_index=None):
        """
        Define message for MessagePassing class

        x_i = head emb
        x_j = tail emb

        1. Get appropriate weight matrix
        2. Type of aggregation depends on mode and prop_type
        3. Aggregation * Weight matrix -> output
        4. Pass output through norm when defined
        """
        comp_weight_matrix = getattr(self, 'w_{}'.format(mode))

        # Get relations embs used in sample
        # For loop edge_type is fully pointing to the loop relation
        rel_sub_embs = torch.index_select(rel_embed, 0, edge_type)

        # 1. Loop...basically same as trip but no scatter (since only one edge per entity)
        # 2. Rel & tail to head (v <- u, r)
        # 3. Quals to head (v <- qv, qr)
        # 4. Main triplet and qual to qual (qv <- v, u, r, qr)
        if mode == "loop":
            comp_agg = self.comp_func(x_j, rel_sub_embs)
        elif prop_type == "trip":
            comp_agg = self.combine_trips(x_j, rel_sub_embs, ent_embed)
        elif prop_type == "qual":
            comp_agg = self.combine_quals(mode, x_j, ent_embed, rel_sub_embs, qualifier_ent, qualifier_rel, qual_index)
        elif prop_type == "both":

            # x_j is original subject!
            # In  -> qv = (s, r, o qr)
            # Out -> s  = (qv, r, o, qr^-1) 

            # Switch to aux embs if specified
            if aux_ent_embs is not None:
                trip_rel_embed = aux_rel_embs[trip_rel]
                trip_obj_embed = aux_ent_embs[both_obj_index]
                
                # When out x_j is not qv so don't need aux
                if mode == "in":
                    x_j = aux_ent_embs[entity_index[1]]

            else:
                trip_rel_embed = rel_embed[trip_rel]
                trip_obj_embed = ent_embed[both_obj_index]

            comp_agg = self.combine_quals_trips(x_j, rel_sub_embs, trip_obj_embed, trip_rel_embed)

        out = torch.mm(comp_agg, comp_weight_matrix)

        # Multiply by normalized adj matrix when applicable
        return out if edge_norm is None else out * edge_norm.view(-1, 1)



    def combine_trips(self, x_j, rel_sub_embs, ent_embed):
        """
        Combine the basic triplet info and sum for given head entity
        """
        return self.comp_func(x_j, rel_sub_embs)


    def combine_quals(self, mode, x_j, ent_embed, rel_embed, ent_index, rel_index, quals_index):
        """
        For a given input combine the qualifier info

        When out it's completely different since no scatter with head triplet entity
        """
        qualifier_emb_rel = rel_embed[rel_index]

        if mode == "out":
            coalesced_quals = self.comp_func(x_j, qualifier_emb_rel)
        else:
            qualifier_emb_ent = ent_embed[ent_index]
            qual_embeddings = self.comp_func(qualifier_emb_ent, qualifier_emb_rel)

            # Add up qual pairs that refer to same triplet 
            coalesced_quals = scatter_add(qual_embeddings, quals_index, dim=0, dim_size=rel_embed.shape[0])

        return coalesced_quals


    def combine_quals_trips(self, x_j, rel_embed, trip_obj_embed, trip_rel_embed):
        """
        For a given input combine the qualifier and triplet info
        
        In:
            x_j = s
            rel_embed = qr
            trip_rel_embed = r
            trip_obj_embed = o

        Out:
            x_j = qv
            rel_embed = qr^-1
            trip_rel_embed = r
            trip_obj_embed = o
        """        
        # phi_r(s, r)
        comp_trip_agg  = self.comp_func(x_j, trip_rel_embed)

        # phi_q(s, qr)
        qual_embeddings = self.comp_func(x_j, rel_embed)

        return self.alpha * comp_trip_agg + (1 - self.alpha) * qual_embeddings


    def comp_func(self, ent_embed, rel_embed):
        """
        phi_r
        """
        if self.opn == 'corr':  
            trans_embed  = ccorr(ent_embed, rel_embed)
        elif self.opn == 'sub':   
            trans_embed  = ent_embed - rel_embed
        elif self.opn == 'mult':  
            trans_embed  = ent_embed * rel_embed
        elif self.opn == 'rotate':
            trans_embed = rotate(ent_embed, rel_embed)
        else: 
            raise NotImplementedError

        return trans_embed



    def update(self, aggr_out):
        return aggr_out


    def compute_norm(self, edge_index, num_ent):
        """
        Computes the normalized adj matrix
        """
        # Row = Head, Col = Tail for given set of triplets
        row, col = edge_index
        edge_weight = torch.ones_like(row).float()
        
        # Num edges for each head entity
        # 0 for entities not in sample
        deg = scatter_add(edge_weight, row, dim=0, dim_size=num_ent)   

        deg_inv = deg.pow(-0.5)                          # D^{-0.5}
        deg_inv[deg_inv == float('inf')] = 0
        norm = deg_inv[row] * edge_weight * deg_inv[col] # D^{-0.5}

        return norm



    def qual_inverse_edges(self, inv_edge_index, quals):
        """
        Create inverse edges for prop_type 'qual'
        """        
        num_quals = quals.size(1) // 2
        new_edge_index = torch.zeros(2, num_quals, dtype=torch.int64).to(self.device)

        # Head and relation for new inv triplets
        # Do + num_rels to get inverse of relations        
        quals_ent_out_index = quals[1, num_quals:]
        quals_rel_out_index = quals[0, num_quals:] + self.num_rels
        
        # Tail of inverse parent triplet...so our final tail indices
        quals_parent_out_index = quals[2, num_quals:]
        inv_tails = inv_edge_index[1][quals_parent_out_index]

        new_edge_index[0] = quals_ent_out_index
        new_edge_index[1] = inv_tails

        return new_edge_index, quals_rel_out_index




