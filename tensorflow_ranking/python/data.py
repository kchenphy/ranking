# Copyright 2019 The TensorFlow Ranking Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Input data parsing for ranking library.

Supports data stored in SequenceExample proto format.

SequenceExample (`tf.SequenceExample`) is defined in:
tensorflow/core/example/example.proto
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools
import numpy as np
import six

import tensorflow as tf

# The document relevance label.
_LABEL_FEATURE = "label"

# Padding labels are set negative so that the corresponding examples can be
# ignored in loss and metrics.
_PADDING_LABEL = -1.


def _get_scalar_default_value(dtype, default_value):
  """Gets the scalar compatible default value."""
  if dtype == tf.string:
    return default_value or ""
  elif default_value is None:
    return 0
  if isinstance(default_value, int) or isinstance(default_value, float):
    return default_value
  elif (isinstance(default_value, list) or
        isinstance(default_value, tuple)) and len(default_value) == 1:
    return default_value[0]
  else:
    raise ValueError("Only scalar or equivalent is allowed in default_value.")


def parse_from_sequence_example(serialized,
                                list_size=None,
                                context_feature_spec=None,
                                example_feature_spec=None):
  """Parses SequenceExample to feature maps.

  The `FixedLenFeature` in `example_feature_spec` is converted to
  `FixedLenSequenceFeature` to parse `feature_list` in SequenceExample. We keep
  track of the non-trivial default_values (e.g., -1 for labels) for features in
  `example_feature_spec` and use them to replace the parsing defaults of the
  SequenceExample (i.e., 0 for numbers and "" for strings). Due to this
  complexity, we only allow scalar non-trivial default values for numbers.

  When `list_size` is None, the 2nd dim of the output Tensors are not fixed and
  vary from batch to batch. When `list_size` is specified as a positive integer,
  truncation or padding is applied so that the 2nd dim of the output Tensors is
  the specified `list_size`.

  Args:
    serialized: (Tensor) A string Tensor for a batch of serialized
      SequenceExample.
    list_size: (int) The number of frames to keep for a SequenceExample. If
      specified, truncation or padding may happen. Otherwise, the output Tensors
      have a dynamic list size.
    context_feature_spec: (dict) A mapping from feature keys to
      `FixedLenFeature` or `VarLenFeature` values for context.
    example_feature_spec: (dict) A mapping from feature keys to
      `FixedLenFeature` or `VarLenFeature` values for the list of examples.
      These features are stored in the `feature_lists` field in SequenceExample.
      `FixedLenFeature` is translated to `FixedLenSequenceFeature` to parse
      SequenceExample. Note that no missing value in the middle of a
      `feature_list` is allowed for frames.

  Returns:
    A mapping from feature keys to `Tensor` or `SparseTensor`.
  """
  if list_size is not None and list_size <= 0:
    list_size = None
  # Convert `FixedLenFeature` in `example_feature_spec` to
  # `FixedLenSequenceFeature` to parse the `feature_lists` in SequenceExample.
  # In addition, we collect non-trivial `default_value`s (neither "" nor 0) for
  # post-processing. This is because no `default_value` except None is allowed
  # for `FixedLenSequenceFeature`. Also, we set allow_missing=True and handle
  # the missing feature_list later.
  fixed_len_sequence_features = {}
  padding_values = {}
  for k, s in six.iteritems(example_feature_spec):
    if not isinstance(s, tf.io.FixedLenFeature):
      continue
    fixed_len_sequence_features[k] = tf.io.FixedLenSequenceFeature(
        s.shape, s.dtype, allow_missing=True)
    scalar = _get_scalar_default_value(s.dtype, s.default_value)
    if scalar and not isinstance(scalar, six.text_type) and scalar != 0:
      padding_values[k] = scalar

  sequence_features = example_feature_spec.copy()
  sequence_features.update(fixed_len_sequence_features)
  context, examples, sizes = tf.io.parse_sequence_example(
      serialized,
      context_features=context_feature_spec,
      sequence_features=sequence_features)

  # Reset to no trivial padding values for example features.
  for k, v in six.iteritems(padding_values):
    tensor = examples[k]  # [batch_size, num_frames, feature_size]
    tensor.get_shape().assert_has_rank(3)
    size = tf.reshape(sizes[k], [-1, 1, 1])  # [batch_size, 1, 1]
    rank = tf.reshape(
        tf.tile(tf.range(tf.shape(tensor)[1]), [tf.shape(tensor)[0]]),
        tf.shape(tensor))
    tensor = tf.where(
        tf.less(rank, tf.cast(size, tf.int32)), tensor,
        v * tf.ones_like(tensor))
    examples[k] = tensor

  list_size_arg = list_size
  if list_size is None:
    # Use dynamic list_size. This is needed to pad missing feature_list.
    list_size_dynamic = tf.reduce_max(
        tf.stack([tf.shape(t)[1] for t in six.itervalues(examples)]))
    list_size = list_size_dynamic

  # Collect features. Truncate or pad example features to normalize the tensor
  # shape: [batch_size, num_frames, ...] --> [batch_size, list_size, ...]
  features = {}
  features.update(context)
  for k, t in six.iteritems(examples):
    # Old shape: [batch_size, num_frames, ...]
    shape = tf.shape(input=t)
    ndims = t.get_shape().rank
    num_frames = shape[1]
    # New shape: [batch_size, list_size, ...]
    new_shape = tf.concat([[shape[0], list_size], shape[2:]], 0)

    def truncate_fn(t=t, ndims=ndims, new_shape=new_shape):
      """Truncates the tensor."""
      if isinstance(t, tf.sparse.SparseTensor):
        return tf.sparse.slice(t, [0] * ndims,
                               tf.cast(new_shape, dtype=tf.int64))
      else:
        return tf.slice(t, [0] * ndims, new_shape)

    def pad_fn(k=k,
               t=t,
               ndims=ndims,
               num_frames=num_frames,
               new_shape=new_shape):
      """Pads the tensor."""
      if isinstance(t, tf.sparse.SparseTensor):
        return tf.sparse.reset_shape(t, new_shape)
      else:
        # Paddings has shape [n, 2] where n is the rank of the tensor.
        paddings = tf.stack([[0, 0], [0, list_size - num_frames]] + [[0, 0]] *
                            (ndims - 2))
        pad_val = _get_scalar_default_value(
            example_feature_spec[k].dtype,
            example_feature_spec[k].default_value)
        return tf.pad(tensor=t, paddings=paddings, constant_values=pad_val)

    tensor = tf.cond(
        pred=num_frames > list_size, true_fn=truncate_fn, false_fn=pad_fn)
    # Infer static shape for Tensor. Set the 2nd dim to None and set_shape
    # merges `static_shape` with the existing static shape of the thensor.
    if not isinstance(tensor, tf.sparse.SparseTensor):
      static_shape = t.get_shape().as_list()
      static_shape[1] = list_size_arg
      tensor.set_shape(static_shape)
    features[k] = tensor

  return features


