import tensorflow as tf
from layers_tf import *
from metrics import *

class Model(object):
    def __init__(self, **kwargs):
        allowed_kwargs = {'name', 'logging'}
        for kwarg in kwargs.keys():
            assert kwarg in allowed_kwargs, 'Invalid keyword argument: ' + kwarg

        for kwarg in kwargs.keys():
            assert kwarg in allowed_kwargs, 'Invalid keyword argument: ' + kwarg
        name = kwargs.get('name')
        if not name:
            name = self.__class__.__name__.lower()
        self.name = name

        logging = kwargs.get('logging', False)
        self.logging = logging

        self.vars = {}
        self.placeholders = {}

        self.layers = []
        self.activations = []

        self.inputs = None
        self.outputs = None

        self.loss = 0
        self.accuracy = 0
        self.optimizer = None
        self.opt_op = None
        self.global_step = tf.Variable(0, trainable=False)

    def _build(self):
        raise NotImplementedError

    def build(self):
        with tf.variable_scope(self.name):
            self._build()

        self.activations.append(self.inputs)
        for layer in self.layers:
            hidden = layer(self.activations[-1])
            self.activations.append(hidden)
        self.outputs = self.activations[-1]

        variables = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope=self.name)
        self.vars = {var.name: var for var in variables}

        self._loss()
        self._accuracy()

        self.opt_op = self.optimizer.minimize(self.loss, global_step=self.global_step)

    def predict(self):
        pass

    def _loss(self):
        raise NotImplementedError

    def _accuracy(self):
        raise NotImplementedError

    def save(self, sess=None):
        if not sess:
            raise AttributeError("TensorFlow session not provided.")
        saver = tf.train.Saver(self.vars)
        save_path = saver.save(sess, "./%s.ckpt" % self.name)
        print("Model saved in file: %s" % save_path)

    def load(self, sess=None):
        if not sess:
            raise AttributeError("TensorFlow session not provided.")
        saver = tf.train.Saver(self.vars)
        save_path = "./%s.ckpt" % self.name
        saver.restore(sess, save_path)
        print("Model restored from file: %s" % save_path)

class GAE(Model):
    def __init__(self, placeholders, input_dim, feat_hidden_dim, num_classes, num_normalized,
                learning_rate, num_basis_functions, hidden, num_users, num_items,
                num_side_features, **kwargs):
        super(GAE, self).__init__(**kwargs)
        self.inputs = (placeholders['u_features'], placeholders['v_features'])
        self.u_features_side = placeholders['u_features_side']
        self.v_features_side = placeholders['v_features_side']

        self.u_features_nonzero = placeholders['u_features_nonzero']
        self.v_features_nonzero = placeholders['v_features_nonzero']
        self.normalized = placeholders['normalized']
        self.normalized_t = placeholders['normalized_t']
        self.dropout = placeholders['dropout']
        self.labels = placeholders['labels']
        self.u_indices = placeholders['user_indices']
        self.v_indices = placeholders['item_indices']
        self.class_values = placeholders['class_values']

        self.num_side_features = num_side_features
        self.feat_hidden_dim = feat_hidden_dim

        if num_side_features > 0:
            self.u_features_side = placeholders['u_features_side']
            self.v_features_side = placeholders['v_features_side']
        else:
            self.u_features_side = None
            self.v_features_side = None

        self.hidden = hidden
        self.num_basis_functions = num_basis_functions
        self.num_classes = num_classes
        self.num_normalized = num_normalized
        self.input_dim = input_dim
        self.num_users = num_users
        self.num_items = num_items
        self.learning_rate = learning_rate

        self.optimizer = tf.train.AdamOptimizer(learning_rate=self.learning_rate, beta1=0.9, beta2=0.999, epsilon=1.e-8)

        self.build()

        moving_average_decay = 0.995
        self.variable_averages = tf.train.ExponentialMovingAverage(moving_average_decay, self.global_step)
        self.variables_averages_op = self.variable_averages.apply(tf.trainable_variables())

        with tf.control_dependencies([self.opt_op]):
            self.training_op = tf.group(self.variables_averages_op)

        self.embeddings = self.activations[0]

        self._rmse()

    def _loss(self):
        self.loss += softmax_cross_entropy(self.outputs, self.labels)

        tf.summary.scalar('loss', self.loss)

    def _accuracy(self):
        self.rmse = expected_rmse(self.outputs, self.labels, self.class_values)

        tf.summary.scalar('rmse_score', self.rmse)

    def _rmse(self):
        self.rmse = expected_rmse(self.outputs, self.labels, self.class_values)
        tf.summary.scalar('rmse_score', self.rmse)

    def _build(self):
        self.layers.append(StackGCN(input_dim=self.input_dim,
                                    output_dim=self.hidden[0],
                                    normalized=self.normalized,
                                    normalized_t=self.normalized_t,
                                    num_normalized=self.num_normalized,
                                    u_features_nonzero=self.u_features_nonzero,
                                    v_features_nonzero=self.v_features_nonzero,
                                    sparse_inputs=True,
                                    act=tf.nn.relu,
                                    dropout=self.dropout,
                                    logging=self.logging,
                                    share_user_item_weights=True))

        self.layers.append(Dense(input_dim=self.num_side_features,
                                 output_dim=self.feat_hidden_dim,
                                 act=tf.nn.relu,
                                 dropout=0.,
                                 logging=self.logging,
                                 bias=True,
                                 share_user_item_weights=False))

        self.layers.append(Dense(input_dim=self.hidden[0]+self.feat_hidden_dim,
                                 output_dim=self.hidden[1],
                                 act=lambda x: x,
                                 dropout=self.dropout,
                                 logging=self.logging,
                                 share_user_item_weights=False))

        self.layers.append(BilinearMixture(num_classes=self.num_classes,
                                           u_indices=self.u_indices,
                                           v_indices=self.v_indices,
                                           input_dim=self.hidden[1],
                                           num_users=self.num_users,
                                           num_items=self.num_items,
                                           user_item_bias=False,
                                           dropout=0.,
                                           act=lambda x: x,
                                           num_weights=self.num_basis_functions,
                                           logging=self.logging,
                                           diagonal=False))

    def build(self):
        with tf.variable_scope(self.name):
            self._build()

        # gcn layer
        layer = self.layers[0]
        gcn_hidden = layer(self.inputs)

        # dense layer for features
        layer = self.layers[1]
        feat_hidden = layer([self.u_features_side, self.v_features_side])

        # concat dense layer
        layer = self.layers[2]

        gcn_u = gcn_hidden[0]
        gcn_v = gcn_hidden[1]
        feat_u = feat_hidden[0]
        feat_v = feat_hidden[1]

        input_u = tf.concat(values=[gcn_u, feat_u], axis=1)
        input_v = tf.concat(values=[gcn_v, feat_v], axis=1)

        concat_hidden = layer([input_u, input_v])

        self.activations.append(concat_hidden)

        for layer in self.layers[3::]:
            hidden = layer(self.activations[-1])
            self.activations.append(hidden)

        self.outputs = self.activations[-1]

        variables = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope=self.name)
        self.vars = {var.name: var for var in variables}

        self._loss()
        self._accuracy()

        self.opt_op = self.optimizer.minimize(self.loss, global_step=self.global_step)
