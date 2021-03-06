## @package core
# Module caffe2.python.core
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from collections import namedtuple, OrderedDict
from past.builtins import basestring
from future.utils import viewitems, viewkeys, viewvalues
from itertools import chain
from six import binary_type, string_types, text_type

from caffe2.proto import caffe2_pb2
from collections import defaultdict
from caffe2.python import scope, utils, workspace
import caffe2.python._import_c_extension as C
import google.protobuf.text_format as protobuftx
import pickle
import numpy as np
import sys

# Mac os specific message
if (sys.platform == 'darwin' and 'leveldb' in C.registered_dbs()):
    print('If you are using homebrew leveldb on a Mac OS, you might see an '
          'error warning you that malloc_zone_unregister() failed. This is '
          'not a caffe2 issue but is due to the homebrew leveldb having an '
          'incompatible memory allocator. It does not affect usage.')

# Convenience redirections to functions inside scope.
DeviceScope = scope.DeviceScope
NameScope = scope.NameScope


# Bring datatype enums to the main namespace
class DataType:
    pass


def _InitDataType():
    for name, value in caffe2_pb2.TensorProto.DataType.items():
        setattr(DataType, name, value)


_InitDataType()


def _GetRegisteredOperators():
    return set(workspace.RegisteredOperators())


_REGISTERED_OPERATORS = _GetRegisteredOperators()


def RefreshRegisteredOperators():
    global _REGISTERED_OPERATORS
    _REGISTERED_OPERATORS = _GetRegisteredOperators()


_GLOBAL_INIT_ARGS = []


def GlobalInit(args):
    _GLOBAL_INIT_ARGS.extend(args[1:])
    C.global_init(args)


def GetGlobalInitArgs():
    return _GLOBAL_INIT_ARGS[:]


def IsOperator(op_type):
    return (op_type in _REGISTERED_OPERATORS)


def IsOperatorWithEngine(op_type, engine):
    return (op_type + "_ENGINE_" + engine in _REGISTERED_OPERATORS)


def DeviceOption(device_type, cuda_gpu_id=0, random_seed=None):
    option = caffe2_pb2.DeviceOption()
    option.device_type = device_type
    option.cuda_gpu_id = cuda_gpu_id
    if random_seed is not None:
        option.random_seed = random_seed
    return option


def InferBlobDevices(net):
    '''
    Compute mapping from parameters to devices by looking at the
    device option of the op that creates the blob has
    '''
    mapping = {}
    for op in net.Proto().op:
        op_device = op.device_option
        if op_device is None:
            op_device = caffe2_pb2.DeviceOption(caffe2_pb2.CPU)
        # TODO: T18892922, use device annotations
        for b in op.output:
            mapping[b] = op_device
    return mapping


def InferOpBlobDevices(op):
    device_info = C.infer_op_input_output_device(op.SerializeToString())
    input_info = []
    output_info = []
    for dev_str in device_info[0]:
        device_option = caffe2_pb2.DeviceOption()
        device_option.ParseFromString(dev_str)
        input_info.append(device_option)
    for dev_str in device_info[1]:
        device_option = caffe2_pb2.DeviceOption()
        device_option.ParseFromString(dev_str)
        output_info.append(device_option)
    return input_info, output_info


GradientSlice = namedtuple('GradientSlice', ['indices', 'values'])


class BlobReference(object):
    """A wrapper around a blob in a net.

    BlobReference gives us a way to refer to the network that the blob is
    generated from. Note that blobs are, essentially, just strings in the
    current workspace.
    """

    def __init__(self, name, net=None):
        """Initializes a blob reference.

        Note that this does not prepends the namescope. If needed, use
        ScopedBlobReference() to prepend the existing namespace.
        """
        if isinstance(name, string_types):
            self._name = name
        elif isinstance(name, binary_type):
            self._name = name.decode('utf-8')
        else:
            self._name = str(name)
        self._from_net = net
        # meta allows helper functions to put whatever metainformation needed
        # there.
        self.meta = {}

    def __hash__(self):
        return hash(self._name)

    def __eq__(self, other):
        if isinstance(other, string_types):
            return self._name == other
        elif isinstance(other, binary_type):
            return self._name == other.decode('utf-8')
        elif isinstance(other, BlobReference):
            return self._name == other._name
        else:
            return False

    def __ne__(self, other):
        return not(self == other)

    def __str__(self):
        return self._name

    def __repr__(self):
        return 'BlobReference("{}")'.format(self._name)

    def __add__(self, other):
        if not isinstance(other, string_types):
            raise RuntimeError('Cannot add BlobReference to a non-string.')
        return BlobReference(self._name + other, self._from_net)

    def __radd__(self, other):
        if not isinstance(other, string_types):
            raise RuntimeError('Cannot add a non-string to BlobReference.')
        return BlobReference(other + self._name, self._from_net)

    def Net(self):
        return self._from_net

    def GetNameScope(self):
        return self._name[:self._name.rfind(scope._NAMESCOPE_SEPARATOR) + 1]

    def _CreateAndAddToNet(self, op_type, inputs=None, *args, **kwargs):
        """Internal function that routes the operator generation to the
        network's __getattr__ function.
        """
        inputs = [] if inputs is None else inputs
        if isinstance(inputs, BlobReference) or isinstance(inputs, string_types):
            inputs = [inputs]
        # add self to the input list.
        inputs.insert(0, self)
        return self._from_net.__getattr__(op_type)(inputs, *args, **kwargs)

    def __getattr__(self, op_type):
        """A wrapper allowing one to initiate operators from a blob reference.

        Example: for a blob reference b that comes from network n, doing
            b.Relu(...)
        is equivalent to doing
            net.Relu([b], ...)
        """
        if op_type.startswith('__'):
            raise AttributeError('Attribute {} not found.'.format(op_type))
        if self._from_net is None:
            raise RuntimeError(
                'You cannot use a blob reference that does not have a net '
                'source to create operators. Create the operator from an '
                'explicit net object.')
        if not IsOperator(op_type):
            raise RuntimeError(
                'Method ' + op_type + ' is not a registered operator.' +
                ' Did you mean: [' +
                ",".join(workspace.C.nearby_opnames(op_type)) + ']'
            )
        return lambda *args, **kwargs: self._CreateAndAddToNet(
            op_type, *args, **kwargs)

    def __dir__(self):
        additional_methods = [
            op
            for op in _REGISTERED_OPERATORS
            if '_ENGINE_' not in op or '_ENGINE_CUDNN' in op]
        return sorted(set(chain(
            dir(type(self)),
            viewkeys(self.__dict__),
            additional_methods
        )))


def ScopedName(name):
    """prefix the name with the current scope."""
    if isinstance(name, binary_type):
        name = name.decode('ascii')
    return scope.CurrentNameScope() + name


def ScopedBlobReference(name, *args, **kwargs):
    """Returns a blob reference with scope prefixed."""
    return BlobReference(ScopedName(name), *args, **kwargs)


def _RectifyInputOutput(blobs, net=None):
    """A helper function to rectify the input or output of the CreateOperator
    interface.
    """
    if isinstance(blobs, string_types) or isinstance(blobs, binary_type):
        # If blobs is a single string, prepend scope.CurrentNameScope()
        # and put it as a list.
        # TODO(jiayq): enforce using BlobReference instead of raw strings.
        return [ScopedBlobReference(blobs, net=net)]
    elif type(blobs) is BlobReference:
        # If blob is a BlobReference, simply put it as a list.
        return [blobs]
    elif type(blobs) in (list, tuple):
        # If blob is a list, we go through it and type check.
        rectified = []
        for blob in blobs:
            if isinstance(blob, string_types) or isinstance(blob, binary_type):
                rectified.append(ScopedBlobReference(blob, net=net))
            elif type(blob) is BlobReference:
                rectified.append(blob)
            else:
                raise TypeError(
                    "I/O blob #{} of unsupported type: {} of type {}"
                    .format(len(rectified), str(blob), type(blob)))
        return rectified
    else:
        raise TypeError(
            "Unknown input/output type: %s of type %s." %
            (str(blobs), type(blobs))
        )


def CreateOperator(
    operator_type,
    inputs,
    outputs,
    name='',
    control_input=None,
    device_option=None,
    arg=None,
    engine=None,
    **kwargs
):
    """A function wrapper that allows one to create operators based on the
    operator type. The type should be a string corresponding to an operator
    registered with Caffe2.
    """
    operator = caffe2_pb2.OperatorDef()
    operator.type = operator_type
    operator.name = name
    # Add rectified inputs and outputs
    inputs = _RectifyInputOutput(inputs)
    outputs = _RectifyInputOutput(outputs)
    operator.input.extend([text_type(i) for i in inputs])
    operator.output.extend([text_type(o) for o in outputs])
    if control_input:
        control_input = _RectifyInputOutput(control_input)
        operator.control_input.extend([text_type(i) for i in control_input])
    # Set device option:
    # (1) If device_option is explicitly set, use device_option.
    # (2) If not, but scope.CurrentDeviceScope() is set,
    #     then we use scope.CurrentDeviceScope().
    # (3) Otherwise, do not set device option.
    if device_option is not None:
        operator.device_option.CopyFrom(device_option)
    elif scope.CurrentDeviceScope() is not None:
        operator.device_option.CopyFrom(scope.CurrentDeviceScope())
    if engine is not None:
        operator.engine = engine
    # random seed is defined in the device option, so we need to do special
    # care.
    if 'random_seed' in kwargs:
        operator.device_option.random_seed = kwargs['random_seed']
        del kwargs['random_seed']
    # Add given arguments that do not need parsing
    if arg is not None:
        operator.arg.extend(arg)
    # Add all other arguments
    for key, value in viewitems(kwargs):
        operator.arg.add().CopyFrom(utils.MakeArgument(key, value))

    if workspace.IsImmediate():
        workspace.RunOperatorImmediate(operator)
    return operator


def _RegisterPythonImpl(
    f, grad_f=None, python_func_type=None, pass_workspace=False
):
    if python_func_type:
        func = python_func_type(f)
        f = func.forward
        grad_f = func.backward
    else:
        if isinstance(f, tuple):
            f = f[0](*f[1], **f[2])
        if isinstance(grad_f, tuple):
            grad_f = grad_f[0](*grad_f[1], **grad_f[2])

    token = C.register_python_op(f, pass_workspace, '')
    if grad_f:
        C.register_python_gradient_op(token, grad_f)
    return token


def CreatePythonOperator(
    f, inputs,
    outputs,
    grad_f=None,
    pass_workspace=False,
    python_func_type=None,
    *args,
    **kwargs
):
    """
    `f` should have a signature (inputs, outputs)

    If `pass_workspace` is True, the signature is changed to
    (inputs, outputs, workspace) where `workspace` is the workspace the op
    is going to run on. This is potentially dangerous (as the op can manipulate
    the workspace directly), use on your own risk.
    """
    kwargs["token"] = _RegisterPythonImpl(
        f, grad_f, python_func_type, pass_workspace=pass_workspace
    )
    return CreateOperator("Python", inputs, outputs, *args, **kwargs)


