# pylint: disable=invalid-name, missing-function-docstring
""" BNT model module from https://github.com/Wayfear/BrainNetworkTransformer"""

from typing import Tuple, Optional
import bisect
import math

import numpy as np

from torch.nn.functional import softmax
from torch.nn import Parameter, TransformerEncoderLayer, functional as F
from torch import nn, optim, Tensor
import torch
from omegaconf import OmegaConf, DictConfig, open_dict


def get_model(cfg: DictConfig, model_cfg: DictConfig):
    return BrainNetworkTransformer(model_cfg)


def default_HPs(cfg: DictConfig):
    model_cfg = {
        "sizes": [360, 100],  # Note: The input node size should not be included here
        "pooling": [False, True],
        "pos_encoding": "none",  # 'identity', 'none'
        "orthogonal": True,
        "freeze_center": True,
        "project_assignment": True,
        "pos_embed_dim": 360,
        "node_sz": cfg.dataset.data_info.main.data_shape[1],
        "node_feature_sz": cfg.dataset.data_info.main.data_shape[2],
        "output_size": cfg.dataset.data_info.main.n_classes,
        "optimizer": {"lr": 1e-4, "weight_decay": 1e-4},
        "scheduler": {
            "mode": "cos",  # ['step', 'poly', 'cos']
            "base_lr": 1e-4,
            "target_lr": 1e-5,
            "decay_factor": 0.1,  # for step mode
            "milestones": [0.3, 0.6, 0.9],
            "poly_power": 2.0,  # for poly mode
            "lr_decay": 0.98,
            "warm_up_from": 0.0,
            "warm_up_steps": 0,
        },
    }
    return OmegaConf.create(model_cfg)


def data_postproc(cfg: DictConfig, model_cfg: DictConfig, original_data):
    # 4 is the number of heads in the BNT TransPoolingEncoder
    if cfg.dataset.data_info.main.data_shape[2] % 4 != 0:
        addendum_size = 4 - cfg.dataset.data_info.main.data_shape[2] % 4
        print(f"Adding {addendum_size} column(s) of zeros to the FNC matrices")

        with open_dict(model_cfg):
            model_cfg.node_feature_sz = model_cfg.node_feature_sz + addendum_size

        for key in original_data:
            fnc = original_data[key]["FNC"]
            addendum = np.zeros((fnc.shape[0], fnc.shape[1], addendum_size))
            expanded_fnc = np.concatenate((fnc, addendum), axis=2)
            original_data[key]["FNC"] = expanded_fnc

            with open_dict(cfg):
                cfg.dataset.data_info[key].data_shape = expanded_fnc.shape

        print("New cfg.dataset.data_info:")
        print(OmegaConf.to_yaml(cfg.dataset.data_info))
        print("New model config:")
        print(OmegaConf.to_yaml(model_cfg))

    return original_data


def get_criterion(cfg: DictConfig, model_cfg: DictConfig):
    return RedSumCEloss()


class RedSumCEloss:
    """Basic Cross-entropy loss"""

    def __init__(self):
        self.ce_loss = nn.CrossEntropyLoss(reduction="sum")

    def __call__(self, logits, target, model, device):
        ce_loss = self.ce_loss(logits, target)

        return ce_loss


def get_optimizer(cfg: DictConfig, model_cfg: DictConfig, model):
    optimizer = optim.Adam(
        model.parameters(),
        lr=model_cfg.optimizer.lr,
        weight_decay=model_cfg.optimizer.weight_decay,
    )

    return optimizer


def get_scheduler(cfg: DictConfig, model_cfg: DictConfig, optimizer):
    return LRScheduler(cfg, model_cfg, optimizer)


