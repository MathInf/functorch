import os
import torch
import torch.nn as nn
from functorch import make_functional_with_buffers, make_fx
from torch.fx.node import map_arg
import torch.fx as fx
from torch.fx.proxy import GraphAppendingTracer
from torch.fx import immutable_collections
import torch.utils._pytree as pytree
import torch.utils.dlpack
from torch.fx.passes import graph_drawer
import os
import copy
from functorch._C import CompileCache
from .python_key import pythonkey_decompose
from .decompositions import register_decomposition

pytree._register_pytree_node(immutable_collections.immutable_list, lambda x: (
    list(x), None), lambda x, c: immutable_collections.immutable_list(x))
pytree._register_pytree_node(immutable_collections.immutable_dict, lambda x: (list(x.values()), list(
    x.keys())), lambda x, c: immutable_collections.immutable_dict({key: value for key, value in zip(c, x)}))

aten = torch.ops.aten

def draw_graph(traced: torch.fx.GraphModule, fname: str, figname: str = "fx_graph", clear_meta = True):
    if clear_meta:
        traced = copy.deepcopy(traced)
    for node in traced.graph.nodes:
        node.meta = {}
    base, ext = os.path.splitext(fname)
    if not ext:
        ext = ".svg"
    print(f"Writing FX graph to file: {base}{ext}")
    g = graph_drawer.FxGraphDrawer(traced, figname)
    x = g.get_main_dot_graph()
    getattr(x, "write_" + ext.lstrip("."))(f"{base}{ext}")

# todo(chilli): clean this up/make it more understandable


def default_partition(fx_module: fx.GraphModule, _joint_inputs):
    bw_nodes = set()
    saved_nodes = set()
    output_node = None
    for n in fx_module.graph.nodes:
        if n.op == 'placeholder' and 'tangents' in n.target:
            bw_nodes.add(n)
        elif n.op != 'output':
            has_color = False

            def is_colored(a):
                nonlocal has_color
                if a in bw_nodes or a in saved_nodes:
                    has_color = True

            def add_saved(a):
                if a not in bw_nodes:
                    saved_nodes.add(a)
            map_arg(n.args, lambda x: is_colored(x))
            map_arg(n.kwargs, lambda x: is_colored(x))
            if has_color:
                bw_nodes.add(n)
                map_arg(n.args, lambda x: add_saved(x))
                map_arg(n.kwargs, lambda x: add_saved(x))
        elif n.op == 'output':
            output_node = n

    num_fwd_outputs = fx_module._out_spec.children_specs[0].num_leaves
    num_bwd_outputs = fx_module._out_spec.children_specs[1].num_leaves
    bw_outputs = output_node.args[0][num_fwd_outputs:]

    bw_graph = fx.Graph()
    value_remap = {}
    for saved_node in saved_nodes:
        value_remap[saved_node] = bw_graph.placeholder(saved_node.name)

    for node in fx_module.graph.nodes:
        if node in bw_nodes or node in bw_outputs:
            value_remap[node] = bw_graph.node_copy(node, lambda n: value_remap[n])

    assert(num_fwd_outputs + num_bwd_outputs == len(output_node.args[0]))
    bwd_outputs = [value_remap[i] for i in bw_outputs]
    if len(bwd_outputs) == 1:
        bwd_outputs = bwd_outputs[0]
    bw_graph.output(bwd_outputs)
    bw_module = fx.GraphModule(fx_module, bw_graph)

    fw_graph = fx.Graph()
    value_remap = {}
    for node in fx_module.graph.nodes:
        if node not in bw_nodes and node.op != 'output':
            value_remap[node] = fw_graph.node_copy(node, lambda n: value_remap[n])

    fwd_outputs = [value_remap[i] for i in output_node.args[0]
                   [:num_fwd_outputs]] + [value_remap[n] for n in saved_nodes]
    if len(fwd_outputs) == 1:
        fwd_outputs = fwd_outputs[0]
    fw_graph.output(fwd_outputs)
    fw_module = fx.GraphModule(fx_module, fw_graph)
    fw_module.graph.lint()
    bw_module.graph.lint()
    return fw_module, bw_module