def GetIndexFromGradientList(g_list, name):
    """A helper function to get the index from a gradient list, None if not
    matching."""
    for i, g in enumerate(g_list):
        if g == name:
            return i
        elif type(g) is GradientSlice:
            if (g.indices == name or g.values == name):
                return i
    return None


OpSSA = namedtuple('OpSSA', ['op', 'in_versions', 'out_versions'])
GradGenMeta = namedtuple('GradGenMeta', ['grad_op', 'idx', 'gradient'])
SparseGradGenMeta = namedtuple('SparseGradGenMeta', [
    'grad_op_indices', 'idx_indices',
    'grad_op_values', 'idx_values',
    'gradient',
])


class IR(object):
    """A simple IR class to keep track of all intermediate representations used
    in the gradient computation.
    """

    def __init__(self, operators):
        # The IR class holds multiple metadata from the forward pass:
        # a) ssa: a list of [op, in_versions, out_versions] recording the
        #    input and the output version of each operator, similar
        #    to a normal SSA form.
        # b) input_count: a dictionary specifying for each blob and
        #    each of its version, how many times it is used as input for another
        #    op.
        # c) frontier: maintaining the current versions of the blobs
        #    we are having in the workspace, after the execution of all the ops
        #    added to the IR so far. This is useful because if a gradient is
        #    trying to access an earlier version of a blob, we can sanity check
        #    that it is no longer there, and thus throw an error.
        # d) gradient_frontier: maps the names of blobs to its version that the
        #    gradient corresponds to.
        # e) gradient_generators: for each blob and each of its version, maps to
        #    a list of operators that generates its gradient together with the
        #    gradient name.
        self.ssa = []
        self.input_usages = defaultdict(lambda: defaultdict(list))
        self.frontier = defaultdict(int)
        self.gradient_frontier = {}
        self.gradient_generators = defaultdict(lambda: defaultdict(list))
        self.out_version_history = defaultdict(list)
        self.in_version_history = defaultdict(list)

        for op in operators:
            self.Play(op)

        self.SanityCheck(operators)

    def SanityCheck(self, operators):
        # Validate StopGradient usage by checking that StopGradient's output
        # is actually passed forward
        for op in operators:
            if op.type == 'StopGradient':
                if op.output[0] not in self.input_usages:
                    raise Exception("""StopGradient's output '{}' is orphan.
You typically want to specify same input and output for
StopGradient. Op:\n\n{}""".format(op.output[0], str(op)))

    def Play(self, op):
        """"Adds an op to the current IR, and update the internal states to
        reflect the blobs and versions after the execution of the op.
        """
        # For input, they are the current version in the dict.
        in_versions = {}
        for s in op.input:
            in_versions[s] = self.frontier[s]
            self.input_usages[s][self.frontier[s]].append(len(self.ssa))
            self.in_version_history[s].append((op, self.frontier[s]))
        # For output, they are the current version plus one. If this is a
        # newly created blob, its version starts with zero.
        out_versions = {}
        for s in op.output:
            if s in self.frontier:
                self.frontier[s] += 1
            out_versions[s] = self.frontier[s]
            self.out_version_history[s].append((op, self.frontier[s]))
        # Add to SSA for bookkeeping.
        self.ssa.append(OpSSA(op, in_versions, out_versions))

    def CheckGradientOperatorInput(
            self, grad_op_input, g_output, fwd_op_idx, locally_generated_blobs):
        """Checks if the gradient operators can be correctly carried out."""
        forward_op, in_versions, out_versions = self.ssa[fwd_op_idx]
        original_index = GetIndexFromGradientList(g_output, grad_op_input)

        # Functions to generate debug help for version-mismatches
        def versionMismatchInfoOut(name):
            s = "DEBUG HELP:\n"
            s += "Maybe you use same output blob twice for different ops?\n"
            s += "== Version history of blob [{}]\n".format(name)
            for (op, vers) in self.out_version_history[name]:
                s += "Version (out) {} <-- {}".format(vers, op)
                s += "\n"
            return s

        def versionMismatchInfoIn(name):
            s = "DEBUG HELP:\n"
            s += "Maybe the blob was overwritten by another op?\n"
            s += "== Version history of blob [{}]\n".format(name)
            for (op, vers) in self.in_version_history[name]:
                s += "version (in) {} <-- {}".format(vers, op)
                s += "\n"
            return s

        # If it is a dense or sparse gradient name, it should match the
        # version of the corresponding output.
        if original_index is not None:
            original_name = forward_op.output[original_index]
            if (out_versions[original_name] !=
                    self.gradient_frontier[original_name]):
                raise RuntimeError(
                    'Gradient name "%s" is expected to correspond '
                    'to version %d of "%s", but currently we have '
                    'version %d.\n\n' % (
                        grad_op_input, out_versions[original_name],
                        original_name,
                        self.gradient_frontier[original_name]) +
                    versionMismatchInfoOut(original_name))
        # If it is an output name, the current version should match the
        # version when the operator was run.
        elif grad_op_input in out_versions:
            if self.frontier[grad_op_input] != out_versions[grad_op_input]:
                raise RuntimeError(
                    'Gradient operator needs output "%s" at version'
                    ' %d, but currently we have version %d.\n\n' % (
                        grad_op_input, out_versions[grad_op_input],
                        self.frontier[grad_op_input]
                    ) + versionMismatchInfoOut(grad_op_input)
                )
        # If it is an input name, the current version should match the
        # version when the operator was run.
        elif grad_op_input in in_versions:
            if (self.frontier[grad_op_input] != in_versions[grad_op_input]):
                raise RuntimeError(
                    'Gradient operator needs input "%s" at version '
                    '%d, but currently we have version %d.\n\n' % (
                        grad_op_input, in_versions[grad_op_input],
                        self.frontier[grad_op_input]
                    ) + versionMismatchInfoIn(grad_op_input)
                )
        # If it is none of the above, it should be a blob that is
        # generated locally by one of the previous gradient operators.
        else:
            if grad_op_input not in locally_generated_blobs:
                raise RuntimeError(
                    'Blob name "%s" not in the scope of operator: '
                    '%s\nand is not generated by any of the local '
                    'gradient operators.' % (grad_op_input, str(forward_op))
                )

    def AppendSparseGenerators(self, sparse_generators):
        # merge indices and values generators for sparse gradients
        for name, input_generators in viewitems(sparse_generators):
            for version, generators in viewitems(input_generators):
                if len(generators) == 1:
                    # either indices or values are generated (but not both)
                    generator = generators[0]
                else:
                    # both indices and values are generated
                    assert(len(generators) == 2)
                    op1_i, idx1_i, op1_v, idx1_v, g1 = generators[0]
                    op2_i, idx2_i, op2_v, idx2_v, g2 = generators[1]
                    assert(g1 == g2)
                    assert(op1_i is None or op2_i is None)
                    assert(op1_v is None or op2_v is None)
                    assert(idx1_i == 0 or idx2_i == 0)
                    assert(idx1_v == 0 or idx2_v == 0)
                    generator = SparseGradGenMeta(
                        op1_i or op2_i, idx1_i + idx2_i,
                        op1_v or op2_v, idx1_v + idx2_v,
                        g1)
                self.gradient_generators[name][version].append(generator)

    def BuildGradientGenerators(  # NOQA
            self, fwd_op_idx, gradient_ops, g_output, g_input):
        """Updates gradient_generators and gradient_frontier"""
        forward_op, in_versions, out_versions = self.ssa[fwd_op_idx]
        locally_generated_blobs = []
        sparse_generators = defaultdict(lambda: defaultdict(list))

        for grad_op in gradient_ops:
            # (1) check that inputs are valid
            for s in grad_op.input:
                self.CheckGradientOperatorInput(
                    s, g_output, fwd_op_idx, locally_generated_blobs)

            # (2) add outputs to the locally generated blobs
            # If an output corresponds to the gradient of an input, we also
            # record it to gradient_generators
            locally_generated_blobs.extend([str(s) for s in grad_op.output])
            for i, output in enumerate(grad_op.output):
                input_index = GetIndexFromGradientList(g_input, output)
                if input_index is not None:
                    input_name = forward_op.input[input_index]
                    input_version = in_versions[input_name]
                    g = g_input[input_index]
                    if type(g) is GradientSlice:
                        # the output corresponds either to the indices or the
                        # values of the sparse gradient. In either case we
                        # create a (partial) SparseGradGenMeta. If necessary,
                        # we'll merge indices and values generators
                        # corresponding to the same gradient in step (3)
                        if g.indices == output:
                            m = SparseGradGenMeta(grad_op, i, None, 0, g)
                        else:
                            assert(g.values == output)
                            m = SparseGradGenMeta(None, 0, grad_op, i, g)
                        sparse_generators[input_name][input_version].append(m)
                    else:
                        self.gradient_generators[input_name][input_version] \
                            .append(GradGenMeta(
                                grad_op, i, g))

        # (3) merge indices and values generators for sparse gradients, and
        # add them to gradient_generators
        self.AppendSparseGenerators(sparse_generators)

        # (4) for ops (e.g., Add, Sum, Sub) which have gradient outputs directly
        # passed from inputs (not computed from gradient ops), we create an
        # GradGenMeta with None grad_op and idx so that the gradient_generators
        # knows where the gradients are coming from. This is needed for creating
        # Sum op to accumulate the gradients from multiple parents.
        for input_index, g in enumerate(g_input):
            input_name = forward_op.input[input_index]
            input_version = in_versions[input_name]
            if not g:
                continue
            if type(g) is GradientSlice:
                if str(g.indices) not in locally_generated_blobs and \
                        str(g.values) not in locally_generated_blobs:
                    self.gradient_generators[input_name][input_version].append(
                        SparseGradGenMeta(None, 0, None, 0, g))
            else:
                if str(g) not in locally_generated_blobs:
                    self.gradient_generators[input_name][input_version].append(
                        GradGenMeta(None, 0, g))

        # Finally, for the gradients specified in g_input, we update the
        # gradient frontier to reflect the input versions that the gradients
        # correspond to.
        for i, g in enumerate(g_input):
            if g is not None:
                input_name = forward_op.input[i]
                input_version = in_versions[input_name]
                self.gradient_frontier[input_name] = input_version

    def _GetSumOpOutputName(self, generator, input_name):
        def remove_suffix(s, suffix):
            if s.endswith(suffix):
                return s[:-len(suffix)]
            return s

        for g in generator:
            if type(g) is GradGenMeta:
                grad_op, idx, _ = g
                if grad_op:
                    return grad_op.output[idx]
            else:
                assert(type(g) is SparseGradGenMeta)
                op_i, idx_i, op_v, idx_v, _ = g
                if op_i:
                    return remove_suffix(op_i.output[idx_i], '_indices')
                if op_v:
                    return remove_suffix(op_v.output[idx_v], '_values')

        return input_name + '_grad'

    def _SetSumOpsDeviceOption(self, sum_ops, generators):
        # we already checked that device options are consistent so we can just
        # use the first one we find
        for generator in generators:
            grad_op = generator.grad_op if type(generator) is GradGenMeta \
                else generator.grad_op_values or generator.grad_op_indices
            if grad_op:
                if grad_op.HasField('device_option'):
                    for op in sum_ops:
                        op.device_option.CopyFrom(grad_op.device_option)
                break

    def _DisambiguateGradOpOutput(self, grad_op, idx, cnt):
        grad_op.output[idx] = (
            '_' + grad_op.output[idx] + '_autosplit_{}'.format(cnt))
        return grad_op.output[idx], cnt + 1

    def _CheckSumOpsConflict(self, out_base_name, g):
        if str(out_base_name) == str(g):
            # TODO not sure what this message really means
            raise RuntimeError(
                'The gradient output of empty gradient op can not '
                'be the same as the normal name of the current '
                'input gradient.')

    def _MakeDenseSumOps(self, generators, out_base_name):
        sum_op_input = []
        cnt = 0

        assert len(generators) > 1

        first_grad_op = True
        for generator in generators:
            grad_op, idx, g = generator
            assert(type(g) is not GradientSlice)
            if grad_op:
                if first_grad_op:
                    first_grad_op = False
                    out = grad_op.output[idx]
                else:
                    out, cnt = self._DisambiguateGradOpOutput(grad_op, idx, cnt)
                sum_op_input.append(out)
            else:
                self._CheckSumOpsConflict(out_base_name, g)
                sum_op_input.append(str(g))

        if out_base_name in sum_op_input:
            # Sum inplace mode works only for the first input
            # So we do a swap
            idx = sum_op_input.index(out_base_name)
            sum_op_input[0], sum_op_input[idx] = (
                sum_op_input[idx], sum_op_input[0]
            )
        sum_ops = [CreateOperator(
            "Sum",
            [BlobReference(x) for x in sum_op_input],
            BlobReference(out_base_name))]
        return sum_ops, out_base_name

    def _MakeSparseSumOps(self, generators, out_base_name):
        indices_concat_input = []
        values_concat_input = []
        cnt_i = 0
        cnt_v = 0

        for generator in generators:
            assert(type(generator) is SparseGradGenMeta)
            op_i, idx_i, op_v, idx_v, g = generator
            if op_i:
                out, cnt_i = self._DisambiguateGradOpOutput(op_i, idx_i, cnt_i)
                indices_concat_input.append(out)
            else:
                self._CheckSumOpsConflict(out_base_name, g.indices)
                indices_concat_input.append(g.indices)
            if op_v:
                out, cnt_v = self._DisambiguateGradOpOutput(op_v, idx_v, cnt_v)
                values_concat_input.append(out)
            else:
                self._CheckSumOpsConflict(out_base_name, g.values)
                values_concat_input.append(g.values)

        indices_concat_output = out_base_name + '_indices_concat'
        indices_concat_split = out_base_name + '_indices_concat_split'
        values_concat_output = out_base_name + '_values_concat'
        values_concat_split = out_base_name + '_values_concat_split'
        # Sum the given sparse representations by simply concatenating the
        # indices (resp. values) tensors together. We don't do any deduplication
        # of indices at this point. This will be done as needed before the
        # optimizer is called
        sum_ops = [
            CreateOperator(
                "Concat",
                [BlobReference(x) for x in indices_concat_input],
                [BlobReference(x) for x in
                    [indices_concat_output, indices_concat_split]],
                axis=0
            ),
            CreateOperator(
                "Concat",
                [BlobReference(x) for x in values_concat_input],
                [BlobReference(x) for x in
                    [values_concat_output, values_concat_split]],
                axis=0
            ),
        ]
        sum_op_output = GradientSlice(
            indices=indices_concat_output,
            values=values_concat_output,
        )
        return sum_ops, sum_op_output

    def _MakeSumOps(self, input_name, input_version):
        generators = self.gradient_generators[input_name][input_version]
        out_base_name = self._GetSumOpOutputName(generators, input_name)
        types = list(set(type(x) for x in generators))
        assert(len(types) == 1)
        if types[0] is GradGenMeta:
            sum_ops, g = self._MakeDenseSumOps(generators, out_base_name)
        else:
            assert(types[0] is SparseGradGenMeta)
            sum_ops, g = self._MakeSparseSumOps(generators, out_base_name)
        self._SetSumOpsDeviceOption(sum_ops, generators)
        return sum_ops, g

    def _VerifyGradientGenerators(self, generator):
        # (1) check if all gradients are of the same type. Aggregating a mix of
        # sparse and dense gradients is not supported yet
        if len({type(g) for g in generator}) > 1:
            raise RuntimeError(
                'Automatic aggregation of a mix of sparse and dense gradients '
                'is not supported yet')

        # If for all the operators that used the operator, none or only one
        # produced the gradient, then no additional sum needs to be carried
        # out.
        if len(generator) < 2:
            return False

        all_gradient_names = []
        all_device_options = []
        for g in generator:
            if type(g) is GradGenMeta:
                if g.grad_op:
                    all_gradient_names.append(g.gradient)
                    all_device_options.append(g.grad_op.device_option)
            else:
                assert(type(g) is SparseGradGenMeta)
                if g.grad_op_indices:
                    all_device_options.append(g.grad_op_indices.device_option)
                if g.grad_op_values:
                    all_device_options.append(g.grad_op_values.device_option)
                    all_gradient_names.append(g.gradient.values)

        # Check if all grad op device options are the same.
        if len(all_device_options) >= 2 and not all(
                d == all_device_options[0] for d in all_device_options[1:]):
            raise RuntimeError('Unexpected behavior: not all grad ops'
                               'have the same device option.')
        return True

    def DoGradientAccumulation(self, fwd_op_idx):
        """For each input name in the forward op, check if we will need to
        add gradient accumulation. If so, do gradient accumulation and return
        the list of gradient operators.

        The criteria for doing gradient accumulation is:
        (1) the specific input version has been used by multiple operators.
        (2) the current fwd_op_idx is the first to use that input, i.e. in the
            backward pass, is the last to optionally generate the gradient for
            the op.
        (3) For the operators that used the input, their gradient operators
            have generated more than 1 gradient.

        When accumulating operators, our current solution is to rename all the
        created gradients with an internal intermediate name, and then add a
        Sum() operator that adds up all the gradients. This may use more memory
        due to intermediate storage, but is usually the fastest approach as one
        can do one single sum for multiple intermediate gradients.
        """
        forward_op, in_versions, out_versions = self.ssa[fwd_op_idx]
        additional_sum_ops = []
        grad_map = {}
        for _i, input_name in enumerate(set(forward_op.input)):
            input_version = in_versions[input_name]
            input_usage = self.input_usages[input_name][input_version]
            if (len(input_usage) <= 1 or fwd_op_idx != input_usage[0]):
                # We do not need to do gradient accumulation yet.
                continue
            generator = self.gradient_generators[input_name][input_version]
            try:
                if not self._VerifyGradientGenerators(generator):
                    continue
            except RuntimeError as err:
                raise RuntimeError(
                    "Gradients for param ''{}'' failed to verify: {}".format(
                        input_name,
                        err
                    )
                )

            # Finally, let's create the sum operator.
            sum_ops, g = self._MakeSumOps(input_name, input_version)
            additional_sum_ops.extend(sum_ops)
            grad_map[input_name] = g
        return additional_sum_ops, grad_map

    def _AppendAutoGradGenerator(self, y, grad, autograd_op):
        # Gradient here is not sparse  as it was generated by
        # a ConstantFill operator. Autogeneration for sparse gradients is
        # not supported
        generator = GradGenMeta(
            autograd_op, 0 if autograd_op else None, str(grad))

        self.gradient_generators[str(y)][self.frontier[str(y)]].append(
            generator)


    def _GetInitGradients(self, ys):
        input_to_grad = {}
        gradient_ops = []
        for y, g in viewitems(ys):
            autograd_op = None
            if g is None:
                autograd_op = CreateOperator(
                    "ConstantFill", [y], [str(y) + "_autogen_grad"],
                    value=1.0)
                gradient_ops.append(autograd_op)
                g = autograd_op.output[0]
            # Since the C++ gradient registry does not have notion of
            # NameScopes, we will convert all references to strings.
            input_to_grad[str(y)] = (
                GradientSlice(str(g[0]), str(g[1]))
                if isinstance(g, GradientSlice) else str(g))
            # Autogenerated gradients are assumed to be provided for the last
            # input version
            if autograd_op is not None:
                self._AppendAutoGradGenerator(y, g, autograd_op)

        return input_to_grad, gradient_ops

    def _GenerateGradientsForForwardOp(
            self, forward_op_idx, input_to_grad):
        new_input_to_grad = {}
        gradient_ops = []
        forward_op, in_versions, out_versions = self.ssa[forward_op_idx]
        g_output = list(
            input_to_grad.get(name, None) for name in forward_op.output)

        if not all(g is None for g in g_output) or (
                forward_op.type == "ZeroGradient"):
            gradient_ops, g_input = GradientRegistry.GetGradientForOp(
                forward_op, g_output)
            # Check if the gradient operators are legal, and update
            # gradient_generators and gradient_frontier
            self.BuildGradientGenerators(
                forward_op_idx, gradient_ops, g_output, g_input)
            # Record the gradient map to all_input_to_grad.
            for name, grad in zip(forward_op.input, g_input):
                # Do not overwrite an existing gradient with a None
                # unless the input is also an output of the op, since
                # we update the blob version when blob is output of an
                # operator.
                if grad is not None or \
                    name not in input_to_grad or \
                        name in list(forward_op.output):
                    new_input_to_grad[name] = grad

        return new_input_to_grad, gradient_ops

    def GetBackwardPass(self, ys):
        """Gets the backward pass that computes the derivatives of given blobs.

        Inputs:
          ys: a list or a dictionary specifying what blobs we want to compute
              derivatives of. If the input is a list, we will automatically
              generate their gradients with all-one values; if the input is a
              dictionary, for any dictionary entries that are not None, we will
              take the corresponding blobs as their gradients; for all those
              that are None, we will auto-fill them with 1.
        """
        if isinstance(ys, list):
            ys = dict((y, None) for y in ys)
        elif not isinstance(ys, dict):
            raise TypeError("ys should either be a list or a dict.")

        # Set the gradient frontier with the initialized external
        # gradients.
        for y in viewkeys(ys):
            self.gradient_frontier[y] = self.frontier[y]
            self.input_usages[str(y)][self.frontier[str(y)]].append(
                len(self.ssa))

        all_input_to_grad, all_gradient_ops = self._GetInitGradients(ys)

        # (2) Now, after having the virtual play above, we now play the ops
        # backwards, creating the gradients along the path. Note that although
        # we are playing it backwards, we cannot refer to variables that are
        # at a version older than current_versions because it is already been
        # overwritten.
        for forward_op_idx in reversed(range(len(self.ssa))):
            input_to_grad, gradient_ops = self._GenerateGradientsForForwardOp(
                forward_op_idx, all_input_to_grad)
            all_input_to_grad.update(input_to_grad)
            all_gradient_ops += gradient_ops

            # If there are multiple use blobs, do gradient accumulation.
            additional_sum_ops, grad_map = self.DoGradientAccumulation(
                forward_op_idx)
            # This line is so that if in an accumulation some of the operators
            # have not produced gradients, they still do not overwrite the
            # general all_input_to_grad map.
            all_input_to_grad.update(grad_map)
            all_gradient_ops += additional_sum_ops

        # (3) Post-processing.
        # After we have done computation for each op, we now have the gradient
        # operators ready. For the output map, we will convert everything to
        # BlobReferences for easier handling in python.
        all_input_to_grad_out = {}
        for key, val in viewitems(all_input_to_grad):
            if val is not None:
                if (isinstance(val, string_types) or
                        isinstance(val, binary_type)):
                    grad_out = BlobReference(val)
                else:
                    grad_out = GradientSlice(BlobReference(val[0]),
                                             BlobReference(val[1]))
                all_input_to_grad_out[BlobReference(key)] = grad_out
        return all_gradient_ops, all_input_to_grad_out