class LRScheduler:
    def __init__(self, cfg: DictConfig, model_cfg: DictConfig, optimizer):
        self.optimizer = optimizer

        self.current_step = 0

        self.scheduler_cfg = model_cfg.scheduler

        self.lr_mode = model_cfg.scheduler.mode
        self.base_lr = model_cfg.scheduler.base_lr
        self.target_lr = model_cfg.scheduler.target_lr

        self.warm_up_from = model_cfg.scheduler.warm_up_from
        self.warm_up_steps = model_cfg.scheduler.warm_up_steps
        self.total_steps = cfg.mode.max_epochs

        self.lr = None

        assert self.lr_mode in ["step", "poly", "cos", "linear", "decay"]

    def step(self, metric):
        assert 0 <= self.current_step <= self.total_steps
        if self.current_step < self.warm_up_steps:
            current_ratio = self.current_step / self.warm_up_steps
            self.lr = (
                self.warm_up_from + (self.base_lr - self.warm_up_from) * current_ratio
            )
        else:
            current_ratio = (self.current_step - self.warm_up_steps) / (
                self.total_steps - self.warm_up_steps
            )
            if self.lr_mode == "step":
                count = bisect.bisect_left(self.scheduler_cfg.milestones, current_ratio)
                self.lr = self.base_lr * pow(self.scheduler_cfg.decay_factor, count)
            elif self.lr_mode == "poly":
                poly = pow(1 - current_ratio, self.scheduler_cfg.poly_power)
                self.lr = self.target_lr + (self.base_lr - self.target_lr) * poly
            elif self.lr_mode == "cos":
                cosine = math.cos(math.pi * current_ratio)
                self.lr = (
                    self.target_lr + (self.base_lr - self.target_lr) * (1 + cosine) / 2
                )
            elif self.lr_mode == "linear":
                self.lr = self.target_lr + (self.base_lr - self.target_lr) * (
                    1 - current_ratio
                )
            elif self.lr_mode == "decay":
                epoch = self.current_step // self.training_config.steps_per_epoch
                self.lr = self.base_lr * self.scheduler_cfg.lr_decay**epoch

        for param_group in self.optimizer.param_groups:
            param_group["lr"] = self.lr
        self.current_step += 1


class BrainNetworkTransformer(nn.Module):
    def __init__(self, model_cfg: DictConfig):
        super().__init__()

        self.attention_list = nn.ModuleList()
        forward_dim = model_cfg.node_feature_sz

        self.pos_encoding = model_cfg.pos_encoding
        if self.pos_encoding == "identity":
            self.node_identity = nn.Parameter(
                torch.zeros(model_cfg.node_sz, model_cfg.pos_embed_dim),
                requires_grad=True,
            )
            forward_dim = model_cfg.node_sz + model_cfg.pos_embed_dim
            nn.init.kaiming_normal_(self.node_identity)

        sizes = model_cfg.sizes
        sizes[0] = model_cfg.node_sz
        in_sizes = [model_cfg.node_sz] + sizes[:-1]
        do_pooling = model_cfg.pooling
        self.do_pooling = do_pooling
        for index, size in enumerate(sizes):
            self.attention_list.append(
                TransPoolingEncoder(
                    input_feature_size=forward_dim,
                    input_node_num=in_sizes[index],
                    hidden_size=1024,
                    output_node_num=size,
                    pooling=do_pooling[index],
                    orthogonal=model_cfg.orthogonal,
                    freeze_center=model_cfg.freeze_center,
                    project_assignment=model_cfg.project_assignment,
                )
            )

        self.dim_reduction = nn.Sequential(nn.Linear(forward_dim, 8), nn.LeakyReLU())

        self.fc = nn.Sequential(
            nn.Linear(8 * sizes[-1], 256),
            nn.LeakyReLU(),
            nn.Linear(256, 32),
            nn.LeakyReLU(),
            nn.Linear(32, model_cfg.output_size),
        )

    def forward(self, node_feature: torch.tensor):
        (
            bz,
            _,
            _,
        ) = node_feature.shape

        if self.pos_encoding == "identity":
            pos_emb = self.node_identity.expand(bz, *self.node_identity.shape)
            node_feature = torch.cat([node_feature, pos_emb], dim=-1)

        assignments = []

        for i, atten in enumerate(self.attention_list):
            node_feature, assignment = atten(node_feature)
            assignments.append(assignment)

        node_feature = self.dim_reduction(node_feature)

        node_feature = node_feature.reshape((bz, -1))

        return self.fc(node_feature)

    def get_attention_weights(self):
        return [atten.get_attention_weights() for atten in self.attention_list]

    def get_cluster_centers(self) -> torch.Tensor:
        """
        Get the cluster centers, as computed by the encoder.

        :return: [number of clusters, hidden dimension] Tensor of dtype float
        """
        return self.dec.get_cluster_centers()

    def loss(self, assignments):
        """
        Compute KL loss for the given assignments. Note that not all encoders contain a pooling layer.
        Inputs: assignments: [batch size, number of clusters]
        Output: KL loss
        """
        decs = list(filter(lambda x: x.is_pooling_enabled(), self.attention_list))
        assignments = list(filter(lambda x: x is not None, assignments))
        loss_all = None

        for index, assignment in enumerate(assignments):
            if loss_all is None:
                loss_all = decs[index].loss(assignment)
            else:
                loss_all += decs[index].loss(assignment)
        return loss_all


