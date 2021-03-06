import logging
import math

from keras.losses import binary_crossentropy
import numpy as np
from sklearn.utils import check_random_state
import tensorflow as tf

from csrank.learner import Learner
from csrank.numpy_util import sigmoid
from csrank.util import progress_bar

logger = logging.getLogger(__name__)


class FATELinearCore(Learner):
    def __init__(
        self,
        n_hidden_set_units=32,
        learning_rate=1e-3,
        batch_size=256,
        loss_function=binary_crossentropy,
        epochs_drop=300,
        drop=0.1,
        random_state=None,
        **kwargs,
    ):
        self.n_hidden_set_units = n_hidden_set_units
        self.learning_rate = learning_rate
        self.batch_size = batch_size
        self.random_state = random_state
        self.loss_function = loss_function
        self.epochs_drop = epochs_drop
        self.drop = drop

    def _construct_model_(self, n_objects):
        self.X = tf.placeholder(
            "float32", [None, n_objects, self.n_object_features_fit_]
        )
        self.Y = tf.placeholder("float32", [None, n_objects])
        std = 1 / np.sqrt(self.n_object_features_fit_)
        self.b1 = tf.Variable(
            self.random_state_.normal(loc=0, scale=std, size=self.n_hidden_set_units),
            dtype=tf.float32,
        )
        self.W1 = tf.Variable(
            self.random_state_.normal(
                loc=0,
                scale=std,
                size=(self.n_object_features_fit_, self.n_hidden_set_units),
            ),
            dtype=tf.float32,
        )
        self.W2 = tf.Variable(
            self.random_state_.normal(
                loc=0,
                scale=std,
                size=(self.n_object_features_fit_ + self.n_hidden_set_units),
            ),
            dtype=tf.float32,
        )
        self.b2 = tf.Variable(
            self.random_state_.normal(loc=0, scale=std, size=1), dtype=tf.float32
        )

        set_rep = (
            tf.reduce_mean(tf.tensordot(self.X, self.W1, axes=1), axis=1) + self.b1
        )

        self.set_rep = tf.reshape(
            tf.tile(set_rep, tf.constant([1, n_objects])),
            (-1, n_objects, self.n_hidden_set_units),
        )
        self.X_con = tf.concat([self.X, self.set_rep], axis=-1)
        scores = tf.sigmoid(tf.tensordot(self.X_con, self.W2, axes=1) + self.b2)
        scores = tf.cast(scores, tf.float32)
        self.loss_ = self.loss_function(self.Y, scores)
        self.optimizer_ = tf.train.GradientDescentOptimizer(
            self.learning_rate
        ).minimize(self.loss_)

    def step_decay(self, epoch):
        step = math.floor((1 + epoch) / self.epochs_drop)
        self.current_lr_ = self.learning_rate * math.pow(self.drop, step)
        self.optimizer_ = tf.train.GradientDescentOptimizer(self.current_lr_).minimize(
            self.loss_
        )

    def _pre_fit(self):
        super()._pre_fit()
        self.random_state_ = check_random_state(self.random_state)

    def fit(
        self, X, Y, epochs=10, callbacks=None, validation_split=0.1, verbose=0, **kwd
    ):
        self._pre_fit()
        # Global Variables Initializer
        n_instances, self.n_objects_fit_, self.n_object_features_fit_ = X.shape
        self._construct_model_(self.n_objects_fit_)
        init = tf.global_variables_initializer()

        with tf.Session() as tf_session:
            tf_session.run(init)
            self._fit_(X, Y, epochs, n_instances, tf_session, verbose)
            training_cost = tf_session.run(self.loss_, feed_dict={self.X: X, self.Y: Y})
            logger.info(
                "Fitting completed {} epochs done with loss {}".format(
                    epochs, training_cost.mean()
                )
            )
            self.weight1_ = tf_session.run(self.W1)
            self.bias1_ = tf_session.run(self.b1)
            self.weight2_ = tf_session.run(self.W2)
            self.bias2_ = tf_session.run(self.b2)

    def _fit_(self, X, Y, epochs, n_instances, tf_session, verbose):
        try:
            for epoch in range(epochs):
                for start in range(0, n_instances, self.batch_size):
                    end = np.min([start + self.batch_size, n_instances])
                    tf_session.run(
                        self.optimizer_,
                        feed_dict={self.X: X[start:end], self.Y: Y[start:end]},
                    )
                    if verbose == 1:
                        progress_bar(end, n_instances, status="Fitting")
                if verbose == 1:
                    c = tf_session.run(self.loss_, feed_dict={self.X: X, self.Y: Y})
                    print("Epoch {}: cost {} ".format((epoch + 1), np.mean(c)))
                if (epoch + 1) % 100 == 0:
                    c = tf_session.run(self.loss_, feed_dict={self.X: X, self.Y: Y})
                    logger.info("Epoch {}: cost {} ".format((epoch + 1), np.mean(c)))
                self.step_decay(epoch)
        except KeyboardInterrupt:
            logger.info("Interrupted")
            c = tf_session.run(self.loss_, feed_dict={self.X: X, self.Y: Y})
            logger.info("Epoch {}: cost {} ".format((epoch + 1), np.mean(c)))

    def _predict_scores_fixed(self, X, **kwargs):
        n_instances, n_objects, n_features = X.shape
        assert n_features == self.n_object_features_fit_
        rep = np.mean(np.dot(X, self.weight1_), axis=1) + self.bias1_
        rep = np.tile(rep[:, np.newaxis, :], (1, n_objects, 1))
        X_n = np.concatenate((X, rep), axis=2)
        scores = np.dot(X_n, self.weight2_) + self.bias2_
        scores = sigmoid(scores)
        return scores
