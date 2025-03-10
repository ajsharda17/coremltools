# Copyright (c) 2021, Apple Inc. All rights reserved.
#
# Use of this source code is governed by a BSD-3-clause license that can be
# found in the LICENSE.txt file or at https://opensource.org/licenses/BSD-3-Clause

import collections
import gc
import os
import warnings

from coremltools import (
    ComputeUnit as _ComputeUnit,
    __version__ as _ct_version,
    _LOWEST_ALLOWED_SPECIFICATION_VERSION_FOR_NEURALNETWORK,
    _LOWEST_ALLOWED_SPECIFICATION_VERSION_FOR_MILPROGRAM,
)
from coremltools.converters.mil.mil.passes.quantization_passes import (
    AbstractQuantizationPass,
    ComputePrecision as precision,
    FP16ComputePrecision
)
from coremltools.converters.mil.input_types import (
    ClassifierConfig,
    ImageType,
    InputType,
    TensorType,
)
from coremltools.converters.mil.converter import mil_convert
from coremltools.converters.mil.mil import Program, types
from coremltools._deps import _HAS_TORCH, _HAS_TF_1, _HAS_TF_2
from coremltools.converters._profile_utils import _profile

from coremltools.models import _METADATA_VERSION, _METADATA_SOURCE
from coremltools.models.utils import _MLPACKAGE_EXTENSION
from coremltools.converters.mil._deployment_compatibility import (
    AvailableTarget,
    check_deployment_compatibility,
)

if _HAS_TF_1:
    import tensorflow as tf
    from coremltools.converters.mil.frontend.tensorflow.load import TF1Loader
if _HAS_TF_2:
    import tensorflow as tf
    from coremltools.converters.mil.frontend.tensorflow2.load import TF2Loader

if _HAS_TORCH:
    import torch
    from coremltools.converters.mil.frontend.torch.load import (
        _torchscript_from_model as pytorch_load,
    )