class InvalidNodeBase(object):
    def __repr__(self):
        return "Invalid Node"


InvalidNode = InvalidNodeBase()


def _extract_graph_with_inputs_outputs(joint_graph, inputs, outputs):
    """
    Given a graph, extracts out a subgraph that takes the specified nodes as inputs and returns the specified outputs.

    This includes specifying non-placeholder nodes as inputs.

    The general strategy is to initialize all inputs with proxies as we
    encounter them, and trace through the graph, only keeping values which take
    in valid proxies. Then, all dead code is eliminated.
    """
    new_graph = fx.Graph()
    tracer = GraphAppendingTracer(new_graph)
    env = {}

    # Add new placeholder nodes in the order specified by the inputs
    new_inputs = {}
    for node in inputs:
        new_node = new_graph.placeholder(node.name)
        new_inputs[node.name] = new_node

    for node in joint_graph.nodes:
        if node in inputs:
            env[node] = fx.Proxy(new_inputs[node.name], tracer)
        elif node.op == 'placeholder':
            env[node] = InvalidNode
        elif node.op == 'call_function':
            def map_arg_to_proxy(x):
                if isinstance(x, fx.Node):
                    out = env[x]
                    return out
                else:
                    return x
            all_args = pytree.tree_flatten((node.args, node.kwargs))[0]
            all_args = [isinstance(env[x], InvalidNodeBase) for x in all_args if isinstance(x, fx.Node)]
            if any(all_args):
                env[node] = InvalidNode
                continue
            args = pytree.tree_map(map_arg_to_proxy, node.args)
            kwargs = pytree.tree_map(map_arg_to_proxy, node.kwargs)
            out = node.target(*args, **kwargs)
            env[node] = out
        elif node.op == 'get_attr':
            new_node = new_graph.node_copy(node, lambda x: env[x])
            env[node] = fx.Proxy(new_node, tracer)
        elif node.op == 'output':
            pass
    new_graph.output([env[x].node for x in outputs])

    new_graph.eliminate_dead_code()
    new_graph.lint()
    return new_graph

def _is_primal(node):
    return node.op == "placeholder" and "tangents" not in node.target

def _is_tangent(node):
    return node.op == "placeholder" and "tangents" in node.target

def _extract_fwd_bwd_outputs(joint_module: fx.GraphModule):
    num_fwd_outputs = joint_module._out_spec.children_specs[0].num_leaves
    outputs = pytree.tree_flatten([node.args for node in joint_module.graph.nodes if node.op == 'output'])[0]
    fwd_outputs = outputs[:num_fwd_outputs]
    bwd_outputs = outputs[num_fwd_outputs:]
    return fwd_outputs, bwd_outputs

def _extract_fwd_bwd_modules(joint_module: fx.GraphModule, saved_values):
    fwd_outputs, bwd_outputs = _extract_fwd_bwd_outputs(joint_module)
    primal_inputs = list(filter(_is_primal, joint_module.graph.nodes))
    tangent_inputs = list(filter(_is_tangent, joint_module.graph.nodes))
    # Construct the forward module
    fwd_graph = _extract_graph_with_inputs_outputs(joint_module.graph, primal_inputs, fwd_outputs + saved_values)
    bwd_graph = _extract_graph_with_inputs_outputs(joint_module.graph, saved_values + tangent_inputs, bwd_outputs)

    # This is to filter out saved values that don't actually end up being used by the backwards pass
    for node in bwd_graph.nodes:
        if node.op == 'placeholder' and not node.users:
            for saved_value in saved_values:
                if saved_value.name == node.name:
                    saved_values.remove(saved_value)
                    break

    # Now, we re-generate the fwd/bwd graphs.
    # NB: This might increase compilation time, but I doubt it matters
    fwd_graph = _extract_graph_with_inputs_outputs(joint_module.graph, primal_inputs, fwd_outputs + saved_values)
    bwd_graph = _extract_graph_with_inputs_outputs(joint_module.graph, saved_values + tangent_inputs, bwd_outputs)

    fwd_module = fx.GraphModule(joint_module, fwd_graph)
    bwd_module = fx.GraphModule(joint_module, bwd_graph)
    return fwd_module, bwd_module