def read_batched_sequence_example_dataset(file_pattern,
                                          batch_size,
                                          list_size,
                                          context_feature_spec,
                                          example_feature_spec,
                                          reader=tf.data.TFRecordDataset,
                                          reader_args=None,
                                          num_epochs=None,
                                          shuffle=True,
                                          shuffle_buffer_size=1000,
                                          shuffle_seed=None,
                                          prefetch_buffer_size=32,
                                          reader_num_threads=10,
                                          sloppy_ordering=True,
                                          drop_final_batch=False):
  """Returns a `Dataset` of features from `SequenceExample`.

  Example:

  ```
  data = [
    sequence_example {
      context {
        feature {
          key: "query_length"
          value { int64_list { value: 3 } }
        }
      }
      feature_lists {
        feature_list {
          key: "unigrams"
          value {
            feature { bytes_list { value: "tensorflow" } }
            feature { bytes_list { value: ["learning" "to" "rank"] } }
          }
        }
        feature_list {
          key: "utility"
          value {
            feature { float_list { value: 0.0 } }
            feature { float_list { value: 1.0 } }
          }
        }
      }
    }
    sequence_example {
      context {
        feature {
          key: "query_length"
          value { int64_list { value: 2 } }
        }
      }
      feature_lists {
        feature_list {
          key: "unigrams"
          value {
            feature { bytes_list { value: "gbdt" } }
            feature { }
          }
        }
        feature_list {
          key: "utility"
          value {
            feature { float_list { value: 0.0 } }
            feature { float_list { value: 0.0 } }
          }
        }
      }
    }
  ]
  ```

  We can use arguments:

  ```
  context_features: {
    "query_length": parsing_ops.FixedenFeature([1], dtypes.int64)
  }
  example_features: {
    "unigrams": parsing_ops.VarLenFeature(dtypes.string),
    "utility": parsing_ops.FixedLenFeature([1], dtypes.float32,
    default_value=[0.])
  }
  batch_size: 2
  ```

  And the expected output is:

  ```python
  {
    "unigrams": SparseTensor(
      indices=array([[0, 0, 0], [0, 1, 0], [0, 1, 1], [0, 1, 2], [1, 0, 0], [1,
      1, 0], [1, 1, 1]]),
      values=["tensorflow", "learning", "to", "rank", "gbdt"],
      dense_shape=array([2, 2, 3])),
    "utility": [[[ 0.], [ 1.]], [[ 0.], [ 0.]]],
    "query_length": [[3], [2]],
  }
  ```

  Args:
    file_pattern: (str | list(str)) List of files or patterns of file paths
      containing tf.SequenceExample protos. See `tf.gfile.Glob` for pattern
      rules.
    batch_size: (int) Number of records to combine in a single batch.
    list_size: (int) The number of frames to keep in a SequenceExample. If
      specified, truncation or padding may happen. Otherwise, set it to None to
      allow dynamic list size.
    context_feature_spec: (dict) A mapping from  feature keys to
      `FixedLenFeature` or `VarLenFeature` values.
    example_feature_spec: (dict) A mapping feature keys to `FixedLenFeature` or
      `VarLenFeature` values.
    reader: A function or class that can be called with a `filenames` tensor and
      (optional) `reader_args` and returns a `Dataset`. Defaults to
      `tf.data.TFRecordDataset`.
    reader_args: (list) Additional argument list to pass to the reader class.
    num_epochs: (int) Number of times to read through the dataset. If None,
      cycles through the dataset forever. Defaults to `None`.
    shuffle: (bool) Indicates whether the input should be shuffled. Defaults to
      `True`.
    shuffle_buffer_size: (int) Buffer size of the ShuffleDataset. A large
      capacity ensures better shuffling but would increase memory usage and
      startup time.
    shuffle_seed: (int) Randomization seed to use for shuffling.
    prefetch_buffer_size: (int) Number of feature batches to prefetch in order
      to improve performance. Recommended value is the number of batches
      consumed per training step (default is 1).
    reader_num_threads: (int) Number of threads used to read records. If greater
      than 1, the results will be interleaved.
    sloppy_ordering: (bool) If `True`, reading performance will be improved at
      the cost of non-deterministic ordering. If `False`, the order of elements
      produced is deterministic prior to shuffling (elements are still
      randomized if `shuffle=True`. Note that if the seed is set, then order of
      elements after shuffling is deterministic). Defaults to `False`.
    drop_final_batch: (bool) If `True`, and the batch size does not evenly
      divide the input dataset size, the final smaller batch will be dropped.
      Defaults to `True`. If `True`, the batch_size can be statically inferred.

  Returns:
    A dataset of `dict` elements. Each `dict` maps feature keys to
    `Tensor` or `SparseTensor` objects. The context features are mapped to a
    rank-2 tensor of shape [batch_size, feature_size], and the example features
    are mapped to a rank-3 tensor of shape [batch_size, list_size,
    feature_size], where list_size is the number of examples.
  """
  # TODO: Move the file reading part into a common function for all
  # batch readers.
  files = tf.data.Dataset.list_files(
      file_pattern, shuffle=shuffle, seed=shuffle_seed)

  reader_args = reader_args or []
  dataset = files.apply(
      tf.data.experimental.parallel_interleave(
          lambda filename: reader(filename, *reader_args),
          cycle_length=reader_num_threads,
          sloppy=sloppy_ordering))

  # Extract values if tensors are stored as key-value tuples. This happens when
  # the reader is tf.data.SSTableDataset.
  if dataset.output_types == (tf.string, tf.string):
    dataset = dataset.map(lambda _, v: v)

  # Repeat and shuffle, if needed.
  if num_epochs != 1:
    dataset = dataset.repeat(num_epochs)
  if shuffle:
    dataset = dataset.shuffle(
        buffer_size=shuffle_buffer_size, seed=shuffle_seed)

  # Apply batching. If drop_remainder is True, allows for static inference of
  # batch size.
  dataset = dataset.batch(
      batch_size, drop_remainder=drop_final_batch or num_epochs is None)

  # Parse batched SequenceExample.
  kwargs = {
      "list_size": list_size,
      "context_feature_spec": context_feature_spec,
      "example_feature_spec": example_feature_spec,
  }
  dataset = dataset.map(
      functools.partial(parse_from_sequence_example, **kwargs))

  # Prefetching allows for data fetching to happen on host while model runs
  # on the accelerator. When run on CPU, makes data fecthing asynchronous.
  dataset = dataset.prefetch(buffer_size=prefetch_buffer_size)

  return dataset