@_profile
def convert(
    model,
    source="auto",
    inputs=None,
    outputs=None,
    classifier_config=None,
    minimum_deployment_target=None,
    convert_to=None,
    compute_precision=None,
    skip_model_load=False,
    compute_units=_ComputeUnit.ALL,
    package_dir=None,
    debug=False,
):
    """
    Convert a TensorFlow or PyTorch model to the Core ML model format as either
    a neural network or an `ML program <https://coremltools.readme.io/docs/ml-programs>`_.
    Some parameters and requirements differ for TensorFlow and PyTorch
    conversions.

    Parameters
    ----------

    model :
        TensorFlow 1, TensorFlow 2, or PyTorch model in one of the following
        formats:

        * TensorFlow versions 1.x
        
            - Frozen `tf.Graph <https://www.tensorflow.org/api_docs/python/tf/Graph>`_
            - Frozen graph (``.pb``) file path
            - `tf.keras.Model <https://www.tensorflow.org/api_docs/python/tf/keras>`_
            -  `HDF5 <https://keras.io/api/models/model_saving_apis/>`_ file path (``.h5``)
            - `SavedModel <https://www.tensorflow.org/guide/saved_model>`_ directory path

        * TensorFlow versions 2.x
        
            - `tf.keras.Model <https://www.tensorflow.org/api_docs/python/tf/keras>`_
            - `HDF5 file path <https://keras.io/api/models/model_saving_apis/>`_ (``.h5``)
            - `SavedModel <https://www.tensorflow.org/guide/saved_model>`_ directory path
            - A `concrete function <https://www.tensorflow.org/guide/concrete_function>`_

        * PyTorch
        
            - A `TorchScript <https://pytorch.org/docs/stable/jit.html>`_ object
            - Path to a ``.pt`` file

    source : str (optional)
    
        One of [``auto``, ``tensorflow``, ``pytorch``, ``milinternal``]. ``auto``
        determines the framework automatically for most cases. Raises
        ``ValueError`` if it fails to determine the source framework.

    inputs : list of ``TensorType`` or ``ImageType``

        * If you specify ``dtype`` with ``TensorType`` or ``ImageType``, it will
          be applied to the input of the converted model. For example, the
          following code snippet will produce a Core ML model with float 16 typed
          inputs.
          
          .. sourcecode:: python

              import coremltools as ct
              mlmodel = ct.convert(keras_model,
                                   inputs=[ct.TensorType(dtype=np.float16)],
                                   minimum_deployment_target=ct.target.macOS13)

        * The following code snippet will produce a Core ML model with the
          ``GRAYSCALE_FLOAT16`` input image type:
          
          .. sourcecode:: python

              import coremltools as ct
              # H : image height, W: image width
              mlmodel = ct.convert(torch_model,
                               inputs=[ct.ImageType(shape=(1, 1, H, W),
                                       color_layout=ct.colorlayout.GRAYSCALE_FLOAT16)],
                               minimum_deployment_target=ct.target.macOS13)

        * TensorFlow 1 and 2 (including tf.keras):
            - The ``inputs`` parameter is optional. If not provided, the inputs
              are placeholder nodes in the model (if the model is a frozen graph)
              or function inputs (if the model is a ``tf.function``).
            - If ``inputs`` is provided, it must be a flat list.
            - The ``inputs`` must correspond to all or some of the placeholder nodes
              in the TF model.
            - If ``name`` is specified with ``TensorType`` and ``ImageType``, it
              must correspond to a placeholder op in the TF graph. The input names
              in the converted Core ML model can later be modifed using the
              ``ct.utils.rename_feature`` API.
            - If ``dtype`` is not specified, it defaults to the ``dtype`` of the
              inputs in the TF model.

        * PyTorch:
            - The ``inputs`` parameter is required.
            - Number of elements in ``inputs`` must match the number of inputs
              of the PyTorch model.
            - ``inputs`` may be a nested list or tuple.
            - ``TensorType`` and ``ImageType`` must have the ``shape`` specified.
            - If the ``name`` argument is specified with ``TensorType`` or
              ``ImageType``, the converted Core ML model will have inputs with
              the same name.
            - If ``dtype`` is missing, it defaults to float 32.

    outputs : list of ``TensorType`` or ``ImageType`` (optional)

        * If you specify ``dtype`` with ``TensorType`` or ``ImageType``,
          it will be applied to the output of the converted model. For example,
          to produce float 16 typed inputs and outputs:
          
          .. sourcecode:: python

              import coremltools as ct
              mlmodel = ct.convert(keras_model,
                                   inputs=[ct.TensorType(dtype=np.float16)],
                                   outputs=[ct.TensorType(dtype=np.float16)],
                                   minimum_deployment_target=ct.target.macOS13)

        * To produce image inputs and outputs:
          
          .. sourcecode:: python

              import coremltools as ct
              # H: image height, W: image width
              mlmodel = ct.convert(torch_model,
                                   inputs=[ct.ImageType(shape=(1, 3, H, W), color_layout=ct.colorlayout.RGB)],
                                   outputs=[ct.ImageType(color_layout=ct.colorlayout.RGB)],
                                   minimum_deployment_target=ct.target.macOS13)

        * TensorFlow 1 and 2 (including tf.keras):

            - If ``outputs`` is not specified, the converter infers outputs from 
              the sink nodes in the graph.
            - If specified, the ``name`` with ``TensorType`` or ``ImageType``
              must correspond to a node in the TF graph. In this case, the model
              will be converted up to that node.

        * PyTorch:

            - If specified, the length of the list must match the number of
              outputs returned by the PyTorch model.
            - If ``name`` is specified, it is applied to the output names of the
              converted Core ML model.

    classifier_config : ClassifierConfig class (optional)
        The configuration if the MLModel is intended to be a classifier.

    minimum_deployment_target : coremltools.target enumeration (optional)
        A member of the ``coremltools.target`` enum.
        The value of this parameter determines the type of the model
        representation produced by the converter. To learn about the differences
        between neural networks and ML programs, see
        `ML Programs <https://coremltools.readme.io/docs/ml-programs>`_.

        - The converter produces a neural network (``neuralnetwork``) if:
          ::
             minimum_deployment_target <= coremltools.target.iOS14/
                                          coremltools.target.macOS11/
                                          coremltools.target.watchOS7/
                                          coremltools.target.tvOS14:

        - The converter produces an ML program (``mlprogram``) if:
          ::
             minimum_deployment_target >= coremltools.target.iOS15/
                                           coremltools.target.macOS12/
                                           coremltools.target.watchOS8/
                                           coremltools.target.tvOS15:

        - If neither the ``minimum_deployment_target`` nor the ``convert_to``
          parameter is specified, the converter produces the neural network
          model type with as minimum of a deployment target as possible.
        - If this parameter is specified and ``convert_to`` is also specified,
          they must be compatible. The following are examples of invalid values:
          ::
            # Invalid:
            convert_to="neuralnetwork", minimum_deployment_target=coremltools.target.iOS15
            # Invalid:
            convert_to="mlprogram", minimum_deployment_target=coremltools.target.iOS14

    convert_to : str (optional)
        Must be one of [``'neuralnetwork'``, ``'mlprogram'``, ``'milinternal'``].
        The value of this parameter determines the type of the model
        representation produced by the converter. To learn about the
        differences between neural networks and ML programs, see
        `ML Programs <https://coremltools.readme.io/docs/ml-programs>`_.
        
        - ``'neuralnetwork'``: Returns an MLModel (``coremltools.models.MLModel``)
          containing a NeuralNetwork proto, which is the original Core ML format.
          The model saved from this returned object is executable either on
          iOS13/macOS10.15/watchOS6/tvOS13 and newer, or on
          iOS14/macOS11/watchOS7/tvOS14 and newer, depending on the layers used
          in the model.
        - ``'mlprogram'`` : Returns an MLModel (``coremltools.models.MLModel``)
          containing a MILSpec.Program proto, which is the Core ML program format.
          The model saved from this returned object is executable on iOS15,
          macOS12, watchOS8, and tvOS15.
        - ``'milinternal'``: Returns an MIL program object
          (``coremltools.converters.mil.Program``). An MIL program is primarily
          used for debugging and inspection. It can be converted to an MLModel for
          execution by using one of the following:
          ::
             ct.convert(mil_program, convert_to="neuralnetwork")
             ct.convert(mil_program, convert_to="mlprogram")

        - If neither the ``minimum_deployment_target`` nor the ``convert_to``
          parameter is specified, the converter produces the neural network
          model type with as minimum of a deployment target as possible.

    compute_precision : coremltools.precision enumeration or ct.transform.FP16ComputePrecision() (optional)

        Use this argument to control the storage precision of the tensors in the
        ML program. Must be one of the following.
        
        - ``coremltools.precision.FLOAT16`` enum: The following transform is
          applied to produce a float 16 program; that is, a program in which all
          the intermediate float tensors are of type float 16 (for ops that
          support that type).
          ::
              coremltools.transform.FP16ComputePrecision(op_selector=
                                                         lambda op:True)

          The above transform iterates through all the ops, looking at each op's
          inputs and outputs. If they are of type float 32, ``cast``
          ops are injected to convert those tensors (also known as `vars`) to
          type float 16.

        - ``coremltools.precision.FLOAT32`` enum: No transform is applied.
          
          The original float32 tensor dtype in the source model is preserved.
          Opt into this option if the default converted model is displaying
          numerical precision issues.

        - ``coremltools.transform.FP16ComputePrecision(op_selector=...)``
          
          Use this option to control which tensors are cast to float 16.
          Before casting the inputs/outputs of any op from float32 to float 16,
          the op_selector function is invoked on the op object. This function
          must return a boolean value. By default it returns ``True`` for every op,
          but you can customize this.
          
          For example:
          ::
             coremltools.transform.FP16ComputePrecision(op_selector=
                                         lambda op: op.op_type != "linear")

          The above casts all the float32 tensors to be float 16, except
          the input/output tensors to any ``linear`` op. See more examples
          below.

        - ``None``: The default
            - When ``convert_to="mlprogram"``, the ``compute_precision`` parameter
              defaults to ``coremltools.precision.FLOAT16``.
            - When ``convert_to="neuralnetwork"``, the ``compute_precision`` parameter
              needs to be ``None`` and has no meaning.
            - For example, you can customize the float 16 precision transform to prevent
              casting all the ``real_div`` ops in the program to float 16
              precision:
              
              .. sourcecode:: python

                  def skip_real_div_ops(op):
                       if op.op_type == "real_div":
                           return False
                       return True
                  
                  model = ct.convert(source_model,
                                     compute_precision=ct.transform.FP16ComputePrecision(op_selector=skip_real_div_ops),
                                     minimum_deployment_target=ct.target.iOS15
                                    )

    skip_model_load : bool
        Set to ``True`` to prevent coremltools from calling into the Core ML framework
        to compile and load the model, post-conversion. In that case, the returned
        model object cannot be used to make a prediction, but can be used to save
        with ``model.save()``. This flag may be used to convert to a newer model type
        on an older Mac, which may raise a runtime warning if done without
        turning this flag on.
        
        Example: Use this flag to suppress a runtime warning when converting to an
        ML program model on macOS 11, since an ML program can only be compiled and
        loaded from macOS12+.
        
        Defaults to ``False``.

    compute_units: coremltools.ComputeUnit
    
        An enum with the following possible values.
        
            - ``coremltools.ComputeUnit.ALL``: Use all compute units available, including the
              neural engine.
            - ``coremltools.ComputeUnit.CPU_ONLY``: Limit the model to only use the CPU.
            - ``coremltools.ComputeUnit.CPU_AND_GPU``: Use both the CPU and GPU, but not the
              neural engine.

    package_dir : str
        Post conversion, the model is saved at a temporary location and
        loaded to form the MLModel object ready for prediction.
        
        * If ``package_dir`` is provided, model will be saved at this location
          rather than creating a temporary directory.
        * If not ``None``, this must be a path to a directory with the extension
          ``.mlpackage``.

    debug : bool
        This flag should generally be ``False`` except for debugging purposes.
        Setting this flag to ``True`` produces the following behavior:
          - For Torch conversion, it will print the list of supported and
            unsupported ops found in the model if conversion fails due to an
            unsupported op.
          - For Tensorflow conversion, it will cause to display extra logging
            and visualizations.

    Returns
    -------
    
    model : ``coremltools.models.MLModel`` or ``coremltools.converters.mil.Program``
        A Core ML MLModel object or MIL program object (see ``convert_to``).

    Examples
    --------
    
    TensorFlow 1, 2 (``model`` is a frozen graph):

        >>> with tf.Graph().as_default() as graph:
        >>>     x = tf.placeholder(tf.float32, shape=(1, 2, 3), name="input")
        >>>     y = tf.nn.relu(x, name="output")

    Automatically infer inputs and outputs:

        >>> mlmodel = ct.convert(graph)
        >>> test_input = np.random.rand(1, 2, 3) - 0.5
        >>> results = mlmodel.predict({"input": test_input})
        >>> print(results['output'])

    TensorFlow 2 (``model`` is a tf.Keras model path):

        >>> x = tf.keras.Input(shape=(32,), name='input')
        >>> y = tf.keras.layers.Dense(16, activation='softmax')(x)
        >>> keras_model = tf.keras.Model(x, y)

        >>> keras_model.save(h5_path)
        >>> mlmodel = ct.convert(h5_path)

        >>> test_input = np.random.rand(2, 32)
        >>> results = mlmodel.predict({'input': test_input})
        >>> print(results['Identity'])

    PyTorch:

        >>> model = torchvision.models.mobilenet_v2()
        >>> model.eval()
        >>> example_input = torch.rand(1, 3, 256, 256)
        >>> traced_model = torch.jit.trace(model, example_input)

        >>> input = ct.TensorType(name='input_name', shape=(1, 3, 256, 256))
        >>> mlmodel = ct.convert(traced_model, inputs=[input])
        >>> results = mlmodel.predict({"input": example_input.numpy()})
        >>> print(results['1651']) # 1651 is the node name given by PyTorch's JIT

    See `Conversion Options <https://coremltools.readme.io/docs/neural-network-conversion>`_ for
    more advanced options.
    """
    _check_deployment_target(minimum_deployment_target)
    outputs_as_strings, outputs_as_tensor_or_image_types = _validate_outputs_argument(outputs)
    exact_source = _determine_source(model, source,
                                     outputs_as_strings,
                                     outputs_as_tensor_or_image_types,
                                     outputs)
    exact_target = _determine_target(convert_to, minimum_deployment_target)
    _validate_conversion_arguments(model, exact_source, inputs, outputs_as_tensor_or_image_types,
                                   classifier_config, compute_precision,
                                   exact_target, minimum_deployment_target)

    if compute_precision is None:
        transforms = [FP16ComputePrecision(op_selector=lambda op: True)] if convert_to != "neuralnetwork" else list()
    elif compute_precision == precision.FLOAT32:
        transforms = list()
    elif compute_precision == precision.FLOAT16:
        transforms = [FP16ComputePrecision(op_selector=lambda op: True)]
    elif isinstance(compute_precision, FP16ComputePrecision):
        transforms = [compute_precision]
    else:
        raise ValueError("Invalid value of the argument 'compute_precision'")

    if package_dir is not None:
        _, ext = os.path.splitext(package_dir)
        if ext != _MLPACKAGE_EXTENSION:
            raise Exception("If package_dir is provided, it must have extension {} (not {})".format(_MLPACKAGE_EXTENSION, ext))

    specification_version = minimum_deployment_target.value if minimum_deployment_target is not None else None
    
    if specification_version is None:
        specification_version = _set_default_specification_version(exact_target)

    mlmodel = mil_convert(
        model,
        convert_from=exact_source,
        convert_to=exact_target,
        inputs=inputs,
        outputs=outputs_as_tensor_or_image_types, # None or list[ct.ImageType/ct.TensorType]
        classifier_config=classifier_config,
        transforms=tuple(transforms),
        skip_model_load=skip_model_load,
        compute_units=compute_units,
        package_dir=package_dir,
        debug=debug,
        specification_version=specification_version,
    )

    if exact_target == 'milinternal':
        return mlmodel # Returns the MIL program

    if minimum_deployment_target is not None:
        check_deployment_compatibility(
            spec=mlmodel.get_spec(),
            representation=exact_target,
            deployment_target=minimum_deployment_target,
        )

    gc.collect()

    mlmodel = _record_build_metadata(mlmodel, exact_source)

    return mlmodel