# def default_partition(joint_module: fx.GraphModule, _joint_inputs):
#     primal_inputs = list(filter(_is_primal, joint_module.graph.nodes))
#     fwd_outputs, bwd_outputs = _extract_fwd_bwd_outputs(joint_module)
#     forward_only_graph = _extract_graph_with_inputs_outputs(joint_module.graph, primal_inputs, fwd_outputs)
#     saved_values = forward_only_graph


def prod(x):
    s = 1
    for i in x:
        s *= i
    return s

import math
def partition_with_recompute_fwd_in_bwd(joint_module: fx.GraphModule, _joint_inputs):
    """
    Partitions the joint graph such that the backward recomputes the forward.
    Recomputing helps in trading off memory bandwidth with computation.

    To create the fwd and bwd graph, we copy the joint graph, manually set the
    outputs to just original forward or backward outputs. And then we run the
    resulting graphs through dead code elimintation.
    """
    try:
        import networkx as nx
    except ImportError:
        raise RuntimeError("Need networkx installed to perform smart recomputation heuristics")
    # draw_graph(joint_module, "joint.svg")
    primal_inputs = list(filter(_is_primal, joint_module.graph.nodes))
    tangent_inputs = list(filter(_is_tangent, joint_module.graph.nodes))
    full_bw_graph = joint_module.graph

    nx_graph = nx.DiGraph()
    tangent_closure = set()
    name_to_node = {}
    for node in full_bw_graph.nodes:
        name_to_node[node.name] = node
        if node.op == 'placeholder' and "tangents" in node.target:
            tangent_closure.add(node)
        if node in tangent_closure:
            for user in node.users:
                tangent_closure.add(user)

    pointwise_ops = [aten.add, aten.sub, aten.div, aten.atan2, aten.mul, aten.max, aten.min, aten.pow, aten.remainder, aten.fmod, aten.__and__, aten.__or__, aten.__xor__, aten.__lshift__, aten.__rshift__, aten.eq, aten.ne, aten.ge, aten.gt, aten.le, aten.lt, aten.abs, aten.bitwise_not, aten.ceil, aten.floor, aten.frac, aten.neg, aten.relu, aten.round, aten.silu, aten.trunc, aten.log, aten.log10, aten.log1p, aten.log2, aten.lgamma, aten.exp, aten.expm1, aten.erf, aten.erfc, aten.cos, aten.acos, aten.cosh, aten.sin, aten.asin, aten.sinh, aten.tan, aten.atan, aten.tanh, aten.atanh, aten.sqrt, aten.rsqrt,  aten.reciprocal, aten.sigmoid, aten.softplus, aten.threshold, aten.threshold_backward, aten.clamp, aten.where, aten.lerp, aten.addcmul, aten.gelu, aten.gelu_backward]
    reduction_ops = [aten.softmax, aten._softmax, aten._softmax_backward_data, aten.sum, aten.mean, aten._grad_sum_to_size, aten.sum_to_size, aten.amax]
    norm_ops = [aten.instance_norm, aten._batch_norm_impl_index, aten.native_batch_norm, aten.batch_norm, aten._batch_norm_impl_index_backward, aten.native_layer_norm, aten.layer_norm, aten.native_layer_norm_backward]
    misc_ops = [aten.to, aten.type_as]

    view_ops = [aten.expand, aten.clone, aten.transpose, aten.t, aten.view, aten._unsafe_view, aten.permute, aten.transpose, aten.t, aten._reshape_alias, aten.squeeze, aten.unsqueeze, aten.reshape]

    recomputable_ops = set(
        pointwise_ops 
        + reduction_ops
        + norm_ops
        + misc_ops
    )

    print("recomputable ops", set([i.target for i in full_bw_graph.nodes if i.op == 'call_function']) & set(recomputable_ops))
    for node in full_bw_graph.nodes:
        if node in tangent_closure:
            nx_graph.add_edge(node.name+"_in", "sink", capacity=math.inf)
            continue
        is_input = False
        if node.op == 'placeholder' and "primals" in node.target:
            nx_graph.add_edge("source", node.name+"_in", capacity=math.inf)
            is_input = True

        if node.target not in recomputable_ops:
            nx_graph.add_edge("source", node.name+"_in", capacity=math.inf)
        
        if not 'tensor_meta' in node.meta:
            weight = math.inf
        else:
            mem_sz = prod(node.meta['tensor_meta'].shape)
            if is_input:
                weight = mem_sz
            else:
                weight = mem_sz * 2


        nx_graph.add_edge(node.name+"_in", node.name+"_out", capacity=weight)
        for user in node.users:
            nx_graph.add_edge(node.name+"_out", user.name+"_in", capacity=math.inf)

    cut_value, partition = nx.minimum_cut(nx_graph, "source", "sink")
    reachable, non_reachable = partition
    cutset = set()
    for u, nbrs in ((n, nx_graph[n]) for n in reachable):
        cutset.update((u, v) for v in nbrs if v in non_reachable)

    cut_nodes = set()
    for node_in, node_out in cutset:
        assert node_in[:-3] == node_out[:-4]
        node_name = node_in[:-3]
        cut_nodes.add(node_name)
    # print(len(cut_nodes), sorted(list(cut_nodes)))


    saved_values = [name_to_node[node] for node in cut_nodes]


    return _extract_fwd_bwd_modules(joint_module, saved_values)