class GradientRegistry(object):
    """GradientRegistry holds the mapping from operators to their gradients."""
    gradient_registry_ = {}

    @classmethod
    def RegisterGradient(cls, op_type):
        """A decorator for registering gradient mappings."""

        def Wrapper(func):
            cls.gradient_registry_[op_type] = func
            return func

        return Wrapper

    @classmethod
    def _GetGradientForOpCC(cls, op_def, g_output):
        # TODO(tulloch) - Propagate GradientWrapper up through the stack.
        def from_untyped(grad):
            if grad is None:
                w = C.GradientWrapper()
                assert w.is_empty()
                return w
            try:
                (indices, values) = grad
                w = C.GradientWrapper()
                w.indices = indices
                w.values = values
                assert w.is_sparse()
                return w
            except ValueError:
                w = C.GradientWrapper()
                w.dense = grad
                assert w.is_dense()
                return w

        g_output = [from_untyped(grad) for grad in g_output]
        grad_defs_str, g_input = C.get_gradient_defs(
            op_def.SerializeToString(), g_output)

        def to_untyped(grad_wrapper):
            if grad_wrapper.is_empty():
                return None
            if grad_wrapper.is_sparse():
                return GradientSlice(grad_wrapper.indices, grad_wrapper.values)
            assert grad_wrapper.is_dense()
            return grad_wrapper.dense

        g_input = [to_untyped(grad_wrapper) for grad_wrapper in g_input]
        grad_defs = []
        for grad_def_str in grad_defs_str:
            grad_def = caffe2_pb2.OperatorDef()
            grad_def.ParseFromString(grad_def_str)
            grad_defs.append(grad_def)
        return grad_defs, g_input

    @classmethod
    def GetGradientForOp(cls, op, g_output):
        try:
            gradient_ops, g_input = cls._GetGradientForOpCC(op, g_output)
        except Exception as e:
            # Not supported in C++; will try python registration next.
            if op.type in cls.gradient_registry_:
                gradient_ops, g_input = cls.gradient_registry_[op.type](
                    op, g_output
                )
            else:
                raise Exception(
                    "Exception when creating the gradient for [{}]: {}.".
                    format(op.type, e)
                )

        if gradient_ops is None:
            return [], g_input
        if type(gradient_ops) is not list:
            gradient_ops = [gradient_ops]
        return gradient_ops, g_input

    @classmethod
    def GetBackwardPass(cls, operators, ys, ys_generate_gradient=False):
        """Gets the backward pass for the list of operators.

        Args:
            operators: a list of operators constituting the forward pass.
            ys: a list or a dictionary specifying what blobs we want to compute
                derivatives of. If the input is a list, we will automatically
                generate their gradients with all-one values; if the input is a
                dictionary, for any dictionary entries that are not None, we'll
                take the corresponding blobs as their gradients; for all those
                that are None, we will auto-fill them with 1.
        Returns:
            gradient_ops: a list of gradient operators to run.
            all_input_to_grads: a map from input to their corresponding
                gradients.
        """
        ir = IR(operators)
        return ir.GetBackwardPass(ys)


