# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================
"""Provides utilities to preprocess images.
Training images are sampled using the provided bounding boxes, and subsequently
cropped to the sampled bounding box. Images are additionally flipped randomly,
then resized to the target output size (without aspect-ratio preservation).
Images used during evaluation are resized (with aspect-ratio preservation) and
centrally cropped.
All images undergo mean color subtraction.
Note that these steps are colloquially referred to as "ResNet preprocessing,"
and they differ from "VGG preprocessing," which does not use bounding boxes
and instead does an aspect-preserving resize followed by random crop during
training. (These both differ from "Inception preprocessing," which introduces
color distortion steps.)
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf

from preprocess.preprocess_ops import *


def _parse_example_proto(example_serialized):
  """Parses an Example proto containing a training example of an image.
  The output of the build_image_data.py image preprocessing script is a dataset
  containing serialized Example protocol buffers. Each Example proto contains
  the following fields (values are included as examples):
    image/height: 462
    image/width: 581
    image/colorspace: 'RGB'
    image/channels: 3
    image/class/label: 615
    image/class/synset: 'n03623198'
    image/class/text: 'knee pad'
    image/object/bbox/xmin: 0.1
    image/object/bbox/xmax: 0.9
    image/object/bbox/ymin: 0.2
    image/object/bbox/ymax: 0.6
    image/object/bbox/label: 615
    image/format: 'JPEG'
    image/filename: 'ILSVRC2012_val_00041207.JPEG'
    image/encoded: <JPEG encoded string>
  Args:
    example_serialized: scalar Tensor tf.string containing a serialized
      Example protocol buffer.
  Returns:
    image_buffer: Tensor tf.string containing the contents of a JPEG file.
    label: Tensor tf.int32 containing the label.
    bbox: 3-D float Tensor of bounding boxes arranged [1, num_boxes, coords]
      where each coordinate is [0, 1) and the coordinates are arranged as
      [ymin, xmin, ymax, xmax].
  """
  # Dense features in Example proto.
  feature_map = {
      'image/encoded': tf.io.FixedLenFeature([], dtype=tf.string,
                                          default_value=''),
      'image/class/label': tf.io.FixedLenFeature([], dtype=tf.int64,
                                              default_value=-1),
      'image/class/text': tf.io.FixedLenFeature([], dtype=tf.string,
                                             default_value=''),
  }
  sparse_float32 = tf.io.VarLenFeature(dtype=tf.float32)
  # Sparse features in Example proto.
  feature_map.update(
      {k: sparse_float32 for k in ['image/object/bbox/xmin',
                                   'image/object/bbox/ymin',
                                   'image/object/bbox/xmax',
                                   'image/object/bbox/ymax']})

  features = tf.io.parse_single_example(example_serialized, feature_map)
  label = tf.cast(features['image/class/label'], dtype=tf.int32)

  xmin = tf.expand_dims(features['image/object/bbox/xmin'].values, 0)
  ymin = tf.expand_dims(features['image/object/bbox/ymin'].values, 0)
  xmax = tf.expand_dims(features['image/object/bbox/xmax'].values, 0)
  ymax = tf.expand_dims(features['image/object/bbox/ymax'].values, 0)

  # Note that we impose an ordering of (y, x) just to make life difficult.
  bbox = tf.concat([ymin, xmin, ymax, xmax], 0)

  # Force the variable number of bounding boxes into the shape
  # [1, num_boxes, coords].
  bbox = tf.expand_dims(bbox, 0)
  bbox = tf.transpose(bbox, [0, 2, 1])

  return features['image/encoded'], label, bbox


def _decode_crop(image_buffer, bbox, num_channels):
  """Crops the given image to a random part of the image, and randomly flips.
  We use the fused decode_and_crop op, which performs better than the two ops
  used separately in series, but note that this requires that the image be
  passed in as an un-decoded string Tensor.
  Args:
    image_buffer: scalar string Tensor representing the raw JPEG image buffer.
    bbox: 3-D float Tensor of bounding boxes arranged [1, num_boxes, coords]
      where each coordinate is [0, 1) and the coordinates are arranged as
      [ymin, xmin, ymax, xmax].
    num_channels: Integer depth of the image buffer for decoding.
  Returns:
    3-D tensor with cropped image.
  """
  # A large fraction of image datasets contain a human-annotated bounding box
  # delineating the region of the image containing the object of interest.  We
  # choose to create a new bounding box for the object which is a randomly
  # distorted version of the human-annotated bounding box that obeys an
  # allowed range of aspect ratios, sizes and overlap with the human-annotated
  # bounding box. If no box is supplied, then we assume the bounding box is
  # the entire image.
  sample_distorted_bounding_box = tf.image.sample_distorted_bounding_box(
      tf.image.extract_jpeg_shape(image_buffer),
      bounding_boxes=bbox,
      min_object_covered=0.1,
      aspect_ratio_range=[0.75, 1.33],
      area_range=[0.05, 1.0],
      max_attempts=100,
      use_image_if_no_bounding_boxes=True)
  bbox_begin, bbox_size, _ = sample_distorted_bounding_box

  # Reassemble the bounding box in the format the crop op requires.
  offset_y, offset_x, _ = tf.unstack(bbox_begin)
  target_height, target_width, _ = tf.unstack(bbox_size)
  crop_window = tf.stack([offset_y, offset_x, target_height, target_width])

  # Use the fused decode and crop op here, which is faster than each in series.
  cropped = tf.image.decode_and_crop_jpeg(
      image_buffer, crop_window, channels=num_channels)

  return cropped


def parse_record(raw_record, is_training):
  """Parses a record containing a training example of an image.
  The input record is parsed into a label and image, and the image is passed
  through preprocessing steps (cropping, flipping, and so on).
  Args:
    raw_record: scalar Tensor tf.string containing a serialized
      Example protocol buffer.
    is_training: A boolean denoting whether the input is for training.
  Returns:
    Tuple with processed image tensor and one-hot-encoded label tensor.
  """
  image_buffer, label, bbox = _parse_example_proto(raw_record)

  image = preprocess_image(
      image_buffer=image_buffer,
      bbox=bbox,
      output_height=224,
      output_width=224,
      num_channels=3,
      is_training=is_training,
      fast_mode=True)

  return image, label


def preprocess_image(image_buffer, bbox, output_height, output_width,
                     num_channels, is_training=False, fast_mode=True):

  if is_training:

    # For training, we want to randomize some of the distortions.
    image = _decode_crop(image_buffer, bbox, num_channels)

    if image.dtype != tf.float32:
      image = tf.image.convert_image_dtype(image, dtype=tf.float32)

    image = resize_image(image, output_height, output_width)

    # Flip to add a little more random distortion in.
    image = tf.image.random_flip_left_right(image)

    # Randomly distort the colors. There are 1 or 4 ways to do it.
    num_distort_cases = 1 if fast_mode else 4
    image = apply_with_random_selector(
      image,
      lambda x, ordering: distort_color(x, ordering, fast_mode),
      num_cases=num_distort_cases)

  else:
    # For validation, we want to decode, resize, then just crop the middle.
    image = tf.image.decode_jpeg(image_buffer, channels=num_channels)

    if image.dtype != tf.float32:
      image = tf.image.convert_image_dtype(image, dtype=tf.float32)

    image = aspect_preserving_resize(image, 256)
    image = central_crop(image, output_height, output_width)

  image.set_shape([output_height, output_width, num_channels])

  return image
