from itertools import combinations
import logging

from keras import backend as K
from keras import Input
from keras import Model
from keras.layers import Activation
from keras.layers import add
from keras.layers import concatenate
from keras.layers import Dense
from keras.layers import Lambda
from keras.losses import binary_crossentropy
from keras.optimizers import SGD
from keras.regularizers import l2
import numpy as np
from sklearn.model_selection import train_test_split

from csrank.core.feta_network import FETANetwork
from csrank.layers import NormalizedDense
from csrank.numpy_util import sigmoid
from .choice_functions import ChoiceFunctions

logger = logging.getLogger(__name__)


class FETAChoiceFunction(ChoiceFunctions, FETANetwork):
    def __init__(
        self,
        n_hidden=2,
        n_units=8,
        add_zeroth_order_model=False,
        max_number_of_objects=10,
        num_subsample=5,
        loss_function=binary_crossentropy,
        batch_normalization=False,
        kernel_regularizer=l2,
        kernel_initializer="lecun_normal",
        activation="selu",
        optimizer=SGD,
        metrics=("binary_accuracy",),
        batch_size=256,
        random_state=None,
        **kwargs,
    ):
        """
            Create a FETA-network architecture for learning choice functions.
            The first-evaluate-then-aggregate approach approximates the context-dependent utility function using the
            first-order utility function :math:`U_1 \\colon \\mathcal{X} \\times \\mathcal{X} \\rightarrow [0,1]`
            and zeroth-order utility function  :math:`U_0 \\colon \\mathcal{X} \\rightarrow [0,1]`.
            The scores each object :math:`x` using a context-dependent utility function :math:`U (x, C_i)`:

            .. math::
                 U(x_i, C_i) = U_0(x_i) + \\frac{1}{n-1} \\sum_{x_j \\in Q \\setminus \\{x_i\\}} U_1(x_i , x_j) \\, .

            Training and prediction complexity is quadratic in the number of objects.
            The choice set is defined as:

            .. math::

                c(Q) = \\{ x_i \\in Q \\lvert \\, U (x_i, C_i) > t \\}

            Parameters
            ----------
            n_hidden : int
                Number of hidden layers
            n_units : int
                Number of hidden units in each layer
            add_zeroth_order_model : bool
                True if the model should include a latent utility function
            max_number_of_objects : int
                The maximum number of objects to train from
            num_subsample : int
                Number of objects to subsample to
            loss_function : function
                Differentiable loss function for the score vector
            batch_normalization : bool
                Whether to use batch normalization in the hidden layers
            kernel_regularizer : uninitialized keras regularizer
                Regularizer to use in the hidden units
            kernel_initializer : function or string
                Initialization function for the weights of each hidden layer
            activation : string or function
                Activation function to use in the hidden units
            optimizer: Class
                Uninitialized optimizer class following the keras optimizer interface.
            optimizer__{kwarg}
                Arguments to be passed to the optimizer on initialization, such as optimizer__lr.
            metrics : list
                List of evaluation metrics (can be non-differentiable)
            batch_size : int
                Batch size to use for training
            random_state : int or object
                Numpy random state
            hidden_dense_layer__{kwarg}
                Arguments to be passed to the Dense layers (or NormalizedDense
                if batch_normalization is enabled). See the keras documentation
                for those classes for available options.
        """
        self._store_kwargs(
            kwargs, {"optimizer__", "kernel_regularizer__", "hidden_dense_layer__"}
        )
        super().__init__(
            n_hidden=n_hidden,
            n_units=n_units,
            add_zeroth_order_model=add_zeroth_order_model,
            max_number_of_objects=max_number_of_objects,
            num_subsample=num_subsample,
            loss_function=loss_function,
            batch_normalization=batch_normalization,
            kernel_regularizer=kernel_regularizer,
            kernel_initializer=kernel_initializer,
            activation=activation,
            optimizer=optimizer,
            metrics=metrics,
            batch_size=batch_size,
            random_state=random_state,
        )

    def _construct_layers(self):
        self.input_layer = Input(
            shape=(self.n_objects_fit_, self.n_object_features_fit_)
        )
        # Todo: Variable sized input
        # X = Input(shape=(None, n_features))
        hidden_dense_kwargs = {
            "kernel_regularizer": self.kernel_regularizer_,
            "kernel_initializer": self.kernel_initializer,
            "activation": self.activation,
        }
        hidden_dense_kwargs.update(self._get_prefix_attributes("hidden_dense_layer__"))
        if self.batch_normalization:
            if self.add_zeroth_order_model:
                self.hidden_layers_zeroth = [
                    NormalizedDense(
                        self.n_units,
                        name="hidden_zeroth_{}".format(x),
                        *hidden_dense_kwargs,
                    )
                    for x in range(self.n_hidden)
                ]
            self.hidden_layers = [
                NormalizedDense(
                    self.n_units, name="hidden_{}".format(x), **hidden_dense_kwargs
                )
                for x in range(self.n_hidden)
            ]
        else:
            if self.add_zeroth_order_model:
                self.hidden_layers_zeroth = [
                    Dense(
                        self.n_units,
                        name="hidden_zeroth_{}".format(x),
                        **hidden_dense_kwargs,
                    )
                    for x in range(self.n_hidden)
                ]
            self.hidden_layers = [
                Dense(self.n_units, name="hidden_{}".format(x), **hidden_dense_kwargs)
                for x in range(self.n_hidden)
            ]
        assert len(self.hidden_layers) == self.n_hidden
        self.output_node = Dense(
            1, activation="linear", kernel_regularizer=self.kernel_regularizer_
        )
        if self.add_zeroth_order_model:
            self.output_node_zeroth = Dense(
                1, activation="linear", kernel_regularizer=self.kernel_regularizer_
            )

    def construct_model(self):
        """
            Construct the :math:`1`-st order and :math:`0`-th order models, which are used to approximate the
            :math:`U_1(x, C(x))` and the :math:`U_0(x)` utilities respectively. For each pair of objects in
            :math:`x_i, x_j \\in Q` :math:`U_1(x, C(x))` we construct :class:`CmpNetCore` with weight sharing to
            approximate a pairwise-matrix. A pairwise matrix with index (i,j) corresponds to the :math:`U_1(x_i,x_j)`
            is a measure of how favorable it is to choose :math:`x_i` over :math:`x_j`. Using this matrix we calculate
            the borda score for each object to calculate :math:`U_1(x, C(x))`. For `0`-th order model we construct
            :math:`\\lvert Q \\lvert` sequential networks whose weights are shared to evaluate the :math:`U_0(x)` for
            each object in the query set :math:`Q`. The output mode is using sigmoid activation.

            Returns
            -------
            model: keras :class:`Model`
                Neural network to learn the FETA utility score
        """

        def create_input_lambda(i):
            return Lambda(lambda x: x[:, i])

        if self.add_zeroth_order_model:
            logger.debug("Create 0th order model")
            zeroth_order_outputs = []
            inputs = []
            for i in range(self.n_objects_fit_):
                x = create_input_lambda(i)(self.input_layer)
                inputs.append(x)
                for hidden in self.hidden_layers_zeroth:
                    x = hidden(x)
                zeroth_order_outputs.append(self.output_node_zeroth(x))
            zeroth_order_scores = concatenate(zeroth_order_outputs)
            logger.debug("0th order model finished")
        logger.debug("Create 1st order model")
        outputs = [list() for _ in range(self.n_objects_fit_)]
        for i, j in combinations(range(self.n_objects_fit_), 2):
            if self.add_zeroth_order_model:
                x1 = inputs[i]
                x2 = inputs[j]
            else:
                x1 = create_input_lambda(i)(self.input_layer)
                x2 = create_input_lambda(j)(self.input_layer)
            x1x2 = concatenate([x1, x2])
            x2x1 = concatenate([x2, x1])

            for hidden in self.hidden_layers:
                x1x2 = hidden(x1x2)
                x2x1 = hidden(x2x1)

            merged_left = concatenate([x1x2, x2x1])
            merged_right = concatenate([x2x1, x1x2])

            N_g = self.output_node(merged_left)
            N_l = self.output_node(merged_right)

            outputs[i].append(N_g)
            outputs[j].append(N_l)
        # convert rows of pairwise matrix to keras layers:
        outputs = [concatenate(x) for x in outputs]

        # compute utility scores:
        scores = [
            Lambda(lambda s: K.mean(s, axis=1, keepdims=True))(x) for x in outputs
        ]
        scores = concatenate(scores)
        logger.debug("1st order model finished")
        if self.add_zeroth_order_model:
            scores = add([scores, zeroth_order_scores])
        scores = Activation("sigmoid")(scores)
        model = Model(inputs=self.input_layer, outputs=scores)
        logger.debug("Compiling complete model...")
        model.compile(
            loss=self.loss_function,
            optimizer=self.optimizer_,
            metrics=list(self.metrics),
        )
        return model

    def _predict_scores_using_pairs(self, X, **kwd):
        scores = super()._predict_scores_using_pairs(X=X, **kwd)
        scores = sigmoid(scores)
        return scores

    def fit(
        self,
        X,
        Y,
        epochs=10,
        callbacks=None,
        validation_split=0.1,
        tune_size=0.1,
        thin_thresholds=1,
        verbose=0,
        **kwd,
    ):
        """
            Fit a FETA-Network for learning a choice function on the provided set of queries X and preferences Y of
            those objects. The provided queries and corresponding preferences are of a fixed size (numpy arrays).

            Parameters
            ----------
            X : numpy array (n_instances, n_objects, n_features)
                Feature vectors of the objects
            Y : numpy array (n_instances, n_objects)
                Choices for given objects in the query
            epochs : int
                Number of epochs to run if training for a fixed query size
            callbacks : list
                List of callbacks to be called during optimization
            validation_split : float (range : [0,1])
                Percentage of instances to split off to validate on
            verbose : bool
                Print verbose information
            tune_size: float (range : [0,1])
                Percentage of instances to split off to tune the threshold for the choice function
            thin_thresholds: int
                The number of instances of scores to skip while tuning the threshold
            **kwd :
                Keyword arguments for the fit function
        """
        self._pre_fit()
        if tune_size > 0:
            X_train, X_val, Y_train, Y_val = train_test_split(
                X, Y, test_size=tune_size, random_state=self.random_state
            )
            try:
                super().fit(
                    X_train,
                    Y_train,
                    epochs,
                    callbacks,
                    validation_split,
                    verbose,
                    **kwd,
                )
            finally:
                logger.info(
                    "Fitting utility function finished. Start tuning threshold."
                )
                self.threshold_ = self._tune_threshold(
                    X_val, Y_val, thin_thresholds=thin_thresholds, verbose=verbose
                )
        else:
            super().fit(X, Y, epochs, callbacks, validation_split, verbose, **kwd)
            self.threshold_ = 0.5

    def sub_sampling(self, X, Y):
        if self.n_objects_fit_ <= self.max_number_of_objects:
            return X, Y
        n_objects = self.max_number_of_objects
        bucket_size = int(X.shape[1] / n_objects)
        X_train = []
        Y_train = []
        for x, y in zip(X, Y):
            ind_1 = np.where(y == 1)[0]
            p_1 = np.zeros(len(ind_1)) + 1 / len(ind_1)
            if (y == 1).sum() < n_objects:
                ind_0 = np.where(y == 0)[0]
                p_0 = np.zeros(len(ind_0)) + 1 / len(ind_0)
                positives = (y == 1).sum() if n_objects > (y == 1).sum() else n_objects
                if positives > bucket_size:
                    cp = (
                        self.random_state.choice(
                            positives, size=bucket_size, replace=False
                        )
                        + 1
                    )
                else:
                    cp = self.random_state.choice(positives, size=bucket_size) + 1
                idx = []
                for c in cp:
                    pos = self.random_state.choice(
                        len(ind_1), size=c, replace=False, p=p_1
                    )
                    if n_objects - c > len(ind_0):
                        neg = self.random_state.choice(
                            len(ind_0), size=n_objects - c, p=p_0
                        )
                    else:
                        neg = self.random_state.choice(
                            len(ind_0), size=n_objects - c, replace=False, p=p_0
                        )
                    p_0[neg] = 0.2 * p_0[neg]
                    p_0 = p_0 / p_0.sum()
                    i = np.concatenate((ind_1[pos], ind_0[neg]))
                    self.random_state.shuffle(i)
                    p_1[pos] = 0.2 * p_1[pos]
                    p_1 = p_1 / p_1.sum()
                    p_0[neg] = 0.2 * p_0[neg]
                    p_0 = p_0 / p_0.sum()
                    idx.append(i)
                idx = np.array(idx)
            else:
                idx = self.random_state.choice(ind_1, size=(bucket_size, n_objects))
                idx = np.array(idx)
            if len(X_train) == 0:
                X_train = x[idx]
                Y_train = y[idx]
            else:
                Y_train = np.concatenate([Y_train, y[idx]], axis=0)
                X_train = np.concatenate([X_train, x[idx]], axis=0)
        logger.info(
            "Sampled instances {} objects {}".format(X_train.shape[0], X_train.shape[1])
        )
        return X_train, Y_train