def get_ssa(net, blob_versions=None):
    """
    Given a net, return a structure containing the version of each input and
    output blob used by each operator.

    Args:
        net:            either a Net or a NetDef
        blob_versions:  (optional) map with current version number for given
                        blob names. If not provided or blob not found, start
                        from version 0.
    Returns:
        Tuple (ssa, blob_versions)
        ssa:            list of tuples (versioned_inputs, versioned_outputs)
                        for each op in the net. A versioned input is a tuple
                        (blob_name, version).
        blob_versions:  updated map with latest version of each blob found in
                        the net.
    """
    proto = net.Proto() if isinstance(net, Net) else net
    assert isinstance(proto, caffe2_pb2.NetDef)
    if blob_versions is None:
        blob_versions = {}
    if isinstance(net, list):
        return [get_ssa(n, blob_versions) for n in net], blob_versions
    for i in proto.external_input:
        if i not in blob_versions:
            blob_versions[str(i)] = 0
    ssa = []
    for op in proto.op:
        if not proto.external_input:
            for i in op.input:
                if i not in blob_versions:
                    blob_versions[i] = 0
        inputs = [(str(i), blob_versions.get(str(i), 0)) for i in op.input]
        for o in op.output:
            blob_versions[str(o)] = blob_versions.get(str(o), 0) + 1
        outputs = [(str(o), blob_versions[str(o)]) for o in op.output]
        ssa.append((inputs, outputs))
    return ssa, blob_versions


def get_undefined_blobs(ssa):
    """
    Given a ssa in the format produced by get_ssa(), return a set of blobs that
    are used before they are defined, which corresponds to inputs at version 0.
    """
    undef_blobs = set()
    for inputs, _outputs in ssa:
        undef_blobs |= set(name for (name, ver) in inputs if ver == 0)
    return undef_blobs


def get_output_producers(ssa):
    """
    Given a ssa in the format produced by get_ssa(), returns a map from
    versioned blob into the operator index that produces that version of
    the blob. A versioned blob is a tuple (blob_name, version).
    """
    producers = {}
    for i, (_inputs, outputs) in enumerate(ssa):
        for o in outputs:
            producers[o] = i
    return producers


def get_op_ids_in_path(ssa, blob_versions, inputs, outputs):
    """
    Given a ssa and blob_versions as produced by get_ssa(), returns the list
    of op indices that are necessary in order to generate the blobs in
    `outputs`, given blobs in `inputs`.
    Consider that the `inputs` are given in their latest version.
    """
    inputs_set = set((str(i), blob_versions[str(i)]) for i in inputs)
    producers = get_output_producers(ssa)
    queue = [(str(o), blob_versions[str(o)]) for o in outputs]
    used_op_ids = set()
    while len(queue) > 0:
        o = queue.pop()
        if (o not in inputs_set) and (o in producers):
            op_id = producers[o]
            if op_id not in used_op_ids:
                used_op_ids |= {op_id}
                inputs, _ = ssa[op_id]
                queue.extend(inputs)
    return sorted(used_op_ids)


def recurrent_network_op_remap(op, prefix, blob_remap):
    """
    Parameters
    ----------
    op : Caffe2 operator (RecurrentNetworkOp or RecurrentNetworkGradientOp).
    prefix: this argument is not used in this function, just for legacy support.
    blob_remap : Dictionary that represents the map from old blob name to new.

    Updates blob names in arguments of RecurrentNetworkOp and
    RecurrentNetworkGradientOp to conform to cloned input and output of both
    operators and also makes sure names of locally generated blobs in arguments
    have the same prefix as the input and output of the operators.
    """

    def get_remapped_str(blob_str):
        if isinstance(blob_str, binary_type):
            blob_str = blob_str.decode('utf-8')
        return blob_remap.get(blob_str, blob_str).encode('utf-8')

    for argument in op.arg:
        if len(argument.strings) > 0:
            for i in range(len(argument.strings)):
                argument.strings[i] = get_remapped_str(argument.strings[i])
        elif argument.name == 'timestep':
            argument.s = get_remapped_str(argument.s)
        elif argument.name.endswith('step_net'):
            # argument is a proto
            remap_proto(argument, blob_remap)


DEFAULT_REMAP_FUNCS = {
    'RecurrentNetwork': recurrent_network_op_remap,
    'RecurrentNetworkGradient': recurrent_network_op_remap,
}


def remap_proto(argument, blob_remap):
    proto = caffe2_pb2.NetDef()
    protobuftx.Merge(argument.s.decode('utf-8'), proto)
    subnet = Net(proto)

    cloned_sub_net = subnet.Clone(
        'cloned_sub_net',
        blob_remap,
    )

    argument.s = str(cloned_sub_net.Proto()).encode('utf-8')


def clone_and_bind_net(net, name, prefix, blob_remap=None, inputs=None,
                       keep_schema=True):
    """
    Clone the given Net, binding its input schema to the given `inputs` record.
    Blob names defined by the net are prepended with the given `prefix`.

    Args:
        net:        the net to clone
        name:       the name of the new net
        prefix:     the prefix to append to local blobs
        blob_remap: (optional) dict with additional blob name remapping.
        inputs:     (optional) input record that will provide actual input
                    values for the cloned net. Must be compatible with the
                    net's input schema or be a strict superset of it
        keep_schema: by default (True), the original schema will be kept and
                     remapped accordingly. otherwise, the schema will be set as
                     inputs or left empty if inputs is not given.
    Returns:
        Tuple (cloned_net, blob_remap)
        clone_net:  the cloned Net
        blob_remap: a map from original blob names into remapped blob names
    """
    from caffe2.python import schema
    assert isinstance(net, Net)
    if blob_remap is None:
        blob_remap = {}
    if inputs is not None:
        assert isinstance(inputs, schema.Field)
        original = net.input_record()
        assert original is not None
        # TODO(azzolini): improve schema type checking
        diff = set(original.field_names()) - set(inputs.field_names())
        assert len(diff) == 0, (
            "Schemas don't match, extra fields {diff} found in the net {name}. "
            "original: {original}; inputs: {inputs}"
            .format(
                diff=diff, name=net.Name(), original=original.field_names(),
                inputs=inputs.field_names()
            )
        )
        original_mapping = dict(zip(original.field_names(),
                                    original.field_blobs()))
        for fn, fb in zip(inputs.field_names(), inputs.field_blobs()):
            if fn in original_mapping:
                blob_remap[str(original_mapping[fn])] = str(fb)
    proto = net.Proto()
    ssa, blob_versions = get_ssa(proto)
    undef_blobs = get_undefined_blobs(ssa)

    for blob in viewkeys(blob_versions):
        if blob in blob_remap:
            continue
        elif blob in undef_blobs:
            blob_remap[blob] = blob
        else:
            blob_remap[blob] = prefix + blob
    cloned_net = net.Clone(name, blob_remap, keep_schema=keep_schema)
    if not keep_schema and inputs:
        cloned_net.set_input_record(inputs)
    return cloned_net, blob_remap