def create_joint_forward_backward(fn):
    def joint_forward_backward(primals, tangents):
        out = fn(*primals)
        primals = [p for p in pytree.tree_flatten(primals)[0] if p.requires_grad]
        backward_out = []
        if primals:  # todo(chilli): Make it support it if not all outputs have gradients
            backward_out = torch.autograd.grad(out, primals, grad_outputs=tangents, allow_unused=True)
        return out, backward_out
    return joint_forward_backward


def draw_joint_graph(graph, joint_inputs, file_name="full_graph.png"):
    draw_graph(graph, file_name)
    return default_partition(graph, joint_inputs)


def normalize_as_list(x):
    if isinstance(x, tuple):
        return list(x)
    elif isinstance(x, list):
        return x
    return [x]


def create_compiled_function(flat_fn, fw_compiler, bw_compiler, partition_fn, decompose):
    # putting these decompositions here since they shouldn't always be used
    # Kinda sketchy ... we use torch.sub here to have the correct scalar => tensor promotion logic
    @register_decomposition(aten.rsub)
    def rsub(a, b, alpha=1):
        return -aten.sub(a, b)

    # This is only valid if we're running the graph without autograd, such as if the backward pass has been traced.
    @register_decomposition(aten.detach)
    def detach_decomposition(x):
        return x

    @register_decomposition(aten._reshape_alias)
    def _reshape_alias(x, shape, strides):
        return aten.view(x, shape)

    joint_forward_backward = create_joint_forward_backward(flat_fn)

    compiled_fw = None
    compiled_bw = None
    num_outs = None

    class CompiledFunction(torch.autograd.Function):
        @staticmethod
        def forward(ctx, *flat_args):
            nonlocal compiled_fw, compiled_bw, num_outs
            if compiled_fw is None:
                out = flat_fn(*flat_args)
                if isinstance(out, (list, tuple)):
                    num_outs = len(out)
                else:
                    num_outs = 1

                joint_inputs = (flat_args, out)
                with torch.enable_grad():
                    if decompose:
                        with pythonkey_decompose():
                            fx_g = make_fx(joint_forward_backward)(*joint_inputs)
                    else:
                        fx_g = make_fx(joint_forward_backward)(*joint_inputs)
                fw_module, bw_module = partition_fn(fx_g, joint_inputs)
                # print(fw_module.code, bw_module.code)

                compiled_fw = fw_compiler(fw_module, flat_args)
                fw_outs = normalize_as_list(compiled_fw(*flat_args))

                sz = []
                for act in fw_outs[num_outs:]:
                    if isinstance(act, torch.nn.parameter.Parameter):
                        act = act.data
                        continue
                    sz.append(act.storage().nbytes())
                print(f"Saved activation GB: {sum(sz)/1e9}")
                bw_args = fw_outs[num_outs:] + fw_outs[0:num_outs]
                compiled_bw = bw_compiler(bw_module, bw_args)
            else:
                fw_outs = normalize_as_list(compiled_fw(*flat_args))
            ctx.save_for_backward(*fw_outs[num_outs:])
            return tuple(fw_outs[0:num_outs])

        @staticmethod
        def backward(ctx, *flat_args):
            # hmm... this doesn't feel right. todo
            # contiguous_args = [t.contiguous() for t in flat_args]
            contiguous_args = [t for t in flat_args]
            out = normalize_as_list(compiled_bw(*ctx.saved_tensors, *contiguous_args))
            out_iter = iter(out)
            grad_out = [next(out_iter) if p else None for p in ctx.needs_input_grad]
            return tuple(grad_out)

    return CompiledFunction


