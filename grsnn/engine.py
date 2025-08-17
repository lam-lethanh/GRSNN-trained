import os
import sys
import logging
from itertools import islice

import torch
from torch import distributed as dist
from torch import nn
from torch.utils import data as torch_data

from torchdrug import data, core, utils
from torchdrug.core import Registry as R
from torchdrug.utils import comm, pretty

from torch.cuda import amp


module = sys.modules[__name__]
logger = logging.getLogger(__name__)


#@R.register("core.Engine")
class Engine(core.Configurable):
    """
    General class that handles everything about training and test of a task.

    This class can perform synchronous distributed parallel training over multiple CPUs or GPUs.
    To invoke parallel training, launch with one of the following commands.

    1. Single-node multi-process case.

    .. code-block:: bash

        python -m torch.distributed.launch --nproc_per_node={number_of_gpus} {your_script.py} {your_arguments...}

    2. Multi-node multi-process case.

    .. code-block:: bash

        python -m torch.distributed.launch --nnodes={number_of_nodes} --node_rank={rank_of_this_node}
        --nproc_per_node={number_of_gpus} {your_script.py} {your_arguments...}

    If :meth:`preprocess` is defined by the task, it will be applied to ``train_set``, ``valid_set`` and ``test_set``.

    Parameters:
        task (nn.Module): task
        train_set (data.Dataset): training set
        valid_set (data.Dataset): validation set
        test_set (data.Dataset): test set
        optimizer (optim.Optimizer): optimizer
        scheduler (lr_scheduler._LRScheduler, optional): scheduler
        gpus (list of int, optional): GPU ids. By default, CPUs will be used.
            For multi-node multi-process case, repeat the GPU ids for each node.
        batch_size (int, optional): batch size of a single CPU / GPU
        gradient_interval (int, optional): perform a gradient update every n batches.
            This creates an equivalent batch size of ``batch_size * gradient_interval`` for optimization.
        num_worker (int, optional): number of CPU workers per GPU
        logger (str or core.LoggerBase, optional): logger type or logger instance.
            Available types are ``logging`` and ``wandb``.
        log_interval (int, optional): log every n gradient updates
    """

    def __init__(self, task, train_set, valid_set, test_set, optimizer, scheduler=None, gpus=None, batch_size=1,
                 gradient_interval=1, num_worker=0, logger="logging", log_interval=100, amp_training=False):
        self.rank = comm.get_rank()
        self.world_size = comm.get_world_size()
        self.gpus = gpus
        self.batch_size = batch_size
        self.gradient_interval = gradient_interval
        self.num_worker = num_worker

        if gpus is None:
            self.device = torch.device("cpu")
        else:
            if len(gpus) != self.world_size:
                error_msg = "World size is %d but found %d GPUs in the argument"
                if self.world_size == 1:
                    error_msg += ". Did you launch with `python -m torch.distributed.launch`?"
                raise ValueError(error_msg % (self.world_size, len(gpus)))
            self.device = torch.device(gpus[self.rank % len(gpus)])

        if self.world_size > 1 and not dist.is_initialized():
            if self.rank == 0:
                module.logger.info("Initializing distributed process group")
            backend = "gloo" if gpus is None else "nccl"
            comm.init_process_group(backend, init_method="env://")

        if hasattr(task, "preprocess"):
            if self.rank == 0:
                module.logger.warning("Preprocess training set")
            # TODO: more elegant implementation
            # handle dynamic parameters in optimizer
            old_params = list(task.parameters())
            print(f"train_set size: {len(train_set)}, valid_set.indices: {valid_set.indices}")
            result = task.preprocess(train_set, valid_set, test_set)
            if result is not None:
                train_set, valid_set, test_set = result
            new_params = list(task.parameters())
            if len(new_params) != len(old_params):
                optimizer.add_param_group({"params": new_params[len(old_params):]})
        if self.world_size > 1:
            task = nn.SyncBatchNorm.convert_sync_batchnorm(task)
        if self.device.type == "cuda":
            task = task.cuda(self.device)

        self.model = task
        self.train_set = train_set
        self.valid_set = valid_set
        self.test_set = test_set
        self.optimizer = optimizer
        self.scheduler = scheduler

        if isinstance(logger, str):
            if logger == "logging":
                logger = core.LoggingLogger()
            elif logger == "wandb":
                logger = core.WandbLogger(project=task.__class__.__name__)
            else:
                raise ValueError("Unknown logger `%s`" % logger)
        self.meter = core.Meter(log_interval=log_interval, silent=self.rank > 0, logger=logger)
        self.meter.log_config(self.config_dict())

        # mixed precision training
        self.amp = amp_training
        if self.amp:
            self.scaler = amp.GradScaler()

    def train(self, num_epoch=1, batch_per_epoch=None):
        """
        Train the model.

        If ``batch_per_epoch`` is specified, randomly draw a subset of the training set for each epoch.
        Otherwise, the whole training set is used for each epoch.

        Parameters:
            num_epoch (int, optional): number of epochs
            batch_per_epoch (int, optional): number of batches per epoch
        """
        sampler = torch_data.DistributedSampler(self.train_set, self.world_size, self.rank)
        dataloader = data.DataLoader(self.train_set, self.batch_size, sampler=sampler, num_workers=self.num_worker)
        batch_per_epoch = batch_per_epoch or len(dataloader)
        model = self.model
        if self.world_size > 1:
            if self.device.type == "cuda":
                model = nn.parallel.DistributedDataParallel(model, device_ids=[self.device],
                                                            find_unused_parameters=True)
            else:
                model = nn.parallel.DistributedDataParallel(model, find_unused_parameters=True)
        model.train()

        for epoch in self.meter(num_epoch):
            sampler.set_epoch(epoch)

            metrics = []
            start_id = 0
            # the last gradient update may contain less than gradient_interval batches
            gradient_interval = min(batch_per_epoch - start_id, self.gradient_interval)

            for batch_id, batch in enumerate(islice(dataloader, batch_per_epoch)):
                if self.device.type == "cuda":
                    batch = utils.cuda(batch, device=self.device)

                if self.amp:
                    with amp.autocast():
                        loss, metric = model(batch)
                        loss = loss / gradient_interval
                    self.scaler.scale(loss).backward()
                else:
                    loss, metric = model(batch)
                    if not loss.requires_grad:
                        raise RuntimeError("Loss doesn't require grad. Did you define any loss in the task?")
                    loss = loss / gradient_interval
                    loss.backward()
                metrics.append(metric)

                if batch_id - start_id + 1 == gradient_interval:
                    if self.amp:
                        self.scaler.step(self.optimizer)
                        self.scaler.update()
                    else:
                        self.optimizer.step()
                    self.optimizer.zero_grad()

                    metric = utils.stack(metrics, dim=0)
                    metric = utils.mean(metric, dim=0)
                    if self.world_size > 1:
                        metric = comm.reduce(metric, op="mean")
                    self.meter.update(metric)

                    metrics = []
                    start_id = batch_id + 1
                    gradient_interval = min(batch_per_epoch - start_id, self.gradient_interval)

            if self.scheduler:
                self.scheduler.step()

    @torch.no_grad()
    def evaluate(self, split, log=True):
        """
        Evaluate the model.

        Parameters:
            split (str): split to evaluate. Can be ``train``, ``valid`` or ``test``.
            log (bool, optional): log metrics or not

        Returns:
            dict: metrics
        """
        if comm.get_rank() == 0:
            logger.warning(pretty.separator)
            logger.warning("Evaluate on %s" % split)
        test_set = getattr(self, "%s_set" % split)
        sampler = torch_data.DistributedSampler(test_set, self.world_size, self.rank)
        dataloader = data.DataLoader(test_set, self.batch_size, sampler=sampler, num_workers=self.num_worker)
        model = self.model

        model.eval()
        preds = []
        targets = []
        for batch in dataloader:
            if self.device.type == "cuda":
                batch = utils.cuda(batch, device=self.device)

            pred, target = model.predict_and_target(batch)
            preds.append(pred)
            targets.append(target)

        pred = utils.cat(preds)
        target = utils.cat(targets)
        if self.world_size > 1:
            pred = comm.cat(pred)
            target = comm.cat(target)
        metric = model.evaluate(pred, target)
        if log:
            self.meter.log(metric, category="%s/epoch" % split)

        return metric

    @torch.no_grad()
    def get_spike_rate(self, split):
        if comm.get_rank() == 0:
            logger.warning(pretty.separator)
            logger.warning("Evaluate on %s" % split)
        test_set = getattr(self, "%s_set" % split)
        sampler = torch_data.DistributedSampler(test_set, self.world_size, self.rank)
        dataloader = data.DataLoader(test_set, self.batch_size, sampler=sampler, num_workers=self.num_worker)
        model = self.model

        model.eval()
        spike_rate = None
        sample_num = 0
        for batch in dataloader:
            if self.device.type == "cuda":
                batch = utils.cuda(batch, device=self.device)

            spike_sum, bs = model.get_spike_rate(batch)
            if spike_rate is None:
                spike_rate = spike_sum
            else:
                spike_rate += spike_sum
            sample_num += bs
        spike_rate /= sample_num

        return spike_rate, torch.mean(spike_rate)

    def load(self, checkpoint, load_optimizer=True, remove_graph=False):
        """
        Load a checkpoint from file.

        Parameters:
            checkpoint (file-like): checkpoint file
            load_optimizer (bool, optional): load optimizer state or not
        """
        if comm.get_rank() == 0:
            logger.warning("Load checkpoint from %s" % checkpoint)
        checkpoint = os.path.expanduser(checkpoint)
        state = torch.load(checkpoint, map_location=self.device)

        if remove_graph:
            if "fact_graph" in state["model"]:
                state["model"].pop("fact_graph")
            if "graph" in state["model"]:
                state["model"].pop("graph")
            if "train_graph" in state["model"]:
                state["model"].pop("train_graph")
            if "valid_graph" in state["model"]:
                state["model"].pop("valid_graph")
            if "test_graph" in state["model"]:
                state["model"].pop("test_graph")
            self.model.load_state_dict(state["model"], strict=False)
        else:
            self.model.load_state_dict(state["model"])

        if load_optimizer:
            self.optimizer.load_state_dict(state["optimizer"])
            for state in self.optimizer.state.values():
                for k, v in state.items():
                    if isinstance(v, torch.Tensor):
                        state[k] = v.to(self.device)

        comm.synchronize()

    def save(self, checkpoint):
        """
        Save checkpoint to file.

        Parameters:
            checkpoint (file-like): checkpoint file
        """
        if comm.get_rank() == 0:
            logger.warning("Save checkpoint to %s" % checkpoint)
        checkpoint = os.path.expanduser(checkpoint)
        if self.rank == 0:
            state = {
                "model": self.model.state_dict(),
                "optimizer": self.optimizer.state_dict()
            }
            torch.save(state, checkpoint)

        comm.synchronize()

    @classmethod
    def load_config_dict(cls, config):
        """
        Construct an instance from the configuration dict.
        """
        if getattr(cls, "_registry_key", cls.__name__) != config["class"]:
            raise ValueError("Expect config class to be `%s`, but found `%s`" % (cls.__name__, config["class"]))

        optimizer_config = config.pop("optimizer")
        new_config = {}
        for k, v in config.items():
            if isinstance(v, dict) and "class" in v:
                v = core.Configurable.load_config_dict(v)
            if k != "class":
                new_config[k] = v
        optimizer_config["params"] = new_config["task"].parameters()
        new_config["optimizer"] = core.Configurable.load_config_dict(optimizer_config)

        return cls(**new_config)

    @property
    def epoch(self):
        """Current epoch."""
        return self.meter.epoch_id