def _get_blob_ref(blob_name_or_ref):
    return (
        blob_name_or_ref if isinstance(input, BlobReference)
        else BlobReference(blob_name_or_ref)
    )


def _recover_record_by_prefix(names, prefix=''):
    """
    Tries to recover record by taking a subset of blob names with
    a given prefix name and interpreting them as schema column names
    """
    from caffe2.python import schema
    column_names = [name[len(prefix):] for name in names
                    if name.startswith(prefix)]
    if not column_names:
        return None
    return schema.from_column_list(
        column_names,
        col_blobs=[_get_blob_ref(prefix + name) for name in column_names])


class Net(object):
    _net_names_used = set()
    operator_registry_ = {}

    @staticmethod
    def current_prefix():
        from caffe2.python.net_builder import NetBuilder
        builder = NetBuilder.current(required=False)
        return builder.name if builder else ''

    @staticmethod
    def _get_next_net_name(basename):
        name = basename = '/'.join(
            x for x in [Net.current_prefix(), basename] if x
        )
        next_idx = 1
        while name in Net._net_names_used:
            name = basename + '_' + str(next_idx)
            next_idx += 1
        Net._net_names_used |= set([name])
        return name

    def __init__(self, name_or_proto):
        """
        Create a Net.
        Args:
            name_or_proto:  If a NetDef is provided, clone it. Otherwise,
                            create an empty net with the given name.
        """
        self._input_record = None
        self._output_record = None
        # Register blobs so that it's guaranteed that different calls to
        # NextBlob/NextScopedBlob always return blobs with different names
        self._registered_blob_names = set()
        self._recreate_lookup_tables = False
        self._op_outputs = set()
        self._external_input_map = set()
        self._attr_dict = defaultdict(list)
        if type(name_or_proto) is caffe2_pb2.NetDef:
            proto = name_or_proto
            # We rae initializing a network by a NetDef. In this case, we will
            # initialize our network with the given netdef.
            self._net = caffe2_pb2.NetDef()
            self._net.CopyFrom(proto)

            existing_outputs = [list(op.output) for op in self._net.op]

            self._external_input_map.update(list(self._net.external_input))

            # Set the next name index properly.
            existing_names = set(
                sum(
                    [list(op.input) for op in self._net.op], []
                ) + sum(
                    existing_outputs, []
                )
            )
            for outs in existing_outputs:
                self._op_outputs.update(outs)

            prefix_len = len(self._net.name + '_blob_')
            autogen_indices = []
            for s in existing_names:
                if s.startswith(self._net.name + '_blob_'):
                    try:
                        autogen_indices.append(int(s[prefix_len]))
                    except ValueError:
                        pass
            if len(autogen_indices):
                self._next_name_index = max(autogen_indices) + 1
            else:
                self._next_name_index = 0
            name = self._net.name
        else:
            name = name_or_proto
            self._net = caffe2_pb2.NetDef()
            self._next_name_index = 0

        # make sure that this net name hasn't been used before
        self._net.name = Net._get_next_net_name(name)

    def AppendNet(self, net):
        assert isinstance(net, Net)
        for i in net.Proto().external_input:
            if (
                i not in self.Proto().external_input and
                i not in self._op_outputs
            ):
                self.Proto().external_input.append(i)

        self.Proto().external_output.extend(
            [
                o for o in net.Proto().external_output
                if o not in self.Proto().external_output
            ]
        )
        self._ExtendOps(net.Proto().op)
        return self

    def LogInfo(self, *msg_or_blobs):
        for msg_or_blob in msg_or_blobs:
            if not isinstance(msg_or_blob, BlobReference):
                blob = self.GivenTensorStringFill(
                    [], self.NextName('log'),
                    shape=[], values=[msg_or_blob])
            else:
                blob = msg_or_blob
            self.Print(blob, [])

    def add_attribute(self, name, obj):
        """
        Add `obj` to the list of attributes in this net under the given `name`.
        Attributes are user-defined objects and have no pre-defined semantics.
        """
        self._attr_dict[name].append(obj)

    def get_attributes(self, name):
        """
        Returns the list of attributes in this net for a given `name`.
        Attributes are user-defined objects added with `add_attribute'.
        """
        return self._attr_dict.get(name, [])

    def set_rand_seed(self, seed=100, sequence_seed=True, seed_on_op_def=False):
        """
        Adds a random seed to each op in the net.
        If sequence_seed is set, the i-th op has rand_seed=`seed + i`
        If seed_on_op_def is set, the op rand_seed=hash(str(op))
        sequence_seed and seed_on_op_def cannot be both set to True.
        """
        assert not (sequence_seed and seed_on_op_def), (
            'sequence_seed and seed_on_op_def cannot be both set to True.')
        for i, op in enumerate(self.Proto().op):
            if sequence_seed:
                curr_seed = seed + i
            elif seed_on_op_def:
                curr_seed = hash(str(op) + str(seed)) % np.iinfo(np.uint32).max
            else:
                curr_seed = seed
            op.device_option.random_seed = curr_seed

    def Name(self):
        return self._net.name

    def __str__(self):
        return self.Name()

    def Const(self, array, blob_out=None, dtype=None):
        if isinstance(array, bool):
            return self.ConstantFill(
                [],
                blob_out or 1,
                dtype=DataType.BOOL,
                value=array)

        if dtype is None:
            array = np.array(array)
        else:
            array = np.array(array, dtype=dtype)

        def do_set(operator):
            return operator(
                [],
                blob_out or 1,
                shape=array.shape,
                values=array.flatten().tolist())

        if array.dtype == np.int32:
            return do_set(self.GivenTensorIntFill)
        elif array.dtype == np.int64:
            return do_set(self.GivenTensorInt64Fill)
        elif array.dtype == np.str:
            return do_set(self.GivenTensorStringFill)
        else:
            return do_set(self.GivenTensorFill)

    def BlobIsDefined(self, blob):
        """
        Returns true if the given BlobReference is produced as output of
        an operator in this net, or if it is provided as an external input.
        """
        if self._recreate_lookup_tables:
            self._RecreateLookupTables()
        name = str(blob)
        return (name in self._op_outputs) or (name in self._external_input_map)

    def UsesBlob(self, blob):
        """
        Returns true iff the given BlobReference is used by any operator
        or this net, or if it is one of the external inputs of the net.
        """
        blob_name = str(blob)
        for op in self._net.op:
            for input in op.input:
                if input == blob_name:
                    return True
        return blob_name in self._external_input_map

    def GetBlobRef(self, blob_name):
        """
        Given the name of a blob produced by this net, return a BlobReference
        to it. If the blob is not produced by any op in this net,
        raises KeyError.
        """
        blob_name = str(blob_name)
        if not self.BlobIsDefined(blob_name):
            raise KeyError('Net does not define blob %s' % blob_name)
        return BlobReference(blob_name, self)

    def Clone(
        self,
        name,
        blob_remap=None,
        op_id_mask=None,
        remap_funcs=None,
        keep_schema=True
    ):
        """
        Clone this net.
        Args:
            name:        name of the cloned net
            blob_remap:  optional map with list of blob names to replace
            op_id_mask:  optional list of operator indices to include in
                         the cloned net. If not provided, all ops are included.
        """
        orig_remap_funcs = {} if remap_funcs is None else remap_funcs
        # by default we want to put RecurrentNetworkOp and
        # RecurrentNetworkGradientOp into remap_funcs, as these two operators
        # also take blobs and proto into the arguments.
        remap_funcs = DEFAULT_REMAP_FUNCS.copy()
        remap_funcs.update(orig_remap_funcs)
        proto = self._net
        new_proto = caffe2_pb2.NetDef()
        new_proto.CopyFrom(proto)
        new_proto.name = name

        if blob_remap is None:
            blob_remap = {}
        if op_id_mask is None:
            op_id_mask = list(range(0, len(proto.op)))

        def get_remapped_str(blob):
            blob_str = str(blob)
            return str(blob_remap.get(blob_str, blob_str))

        def remap_list(proto_list):
            new_list = [get_remapped_str(b) for b in proto_list]
            del proto_list[:]
            proto_list.extend(new_list)

        def remap_op(op):
            new_op = caffe2_pb2.OperatorDef()
            new_op.CopyFrom(op)
            remap_list(new_op.input)
            remap_list(new_op.output)
            if new_op.type in remap_funcs:
                remap_funcs[new_op.type](
                    new_op,
                    (name + '/') if name else '',
                    blob_remap,
                )
            return new_op

        del new_proto.op[:]
        new_proto.op.extend([remap_op(proto.op[op_id]) for op_id in op_id_mask])
        remap_list(new_proto.external_input)
        remap_list(new_proto.external_output)
        new_net = Net(new_proto)

        if keep_schema:
            from caffe2.python import schema
            if self._input_record:
                new_net._input_record = schema.from_blob_list(
                    self._input_record,
                    [
                        BlobReference(get_remapped_str(blob), net=new_net)
                        for blob in self._input_record.field_blobs()
                    ],
                )
            if self._output_record:
                new_net._output_record = schema.from_blob_list(
                    self._output_record,
                    [
                        BlobReference(get_remapped_str(blob), net=new_net)
                        for blob in self._output_record.field_blobs()
                    ],
                )

        new_net._attr_dict.update(self._attr_dict)
        return new_net

    def ClonePartial(self, name, inputs, outputs, remap_funcs=None):
        """
        Clone this net, including only ops that are necessary in order to
        compute `outputs` given `inputs`. Return references to the cloned
        outputs. Internal blobs (blobs that are produced and consumed inside
        the net but not used as outputs) will be remapped to avoid name
        conflict.

        Args:
            name:    the name of the cloned net
            inputs:  map where the keys correspond to BlobReferences in the
                     original net, and the values correspond to external inputs
                     in the partially cloned net. If `inputs` is a list, don't
                     remap input names.
            outputs: outputs to be produced by the cloned net.

        Returns:
            Tuple (new_net, new_outputs)
                new_net:       a new Net object.
                new_outputs:   list of BlobReferences corresponding to the
                               outputs produced by new_net.
        """
        input_is_pair_list = isinstance(inputs, list) and all(
            isinstance(i, tuple) and len(i) == 2 for i in inputs)
        inputs = (
            inputs if isinstance(inputs, (dict, OrderedDict)) else
            OrderedDict(inputs) if input_is_pair_list else
            OrderedDict(zip(inputs, inputs)))
        for output in outputs:
            assert self.BlobIsDefined(output)
        input_names = {str(k): str(v) for k, v in viewitems(inputs)}
        output_names = [str(o) for o in outputs]
        proto = self._net
        blob_versions = {str(i): 0 for i in inputs}
        ssa, blob_versions = get_ssa(proto, blob_versions)
        used_op_ids = get_op_ids_in_path(ssa, blob_versions, inputs, outputs)
        disallowed_op_ids = get_op_ids_in_path(ssa, blob_versions, [], inputs)
        assert len(set(used_op_ids) & set(disallowed_op_ids)) == 0, (
            'Cannot partially clone net: some of the ops required would ' +
            'generate the given input.')

        sub_ssa = [op for i, op in enumerate(ssa) if i in used_op_ids]
        undef_blobs = get_undefined_blobs(sub_ssa) - set(viewkeys(input_names))
        prefix = (name + '/') if name else ''

        def remap(blob_name):
            if blob_name in input_names:
                return input_names[blob_name]
            elif blob_name in undef_blobs:
                return blob_name
            else:
                return prefix + blob_name

        blob_mapping = {b: remap(b) for b in viewkeys(blob_versions)}
        new_net = self.Clone(name, blob_mapping, used_op_ids, remap_funcs)
        new_in = [
            blob_mapping[i] for i in viewkeys(input_names)] + list(undef_blobs)
        new_out = [blob_mapping[o] for o in output_names]
        del new_net.Proto().external_input[:]
        new_net.Proto().external_input.extend(new_in)
        new_net._external_input_map = set(list(new_in))
        del new_net.Proto().external_output[:]
        new_net.Proto().external_output.extend(new_out)
        return new_net, [new_net.GetBlobRef(o) for o in new_out]

    def Proto(self):
        self._InvalidateLookupTables()
        return self._net

    def PopulateProtoWithFileName(self):
        net_tb = workspace.operator_tracebacks.get(self.Name(), None)
        if net_tb is not None:
            for idx, op in enumerate(self.Proto().op):
                if idx in net_tb:
                    op.name = ':'.join(map(str, net_tb[idx][0]))

    def NextScopedBlob(self, prefix='unnamed'):
        """Return the blob that has not been defined or registered in the
        current net. It returns `ScopedBlobReference(prefix)`, if it's valid,
        otherwise `ScopedBlobReference(prefix) + '_auto_' + ?`. Different calls
        is guaranteed to return blob with different names.
        """
        output_blob_base = ScopedName(prefix)
        return self.NextBlob(output_blob_base)

    def NextBlob(self, prefix='unnamed'):
        """Return the blob that has not been defined or registered in the
        current net. It returns `BlobReference(prefix)`, if it's valid,
        otherwise `BlobReference(prefix) + '_auto_' + ?`. Different calls
        is guaranteed to return blob with different names."""
        output_blob_base = BlobReference(prefix)
        output_blob = output_blob_base
        index = 0
        while str(output_blob) in self._registered_blob_names or (
                self.BlobIsDefined(output_blob)):
            output_blob = output_blob_base + '_auto_' + str(index)
            index += 1

        self._registered_blob_names.add(str(output_blob))
        return output_blob

    def NextName(self, prefix=None, output_id=None):
        """Returns the next name to be used, if you do not want to explicitly
        name your blob. [Deprecated, use NextBlob, NextScopedBlob instead]"""
        if prefix:
            output_name_base = self._net.name + '/' + prefix
            output_name = output_name_base
            if output_id is not None:
                output_name += ':' + str(output_id)
            index = 2
            while self.BlobIsDefined(str(ScopedBlobReference(output_name))):
                output_name = output_name_base + '_' + str(index)
                if output_id is not None:
                    output_name += ':' + str(output_id)
                index += 1
        else:
            output_name = self._net.name + '_blob_' + str(self._next_name_index)
            self._next_name_index += 1
        return str(output_name)

    def _ExtendOps(self, new_ops):
        self._net.op.extend(new_ops)
        for op in new_ops:
            self._op_outputs.update([text_type(o) for o in op.output])

    def _CheckLookupTables(self):
        '''
        Called from unit tests to validate the internal lookup tables
        match the protobuf contents.
        '''
        test_op_outputs = set()
        for op in self._net.op:
            for o in op.output:
                test_op_outputs.add(o)

        test_external_inp = set()
        for inp in self._net.external_input:
            test_external_inp.add(inp)

        assert test_op_outputs.difference(self._op_outputs) == set()
        assert test_external_inp.difference(self._external_input_map) == set()

    def _InvalidateLookupTables(self):
        self._recreate_lookup_tables = True

    def _RecreateLookupTables(self):
        self._op_outputs = set()
        for op in self._net.op:
            for o in op.output:
                self._op_outputs.add(o)

        self._external_input_map = set()
        for inp in self._net.external_input:
            self._external_input_map.add(inp)

        self._recreate_lookup_tables = False

    def AddGradientOperators(self, ys, skip=0):
        """Add the gradient for operators in the net.

        Inputs:
          ys: a list or a dictionary specifying what blobs we want to compute
              derivatives of. If the input is a list, we will automatically
              generate their gradients with all-one values; if the input is a
              dictionary, for any dictionary entries that are not None, we will
              take the corresponding blobs as their gradients; for all those
              that are None, we will auto-fill them with 1.
          skip: skips the first n operators. This is provided mainly because a
              lot of nets may use the first few operators for data generation
              like stuff which really do not need to have gradients.

        Outputs:
          returns a map from the blob name in the input network to a blob
          containing gradient or a GradientSlice in case of sparse gradient

        Currently, this is hard-coded for float operators if there are branches
        (i.e. a blob is used as input to multiple operators). This is because
        the gradient accumulation (Sum) is float only right now.
        """

        grad_ops, input_to_grad = GradientRegistry.GetBackwardPass(
            self._net.op[skip:], ys)
        # Check if in immediate mode: the grad_ops are actually being produced
        # by C++ and bypasses the CreateOperator() call, so in immediate mode
        # we will have to explicitly run them.
        if workspace.IsImmediate():
            for op in grad_ops:
                workspace.RunOperatorImmediate(op)
        self._ExtendOps(grad_ops)
        return input_to_grad

    def AddExternalInput(self, *inputs):
        assert len(inputs) > 0
        refs = []
        for input in inputs:
            input_name = str(input)
            assert str(input) not in self._external_input_map, (
                'Net already contains an input named %s' % input_name)
        for input in inputs:
            input_name = str(input)
            self._net.external_input.extend([input_name])
            self._external_input_map.update([input_name])
            refs.append(_get_blob_ref(input_name))

        return refs[0] if len(refs) == 1 else refs

    def AddExternalOutput(self, *outputs):
        for output in outputs:
            assert isinstance(output, BlobReference)
            assert self.BlobIsDefined(output)
        for output in outputs:
            self.Proto().external_output.extend([str(output)])

    def AddScopedExternalInputs(self, *inputs):
        res = self.AddExternalInput(
            * [ScopedBlobReference(b) for b in inputs]
        )
        if not isinstance(res, list):
            res = [res]
        return res

    def AddScopedExternalOutputs(self, *outputs):
        return self.AddExternalOutput(
            * [ScopedBlobReference(b) for b in outputs]
        )

    @property
    def external_inputs(self):
        return [_get_blob_ref(x) for x in self._net.external_input]

    @property
    def external_outputs(self):
        return [_get_blob_ref(x) for x in self._net.external_output]

    def set_input_record(self, input_record):
        from caffe2.python import schema
        assert self._input_record is None or (input_record.has_blobs() and
            set(input_record.field_blobs()) ==
            set(self._input_record.field_blobs())), (
            'Input schema cannot be reset')
        if not input_record.has_blobs():
            with NameScope(self.Name()):
                self._input_record = schema.NewRecord(self, input_record)
        else:
            self._input_record = input_record
            for blob in input_record.field_blobs():
                if blob not in self.external_inputs:
                    self.AddExternalInput(blob)
        return self._input_record

    def recover_input_record_by_prefix(self, prefix):
        """
        Tries to recover input record by taking a subset of external_inputs with
        a given prefix name and interpreting them as schema column names
        """
        record = _recover_record_by_prefix(self._net.external_input, prefix)
        if record:
            self.set_input_record(record)

    def set_output_record(self, record):
        assert self._output_record is None or (record.has_blobs() and
            set(record.field_blobs()) ==
            set(self._output_record.field_blobs())), (
            'Output schema cannot be reset')
        for blob in record.field_blobs():
            assert self.BlobIsDefined(blob), "{} is not defined".format(blob)
        for blob in record.field_blobs():
            self.AddExternalOutput(blob)
        self._output_record = record

    def recover_output_record_by_prefix(self, prefix):
        """
        Tries to recover out record by taking a subset of external_outputs with
        a given prefix name and interpreting them as schema column names
        """
        record = _recover_record_by_prefix(self._net.external_output, prefix)
        if record:
            self.set_output_record(record)

    def AppendOutputRecordField(self, field_name, record):
        from caffe2.python import schema
        assert self._output_record is not None, (
            'Tried to append to missing output record'
        )
        for blob in record.field_blobs():
            assert self.BlobIsDefined(blob)
        for blob in record.field_blobs():
            self.AddExternalOutput(blob)
        self._output_record = self._output_record + schema.Struct(
            (field_name, record)
        )

    def input_record(self):
        return self._input_record

    def output_record(self):
        return self._output_record

    def AddExternalInputs(self, *inputs):
        return self.AddExternalInput(*inputs)

    def AddExternalOutputs(self, *outputs):
        self.AddExternalOutput(*outputs)

    def DeduplicateGradientSlices(self, g, aggregator='sum'):
        assert isinstance(g, GradientSlice)
        unique, remapping = self.Unique([g.indices], 2, engine='SparseHash')
        if aggregator.lower() == 'sum':
            new_g = self.UnsortedSegmentSum([g.values, remapping], 1)
        elif aggregator.lower() == 'mean':
            new_g = self.UnsortedSegmentMean([g.values, remapping], 1)
        else:
            raise ValueError('{} is not supported'.format(aggregator))
        return GradientSlice(indices=unique, values=new_g)

    def RunAllOnGPU(self, gpu_id=0, use_cudnn=False):
        """A convenient function to run everything on the GPU."""
        device_option = caffe2_pb2.DeviceOption()
        device_option.device_type = caffe2_pb2.CUDA
        device_option.cuda_gpu_id = gpu_id
        self._net.device_option.CopyFrom(device_option)
        if use_cudnn:
            for op in self._net.op:
                op.engine = "CUDNN"
    def RunAllOnMKL(self):
        """A convenient function to run everything on the GPU."""
        device_option = caffe2_pb2.DeviceOption()
        device_option.device_type = caffe2_pb2.MKLDNN
        self._net.device_option.CopyFrom(device_option)

    def _CreateAndAddToSelf(self, op_type, inputs, outputs=None, **kwargs):
        """A helper function to create an operator and add it to self.
        """
        inputs = _RectifyInputOutput(inputs)
        for input in inputs:
            if not self.BlobIsDefined(input):
                assert input.Net() != self
                self.AddExternalInput(input)
        if outputs is None:
            # If we do not specify an output, we will assume that this op
            # produces one output in this case.
            outputs = self.NextName(prefix=op_type)
        elif type(outputs) is int:
            # In this case, we will auto-fill the given number of outputs
            # with auto-generated names.
            outputs = [
                self.NextName(prefix=op_type, output_id=i)
                for i in range(outputs)]
        outputs = _RectifyInputOutput(outputs, net=self)
        op = CreateOperator(op_type, inputs, outputs, **kwargs)
        self._ExtendOps([op])

        workspace.operator_tracebacks[self.Name()][
            len(self._net.op) - 1] = _extract_stacktrace()

        if len(op.output) == 0:
            return
        elif len(op.output) == 1:
            return BlobReference(op.output[0], self)
        else:
            return tuple(BlobReference(o, self) for o in op.output)

    def __getattr__(self, op_type):
        if op_type.startswith('__'):
            raise AttributeError('Attribute {} not found.'.format(op_type))
        if not IsOperator(op_type) and not IsOperatorWithEngine(op_type, "CUDNN"):
            raise AttributeError(
                'Method ' + op_type + ' is not a registered operator.' +
                ' Did you mean: [' +
                ",".join(workspace.C.nearby_opnames(op_type)) + ']'
            )
        return lambda *args, **kwargs: self._CreateAndAddToSelf(
            op_type, *args, **kwargs)

    def __dir__(self):
        additional_methods = [
            op
            for op in _REGISTERED_OPERATORS
            if '_ENGINE_' not in op]
        return sorted(set(chain(
            dir(type(self)),
            viewkeys(self.__dict__),
            additional_methods
        )))

    def Python(
        self,
        f,
        grad_f=None,
        python_func_type=None,
        pass_workspace=False,
        grad_output_indices=None,
        grad_input_indices=None
    ):
        """
        Registers and returns a python operator.

        `f` and `grad_f` can be one of the following:
            - a function with signature (inputs, outputs), where inputs and
              outputs are a list of CPUTensor objects. This function will be
              called from C++ everytime the operator is executed.
            - a tuple (func, args, kwargs), here `func` is a callable, args is
              an argument list, and kwargs is a dict list. The call:
                  f = func(*args, kwargs)
              will be performed locally at node initialization time, on all of
              the nodes of the job, returning `f`, a callable that will be used
              as the python operator function to be called during Net execution.
              This is to be used when using python operator in a distributed
              context, and allows to create and keep local python state across
              calls to the operator.

        `python_func_type` is a type of an object that constructed as
        python_func_type(f) and provides an implementation to forward and
        backward functions. Its useful in such a case where users needs
        a statefull PythonOp (ex: use autograd for computing grad_f).

        If `pass_workspace` is True, the signature is changed to
        (inputs, outputs, workspace) where `workspace` is the workspace the op
        is going to run on. This is potentially dangerous (as the op can
        manipulate the workspace directly), use on your own risk.

        If a gradient function is specified (`grad_f`), by default its inputs
        will be: (1) all inputs to `f`, (2) followed by all outputs of `f`, (3)
        and then all gradient outputs of `f`. The outputs of `grad_f` will be
        (by default) all gradient inputs to `f`. If a subset of the gradient
        outputs or gradient inputs is desired instead, then the subsets can be
        specified by providing `grad_output_indices` and/or `grad_input_indices`
        which identify the indices of `f`'s inputs and outputs which have
        gradients.
        """
        assert(IsOperator('Python'))

        def make_builder(t):
            if not isinstance(t, tuple):
                return ''
            assert len(t) == 3, 'Expected builder tuple (func, args, kwargs)'
            func, args, kwargs = t
            normalized = (func, tuple(args), dict(kwargs))
            return pickle.dumps(normalized)

        f_builder = make_builder(f)
        grad_f_builder = make_builder(grad_f)

        assert (not grad_f) or ((not f_builder) == (not grad_f_builder)), (
            'A tuple has to be passed to both f and grad_f or neither.')

        core_kwargs = {}
        if f_builder:
            core_kwargs['pickled_builder'] = f_builder
            core_kwargs['pickled_grad_builder'] = grad_f_builder
            core_kwargs['pass_workspace'] = pass_workspace
        else:
            core_kwargs['token'] = _RegisterPythonImpl(
                f, grad_f, python_func_type, pass_workspace=pass_workspace)

        grad_output_indices = grad_output_indices or []
        grad_input_indices = grad_input_indices or []
        return lambda *args, **kwargs: self._CreateAndAddToSelf(
            'Python',
            grad_output_indices=grad_output_indices,
            grad_input_indices=grad_input_indices,
            *args,
            **dict(chain(viewitems(kwargs), viewitems(core_kwargs)))
        )

    def is_external_input(self, blob):
        name = str(blob)
        return name in self._external_input_map

    def extend_ops(self, new_ops):
        return self._ExtendOps(new_ops)


