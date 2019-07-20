import tensorflow as tf
import numpy as np
from nets.net import Net
from utils.active_shift.active_shift2d_op import active_shift2d


class ShiftNet(Net):

  def group_shift(self, x, active=False):

    kernel = np.array([
      [-1, -1, -1, 0, 0, 0, 1, 1, 1],
      [-1, 0, 1, -1, 0, 1, -1, 0, 1]
    ], dtype=np.float32)

    c_in = self.get_shape(x)[1]
    num_pattern = kernel.shape[1]
    assert c_in % num_pattern == 0, 'channel can not be divided by %d' % num_pattern

    kernels = np.tile(kernel, c_in//num_pattern)
    if active:
      kernels = tf.get_variable(name='shift', shape=[2, c_in], initializer=tf.initializers.constant(kernels))
    else:
      kernels = tf.constant(kernels, dtype=tf.float32)
    x = active_shift2d(x, kernels, strides=[1, 1, 1, 1], paddings=[0, 0, 0, 0])

    return x

  def CSC(self, x, c_out, stride=1, expansion=6, active=False):
    c_in = self.get_shape(x)[1]
    orig_x = x

    with tf.variable_scope('C0'):
      x = self.batch_norm(x)
      x = self.activation(x)
      x = self.conv(x, 1, c_in * expansion, stride=1)

    x = self.group_shift(x, active=active)

    with tf.variable_scope('C1'):
      x = self.batch_norm(x)
      x = self.activation(x)
      x = self.conv(x, 1, c_in, stride=stride)

    if stride is not 1 or c_in != c_out:
      orig_x = self.pool(orig_x, type='AVG', ksize=3, stride=2)
      x = tf.concat([x, orig_x], axis=1)
    else:
      x += orig_x

    return x

  def model(self, x):

    print('ShiftNet for %s dataset' % self.dataset)

    if self.dataset in ['cifar10', 'cifar100']:

      num_residual = 9  # totoal layer: 6n+2
      strides = [1, 2, 2]
      filters = [18, 36, 72]
      expansion = 6
      active = False

      with tf.variable_scope('init'):
        x = self.conv(x, 3, filters[0])

      for i in range(len(filters)):
        for j in range(num_residual):
          with tf.variable_scope('U%d-%d' % (i, j)):
            x = self.CSC(x, filters[i], stride=strides[i] if j == 0 else 1, expansion=expansion, active=active)

      with tf.variable_scope('global_avg_pool'):
        x = self.batch_norm(x)
        x = self.activation(x)
        x = self.pool(x, 'GLO')

      with tf.variable_scope('logit'):
        x = self.fc(x, self.shape_y[1], name='fc')

      return x

    elif self.dataset in ['imagenet']:

      # TODO: implement other shift kernel, e.g., 5x5, 7x7.

      with tf.variable_scope('init'):
        x = self.conv(x, 7, 32, stride=2)

      strides = [2, 1, 2, 1, 2, 1, 2, 1]
      repeat = [1, 4, 1, 5, 1, 6, 1, 2]
      expansion = [4, 4, 4, 3, 3, 2, 2, 1]
      c_out = [64, 64, 128, 128, 256, 256, 512, 512]

      for i in range(len(strides)):
        for j in range(repeat[i]):
          with tf.variable_scope('U%d-%d' % (i, j)):
            x = self.CSC(x, c_out[i], strides[i], expansion[i])

      with tf.variable_scope('global_avg_pool'):
        x = self.batch_norm(x)
        x = self.activation(x)
        x = self.pool(x, 'GLO')

      with tf.variable_scope('logit'):
        x = self.fc(x, self.shape_y[1], name='fc')

      return x