class TransPoolingEncoder(nn.Module):
    """
    Transformer encoder with Pooling mechanism.
    Input size: (batch_size, input_node_num, input_feature_size)
    Output size: (batch_size, output_node_num, input_feature_size)
    """

    def __init__(
        self,
        input_feature_size,
        input_node_num,
        hidden_size,
        output_node_num,
        pooling=True,
        orthogonal=True,
        freeze_center=False,
        project_assignment=True,
    ):
        super().__init__()
        self.transformer = InterpretableTransformerEncoder(
            d_model=input_feature_size,
            nhead=4,
            dim_feedforward=hidden_size,
            batch_first=True,
        )

        self.pooling = pooling
        if pooling:
            encoder_hidden_size = 32
            self.encoder = nn.Sequential(
                nn.Linear(input_feature_size * input_node_num, encoder_hidden_size),
                nn.LeakyReLU(),
                nn.Linear(encoder_hidden_size, encoder_hidden_size),
                nn.LeakyReLU(),
                nn.Linear(encoder_hidden_size, input_feature_size * input_node_num),
            )
            self.dec = DEC(
                cluster_number=output_node_num,
                hidden_dimension=input_feature_size,
                encoder=self.encoder,
                orthogonal=orthogonal,
                freeze_center=freeze_center,
                project_assignment=project_assignment,
            )

    def is_pooling_enabled(self):
        return self.pooling

    def forward(self, x):
        x = self.transformer(x)
        if self.pooling:
            x, assignment = self.dec(x)
            return x, assignment
        return x, None

    def get_attention_weights(self):
        return self.transformer.get_attention_weights()

    def loss(self, assignment):
        return self.dec.loss(assignment)