def copy_func_between_devices(src, dst):
    CPU = caffe2_pb2.CPU
    CUDA = caffe2_pb2.CUDA

    if src.device_type == CPU and dst.device_type == CPU:
        return None

    if src.device_type == CUDA and dst.device_type == CUDA:
        if src.cuda_gpu_id == dst.cuda_gpu_id:
            return None
        else:
            def fun(net, *args, **kw):
                with DeviceScope(dst):
                    return net.Copy(*args, **kw)
            return fun

    if src.device_type == CUDA and dst.device_type == CPU:
        def fun(net, *args, **kw):
            with DeviceScope(src):
                return net.CopyGPUToCPU(*args, **kw)
        return fun

    if src.device_type == CPU and dst.device_type == CUDA:
        def fun(net, *args, **kw):
            with DeviceScope(dst):
                return net.CopyCPUToGPU(*args, **kw)
        return fun

    raise ValueError('Non-supported devices: %s and %s' % (src, dst))


def device_equal(src, dst):
    '''
    We are using this fucntion instead of == operator because optional-value
    comparison between empty device_options and {device_type:0, cuda_gpu_id:0}
    returns not equal in some cases.
    '''
    return src.device_type == dst.device_type and src.cuda_gpu_id == dst.cuda_gpu_id


class RemapEntry:
    def __init__(self, blob, device):
        self.blob = blob
        self.device = device

    def __eq__(self, other):
        return self.blob == other.blob and self.device == other.device

    def __hash__(self):
        return hash(self.blob + str(self.device))