class _CompileCache(CompileCache):
    pass

# using a C++-based pytree reduces the overhead by about 50%
try:
    import tree
    HAS_TREE = True
except ImportError:
    HAS_TREE = False
compile_cache = None

# Inspired by autodidax (thanks!)


class PytreeThunk:
    spec = None
    # These are some kinda dumb microoptimizations that save about 3-4 us of overhead.
    is_simple = None  # if the output spec is a tuple/list, we won't bother unflattening it.
    is_really_simple = None  # if the output spec is a LeafSpec

    def set(self, spec):
        assert self.spec is None or self.spec == spec
        self.spec = spec
        if type(self.spec) in [tuple, list] and all([isinstance(i, pytree.LeafSpec) for i in spec.children_specs]):
            self.is_simple = True
        if isinstance(self.spec, pytree.LeafSpec):
            self.is_really_simple = True

    def unflatten(self, x):
        if self.is_really_simple:
            return x[0]
        if self.is_simple:
            return x
        return pytree.tree_unflatten(x, self.spec)


def compiled_function(
    fn, fw_compiler, bw_compiler, partition_fn=default_partition, decompose=False, hasher_type="StaticShapeHasher"
):
    global compile_cache
    if compile_cache is None:
        compile_cache = CompileCache()
    cached_res = None

    fn_id = id(fn)

    def returned_function(*args, **kwargs):
        global compile_cache
        nonlocal cached_res
        if HAS_TREE:
            flattened_args = tree.flatten((args, kwargs))
        else:
            flattened_args, _ = pytree.tree_flatten((args, kwargs))
        num_args = len(flattened_args)
        # Check if the fn is already compiled
        cached_res = compile_cache.at(fn_id, num_args, hasher_type, *flattened_args)

        # Compile the function and save it in the cache
        if cached_res is None:
            # Compile a new function
            flattened_args, args_spec = pytree.tree_flatten((args, kwargs))
            out_spec = PytreeThunk()

            def flat_fn(*args):
                nonlocal out_spec
                args, kwargs = pytree.tree_unflatten(args, args_spec)
                tree_out = fn(*args, **kwargs)
                flat_out = pytree.tree_flatten(tree_out)
                out_spec.set(flat_out[1])
                return flat_out[0]
            compiled_fn = create_compiled_function(
                flat_fn, fw_compiler, bw_compiler, partition_fn, decompose
            ).apply
            cached_res = (compiled_fn, out_spec)
            # Save the compiled_fn in the cache
            compile_cache.insert(
                fn_id, num_args, hasher_type, cached_res, *flattened_args
            )



        cached_fn, out_spec = cached_res
        out = cached_fn(*flattened_args)
        return out_spec.unflatten(out)

    return returned_function


def num_of_recompilations():
    global compile_cache
    if compile_cache is None:
        return 0
    return compile_cache.size()


def clear_compile_cache():
    global compile_cache
    if compile_cache is not None:
        compile_cache.clear()
        compile_cache = None


def compiled_module(mod, *args, **kwargs):
    func_mod, params, buffers = make_functional_with_buffers(mod)
    compiled_f = compiled_function(func_mod, *args, **kwargs)

    class CompiledModule(nn.Module):
        def __init__(self):
            super(CompiledModule, self).__init__()
            self.orig_module = mod

        def forward(self, *args, **kwargs):
            return compiled_f(
                tuple(self.parameters()),
                tuple(self.buffers()),
                *args,
                **kwargs
            )

    return CompiledModule()


aot_function = compiled_function
aot_module = compiled_module