def build_sequence_example_serving_input_receiver_fn(input_size,
                                                     context_feature_spec,
                                                     example_feature_spec,
                                                     default_batch_size=None):
  """Creates a serving_input_receiver_fn for `SequenceExample` inputs.

  A string placeholder is used for inputs. Note that the context_feature_spec
  and example_feature_spec shouldn't contain weights, labels or training
  only features in general.

  Args:
    input_size: (int) The number of frames to keep in a SequenceExample. If
      specified, truncation or padding may happen. Otherwise, set it to None to
      allow dynamic list size (recommended).
    context_feature_spec: (dict) Map from feature keys to `FixedLenFeature` or
      `VarLenFeature` values.
    example_feature_spec: (dict) Map from  feature keys to `FixedLenFeature` or
      `VarLenFeature` values.
    default_batch_size: (int) Number of query examples expected per batch. Leave
      unset for variable batch size (recommended).

  Returns:
    A `tf.estimator.export.ServingInputReceiver` object, which packages the
    placeholders and the resulting feature Tensors together.
  """

  def serving_input_receiver_fn():
    """An input function on serialized SequenceExample protos."""
    serialized_sequence_example = tf.compat.v1.placeholder(
        dtype=tf.string,
        shape=[default_batch_size],
        name="input_sequence_example_tensor")
    receiver_tensors = {"sequence_example": serialized_sequence_example}
    features = parse_from_sequence_example(
        serialized_sequence_example,
        list_size=input_size,
        context_feature_spec=context_feature_spec,
        example_feature_spec=example_feature_spec)

    return tf.estimator.export.ServingInputReceiver(features, receiver_tensors)

  return serving_input_receiver_fn