def InjectCrossDeviceCopies(net, blob_to_device=None):
    '''
    Injecting Copy functions between device within a net. Users can provide
    a net with part of operators using different device_options. This method
    will automatically create a new net with Copy ops inserted in it.

    Inputs:
      blob_to_device: If not None, it is a map of blobs and their device locations.
    Outputs:
      new_net: A new net with CopyCPUToGPU inserted with correct device option

      required_external_to_device:
               A mapping between unresolved external inputs and their
               required device options.
    Assumptions:
      1. every external inputs of this net is already in blob_to_device!
      2. if not, this function will use net device option
    '''
    new_net = net.Clone(net._net.name + '_cross_device', keep_schema=True)
    del new_net._net.op[:]
    blob_to_device = blob_to_device or {}
    # remapping of input blobs for each op.
    blob_remap = {}
    temp_remap = {}
    net_option = net._net.device_option or caffe2_pb2.DeviceOption()

    for op in net._net.op:
        temp_remap.clear()
        # Get where inputs and outputs should be
        input_dev, output_dev = InferOpBlobDevices(op)

        for dev, input in zip(input_dev, op.input):
            assert net.BlobIsDefined(input), \
                "input {} should be defined in the net.".format(input)
            if input not in blob_to_device:
                if net.is_external_input(input):
                    blob_to_device[input] = net_option
                else:
                    raise AttributeError(
                        "No device information found for blob {}.".
                        format(input)
                    )

            if not device_equal(blob_to_device[input], dev):
                # reuse already moved input
                if (RemapEntry(input, dev) in blob_remap and
                        blob_to_device[blob_remap[RemapEntry(input, dev)]] == dev):
                    temp_remap[input] = blob_remap[RemapEntry(input, dev)]
                else:
                    # need to make input on correct device.
                    copy_func = copy_func_between_devices(
                        blob_to_device[input], dev
                    )

                    def _gen_new_name(blob, device_option):
                        CPU = caffe2_pb2.CPU
                        CUDA = caffe2_pb2.CUDA
                        if device_option.device_type == CPU:
                            suffix = '_cpu'
                        elif device_option.device_type == CUDA:
                            suffix = '_cuda_' + str(device_option.cuda_gpu_id)
                        else:
                            raise RuntimeError(
                                "Unknown device type: {}".
                                format(device_option.device_type)
                            )
                        return blob + suffix

                    new_name = _gen_new_name(input, dev)
                    copy_func(new_net, input, new_name)
                    blob_remap[RemapEntry(input, dev)] = new_name
                    temp_remap[input] = new_name
                    blob_to_device[new_name] = dev

        # Enforcing no reuse blob between operators. In-place blob usage in an
        # op is allowed. This is based on the assumption that in-place op has
        # same device info
        for out_blob, device in zip(op.output, output_dev):
            if out_blob in blob_to_device and (
                out_blob not in op.input and
                not device_equal(blob_to_device[out_blob], device)
            ):
                raise RuntimeError(
                    "In-place blob: {} is not supported between operators "
                    "with different device option previous:{} now: {}. "
                    "Failed op:\n {}".format(
                        out_blob, blob_to_device[out_blob], device, op
                    )
                )
        blob_to_device.update({o: d for d, o in zip(output_dev, op.output)})
        new_op = caffe2_pb2.OperatorDef()
        new_op.CopyFrom(op)

        new_list = [temp_remap.get(b, b) for b in new_op.input]
        del new_op.input[:]
        new_op.input.extend(new_list)
        new_net.extend_ops([new_op])

    return new_net, blob_to_device


def InjectDeviceCopiesAmongNets(nets, blob_to_device_init=None):
    """
    Takes in a list of nets. They usually represent your whole execution graph.
    This function will insert cross device copy functions to all nets, and resolve
    inter-net external inputs dependencies. This method will insert Copy funcitons if
    external inputs of a net is produced on different device than it is required.
    Inputs:
      nets: a list of nets
    Outputs:
      new_nets: a list of new nets with device difference solved.

    Some notes from wyiming:
      1. You MUST pass nets in execution order. e.g. [train_init, train]
    """
    assert isinstance(nets, list), \
        "nets {} should be a list of nets.".format(str(nets))
    assert all(isinstance(net, Net) for net in nets), \
        "nets {} should be a list of nets.".format(str(nets))
    # A holistic blob to device mapping.
    blob_to_device = blob_to_device_init or {}
    new_nets = []

    for net in nets:
        new_net, blob_to_device = InjectCrossDeviceCopies(
            net, blob_to_device=blob_to_device
        )
        new_nets.append(new_net)

    return new_nets, blob_to_device


