from typing import Optional

import torch
from torch.utils._pytree import tree_map

from colossalai.device.device_mesh import DeviceMesh
from colossalai.tensor.d_tensor.layout import Layout
from colossalai.tensor.shape_consistency import ShapeConsistencyManager, to_global
from colossalai.tensor.sharding_spec import ShardingSpec

shape_consistency_manager = ShapeConsistencyManager()


class DTensor(torch.Tensor):

    def __init__(self, local_tensor: torch.Tensor, dist_layout: Layout):
        self.local_tensor = local_tensor
        self.data_type = local_tensor.dtype
        self.entire_shape = local_tensor.shape
        if dist_layout.entire_shape is None:
            dist_layout.entire_shape = self.entire_shape
        self.dist_layout = dist_layout
        self._apply_layout()

    @staticmethod
    def __new__(cls, local_tensor, layout):
        return torch.Tensor._make_subclass(cls, local_tensor, local_tensor.requires_grad)

    def __repr__(self):
        return f"DTensor({self.to_global()}, {self.dist_layout})"

    def __str__(self):
        return self.__repr__()

    def layout_convert(self, target_layout):
        '''
        Convert the layout of the tensor from source_spec to target_spec.
        '''
        source_spec = convert_layout_to_sharding_spec(self.dist_layout)
        target_spec = convert_layout_to_sharding_spec(target_layout)
        self.local_tensor = shape_consistency_manager.apply_for_autoparallel_runtime(
            self.local_tensor, source_spec, target_spec)
        self.dist_layout = target_layout

    def _apply_layout(self):
        '''
        Apply the layout to the local tensor during initializing process.
        '''
        source_spec = construct_default_sharding_spec(self.local_tensor, self.device_mesh)
        target_spec = convert_layout_to_sharding_spec(self.dist_layout)
        self.local_tensor = shape_consistency_manager.apply_for_autoparallel_runtime(
            self.local_tensor, source_spec, target_spec)

    @classmethod
    def __torch_function__(cls, func, types, args=(), kwargs=None):
        if kwargs is None:
            kwargs = {}

        def filter_arg(arg):
            if isinstance(arg, DTensor):
                return arg.local_tensor
            else:
                return arg

        args = tree_map(filter_arg, args)
        kwargs = tree_map(filter_arg, kwargs)
        # if we want to convert the result into DTensor, we need to infer the layout of result from the layout of input tensors
        # and op type.

        return func(*args, **kwargs)

    @property
    def device_mesh(self):
        '''
        Return the device mesh of the tensor.
        '''
        return self.dist_layout.device_mesh

    @property
    def sharding_spec(self):
        '''
        Return the sharding specification of the tensor.
        '''
        return self.dist_layout.sharding_spec

    def to(self, *args, **kwargs):
        '''
        Move the tensor to a new device or convert the tensor to a new dtype.
        '''
        self.local_tensor = self.local_tensor.to(*args, **kwargs)
        self.data_type = self.local_tensor.dtype
        self.dist_layout.device_type = self.local_tensor.device
        # TODO: update the device mesh process groups or we should just cache
        # both the cpu process groups and the cuda process groups?
        return self

    def to_local(self):
        '''
        Return the local tensor in this rank.
        '''
        return self.local_tensor

    def to_global(self):
        '''
        Recover the global tensor from the distributed tensor.

        Note: This function will all_gather the local tensor to the global tensor and it
        will not change the layout of the DTensor. This function is mainly used for debugging or
        check the correctness of the distributed tensor.
        '''
        return to_global(self.local_tensor, convert_layout_to_sharding_spec(self.dist_layout))


def distribute_tensor(local_tensor: torch.Tensor, dist_layout: Layout) -> DTensor:
    '''
    Distribute the local tensor to the distributed tensor according to the dist_layout specified.

    Args:
        local_tensor: tensor to be distributed.
        dist_layout: the layout specification of the distributed tensor.

    Returns:
        A 'DTensor' object.
    '''
    return DTensor(local_tensor, dist_layout)


def distribute_module(module: torch.nn.Module, partition_fn: Optional[callable] = None) -> torch.nn.Module:
    '''
    This function converts all the parameters in the module to DTensor(DParam).

    Note: This function is subject to future change as the DParam has not been implemented yet.
    '''
    for name, param in module.named_parameters():
        if param is not None and not isinstance(param, DTensor):
            # TODO: we could convert the parameter to DParam here,
            # the type of the parameter could be an optional argument.
            setattr(module, name, torch.nn.Parameter(partition_fn(name, param.data)))
    return module


def convert_layout_to_sharding_spec(layout: Layout) -> ShardingSpec:
    '''
    Convert the layout from Layout class to ShardingSpec class.
    '''
    return ShardingSpec(device_mesh=layout.device_mesh,
                        entire_shape=layout.entire_shape,
                        dim_partition_dict=layout.sharding_spec.dim_partition_dict)


def construct_default_sharding_spec(
    tensor: torch.Tensor,
    device_mesh: DeviceMesh,
) -> ShardingSpec:
    '''
    Construct the default sharding specification for the tensor.
    '''
    return ShardingSpec(device_mesh=device_mesh, entire_shape=tensor.shape, dim_partition_dict={})