def _set_default_specification_version(target):
    if target == "neuralnetwork":
        return _LOWEST_ALLOWED_SPECIFICATION_VERSION_FOR_NEURALNETWORK
    elif target == "mlprogram":
        return _LOWEST_ALLOWED_SPECIFICATION_VERSION_FOR_MILPROGRAM
    elif target == "milinternal":
        return None
    else:
        raise NotImplementedError("Backend converter {} not implemented".format(target))


def _check_deployment_target(minimum_deployment_target):
    if minimum_deployment_target is not None and \
        not isinstance(minimum_deployment_target, AvailableTarget):
        msg = (
            "Unrecognized value of argument 'minimum_deployment_target': {}. "
            "It needs to be a member of 'coremltools.target' enumeration. "
            "For example, coremltools.target.iOS13"
        )
        raise TypeError(msg.format(minimum_deployment_target))

def _validate_outputs_argument(outputs):
    """
    - validate properties that the "outputs" argument must satisfy, for instance, it should either be a list
      of ct.ImageType/ct.TensorType or a list of strings, etc.
    - return : tuple
        - (outputs_as_strings, outputs_as_tensor_or_image_types)
        - outputs_as_strings: list[str]
        - outputs_as_tensor_or_image_types : list[ct.ImageType] or list[ct.TensorType]
    """
    if outputs is None:
        return None, None
    else:
        if not isinstance(outputs, list):
            msg = '"outputs" must be of type list'
            raise ValueError(msg)
        if len(outputs) == 0:
            return None, None
        if not all([isinstance(t, TensorType) or isinstance(t, ImageType) or isinstance(t, str) for t in outputs]):
            msg = '"outputs" must be a list of type ct.TensorType or ct.ImageType or strings'
            raise ValueError(msg)

        msg_inconsistent_types = 'all elements of "outputs" must either be of type str ' \
                                 'or of types ct.ImageType/ct.TensorType'
        if isinstance(outputs[0], str):
            # if one of the elements is a string, all elements must be strings
            if not all([isinstance(t, str) for t in outputs]):
                raise ValueError(msg_inconsistent_types)
            return outputs, [TensorType(name=name) for name in outputs]

        if isinstance(outputs[0], InputType):
            if not all([isinstance(t, TensorType) or isinstance(t, ImageType) for t in outputs]):
                raise ValueError(msg_inconsistent_types)
            if any([t.shape is not None for t in outputs]):
                msg = "The 'shape' argument must not be specified for the outputs, since it is " \
                      "automatically inferred from the input shapes and the ops in the model"
                raise ValueError(msg)
            for out_ in outputs:
                if isinstance(out_, TensorType):
                    if out_.default_value is not None:
                        raise ValueError("The 'default_value' argument must not be specified for the outputs")
                if isinstance(out_, ImageType):
                    if out_.scale != 1.0:
                        raise ValueError("'scale' must be 1.0 for a output of ImageType")
                    if not (out_.bias is None or out_.bias == 0.0 or out_.bias == [0.0, 0.0, 0.0]):
                        raise ValueError("'bias' must be None or 0 for an output of ImageType")
                    if out_.channel_first is not None:
                        raise ValueError("'channel_first' must be None for an output of ImageType")
            output_names = [t.name for t in outputs]
            # verify that either all of the entries in output_names is "None" or none of them is "None"
            msg_consistent_names = 'Either none or all the outputs must have the "name" argument specified'
            if output_names[0] is None and not all([name is None for name in output_names]):
                raise ValueError(msg_consistent_names)
            if output_names[0] is not None and not all([name is not None for name in output_names]):
                raise ValueError(msg_consistent_names)
            if output_names[0] is not None:
                if len(set(output_names)) != len(output_names):
                    raise ValueError("Duplicate names provided in 'outputs'")
            if output_names[0] is None:
                return None, outputs
            else:
                return output_names, outputs