def _libsvm_parse_line(libsvm_line):
  """Parses a single LibSVM line to a query ID and a feature dictionary.

  Args:
    libsvm_line: (string) input line in LibSVM format.

  Returns:
    A tuple of query ID and a dict mapping from feature ID (string) to value
    (float). "label" is a special feature ID that represents the relevance
    grade.
  """
  tokens = libsvm_line.split()
  qid = int(tokens[1].split(":")[1])

  features = {_LABEL_FEATURE: float(tokens[0])}
  key_values = [key_value.split(":") for key_value in tokens[2:]]
  features.update({key: float(value) for (key, value) in key_values})

  return qid, features


def _libsvm_generate(num_features, list_size, doc_list):
  """Unpacks a list of document features into `Tensor`s.

  Args:
    num_features: An integer representing the number of features per instance.
    list_size: Size of the document list per query.
    doc_list: A list of dictionaries (one per document) where each dictionary is
      a mapping from feature ID (string) to feature value (float).

  Returns:
    A tuple consisting of a dictionary (feature ID to `Tensor`s) and a label
    `Tensor`.
  """
  # Construct output variables.
  features = {}
  for fid in range(num_features):
    features[str(fid + 1)] = np.zeros([list_size, 1], dtype=np.float32)
  labels = np.ones([list_size], dtype=np.float32) * (_PADDING_LABEL)

  # Shuffle the document list and trim to a prescribed list_size.
  np.random.shuffle(doc_list)

  if len(doc_list) > list_size:
    doc_list = doc_list[:list_size]

  # Fill in the output Tensors with feature and label values.
  for idx, doc in enumerate(doc_list):
    for feature_id, value in six.iteritems(doc):
      if feature_id == _LABEL_FEATURE:
        labels[idx] = value
      else:
        features.get(feature_id)[idx, 0] = value

  return features, labels


def libsvm_generator(path, num_features, list_size, seed=None):
  """Parses a LibSVM-formatted input file and aggregates data points by qid.

  Args:
    path: (string) path to dataset in the LibSVM format.
    num_features: An integer representing the number of features per instance.
    list_size: Size of the document list per query.
    seed: Randomization seed used when shuffling the document list.

  Returns:
    A generator function that can be passed to tf.data.Dataset.from_generator().
  """
  if seed is not None:
    np.random.seed(seed)

  def inner_generator():
    """Produces a generator ready for tf.data.Dataset.from_generator.

    It is assumed that data points in a LibSVM-formatted input file are
    sorted by query ID before being presented to this function. This
    assumption simplifies the parsing and aggregation logic: We consume
    lines sequentially and accumulate query-document features until a
    new query ID is observed, at which point the accumulated data points
    are massaged into a tf.data.Dataset compatible representation.

    Yields:
      A tuple of feature and label `Tensor`s.
    """
    # A buffer where observed query-document features will be stored.
    # It is a list of dictionaries, one per query-document pair, where
    # each dictionary is a mapping from a feature ID to a feature value.
    doc_list = []

    with tf.io.gfile.GFile(path, "r") as f:
      # cur indicates the current query ID.
      cur = -1

      for line in f:
        qid, doc = _libsvm_parse_line(line)
        if cur < 0:
          cur = qid

        # If qid is not new store the data and move onto the next line.
        if qid == cur:
          doc_list.append(doc)
          continue

        yield _libsvm_generate(num_features, list_size, doc_list)

        # Reset current pointer and re-initialize document list.
        cur = qid
        doc_list = [doc]

    yield _libsvm_generate(num_features, list_size, doc_list)

  return inner_generator