class DEC(nn.Module):
    def __init__(
        self,
        cluster_number: int,
        hidden_dimension: int,
        encoder: torch.nn.Module,
        alpha: float = 1.0,
        orthogonal=True,
        freeze_center=True,
        project_assignment=True,
    ):
        """
        Module which holds all the moving parts of the DEC algorithm, as described in
        Xie/Girshick/Farhadi; this includes the AutoEncoder stage and the ClusterAssignment stage.

        :param cluster_number: number of clusters
        :param hidden_dimension: hidden dimension, output of the encoder
        :param encoder: encoder to use
        :param alpha: parameter representing the degrees of freedom in the t-distribution, default 1.0
        """
        super(DEC, self).__init__()
        self.encoder = encoder
        self.hidden_dimension = hidden_dimension
        self.cluster_number = cluster_number
        self.alpha = alpha
        self.assignment = ClusterAssignment(
            cluster_number,
            self.hidden_dimension,
            alpha,
            orthogonal=orthogonal,
            freeze_center=freeze_center,
            project_assignment=project_assignment,
        )

        self.loss_fn = nn.KLDivLoss(size_average=False)

    def forward(self, batch: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Compute the cluster assignment using the ClusterAssignment after running the batch
        through the encoder part of the associated AutoEncoder module.

        :param batch: [batch size, embedding dimension] FloatTensor
        :return: [batch size, number of clusters] FloatTensor
        """
        node_num = batch.size(1)
        batch_size = batch.size(0)

        # [batch size, embedding dimension]
        flattened_batch = batch.view(batch_size, -1)
        encoded = self.encoder(flattened_batch)
        # [batch size * node_num, hidden dimension]
        encoded = encoded.view(batch_size * node_num, -1)
        # [batch size * node_num, cluster_number]
        assignment = self.assignment(encoded)
        # [batch size, node_num, cluster_number]
        assignment = assignment.view(batch_size, node_num, -1)
        # [batch size, node_num, hidden dimension]
        encoded = encoded.view(batch_size, node_num, -1)
        # Multiply the encoded vectors by the cluster assignment to get the final node representations
        # [batch size, cluster_number, hidden dimension]
        node_repr = torch.bmm(assignment.transpose(1, 2), encoded)
        return node_repr, assignment

    def target_distribution(self, batch: torch.Tensor) -> torch.Tensor:
        """
        Compute the target distribution p_ij, given the batch (q_ij), as in 3.1.3 Equation 3 of
        Xie/Girshick/Farhadi; this is used the KL-divergence loss function.

        :param batch: [batch size, number of clusters] Tensor of dtype float
        :return: [batch size, number of clusters] Tensor of dtype float
        """
        weight = (batch**2) / torch.sum(batch, 0)
        return (weight.t() / torch.sum(weight, 1)).t()

    def loss(self, assignment):
        flattened_assignment = assignment.view(-1, assignment.size(-1))
        target = self.target_distribution(flattened_assignment).detach()
        return self.loss_fn(
            flattened_assignment.log(), target
        ) / flattened_assignment.size(0)

    def get_cluster_centers(self) -> torch.Tensor:
        """
        Get the cluster centers, as computed by the encoder.

        :return: [number of clusters, hidden dimension] Tensor of dtype float
        """
        return self.assignment.get_cluster_centers()


class ClusterAssignment(nn.Module):
    def __init__(
        self,
        cluster_number: int,
        embedding_dimension: int,
        alpha: float = 1.0,
        cluster_centers: Optional[torch.Tensor] = None,
        orthogonal=True,
        freeze_center=True,
        project_assignment=True,
    ) -> None:
        """
        Module to handle the soft assignment, for a description see in 3.1.1. in Xie/Girshick/Farhadi,
        where the Student's t-distribution is used measure similarity between feature vector and each
        cluster centroid.

        :param cluster_number: number of clusters
        :param embedding_dimension: embedding dimension of feature vectors
        :param alpha: parameter representing the degrees of freedom in the t-distribution, default 1.0
        :param cluster_centers: clusters centers to initialise, if None then use Xavier uniform
        """
        super(ClusterAssignment, self).__init__()
        self.embedding_dimension = embedding_dimension
        self.cluster_number = cluster_number
        self.alpha = alpha
        self.project_assignment = project_assignment
        if cluster_centers is None:
            initial_cluster_centers = torch.zeros(
                self.cluster_number, self.embedding_dimension, dtype=torch.float
            )
            nn.init.xavier_uniform_(initial_cluster_centers)

        else:
            initial_cluster_centers = cluster_centers

        if orthogonal:
            orthogonal_cluster_centers = torch.zeros(
                self.cluster_number, self.embedding_dimension, dtype=torch.float
            )
            orthogonal_cluster_centers[0] = initial_cluster_centers[0]
            for i in range(1, cluster_number):
                project = 0
                for j in range(i):
                    project += self.project(
                        initial_cluster_centers[j], initial_cluster_centers[i]
                    )
                initial_cluster_centers[i] -= project
                orthogonal_cluster_centers[i] = initial_cluster_centers[i] / torch.norm(
                    initial_cluster_centers[i], p=2
                )

            initial_cluster_centers = orthogonal_cluster_centers

        self.cluster_centers = Parameter(
            initial_cluster_centers, requires_grad=(not freeze_center)
        )

    @staticmethod
    def project(u, v):
        return (torch.dot(u, v) / torch.dot(u, u)) * u

    def forward(self, batch: torch.Tensor) -> torch.Tensor:
        """
        Compute the soft assignment for a batch of feature vectors, returning a batch of assignments
        for each cluster.

        :param batch: FloatTensor of [batch size, embedding dimension]
        :return: FloatTensor [batch size, number of clusters]
        """

        if self.project_assignment:
            assignment = batch @ self.cluster_centers.T
            # prove
            assignment = torch.pow(assignment, 2)

            norm = torch.norm(self.cluster_centers, p=2, dim=-1)
            soft_assign = assignment / norm
            return softmax(soft_assign, dim=-1)

        else:
            norm_squared = torch.sum(
                (batch.unsqueeze(1) - self.cluster_centers) ** 2, 2
            )
            numerator = 1.0 / (1.0 + (norm_squared / self.alpha))
            power = float(self.alpha + 1) / 2
            numerator = numerator**power
            return numerator / torch.sum(numerator, dim=1, keepdim=True)

    def get_cluster_centers(self) -> torch.Tensor:
        """
        Get the cluster centers.

        :return: FloatTensor [number of clusters, embedding dimension]
        """
        return self.cluster_centers


class InterpretableTransformerEncoder(TransformerEncoderLayer):
    def __init__(
        self,
        d_model,
        nhead,
        dim_feedforward=2048,
        dropout=0.1,
        activation=F.relu,
        layer_norm_eps=1e-5,
        batch_first=False,
        norm_first=False,
        device=None,
        dtype=None,
    ) -> None:
        super().__init__(
            d_model,
            nhead,
            dim_feedforward,
            dropout,
            activation,
            layer_norm_eps,
            batch_first,
            norm_first,
            device,
            dtype,
        )
        self.attention_weights: Optional[Tensor] = None

    def _sa_block(
        self,
        x: Tensor,
        attn_mask: Optional[Tensor],
        key_padding_mask: Optional[Tensor],
        is_causal: bool = False,
    ) -> Tensor:
        x, weights = self.self_attn(
            x,
            x,
            x,
            attn_mask=attn_mask,
            key_padding_mask=key_padding_mask,
            need_weights=True,
        )
        self.attention_weights = weights
        return self.dropout1(x)

    def get_attention_weights(self) -> Optional[Tensor]:
        return self.attention_weights