def _validate_conversion_arguments(model,
                                   exact_source,
                                   inputs,
                                   outputs,
                                   classifier_config,
                                   compute_precision,
                                   convert_to,
                                   minimum_deployment_target,
                                   ):
    """
    Validate and process model, inputs, classifier_config based on
    `exact_source` (which cannot be `auto`)
    """
    def raise_if_duplicated(input_list):
        # Detect duplicated inputs
        input_names = [t.name for t in input_list if t.name is not None]
        dups = [
            item
            for item, count in collections.Counter(input_names).items()
            if count > 1
        ]
        if len(dups) > 0:
            raise ValueError("Duplicated inputs: {}".format(dups))

    def _flatten_list(_inputs):
        ret = []
        for _input in _inputs:
            if isinstance(_input, (list, tuple)):
                ret.extend(_flatten_list(_input))
            elif isinstance(_input, InputType):
                ret.append(_input)
            else:
                raise ValueError(
                    "Unknown type {} for flattening into InputType.".format(
                        type(_input)
                    )
                )
        return ret

    flat_inputs = None
    if inputs is not None:
        if not isinstance(inputs, list):
            msg = '"inputs" must be of type list'
            raise ValueError(msg)

        # get flattened inputs
        flat_inputs = _flatten_list(inputs)
        for t in flat_inputs:
            if not isinstance(t, InputType):
                msg = 'inputs must be a list of type ct.TensorType or ct.ImageType'
                raise ValueError(msg)
            if t.dtype == types.fp16:
                if not (minimum_deployment_target is not None and \
                    minimum_deployment_target >= AvailableTarget.iOS16):
                    msg = "float16 dtype for inputs is only supported for deployment target >= iOS16/macOS13/watchOS9/tvOS16"
                    raise TypeError(msg)

    if outputs is not None:
        for t in outputs:
            if t.dtype == types.fp16:
                if not (minimum_deployment_target is not None and \
                    minimum_deployment_target >= AvailableTarget.iOS16):
                    msg = "float16 dtype for outputs is only supported for deployment target >= iOS16/macOS13/watchOS9/tvOS16"
                    raise TypeError(msg)

    if classifier_config is not None:
        if not isinstance(classifier_config, ClassifierConfig):
            msg = '"classifier_config" must be of type ClassifierConfig'
            raise ValueError(msg)

    if convert_to.lower() == 'neuralnetwork' and compute_precision is not None:
        msg = "compute_precision is only supported for mlprogram target and must be None if target=='neuralnetwork'.\n" \
              "Note that target may be implicitly set depending on the minimum_deployment_target.\n" \
              "See minimum_deployment_target for more details."
        raise ValueError(msg)

    if compute_precision is not None:
        if compute_precision not in [precision.FLOAT32, precision.FLOAT16]:
            if not isinstance(compute_precision, FP16ComputePrecision):
                msg = "'compute_precision' must be either coremltools.precision.FLOAT32 or coremltools.precision.FLOAT16" \
                      " or of type coremltools.transform.FP16ComputePrecision()"
                raise ValueError(msg)

    if exact_source in {"tensorflow", "tensorflow2"}:
        if exact_source == "tensorflow" and not _HAS_TF_1:
            msg = 'Converter was called with source="tensorflow", ' +\
                    'but missing tensorflow package'
            raise ValueError(msg)

        if inputs is not None:
            raise_if_duplicated(inputs)

        if inputs is not None and not all(
            [isinstance(_input, InputType) for _input in inputs]
        ):
            raise ValueError("Input should be a list of TensorType or ImageType")

    elif exact_source == "pytorch":
        if inputs is None:
            msg = 'Expected argument for pytorch "inputs" not provided'
            raise ValueError(msg)

        raise_if_duplicated(flat_inputs)
        if inputs is not None and not all(
            [isinstance(_input, InputType) for _input in flat_inputs]
        ):
            raise ValueError(
                "Input should be a list/tuple (or nested lists/tuples) of TensorType or ImageType"
            )

    elif exact_source == "milinternal":
        if not isinstance(model, Program):
            msg = "Converter was asked to convert MIL input, but input is not a MIL program!"
            raise ValueError(msg)