def InjectDeviceCopiesAmongNetsWithoutB2D(nets, blob_to_device_init=None):
    new_nets, _ = InjectDeviceCopiesAmongNets(nets, blob_to_device_init)
    return new_nets


def get_net_name(netlike):
    if isinstance(netlike, Net):
        return netlike.Proto().name
    elif isinstance(netlike, caffe2_pb2.NetDef):
        return netlike.name
    else:
        return netlike


def output_to_list(op_output):
    """
    Ensures that the output of an operator is a list.
    Use when an operator has a variable number of outputs, but a list of
    outputs is desired even when number of outputs is 1.

    Args:
        op_output: Either a BlobReferenece or an iterable of BlobReferences.

    Returns:
        A list of BlobReferences.
    """
    assert type(op_output) in (list, tuple, BlobReference)
    return (
        [op_output]
        if isinstance(op_output, BlobReference) else list(op_output))


def _add_net_to_dict(net_dict, net):
    name = get_net_name(net)
    if name in net_dict:
        assert net_dict[name] is None or net == net_dict[name], (
            'Different nets with same name: ' + name)
        return False
    else:
        net_dict[name] = net if isinstance(net, Net) else None
        return True


class ExecutionStep(object):
    _step_names_used = set()

    @staticmethod
    def _get_next_step_name(basename):
        name = basename
        next_idx = 1
        while name in ExecutionStep._step_names_used:
            name = basename + '_' + str(next_idx)
            next_idx += 1
        ExecutionStep._step_names_used |= set([name])
        return name

    def __init__(self, name, nets=None, num_iter=None):
        self._step = caffe2_pb2.ExecutionStep()
        self._step.name = name or ExecutionStep._get_next_step_name('step')
        self._net_dict = OrderedDict()
        self._is_used = False
        self._substeps = []
        if nets is not None:
            if type(nets) is Net:
                nets = [nets]
            for net in nets:
                if _add_net_to_dict(self._net_dict, net):
                    self._step.network.extend([get_net_name(net)])
        if num_iter is not None:
            self._step.num_iter = num_iter

    def get_net(self, name):
        return self._net_dict[name]

    def Name(self):
        return self._step.name

    def __str__(self):
        return self._step.name

    def _assert_can_mutate(self):
        assert not self._is_used, (
            'Cannot mutate a step that has already been added to a plan/step.')

    def _notify_is_used(self):
        self._is_used = True

    def Proto(self):
        return self._step

    def HasNets(self):
        return self._step.network is not None and (
            len(self._step.network) > 0)

    def HasSubsteps(self):
        return self._step.substep is not None and (
            len(self._step.substep) > 0)

    def Nets(self):
        return list(viewvalues(self._net_dict))

    def Substeps(self):
        return self._substeps

    def SetIter(self, num_iter):
        self._assert_can_mutate()
        self._step.num_iter = num_iter

    def SetCreateWorkspace(self, create_workspace):
        self._assert_can_mutate()
        self._step.create_workspace = create_workspace

    def SetNumConcurrentInstances(self, num_concurrent_instances):
        self._assert_can_mutate()
        self._step.num_concurrent_instances = num_concurrent_instances

    def SetOnlyOnce(self, only_once):
        self._assert_can_mutate()
        self._step.only_once = only_once

    def SetShouldStopBlob(self, should_stop_blob):
        assert isinstance(should_stop_blob, BlobReference), (
            "expects BlobReference here, got {}".format(type(should_stop_blob)))
        self._assert_can_mutate()
        self._step.should_stop_blob = str(should_stop_blob)

    def RunEveryMillis(self, interval):
        """
        Run this step every interval millisecods, as long as its
        siblings are still running. It is guaranteed that, after all
        siblings finish, this step will run at least one.

        This property is ignored for top-level ExecutionSteps.
        """
        self._step.run_every_ms = interval

    def SetReportNet(self, report_net, report_interval):
        """ DEPRECATED. Use RunEveryMillis instead. """
        self._assert_can_mutate()
        _add_net_to_dict(self._net_dict, report_net)
        self._step.report_net = get_net_name(report_net)
        self._step.report_interval = report_interval

    def AddSubstep(self, substep):
        self._assert_can_mutate()
        assert not self.HasNets(), 'Cannot have both network and substeps.'
        if isinstance(substep, ExecutionStep):
            substep._notify_is_used()
            if not substep.HasNets() and not substep.HasSubsteps():
                return self
            for net in substep.Nets():
                _add_net_to_dict(self._net_dict, net)
            self._substeps.append(substep)
            proto = substep.Proto()
        else:
            proto = substep
        self._step.substep.add().CopyFrom(proto)
        return self

    def SetConcurrentSubsteps(self, concurrent_substeps):
        self._assert_can_mutate()
        assert not self.HasNets(), 'Cannot have both network and substeps.'
        self._step.concurrent_substeps = concurrent_substeps

    def AddNet(self, net):
        self._assert_can_mutate()
        assert not self.HasSubsteps(), 'Cannot have both network and substeps.'
        assert isinstance(net, Net)
        _add_net_to_dict(self._net_dict, net)
        self._step.network.extend([get_net_name(net)])
        return self

    def get_all_attributes(self, name):
        """
        Return the list of all attributes under the given `name`, present in
        all of the nets used in this execution step and its children.
        """
        return [
            attr
            for net in viewvalues(self._net_dict)
            for attr in net.get_attributes(name)
        ]


def add_nets_in_order(step, net_list):
    proto = step.Proto()
    for substep in step.Substeps():
        add_nets_in_order(substep, net_list)
    for net in proto.network:
        if net not in net_list:
            net_list.append(net)
    # FIXME(azzolini): This is actually wrong. Report nets should be
    # instantiated first since they may run before any substep is run.
    # However, curerntly, Reporter depends on this behavior.
    if proto.report_net and proto.report_net not in net_list:
        net_list.append(proto.report_net)


class Plan(object):

    def __init__(self, name_or_step):
        self._plan = caffe2_pb2.PlanDef()
        self._net_dict = OrderedDict()
        if isinstance(name_or_step, ExecutionStep):
            self._plan.name = name_or_step.Name()
            self.AddStep(name_or_step)
        elif isinstance(name_or_step, basestring):
            self._plan.name = name_or_step
        else:
            raise ValueError('name_or_step must be a string or ExecutionStep')

    def __str__(self):
        return self._plan.name

    def Proto(self):
        return self._plan

    def AddNets(self, nets):
        for net in nets:
            if _add_net_to_dict(self._net_dict, net):
                assert isinstance(net, Net)
                self._plan.network.add().CopyFrom(net.Proto())

    def Nets(self):
        return list(viewvalues(self._net_dict))

    def AddStep(self, step):
        assert isinstance(step, ExecutionStep)
        step._notify_is_used()
        if not step.HasNets() and not step.HasSubsteps():
            return
        self._plan.execution_step.add().CopyFrom(step.Proto())
        # nets need to be added to the plan in order of usage
        net_list = []
        add_nets_in_order(step, net_list)
        self.AddNets([step.get_net(n) for n in net_list])

    def get_all_attributes(self, name):
        """
        Return the list of all attributes under the given `name`, present in
        all of the nets used in this plan.
        """
        return [
            attr
            for net in viewvalues(self._net_dict)
            for attr in net.get_attributes(name)
        ]


def to_execution_step(step_or_nets, default_name=None):
    from caffe2.python.net_builder import NetBuilder
    if isinstance(step_or_nets, ExecutionStep):
        return step_or_nets

    stop_blob = None
    if not default_name and hasattr(step_or_nets, 'name'):
        default_name = step_or_nets.name
    if isinstance(step_or_nets, NetBuilder):
        stop_blob = step_or_nets._stop_blob
        step_or_nets = step_or_nets.get()
    return execution_step(
        default_name, step_or_nets, should_stop_blob=stop_blob)


def execution_step(default_name,
                   steps_or_nets,
                   num_iter=None,
                   report_net=None,
                   report_interval=None,
                   concurrent_substeps=None,
                   should_stop_blob=None,
                   only_once=None,
                   num_concurrent_instances=None,
                   create_workspace=False):
    """
    Helper for creating an ExecutionStep.
    - steps_or_nets can be:
      - None
      - Net
      - ExecutionStep
      - list<Net>
      - list<ExecutionStep>
    - should_stop_blob is either None or a scalar boolean blob.
      - This blob is checked AFTER every substeps/subnets.
      - If specified and true, then this step will return immediately.
      - Be sure to handle race conditions if setting from concurrent threads.
    - if no should_stop_blob or num_iter is provided, defaults to num_iter=1
    """
    assert should_stop_blob is None or num_iter is None, (
        'Cannot set both should_stop_blob and num_iter.')
    if should_stop_blob is None and num_iter is None:
        num_iter = 1

    step = ExecutionStep(default_name)
    if should_stop_blob is not None:
        step.SetShouldStopBlob(should_stop_blob)
    if num_iter is not None:
        step.SetIter(num_iter)
    if only_once is not None:
        step.SetOnlyOnce(only_once)
    if concurrent_substeps is not None:
        step.SetConcurrentSubsteps(concurrent_substeps)
    if report_net is not None:
        assert report_interval is not None
        step.SetReportNet(report_net, report_interval)
    if num_concurrent_instances is not None:
        step.SetNumConcurrentInstances(num_concurrent_instances)
    if create_workspace:
        step.SetCreateWorkspace(True)

    if isinstance(steps_or_nets, ExecutionStep):
        step.AddSubstep(steps_or_nets)
    elif isinstance(steps_or_nets, Net):
        step.AddNet(steps_or_nets)
    elif isinstance(steps_or_nets, list):
        if all(isinstance(x, Net) for x in steps_or_nets):
            for x in steps_or_nets:
                step.AddNet(x)
        else:
            for x in steps_or_nets:
                step.AddSubstep(to_execution_step(x))
    elif steps_or_nets:
        raise ValueError(
            'steps_or_nets must be a step, a net, or a list of nets or steps.')
    return step


def scoped_execution_step(name, *args, **kwargs):
    """Same as execution_step() except that the step name is scoped."""
    default_name = ScopedName(name) if name else name
    return execution_step(default_name, *args, **kwargs)


def _extract_stacktrace():
    '''
    This function extracts stacktrace without file system access
    by purely using sys._getframe() and removes part that belongs to
    this file (core.py). We are not using inspect module because
    its just a wrapper on top of sys._getframe() whos
    logis is based on accessing source files on disk - exactly what
    we are trying to avoid here. Same stands for traceback module

    The reason for file system access avoidance is that
    if code is located on an NFS, file access might be slow

    Function returns a list of tuples (file_name, line_number)
    '''

    current_file_name = __name__.replace('.', '/') + ".py"
    result = []
    frame = sys._getframe(1)
    # We just go down the frame stack in a loop
    while frame:
        if current_file_name not in frame.f_code.co_filename:
            # Its important to extract information from the frame here
            # as frame's current line most probably will change later.
            result.append((frame.f_code.co_filename, frame.f_lineno))
        frame = frame.f_back
    return result
