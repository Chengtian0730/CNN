import tensorflow as tf
from nets.net import Net


class MLP(Net):

  def model(self, x):

    x = self._reshape(x)

    with tf.variable_scope('fc0'):
      x = self._fc(x, 256, name='fc0')
      x = self._activation(x)
    with tf.variable_scope('last'):
      x = self._fc(x, self.shape_y[1], name='fc_last')

    return x