def _determine_source(model, source,
                      output_names,
                      outputs_as_tensor_or_image_types,
                      output_argument_as_specified_by_user):
    """
    Infer source (which can be auto) to the precise framework.
    """
    source = source.lower()
    if source not in {"auto", "tensorflow", "pytorch", "milinternal"}:
        msg = (
            'Unrecognized value of argument "source": {}. '
            'It must be one of ["auto", "tensorflow", "pytorch"].'
        )
        raise ValueError(msg.format(source))


    # Determine tensorflow version
    if source == "tensorflow" and _HAS_TF_2:
        return "tensorflow2"

    if source != 'auto':
        return source

    # Determine `auto` source
    if source == "auto" and _HAS_TF_1:
        try:
            loader = TF1Loader(model, outputs=outputs_as_tensor_or_image_types)
            loader._graph_def_from_model(output_names=output_names)
            return "tensorflow"
        except:
            pass

    if source == "auto" and _HAS_TF_2:
        try:
            loader = TF2Loader(model, outputs=outputs_as_tensor_or_image_types)
            loader._graph_def_from_model(output_names=output_names)
            return "tensorflow2"
        except:
            pass

    if source == "auto" and _HAS_TORCH:
        is_torch_load_successful = False
        try:
            pytorch_load(model)
            is_torch_load_successful = True
        except:
            pass
        if is_torch_load_successful:
            # validate that the outputs passed by the user are of type ImageType/TensorType
            if output_argument_as_specified_by_user is not None and \
                not all([isinstance(t, TensorType) or isinstance(t, ImageType) \
                        for t in output_argument_as_specified_by_user]):
                msg = '"outputs" must be a list of type ct.TensorType or ct.ImageType for pytorch conversion'
                raise ValueError(msg)
            return "pytorch"


    if source == "auto" and isinstance(model, Program):
        return "milinternal"

    msg = (
        "Unable to determine the type of the model, i.e. the source framework. "
        'Please provide the value of argument "source", from one of '
        '["tensorflow", "pytorch", "milinternal"]. Note that model conversion requires the '
        "source package that generates the model. Please make sure you have "
        "the appropriate version of source package installed. E.g., if you're "
        "converting model originally trained with TensorFlow 1.14, make sure "
        "you have `tensorflow==1.14` installed."
    )
    raise ValueError(msg)

def _determine_target(convert_to, minimum_deployment_target):
    """
    Infer the precise backend target, which could be one of ``milinternal``, ``neuralnetwork`` or ``mlprogram``
    """
    if minimum_deployment_target is not None:
        if convert_to == "mlprogram" and \
            minimum_deployment_target < AvailableTarget.iOS15:
                msg = "When 'convert_to' is {}, the minimum deployment target must be at least iOS15/macOS12/watchOS8/tvOS15"
                raise ValueError(msg.format(convert_to))

        if convert_to == "neuralnetwork" and \
            minimum_deployment_target >= AvailableTarget.iOS15:
            msg = "If minimum deployment target is iOS15/macOS12/watchOS8/tvOS15 or higher, then " \
                  "'convert_to' cannot be {}. It must be 'mlprogram'"
            raise ValueError(msg.format(convert_to))

    if convert_to is not None:
        return convert_to
    else:
        if minimum_deployment_target is None:
            return "neuralnetwork"
        elif minimum_deployment_target <= AvailableTarget.iOS14:
            return "neuralnetwork"
        else:
            return "mlprogram"


def _get_metadata_from_mlmodel(mlmodel):
    # Copy from source mlmodel if metadata info exists
    src_pkg_version = mlmodel.user_defined_metadata[_METADATA_SOURCE]
    coremltools_version = mlmodel.user_defined_metadata[_METADATA_VERSION]

    src_pkg_version_list = src_pkg_version.split("==")
    if len(src_pkg_version_list) == 0:
        src_pkg, pkg_ver = None, None
    elif len(src_pkg_version_list) == 1:
        src_pkg, pkg_ver = src_pkg_version_list[0], ""
    elif len(src_pkg_version_list) == 2:
        src_pkg, pkg_ver = src_pkg_version_list
    else:
        raise AssertionError("Unable to parse src_pkg_version")

    build_info = {'coremltools-version': _ct_version if not coremltools_version else coremltools_version}
    if src_pkg is not None and pkg_ver is not None:
        build_info['coremltools-component-' + src_pkg] = str(pkg_ver)

    return build_info


def _record_build_metadata(mlmodel, exact_source):
    # recording metadata: coremltools version, source framework and version
    if exact_source in {"tensorflow", "tensorflow2"} and (_HAS_TF_1 or _HAS_TF_2):
        src_pkg_version = "tensorflow=={0}".format(tf.__version__)
    elif exact_source == "pytorch" and _HAS_TORCH:
        src_pkg_version = "torch=={0}".format(torch.__version__)
    elif exact_source == 'milinternal':
        src_pkg_version = "milinternal"
    else:
        raise ValueError('Unsupported source {}'.format(exact_source))

    mlmodel.user_defined_metadata[_METADATA_SOURCE] = src_pkg_version
    mlmodel.user_defined_metadata[_METADATA_VERSION] = _ct_version

    build_info = _get_metadata_from_mlmodel(mlmodel)

    mlmodel._set_build_info_mil_attributes(build_info)

    return mlmodel
