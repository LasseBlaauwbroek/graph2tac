from typing import List, Dict, Iterable, Tuple, Callable, Optional, Union

import yaml
import tensorflow as tf
import tensorflow_gnn as tfgnn
from pathlib import Path
import numpy as np

from graph2tac.loader.data_classes import GraphConstants
from graph2tac.tfgnn.graph_schema import proofstate_graph_spec, batch_graph_spec, strip_graph
from graph2tac.tfgnn.models import (RepeatScalarGraph,
                                    GraphEmbedding,
                                    LogitsFromEmbeddings,
                                    get_gnn_constructor,
                                    get_arguments_head_constructor,
                                    get_tactic_head_constructor,
                                    get_definition_head_constructor)


BASE_TACTIC_PREDICTION = 'base_tactic_prediction'
LOCAL_ARGUMENT_PREDICTION = 'local_argument_prediction'
GLOBAL_ARGUMENT_PREDICTION = 'global_argument_prediction'


def arguments_filter(y_true: tf.RaggedTensor, y_pred: tf.Tensor) -> Tuple[tf.Tensor, tf.Tensor]:
    """
    Extracts the local arguments which are not None from the ground truth and predictions

    @param y_true: the labels for the arguments, with shape [batch_size, 1, None(num_arguments)]
    @param y_pred: the logits for the arguments, with shape [batch_size, max(num_arguments), context_size]
    @return: a tuple whose first element contains the not-None arguments, the second element being the logits corresponding to each not-None argument
    """
    # convert y_true to a dense tensor padding with -1 values (also used for None arguments);
    # remove spurious dimension (y_true was created from a non-scalar graph)
    # [ batch_size, max(num_arguments) ]
    arguments_tensor = tf.squeeze(y_true.to_tensor(default_value=-1), axis=1)

    # we want to compute only go over the positions that are not None
    positions = tf.where(arguments_tensor != -1)

    # keep only these positions in the both y_true and y_pred
    arguments_true = tf.gather_nd(arguments_tensor, positions)
    arguments_pred = tf.gather_nd(y_pred, positions)
    return arguments_true, arguments_pred

@tf.function
def ragged_logits(y_true: tf.RaggedTensor, y_pred: tf.Tensor) -> Tuple[tf.RaggedTensor]:
    """
    Extracts the logits for valid argument positions using the shape of y_true

    @param y_true: the labels for the arguments, with shape [batch_size, 1, None(num_arguments)]
    @param y_pred: the logits for the arguments, with shape [batch_size, max(num_arguments), context_size]
    @return: a ragged tensor of logits, with shape [batch_size, None(num_arguments), context_size]
    """
    y_true = tf.squeeze(y_true, 1).with_row_splits_dtype(tf.int64) # [batch_size, None(num_arguments)]

    if tf.shape(y_pred)[-1] == 0:
        # no context
        # return new tensor with a dummy context of 1
        # shape: [batch_size, None(num_arguments), 1]
        logits = tf.expand_dims(tf.math.log(tf.zeros_like(y_true, tf.float32)), axis=-1)
    else:
        #shape: [batch_size, None(num_arguments), context_size]
        logits = tf.RaggedTensor.from_tensor(y_pred, lengths=y_true.row_lengths())
    return logits

def convert_ragged_logits_to_dense(logits: tf.RaggedTensor) -> tf.Tensor:
    # logits shape: [batch, None(args), None(context)]

    # expand context (replacing unused context element logits with -inf)
    # shape: [batch, None(args), max(context)]
    logits = logits.with_values(logits.values.to_tensor(default_value=-np.inf))

    # expand args (replacing unused arg position logits with 0.0)
    # shape: [batch, max(args), max(context)]
    logits = logits.to_tensor(default_value=0.0)

    return logits

@tf.function
def _local_arguments_pred(y_true: tf.Tensor, y_pred: tf.Tensor) -> tf.Tensor:
    return tf.argmax(y_pred, axis=-1) if tf.shape(y_pred)[-1] > 0 else tf.zeros_like(y_true)


class LocalArgumentSparseCategoricalCrossentropy(tf.keras.losses.Loss):
    """
    Used to compute the sparse categorical crossentropy loss for the local argument prediction task.

    NOTES:
        - `-1` arguments correspond to `None`
        - logits **are not** assumed to be normalized
    """
    def call(self, y_true, y_pred):
        """
        @param y_true: ids for the arguments, with shape [ batch_size, 1, None(num_arguments) ]
        @param y_pred: logits for each argument position, with shape [ batch_size, None(num_arguments), num_categories ]
        @return: a vector of length equal to the total number of not-None arguments within this batch
        """
        arguments_true, arguments_pred = arguments_filter(y_true, y_pred)

        if tf.size(arguments_pred) == 0:
            # deal with the edge case where there is no local context or no local arguments to predict in the batch
            return tf.zeros_like(arguments_true, dtype=tf.float32)
        else:
            # the local context is non-empty and we have at least one argument, so the following doesn't fail
            return tf.nn.sparse_softmax_cross_entropy_with_logits(arguments_true, arguments_pred)


class ArgumentSparseCategoricalCrossentropy(tf.keras.losses.Loss):
    """
    Sparse categorical crossentropy loss for local and global args in the global argument prediction task.
    
    NOTES:
        - `-1` arguments correspond to `None` or a different type of argument (e.g. local vs global)
        - logits **are** assumed to be normalized
        - `sum_loss_over_tactic` parameter:
            - If `True`, the losses are summed across all arguments within a batch.
              When the local and global losses are added together, the combined loss is the negative
              log probability of the ground truth sequence of (non-None) arguments.
              Further, if argument loss weight is 1.0 then when the tactic, local, and global
              losses are added together, the combined loss is equal to the negative log probability of
              the full ground truth tactic, including base tactic and all (non-None) arguments.
            - If `False`, the "batch_size" of the output will not necessarily be the same as the number
              of elements in the batch.  This has the effect that the loss will be averaged by the number
              of global (or local) arguments in the batch before combining with other losses.
    """

    def __init__(self, sum_loss_over_tactic: bool, **kwargs):
        """
        @param average_per_tactic: whether to sum the argument losses over tactic count argument
        """
        super().__init__(**kwargs)
        self.sum_loss_over_tactic = sum_loss_over_tactic

    @staticmethod
    def arguments_filter(y_true: tf.RaggedTensor, y_pred: tf.RaggedTensor) -> Tuple[tf.RaggedTensor, tf.RaggedTensor]:
        """
        Extracts the local arguments which are not None from the ground truth and predictions.
        
        Returns a pair of ragged tensors with shapes:
        - [batch_size, None(num_nonempty_args)]
        - [batch_size, None(num_nonempty_args), None(context)]
        @param y_true: the labels for the arguments, with shape [batch_size, 1, None(num_arguments)]
        @param y_pred: the logits for the arguments, with shape [batch_size, None(num_arguments),  None(context)]
        @return: a tuple whose first element contains the non-None arguments, the second element being the logits corresponding to each non-None argument
        """
        # convert y_true to a dense tensor padding with -1 values (also used for None arguments);
        # remove spurious dimension (y_true was created from a non-scalar graph)
        # [ batch_size, max(num_arguments) ]
        arguments_tensor = tf.squeeze(y_true.to_tensor(default_value=-1), axis=1)

        # we want to compute only over the positions that are not None
        nrows = tf.shape(y_true, out_type=tf.int64)[0]
        positions = tf.where(arguments_tensor != -1)
        row_ids = positions[:, 0]

        # keep only these positions in the both y_true and y_pred
        arguments_true = tf.RaggedTensor.from_value_rowids(
            values=tf.gather_nd(arguments_tensor, positions),
            value_rowids=row_ids,
            nrows=nrows
        )
        arguments_pred = tf.RaggedTensor.from_value_rowids(
            values=tf.gather_nd(y_pred, positions),
            value_rowids=row_ids,
            nrows=nrows
        )
        # output shape: [batch, None(args)], [batch, None(args), globals]
        return arguments_true, arguments_pred

    @staticmethod
    def convert_to_ragged(y_true, y_pred):

        # y_true: # [batch_size, 1, None(args)]
        # y_pred: # [batch_size, max(num_arguments), context_size]

        # remove spurious dimension (y_true was created from a non-scalar graph)
        # find lengths
        # shape: [batch]
        y_true_lengths = tf.squeeze(y_true, 1).row_lengths()

        # use y_true_lenghts to set outer ragged shape
        # shape: [batch, None(args), context_size]
        y_pred = tf.RaggedTensor.from_tensor(y_pred, lengths=y_true_lengths)

        def tensor_to_ragged_filter(x, value_to_filter):
            # x shape: # [nrows, ncols]
            nrows = tf.shape(y_true, out_type=tf.int64)[0]
            positions = tf.where(x != value_to_filter)  # [nrows, 2]
            row_ids = positions[:, 0]  # [nrows]

            # return shape: [nrows, None(cols)]
            return tf.RaggedTensor.from_value_rowids(
                values=tf.gather_nd(x, positions),
                value_rowids=row_ids,
                nrows=nrows
            )

        # filter out -inf
        # shape: [batch, None(args), None(context)]
        y_pred = tf.ragged.map_flat_values(lambda x: tensor_to_ragged_filter(x, -np.inf), y_pred)

        return y_pred


    def call(self, y_true, y_pred):
        """
        @param y_true: ids for the arguments, with shape [ batch_size, 1, None(num_arguments) ]
        @param y_pred: logits for each argument position, with shape [ batch_size, None(num_arguments), num_categories ]
        @return: a vector of length equal to either the size of the batch or the number of arguments of the given type
        """
        # y_true shape: [batch, None(args)]
        # y_pred shape: [batch, None(args), None(context)]

        # filter out any arguments which have index -1, i.e. are None or of a different kind (global vs local)
        # shape: [batch, None(args)], [batch, None(args), None(context)]
        arguments_true, arguments_pred = self.arguments_filter(y_true, y_pred)
        
        # compute the cross entropy loss by using gather to find the corresponding logit
        # (and flip the sign to get cross entropy)
        arg_losses = -tf.gather(arguments_pred, arguments_true, batch_dims=2)

        # return the losses as a list of losses (it will be reduced automatically by keras to a single number)
        if self.sum_loss_over_tactic:
            # sum over all arguments in a batch element
            # shape: [batch]
            return tf.reduce_sum(arg_losses, axis=-1)
        else:
            # return one loss for each argument in the in the batch
            # shape: [num of args in ground truth for batch]
            return arg_losses.flat_values


class ArgumentSparseCategoricalAccuracy(tf.keras.metrics.SparseCategoricalAccuracy):
    """
    Per-argument sparse categorical accuracy, excluding None arguments.
    """
    def update_state(self, y_true, y_pred, sample_weight=None):
        # y_true shape: [batch, None(args)]
        # y_pred shape: [batch, None(args), None(context)]

        # filter out any arguments which have index -1, i.e. are None or of a different kind (global vs local)
        # shape: [batch, None(args)], [batch, None(args), None(context)]
        arguments_true, arguments_pred = ArgumentSparseCategoricalCrossentropy.arguments_filter(y_true, y_pred)
        
        # TODO(jrute): Is this the best way?  I'm increasing the context size again to be non-ragged.
        # shape: [batch, None(args), context_size]
        arguments_pred = arguments_pred.with_values(arguments_pred.values.to_tensor(default_value=-np.inf))

        if tf.shape(arguments_pred)[-1] > 0:
            super().update_state(arguments_true, arguments_pred, sample_weight)


class DefinitionNormSquaredLoss(tf.keras.losses.Loss):
    """
    Norm squared loss
    """
    def call(self, y_true, y_pred):
        # ignore y_true as it is zero
        return tf.reduce_sum(y_pred * y_pred, axis=-1)


class MixedMetricsCallback(tf.keras.callbacks.Callback):
    def on_train_begin(self, logs=None):
        for metric in self.model.mixed_metrics:
            metric.reset_state()

    def on_test_begin(self, logs=None):
        for metric in self.model.mixed_metrics:
            metric.reset_state()

    def on_predict_begin(self, logs=None):
        for metric in self.model.mixed_metrics:
            metric.reset_state()

    def on_epoch_begin(self, batch, logs=None):
        for metric in self.model.mixed_metrics:
            metric.reset_state()


class LocalArgumentModel(tf.keras.Model):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.arguments_seq_accuracy = tf.keras.metrics.Mean(name=LocalArgumentPrediction.ARGUMENTS_SEQ_ACCURACY)
        self.strict_accuracy = tf.keras.metrics.Mean(name=LocalArgumentPrediction.STRICT_ACCURACY)
        self.mixed_metrics = [self.arguments_seq_accuracy, self.strict_accuracy]

    def compute_metrics(self, x, y, y_pred, sample_weight):
        metric_results = super().compute_metrics(x, y, y_pred, sample_weight)

        tactic = y[LocalArgumentPrediction.TACTIC_LOGITS]
        tactic_logits = y_pred[LocalArgumentPrediction.TACTIC_LOGITS]
        tactic_accuracy = tf.keras.metrics.sparse_categorical_accuracy(tactic, tactic_logits)

        local_arguments = y[LocalArgumentPrediction.LOCAL_ARGUMENTS_LOGITS]
        local_arguments_logits = y_pred[LocalArgumentPrediction.LOCAL_ARGUMENTS_LOGITS]
        local_arguments_true = tf.squeeze(local_arguments.to_tensor(default_value=0), axis=1)
        local_arguments_pred = _local_arguments_pred(local_arguments_true, local_arguments_logits)
        arguments_seq_accuracy = tf.cast(tf.reduce_all(local_arguments_true == local_arguments_pred, axis=-1), dtype=tf.float32)
        arguments_seq_mask = tf.cast(tf.reduce_min(local_arguments_true, axis=-1) > -1, dtype=tf.float32)

        self.arguments_seq_accuracy.update_state(arguments_seq_mask * arguments_seq_accuracy,
                                                 sample_weight=sample_weight)
        self.strict_accuracy.update_state(arguments_seq_mask * arguments_seq_accuracy * tactic_accuracy,
                                          sample_weight=sample_weight)

        metric_results[LocalArgumentPrediction.ARGUMENTS_SEQ_ACCURACY] = self.arguments_seq_accuracy.result()
        metric_results[LocalArgumentPrediction.STRICT_ACCURACY] = self.strict_accuracy.result()
        return metric_results


class GlobalArgumentModel(tf.keras.Model):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.arguments_seq_accuracy = tf.keras.metrics.Mean(name=GlobalArgumentPrediction.ARGUMENTS_SEQ_ACCURACY)
        self.strict_accuracy = tf.keras.metrics.Mean(name=GlobalArgumentPrediction.STRICT_ACCURACY)
        self.mixed_metrics = [self.arguments_seq_accuracy, self.strict_accuracy]

    def compute_metrics(self, x, y, y_pred, sample_weight):
        metric_results = super().compute_metrics(x, y, y_pred, sample_weight)

        # tactic accuracy
        tactic = y[LocalArgumentPrediction.TACTIC_LOGITS]
        tactic_logits = y_pred[LocalArgumentPrediction.TACTIC_LOGITS]
        tactic_accuracy = tf.keras.metrics.sparse_categorical_accuracy(tactic, tactic_logits)

        # local arguments
        local_arguments = y[GlobalArgumentPrediction.LOCAL_ARGUMENTS_LOGITS]  # [batch, None(args)]
        local_arguments_logits = y_pred[GlobalArgumentPrediction.LOCAL_ARGUMENTS_LOGITS]  # [batch, None(args), None(context]
        local_arguments_logits = local_arguments_logits.to_tensor(default_value=-np.inf)  # [batch, max(args), context] # FIXME(jrute) 
        local_true = tf.squeeze(local_arguments, 1)  # [batch, None(args)]
        local_logits = ragged_logits(local_arguments, local_arguments_logits)  # [batch, None(args), context]
        local_best_logit = tf.ragged.map_flat_values(tf.reduce_max, local_logits, axis=-1)  # [batch, None(args)]
        local_pred = tf.ragged.map_flat_values(tf.argmax, local_logits, axis=-1, output_type=tf.int64)  # [batch, None(args)]
        
        
        # global arguments
        global_arguments = y[GlobalArgumentPrediction.GLOBAL_ARGUMENTS_LOGITS]  # [batch, None(args)]
        global_arguments_logits = y_pred[GlobalArgumentPrediction.GLOBAL_ARGUMENTS_LOGITS]  # [batch, None(args), None(context]
        global_arguments_logits = global_arguments_logits.to_tensor(default_value=-np.inf)  # [batch, max(args), context] # FIXME(jrute) 
        global_true = tf.squeeze(global_arguments, 1)  # [batch, None(args)]
        global_logits = ragged_logits(global_arguments, global_arguments_logits)
        global_best_logit = tf.ragged.map_flat_values(tf.reduce_max, global_logits, axis=-1)  # [batch, None(args)]
        global_pred = tf.ragged.map_flat_values(tf.argmax, global_logits, axis=-1, output_type=tf.int64)  # [batch, None(args)]

        # all arguments
        # for ground truth, we can take which ever argument is higher since the other is -1
        # for the ground true, local and global can both be -1 which means the true argument is None
        arg_true_is_local = (local_true >= global_true)  # [batch, None(args)]
        arg_true_ix = tf.where(local_true >= global_true, local_true, global_true)  # [batch, None(args)]

        arg_pred_is_local = (local_best_logit >= global_best_logit)  # [batch, None(args)]
        arg_pred_ix = tf.where(local_best_logit >= global_best_logit, local_pred, global_pred)  # [batch, None(args)]
        
        # strict accuracies
        # check that every argument in the sequence is correct
        # if any are None (i.e. ground truth local and global is -1) then it is marked as incorrect (since model can't produce None by design)
        seq_arg_accuracy_is_local =  tf.reduce_all(tf.equal(arg_true_is_local.with_row_splits_dtype(tf.int64), arg_pred_is_local), axis=-1)  # [batch]
        seq_arg_accuracy_ix =  tf.reduce_all(tf.equal(arg_true_ix.with_row_splits_dtype(tf.int64), arg_pred_ix), axis=-1)  # [batch]
        seq_arg_accuracy = tf.cast(seq_arg_accuracy_is_local & seq_arg_accuracy_ix, dtype=tf.float32)  # [batch]
        strict_accuracy = tactic_accuracy * seq_arg_accuracy  # [batch]

        self.arguments_seq_accuracy.update_state(seq_arg_accuracy, sample_weight=sample_weight)
        self.strict_accuracy.update_state(strict_accuracy, sample_weight=sample_weight)

        metric_results[GlobalArgumentPrediction.ARGUMENTS_SEQ_ACCURACY] = self.arguments_seq_accuracy.result()
        metric_results[GlobalArgumentPrediction.STRICT_ACCURACY] = self.strict_accuracy.result()
        return metric_results


class PredictionTask:
    """
    Base class for the various prediction tasks that we will define.

    Subclasses should implement the following methods:
        - create_input_output
        - create_train_model
        - create_inference_model
        - loss
        - metrics

    Additionally, they may override the following methods
        - loss_weights
        - callbacks

    They can also implement any other methods that may be necessary (for prediction, see graph2tac.tfgnn. predict).
    """
    create_input_output: Callable[[tfgnn.GraphTensor], Tuple[tfgnn.GraphTensor, Union[tf.Tensor, tf.RaggedTensor]]]
    create_train_model: Callable[[], tf.keras.Model]
    create_inference_model: Callable[..., tf.keras.Model]
    loss: Callable[[], Dict[str, tf.keras.losses.Loss]]
    metrics: Callable[[], Dict[str, List[tf.keras.metrics.Metric]]]

    PROOFSTATE_GRAPH = 'proofstate_graph'

    def __init__(self,
                 graph_constants: GraphConstants,
                 hidden_size: int,
                 unit_norm_embs: bool,
                 gnn_type: str,
                 gnn_config: dict
                 ):
        """
        @param graph_constants: a GraphConstants object for the graphs that will be consumed by the model
        @param hidden_size: the (globally shared) hidden size
        @param unit_norm_embs: whether to restrict embeddings to the unit norm
        @param gnn_type: the type of GNN component to use
        @param gnn_config: the hyperparameters to be passed to GNN constructor
        """
        self._graph_constants = graph_constants
        self._hidden_size = hidden_size
        self._unit_norm_embs = unit_norm_embs
        self._gnn_type = gnn_type

        # we have to clear the Keras session to make sure layer names are consistently chosen
        # NOTE: this would break multi-gpu training using MirroredStrategy, so be careful with layer names in that case
        # if not isinstance(tf.distribute.get_strategy(), tf.distribute.MirroredStrategy) and not isinstance(tf.distribute.get_strategy, tf.distribute.OneDeviceStrategy):
        #     tf.keras.backend.clear_session()

        # create and initialize node and edge embeddings
        self.graph_embedding = GraphEmbedding(
            node_label_num=graph_constants.node_label_num,
            edge_label_num=graph_constants.edge_label_num,
            hidden_size=hidden_size,
            unit_normalize=unit_norm_embs
        )
        self.graph_embedding.lookup_node_embedding(tf.range(graph_constants.node_label_num))

        # create the GNN component
        gnn_constructor = get_gnn_constructor(gnn_type)
        self.gnn = gnn_constructor(hidden_size=hidden_size, **gnn_config)

        # create checkpoint with both layers created above
        self.checkpoint = tf.train.Checkpoint(graph_embedding=self.graph_embedding, gnn=self.gnn)

    def get_config(self):
        gnn_config = self.gnn.get_config()
        gnn_config.pop('hidden_size')

        return {
            'hidden_size': self._hidden_size,
            'gnn_type': self._gnn_type,
            'unit_norm_embs': self._unit_norm_embs,
            'gnn_config': gnn_config,
        }

    @staticmethod
    def from_yaml_config(graph_constants: GraphConstants,
                         yaml_filepath: Path
                         ) -> Union["TacticPrediction", "LocalArgumentPrediction", "GlobalArgumentPrediction"]:
        """
        Create an instance of this class from a YAML configuration file.

        @param graph_constants: a GraphConstants object for the graphs that will be consumed by the model
        @param yaml_filepath: the filepath to a YAML file containing all other arguments to the constructor
        @return: a PredictionTask object
        """
        with yaml_filepath.open() as yaml_file:
            task_config = yaml.load(yaml_file, Loader=yaml.SafeLoader)

        prediction_task_type = task_config.pop('prediction_task_type')
        prediction_task_constructor = get_prediction_task_constructor(prediction_task_type)
        return prediction_task_constructor(graph_constants=graph_constants, **task_config)

    def loss_weights(self) -> Dict[str, float]:
        """
        Provides the loss weights for this task, to be used with keras model.compile()

        @return: a dictionary mapping the model's outputs to their corresponding loss weights
        """
        return {loss_name: 1.0 for loss_name in self.loss().keys()}

    def callbacks(self) -> List[tf.keras.callbacks.Callback]:
        """
        Provides basic callbacks for this task, to be used with keras model.compile()

        @return: a list of keras callbacks
        """
        return []

    def from_trainer_checkpoint(self, save_path: str) -> None:
        """
        Loads a checkpoint created by the training script.

        @param save_path: the full path to the checkpoint we want to load
        """
        load_status = tf.train.Checkpoint(prediction_task=self.checkpoint).restore(save_path)
        load_status.expect_partial().assert_nontrivial_match().run_restore_ops()


class TacticPrediction(PredictionTask):
    """
    Wrapper for the base tactic prediction task.
    """
    TACTIC: str = 'tactic'
    TACTIC_LOGITS = 'tactic_logits'
    TACTIC_MASK = 'tactic_mask'

    def __init__(self,
                 tactic_embedding_size: int,
                 tactic_head_type: str,
                 tactic_head_config: dict,
                 **kwargs):
        """
        @param tactic_embedding_size: the (globally shared) size of tactic embeddings
        @param tactic_head_type: the type of tactic head to use
        @param tactic_head_config: the hyperparameters to be passed to tactic_head_function
        @param kwargs: other arguments are passed on to the PredictionTask class constructor
        """
        super().__init__(**kwargs)
        self._tactic_embedding_size = tactic_embedding_size
        self._tactic_head_type = tactic_head_type

        # create and initialize tactic embeddings
        self.tactic_embedding = tf.keras.layers.Embedding(input_dim=self._graph_constants.tactic_num,
                                                          output_dim=tactic_embedding_size)
        self.tactic_embedding(tf.range(self._graph_constants.tactic_num))

        # create the tactic head
        tactic_head_constructor = get_tactic_head_constructor(tactic_head_type)
        self.tactic_head = tactic_head_constructor(tactic_embedding_size=tactic_embedding_size, **tactic_head_config)

        # a layer to compute tactic logits from tactic embeddings
        self.tactic_logits_from_embeddings = LogitsFromEmbeddings(
            embedding_matrix=self.tactic_embedding.embeddings,
            cosine_similarity=False,
            name=self.TACTIC_LOGITS
        )

        # update checkpoint with new layers
        self.checkpoint.tactic_embedding = self.tactic_embedding
        self.checkpoint.tactic_head = self.tactic_head
        self.checkpoint.tactic_logits_from_embeddings = self.tactic_logits_from_embeddings

    @staticmethod
    def _top_k_tactics(tactic_logits: tf.Tensor,
                       tactic_mask: tf.Tensor,
                       tactic_expand_bound: int
                       ) -> Tuple[tf.Tensor, tf.Tensor]:
        tactic_logits = tf.math.log_softmax(tactic_logits + tf.math.log(tf.cast(tactic_mask, tf.float32)), axis=-1)

        top_k = tf.math.top_k(tactic_logits, k=tactic_expand_bound)
        return top_k.indices, top_k.values

    def _tactic_logits_and_hidden_graph(self,
                                        scalar_proofstate_graph: tfgnn.GraphTensor
                                        ) -> Tuple[tf.Tensor, tfgnn.GraphTensor]:
        bare_graph = strip_graph(scalar_proofstate_graph)
        embedded_graph = self.graph_embedding(bare_graph)  # noqa [ PyCallingNonCallable ]
        hidden_graph = self.gnn(embedded_graph)
        
        tactic_embedding = self.tactic_head(hidden_graph)
        tactic_logits = self.tactic_logits_from_embeddings(tactic_embedding)  # noqa [ PyCallingNonCallable ]
        return tactic_logits, hidden_graph

    def get_config(self):
        config = super().get_config()

        tactic_head_config = self.tactic_head.get_config()
        tactic_head_config.pop('tactic_embedding_size')

        config.update({
            'prediction_task_type': BASE_TACTIC_PREDICTION,
            'tactic_embedding_size': self._tactic_embedding_size,
            'tactic_head_type': self._tactic_head_type,
            'tactic_head_config': tactic_head_config
        })
        return config

    def create_train_model(self) -> tf.keras.Model:
        """
        Combines a GNN component with a tactic head to produce an end-to-end model for the base tactic prediction task.
        The resulting model is for training purposes, and produces tactic logits.

        @return: a keras model consuming proof-state graphs and producing tactic logits
        """

        proofstate_graph = tf.keras.layers.Input(type_spec=batch_graph_spec(proofstate_graph_spec),
                                                 name=self.PROOFSTATE_GRAPH)
        scalar_proofstate_graph = proofstate_graph.merge_batch_to_components()

        tactic_logits, _ = self._tactic_logits_and_hidden_graph(scalar_proofstate_graph)

        return tf.keras.Model(inputs=proofstate_graph, outputs={TacticPrediction.TACTIC_LOGITS: tactic_logits})

    def create_inference_model(self, tactic_expand_bound: int, graph_constants: GraphConstants) -> tf.keras.Model:
        """
        Combines a GNN component with a tactic head to produce an end-to-end model for the base tactic prediction task.
        The resulting model is for inference purposes, and produces tactic predictions and logits.

        @warning: we do not use the GraphConstants saved in this task since during inference these may not be up-to-date
        @tactic_expand_bound: the number of base tactic predictions to produce for each proofstate
        @graph_constants: the graph constants to use during inference (with a possibly updated global context)
        @return: a keras model consuming proof-state graphs and producing tactic predictions and logits
        """
        proofstate_graph = tf.keras.layers.Input(type_spec=batch_graph_spec(proofstate_graph_spec),
                                                 name=self.PROOFSTATE_GRAPH)
        scalar_proofstate_graph = proofstate_graph.merge_batch_to_components()

        tactic_mask = tf.keras.Input(shape=(graph_constants.tactic_num,), dtype=tf.bool, name=self.TACTIC_MASK)

        tactic_logits, _ = self._tactic_logits_and_hidden_graph(scalar_proofstate_graph)

        # [tactic_num, ]
        no_argument_tactics_mask = tf.constant(graph_constants.tactic_index_to_numargs, dtype = tf.int64) == 0

        # [batch_size, tactic_num]
        proofstate_tactic_mask = tf.repeat(tf.expand_dims(no_argument_tactics_mask, axis=0), proofstate_graph.total_num_components, axis=0)

        top_k_indices, top_k_values = self._top_k_tactics(tactic_logits=tactic_logits,
                                                          tactic_mask=proofstate_tactic_mask & tactic_mask,
                                                          tactic_expand_bound=tactic_expand_bound)
        return tf.keras.Model(inputs={self.PROOFSTATE_GRAPH: proofstate_graph, self.TACTIC_MASK: tactic_mask},
                              outputs={self.TACTIC: tf.transpose(top_k_indices),
                                       self.TACTIC_LOGITS: tf.transpose(top_k_values)})

    @staticmethod
    def create_input_output(graph_tensor: tfgnn.GraphTensor) -> Tuple[tfgnn.GraphTensor, Dict[str, tf.Tensor]]:
        return graph_tensor, {TacticPrediction.TACTIC_LOGITS: graph_tensor.context['tactic']}

    @staticmethod
    def loss() -> Dict[str, tf.keras.losses.Loss]:
        return {TacticPrediction.TACTIC_LOGITS: tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True)}

    @staticmethod
    def metrics() -> Dict[str, List[tf.keras.metrics.Metric]]:
        return {TacticPrediction.TACTIC_LOGITS: [tf.keras.metrics.SparseCategoricalAccuracy(name="accuracy")]}


class GlobalEmbeddings(tf.keras.layers.Layer):
    def __init__(
        self,
        global_embeddings_layer,
        name="global_embeddings",
        **kwargs
    ):
        super().__init__(name=name, **kwargs)
        self.global_embeddings_layer = global_embeddings_layer

    def call(
        self, 
        global_context: tf.RaggedTensor,
        training=False
    ) -> tf.Tensor: # [batch, None(context), hdim]
        # [valid_context, hdim]
        all_embeddings = self.global_embeddings_layer.get_keys_embeddings()
        # repeat the global context for each batch
        # [batch, valid_context, hdim]
        batch_size = tf.shape(global_context)[0]
        global_embeddings = tf.tile(tf.expand_dims(all_embeddings, axis=0), multiples=[batch_size, 1, 1])
        # [batch, None(context), hdim]
        return tf.RaggedTensor.from_tensor(global_embeddings, ragged_rank=1)



class QueryKeyMul(tf.keras.layers.Layer):
    def __init__(
        self,
        method="broadcast_ragged",
        name="query_key_mul",
        **kwargs
    ):
        super().__init__(name=name, **kwargs)
        self.method = method

    @staticmethod
    def _mul_map_fn(queries, keys):
        @tf.function
        def linear_op(qk):
            x = tf.einsum("ij, kj -> ik", qk[0], qk[1])
            x = tf.RaggedTensor.from_tensor(x)
            return x
        return tf.map_fn(
            linear_op,
            elems=[queries, keys],
            fn_output_signature=tf.RaggedTensorSpec(shape=[None, None])
        )
    
    @staticmethod
    def _mul_broadcast_ragged(queries, keys):
        keys_values = tf.gather(keys, queries.value_rowids())  # [batch-args, None(context), hdim]
        queries_values_values = tf.gather(queries.values, keys_values.value_rowids())  # [batch-args-context, hdim]
        logits_values_values = tf.einsum("ij,ij->i", keys_values.values, queries_values_values)  # [batch-args-context]
        logits_values = keys_values.with_values(logits_values_values)  # [batch-args, None(context)]
        logits = queries.with_values(logits_values)  # [batch, None(args), None(context)]
        return logits
    
    @staticmethod
    def _mul_ragged_to_dense_to_ragged(queries, keys):
        queries_dense = queries.to_tensor()    # [batch, max(args), hdim]
        keys_dense = keys.to_tensor()         # [batch, max(context), hdim]
        logits_dense = tf.einsum("ijl,ikl->ijk", queries_dense, keys_dense)  # [batch, max(args), max(context)]
        logits_part_ragged = tf.RaggedTensor.from_tensor(logits_dense, lengths=queries.row_lengths())  # [batch, None(args), max(context)]
        lengths = queries.with_values(tf.gather(keys.row_lengths(), queries.value_rowids()))  # [batch, None(args)]
        logits_values = tf.RaggedTensor.from_tensor(logits_part_ragged.values, lengths=lengths.values)
        logits = logits_part_ragged.with_values(logits_values)
        return logits

    def call(
        self, 
        queries: tf.RaggedTensor, # [batch, None(args), hdim]
        keys: tf.RaggedTensor, # [batch, None(context), hdim]
        training=False
    ) -> tf.RaggedTensor: # [batch, None(args), None(context)]
        queries = queries.with_row_splits_dtype(tf.int64)
        keys = keys.with_row_splits_dtype(tf.int64)
        if self.method == "map_fn":
            return self._mul_map_fn(queries, keys)
        elif self.method == "broadcast_ragged":
            return self._mul_broadcast_ragged(queries, keys)
        elif self.method == "ragged_to_dense_to_ragged":
            return self._mul_ragged_to_dense_to_ragged(queries, keys)
        else:
            raise Exception(f"Unsupported multiplication method: {self.method}")

class QueryKeyMulGlobal(tf.keras.layers.Layer):
    def __init__(
        self,
        name="query_key_mul_global",
        cosine_similarity: bool = True,
        temp: Optional[tf.Variable] = None,
        **kwargs
    ):
        super().__init__(name=name, **kwargs)
        self._cosine_similarity = cosine_similarity
        if self._cosine_similarity:
            # since cosine similarity is between -1.0 and 1.0
            # we add a learned temperature parameter
            # so logits can be in a wider or narrower range -1/temp to 1/temp

            assert temp is not None
            self._temp = temp
        self.query_key_mul = QueryKeyMul()

    def normalize_tensor(self, x: tf.Tensor) ->  tf.Tensor:
        x_norm = tf.norm(x, axis=-1, keepdims=True)
        return tf.math.divide_no_nan(x, x_norm)

    def normalize_ragged(self, rt: tf.RaggedTensor) -> tf.RaggedTensor:
        return tf.ragged.map_flat_values(self.normalize_tensor, rt)

    def call(
        self, 
        queries: tf.RaggedTensor, # [batch, None(args), hdim]
        keys: tf.RaggedTensor, # [batch, context, hdim]
        training=False
    ) -> tf.Tensor: # [batch, max(args), context]        
        if self._cosine_similarity:
            # normalize embeddings before taking inner product
            keys = self.normalize_ragged(keys)
            queries = self.normalize_ragged(queries)
            
        logits = self.query_key_mul(queries, keys)

        if self._cosine_similarity:
            logits = logits / self._temp
        
        return logits


class LocalArgumentPrediction(TacticPrediction):
    """
    Wrapper for the base tactic plus local argument prediction tasks.
    """
    LOCAL_ARGUMENTS_LOGITS = 'local_arguments_logits'

    ARGUMENTS_SEQ_ACCURACY = 'arguments_seq_accuracy'
    STRICT_ACCURACY = 'strict_accuracy'

    def __init__(self,
                 arguments_head_type: str,
                 arguments_head_config: dict,
                 arguments_loss_coefficient: float = 1.0,
                 **kwargs
                 ):
        """
        @param arguments_head_type: the type of arguments head to use
        @param arguments_head_config: the hyperparameters to be used for the arguments head
        @param arguments_loss_coefficient: the weight of the loss term for the arguments (base tactic loss has weight 1)
        @param kwargs: other arguments are  passed to the TacticPrediction constructor
        """
        super().__init__(**kwargs)
        self._arguments_head_type = arguments_head_type
        self._arguments_loss_coefficient = arguments_loss_coefficient

        # create arguments head
        arguments_head_constructor = get_arguments_head_constructor(arguments_head_type)
        self.arguments_head = arguments_head_constructor(hidden_size=self._hidden_size,
                                                         tactic_embedding_size=self._tactic_embedding_size,
                                                         **arguments_head_config)

        # we use trivial lambda layers to appropriately rename outputs
        self.local_arguments_logits_output = tf.keras.layers.Lambda(lambda x: x, name=self.LOCAL_ARGUMENTS_LOGITS)

        # update checkpoint with new layers
        self.checkpoint.arguments_head = self.arguments_head

    def get_config(self):
        config = super().get_config()

        arguments_head_config = self.arguments_head.get_config()
        arguments_head_config.pop('hidden_size')
        arguments_head_config.pop('tactic_embedding_size')

        config.update({
            'prediction_task_type': LOCAL_ARGUMENT_PREDICTION,
            'arguments_head_type': self._arguments_head_type,
            'arguments_head_config': arguments_head_config,
            'arguments_loss_coefficient': self._arguments_loss_coefficient
        })
        return config

    @staticmethod
    def _local_context_hidden(
        scalar_proofstate_graph: tfgnn.GraphTensor,
        hidden_graph: tfgnn.GraphTensor,
    ) -> tf.RaggedTensor:  # [batch_size, None(local_context), hidden_size]
        """
        Computes logits for local arguments from the hidden states and the local context node ids.
        """
        # the sizes of the components of this graph
        # [ batch_size, ]
        sizes = tf.cast(scalar_proofstate_graph.node_sets['node'].sizes, dtype=tf.int64)

        # the offsets of the node ids for each graph component
        # [ batch_size, 1 ]
        cumulative_sizes = tf.expand_dims(tf.cumsum(sizes, exclusive=True), axis=-1)

        # the node ids for the local context nodes, shifted per components
        # [ batch_size, None(num_context_nodes) ]
        local_context_ids = cumulative_sizes + scalar_proofstate_graph.context['local_context_ids']

        # the hidden states for the nodes in the local context
        # [ batch_size, None(num_context_nodes), hidden_size ]
        return tf.gather(hidden_graph.node_sets['node']['hidden_state'],
                                               local_context_ids).with_row_splits_dtype(tf.int64)

    def _hidden_state_sequences(self, hidden_graph: tfgnn.GraphTensor, tactic: tf.Tensor) -> tf.RaggedTensor:
        num_arguments = tf.gather(tf.constant(self._graph_constants.tactic_index_to_numargs, dtype=tf.int64), tactic)
        return self.arguments_head((hidden_graph, self.tactic_embedding(tactic), num_arguments))

    @staticmethod
    def _reshape_inference_logits(logits: tf.Tensor, tactic_expand_bound: int) -> tf.Tensor:
        num_arguments = tf.shape(logits)[1]
        num_logits = tf.shape(logits)[2]
        return tf.reshape(logits, shape=(tactic_expand_bound, -1, num_arguments, num_logits))

    @classmethod
    def _reshape_global_inference_logits(cls, logits: tf.Tensor, tactic_expand_bound: int, dyn_global_context: tf.RaggedTensor) -> tf.Tensor:
        global_logits = cls._reshape_inference_logits(logits, tactic_expand_bound)
        return tf.gather(global_logits, dyn_global_context[0], axis=3)

    def create_train_model(self) -> tf.keras.Model:
        """
        Combines a GNN component with a tactic head and an arguments head to produce an end-to-end model for the
        local argument prediction task. The resulting model is weakly autoregressive and for training purposes only,
        producing both tactic logits and argument logits for each local context node and argument position.

        @return: a keras model consuming graphs and producing tactic logits and local arguments logits
        """
        proofstate_graph = tf.keras.layers.Input(type_spec=batch_graph_spec(proofstate_graph_spec),
                                                 name=self.PROOFSTATE_GRAPH)
        scalar_proofstate_graph = proofstate_graph.merge_batch_to_components()

        tactic_logits, hidden_graph = self._tactic_logits_and_hidden_graph(scalar_proofstate_graph)

        hidden_state_sequences = self._hidden_state_sequences(hidden_graph=hidden_graph,
                                                              tactic=scalar_proofstate_graph.context['tactic'])

        local_arguments_logits = self._local_arguments_logits(scalar_proofstate_graph=scalar_proofstate_graph,
                                                              hidden_graph=hidden_graph,
                                                              hidden_state_sequences=hidden_state_sequences)
        local_arguments_logits_output = self.local_arguments_logits_output(local_arguments_logits)

        return LocalArgumentModel(inputs=proofstate_graph,
                                  outputs={self.TACTIC_LOGITS: tactic_logits,
                                           self.LOCAL_ARGUMENTS_LOGITS: local_arguments_logits_output})

    def create_inference_model(self, tactic_expand_bound: int, graph_constants: GraphConstants) -> tf.keras.Model:
        """
        Combines a GNN component with a tactic head and an arguments head to produce an end-to-end model for the
        local argument prediction task. The resulting model is weakly autoregressive and for inference purposes only,
        producing base tactic, their logits and argument logits for each local context node and argument position.

        @warning: we do not use the GraphConstants saved in this task since during inference these may not be up-to-date
        @param tactic_expand_bound: the number of base tactic predictions to produce for each proofstate
        @param graph_constants: the graph constants to use during inference (with a possibly updated global context)
        @return: a keras model consuming graphs and producing tactic logits and local arguments logits
        """
        proofstate_graph = tf.keras.layers.Input(type_spec=batch_graph_spec(proofstate_graph_spec),
                                                 name=self.PROOFSTATE_GRAPH)
        scalar_proofstate_graph = proofstate_graph.merge_batch_to_components()

        tactic_mask = tf.keras.Input(shape=(graph_constants.tactic_num,), dtype=tf.bool, name=self.TACTIC_MASK)

        tactic_logits, hidden_graph = self._tactic_logits_and_hidden_graph(scalar_proofstate_graph)

        # [tactic_num, ]
        no_argument_tactics_mask = tf.constant(graph_constants.tactic_index_to_numargs, dtype = tf.int64) == 0
        all_tactics_mask = tf.ones(graph_constants.tactic_num, dtype=tf.bool)

        # [batch_size, ]
        no_context_proofstates = scalar_proofstate_graph.context['local_context_ids'].row_lengths() == 0

        # [batch_size, tactic_num]
        proofstate_tactic_mask = tf.where(tf.expand_dims(no_context_proofstates, axis=-1),
                                 tf.expand_dims(no_argument_tactics_mask, axis=0),
                                 tf.expand_dims(all_tactics_mask, axis=0))

        top_k_indices, top_k_values = self._top_k_tactics(tactic_logits=tactic_logits,
                                                          tactic_mask=proofstate_tactic_mask & tactic_mask,
                                                          tactic_expand_bound=tactic_expand_bound)

        tactic = tf.reshape(tf.transpose(top_k_indices), shape=(tf.size(top_k_indices),))

        repeat_scalar_graph = RepeatScalarGraph(num_repetitions=tactic_expand_bound)
        scalar_proofstate_graph = repeat_scalar_graph(scalar_proofstate_graph) # noqa
        hidden_graph = repeat_scalar_graph(hidden_graph) # noqa

        hidden_state_sequences = self._hidden_state_sequences(hidden_graph=hidden_graph,
                                                              tactic=tactic)
        local_arguments_logits = self._local_arguments_logits(scalar_proofstate_graph, hidden_graph,
                                                              hidden_state_sequences)

        local_arguments_logits_output = self._reshape_inference_logits(logits=local_arguments_logits,
                                                                       tactic_expand_bound=tactic_expand_bound)

        return tf.keras.Model(inputs={self.PROOFSTATE_GRAPH: proofstate_graph, self.TACTIC_MASK: tactic_mask},
                              outputs={self.TACTIC: tf.transpose(top_k_indices),
                                       self.TACTIC_LOGITS: tf.transpose(top_k_values),
                                       self.LOCAL_ARGUMENTS_LOGITS: local_arguments_logits_output
                                       })

    @staticmethod
    def create_input_output(graph_tensor: tfgnn.GraphTensor) -> Tuple[tfgnn.GraphTensor, Dict[str, Union[tf.Tensor, tf.RaggedTensor]]]:
        outputs = {LocalArgumentPrediction.TACTIC_LOGITS: graph_tensor.context['tactic'],
                   LocalArgumentPrediction.LOCAL_ARGUMENTS_LOGITS: graph_tensor.context['local_arguments']}
        return graph_tensor, outputs

    @staticmethod
    def loss() -> Dict[str, tf.keras.losses.Loss]:
        return {LocalArgumentPrediction.TACTIC_LOGITS: tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True),
                LocalArgumentPrediction.LOCAL_ARGUMENTS_LOGITS: LocalArgumentSparseCategoricalCrossentropy()}

    @staticmethod
    def metrics() -> Dict[str, Iterable[tf.keras.metrics.Metric]]:
        return {LocalArgumentPrediction.TACTIC_LOGITS: [tf.keras.metrics.SparseCategoricalAccuracy(name='accuracy')],
                LocalArgumentPrediction.LOCAL_ARGUMENTS_LOGITS: [ArgumentSparseCategoricalAccuracy(name='accuracy')]}

    def loss_weights(self) -> Dict[str, float]:
        return {LocalArgumentPrediction.TACTIC_LOGITS: 1.0,
                LocalArgumentPrediction.LOCAL_ARGUMENTS_LOGITS: self._arguments_loss_coefficient}

    def callbacks(self) -> List[tf.keras.callbacks.Callback]:
        return [MixedMetricsCallback()]


class GlobalArgumentPrediction(LocalArgumentPrediction):
    """
    Wrapper for the base tactic plus local and global argument prediction tasks.
    """
    GLOBAL_ARGUMENTS_LOGITS = 'global_arguments_logits'

    def __init__(self,
                 dynamic_global_context: bool = False,
                 global_cosine_similarity: bool = False,
                 sum_loss_over_tactic: bool = False,
                 **kwargs):
        """
        @param dynamic_global_context: whether to restrict the global context to available definitions only
        @param global_cosine_similarity: whether to use cosine similarity to calculate global arg logits
        @param sum_loss_over_tactic: whether to sum the argument losses over the tactic
        @param kwargs: arguments to be passed to the LocalArgumentPrediction constructor
        """
        super().__init__(**kwargs)
        self._dynamic_global_context = dynamic_global_context
        self._sum_loss_over_tactic = sum_loss_over_tactic
        self._global_cosine_similarity = global_cosine_similarity
        
        self.global_arguments_head = tf.keras.layers.Dense(self._hidden_size)
        self.local_arguments_head = tf.keras.layers.Dense(self._hidden_size)

        # create a layer to extract logits from the node label embeddings
        self.global_arguments_logits = LogitsFromEmbeddings(
            embedding_matrix=self.graph_embedding.get_node_embeddings(),
            cosine_similarity=self._global_cosine_similarity
        )
        self.global_embeddings = GlobalEmbeddings(self.global_arguments_logits)
        self.global_logits = QueryKeyMulGlobal(
            cosine_similarity=self._global_cosine_similarity,
            temp=self.global_arguments_logits._temp if self._global_cosine_similarity else None
        )

        # we use trivial lambda layers to appropriately rename outputs
        self.local_arguments_logits_output = tf.keras.layers.Lambda(lambda x: x, name=self.LOCAL_ARGUMENTS_LOGITS)
        self.global_arguments_logits_output = tf.keras.layers.Lambda(lambda x: x, name=self.GLOBAL_ARGUMENTS_LOGITS)

        # update checkpoint with new layers
        self.checkpoint.local_arguments_head = self.local_arguments_head
        self.checkpoint.global_arguments_head = self.global_arguments_head
        self.checkpoint.global_arguments_logits = self.global_arguments_logits

    def get_config(self):
        config = super().get_config()

        config.update({
            'prediction_task_type': GLOBAL_ARGUMENT_PREDICTION,
            'dynamic_global_context': self._dynamic_global_context,
            'global_cosine_similarity': self._global_cosine_similarity,
            'sum_loss_over_tactic': self._sum_loss_over_tactic
        })
        return config

    @staticmethod
    def _global_arguments_logits_mask(scalar_proofstate_graph: tfgnn.GraphTensor,
                                      global_context_size: int) -> tf.Tensor:
        """
        @param scalar_proofstate_graph: the proofstate graph containing ids for the available global context definitions
        @param global_context_size: the size of the full global context
        @return: a mask for logits of the global context, taking into account the definitions that are actually available
        """
        global_context_ids = scalar_proofstate_graph.context['global_context_ids']

        indices = tf.stack([
            tf.cast(global_context_ids.value_rowids(), tf.int64),
            global_context_ids.values
        ], axis = -1)
        updates = tf.ones_like(global_context_ids.values, dtype=tf.float32)
        shape = [global_context_ids.nrows(), global_context_size]

        return tf.math.log(tf.scatter_nd(indices, updates, shape))  # [batch_size, global_cxt]

    @staticmethod
    def _normalize_logits(local_arguments_logits: tf.Tensor, global_arguments_logits: tf.Tensor) -> Tuple[
        tf.Tensor, tf.Tensor]:
        """
        Normalize local and global arguments logits making sure the log_softmax is numerically stable.
        """
        local_arguments_max_logit = tf.reduce_max(local_arguments_logits, axis=-1)
        global_arguments_max_logit = tf.reduce_max(global_arguments_logits, axis=-1)
        arguments_max_logit = tf.reduce_max(tf.stack([local_arguments_max_logit, global_arguments_max_logit], axis=-1),
                                            axis=-1, keepdims=True)
        local_arguments_logits -= arguments_max_logit
        global_arguments_logits -= arguments_max_logit
        
        local_arguments_logits_norm = tf.reduce_sum(tf.exp(local_arguments_logits), axis=-1, keepdims=True)
        global_arguments_logits_norm = tf.reduce_sum(tf.exp(global_arguments_logits), axis=-1, keepdims=True)
        norm = -tf.math.log(local_arguments_logits_norm + global_arguments_logits_norm)
        return local_arguments_logits + norm, global_arguments_logits + norm

    def convert_logits_to_ragged(self, pair) -> tf.RaggedTensor:
        # logits: [batch_size, max(args), context]
        #   - context is the full context (expanded to max size in case of local args)
        # context_ids: [batch_size, None(context)]
        #   - None(context) is the available context
        # arg_cnt: [batch_size]
        logits, context_ids, arg_cnts, label = pair
        
        # make arg dimension ragged in global_logits
        # shape: [batch_size, None(args), context]
        logits_ = tf.RaggedTensor.from_tensor(logits, lengths=tf.cast(arg_cnts, tf.int64))

        # duplicate context list for every arg position
        # shape: [batch_size, None(args), None(context)]
        context_ids_ = logits_.with_values(
            tf.gather(context_ids, logits_.value_rowids()).with_row_splits_dtype(tf.int64)
        )

        # make cxt dimension ragged
        # shape: [batch_size, None(args), None(context)]
        if label == "global":
            # for global_logits, must select available args (in given order)
            logits_ragged = tf.gather(logits_, context_ids_, batch_dims=2)
        else:
            # local logits are already in the correct order.  Just filter extra -inf values at the end of each row.
            logits_ragged = context_ids_.with_values(
                tf.RaggedTensor.from_tensor(logits_.values, lengths=context_ids_.values.row_lengths())
            )

        
        return logits_ragged

    def create_train_model(self) -> tf.keras.Model:
        """
        Combines a GNN component with a tactic head and an arguments head to produce an end-to-end model for the
        global argument prediction task. The resulting model is for training purposes, and produces tactic logits
        and argument logits (for each local context node / global context id per argument position).

        @return: a keras model consuming graphs and producing tactic and local/global arguments logits
        """
        proofstate_graph = tf.keras.layers.Input(type_spec=batch_graph_spec(proofstate_graph_spec),
                                                 name=self.PROOFSTATE_GRAPH)
        scalar_proofstate_graph = proofstate_graph.merge_batch_to_components()

        tactic_logits, hidden_graph = self._tactic_logits_and_hidden_graph(scalar_proofstate_graph)

        hidden_state_sequences = self._hidden_state_sequences(hidden_graph=hidden_graph,
                                                              tactic=scalar_proofstate_graph.context['tactic'])
        arg_cnts = hidden_state_sequences.row_lengths()
        # [batch, None(args), hdim]
        local_hidden_state_sequences = self.local_arguments_head(hidden_state_sequences)
        # [batch, None(context), hdim]
        local_context_hidden = self._local_context_hidden(
            scalar_proofstate_graph,
            hidden_graph,
        )
        # [batch_size, None(args), None(context)]
        local_arguments_logits = QueryKeyMul()(local_hidden_state_sequences, local_context_hidden)
        # [batch, max(args), max(context)]
        local_arguments_logits = convert_ragged_logits_to_dense(local_arguments_logits)
        
        # [batch, None(args), hdim]
        global_hidden_state_sequences = self.global_arguments_head(hidden_state_sequences)
        # [batch, context, hdim]
        global_embeddings = self.global_embeddings(scalar_proofstate_graph.context['global_context_ids'])
        # [batch, None(args), None(context)]
        global_arguments_logits = self.global_logits(queries=global_hidden_state_sequences, keys=global_embeddings)
        # [batch, max(args), max(context)]
        global_arguments_logits = convert_ragged_logits_to_dense(global_arguments_logits)
        if self._dynamic_global_context:
            global_arguments_logits_mask = self._global_arguments_logits_mask(scalar_proofstate_graph=scalar_proofstate_graph, global_context_size=len(self._graph_constants.global_context))
            global_arguments_logits += tf.expand_dims(global_arguments_logits_mask, axis=1)

        normalized_local_arguments_logits, normalized_global_arguments_logits = self._normalize_logits(local_arguments_logits=local_arguments_logits, global_arguments_logits=global_arguments_logits)

        # TODO(jrute): These are temporary conversions from non-ragged to ragged tensors.
        new_local_arguments_logits = tf.keras.layers.Lambda(self.convert_logits_to_ragged)((
            normalized_local_arguments_logits, 
            scalar_proofstate_graph.context['local_context_ids'], 
            arg_cnts,
            "local"))
        new_global_arguments_logits = tf.keras.layers.Lambda(self.convert_logits_to_ragged)((
            normalized_global_arguments_logits, 
            scalar_proofstate_graph.context['global_context_ids'], 
            arg_cnts,
            "global"))

        local_arguments_logits_output = self.local_arguments_logits_output(new_local_arguments_logits)
        global_arguments_logits_output = self.global_arguments_logits_output(new_global_arguments_logits)
        
        return GlobalArgumentModel(inputs=proofstate_graph,
                                   outputs={self.TACTIC_LOGITS: tactic_logits,
                                            self.LOCAL_ARGUMENTS_LOGITS: local_arguments_logits_output,
                                            self.GLOBAL_ARGUMENTS_LOGITS: global_arguments_logits_output})

    def create_inference_model(self, tactic_expand_bound: int, graph_constants: GraphConstants) -> tf.keras.Model:
        """
        Combines a GNN component with a tactic head and an arguments head to produce an end-to-end model for the
        global argument prediction task. The resulting model is for inference purposes, and produces tactics, their logits
        and argument logits (for each local context node / global context id per argument position).

        @warning: we do not use the GraphConstants saved in this task since during inference these may not be up-to-date
        @param tactic_expand_bound: the number of base tactic predictions to produce for each proofstate
        @param graph_constants: the graph constants to use during inference (with a possibly updated global context)
        @return: a keras model consuming graphs and producing tactic and local/global arguments logits
        """
        proofstate_graph = tf.keras.layers.Input(type_spec=batch_graph_spec(proofstate_graph_spec),
                                                 name=self.PROOFSTATE_GRAPH)
        scalar_proofstate_graph = proofstate_graph.merge_batch_to_components()

        tactic_mask = tf.keras.Input(shape=(graph_constants.tactic_num,), dtype=tf.bool, name=self.TACTIC_MASK)

        tactic_logits, hidden_graph = self._tactic_logits_and_hidden_graph(scalar_proofstate_graph)

        # [tactic_num, ]
        no_argument_tactics_mask = tf.constant(graph_constants.tactic_index_to_numargs, dtype = tf.int64) == 0
        all_tactics_mask = tf.ones(graph_constants.tactic_num, dtype=tf.bool)

        # [batch_size, ]
        no_local_context_proofstates = scalar_proofstate_graph.context['local_context_ids'].row_lengths() == 0
        no_global_context_proofstates = tf.fill(dims=(proofstate_graph.total_num_components,),
                                                value=len(graph_constants.global_context) == 0)
        no_context_proofstates = no_local_context_proofstates & no_global_context_proofstates

        # [batch_size, tactic_num]
        proofstate_tactic_mask = tf.where(tf.expand_dims(no_context_proofstates, axis=-1),
                                          tf.expand_dims(no_argument_tactics_mask, axis=0),
                                          tf.expand_dims(all_tactics_mask, axis=0))

        top_k_indices, top_k_values = self._top_k_tactics(tactic_logits=tactic_logits,
                                                          tactic_mask=proofstate_tactic_mask & tactic_mask,
                                                          tactic_expand_bound=tactic_expand_bound)

        tactic = tf.reshape(tf.transpose(top_k_indices), shape=(tf.size(top_k_indices),))

        repeat_scalar_graph = RepeatScalarGraph(num_repetitions=tactic_expand_bound)
        scalar_proofstate_graph = repeat_scalar_graph(scalar_proofstate_graph)  # noqa [ PyCallingNonCallable ]
        hidden_graph = repeat_scalar_graph(hidden_graph)  # noqa [ PyCallingNonCallable ]

        hidden_state_sequences = self._hidden_state_sequences(hidden_graph=hidden_graph, tactic=tactic)
        # [batch, None(args), hdim]
        local_hidden_state_sequences = self.local_arguments_head(hidden_state_sequences)
        # [batch, None(context), hdim]
        local_context_hidden = self._local_context_hidden(
            scalar_proofstate_graph,
            hidden_graph,
        )
        # [batch_size, None(args), None(context)]
        local_arguments_logits = QueryKeyMul()(local_hidden_state_sequences, local_context_hidden)
        # [batch, max(args), max(context)]
        local_arguments_logits = convert_ragged_logits_to_dense(local_arguments_logits)
        
        # [batch, None(args), hdim]
        global_hidden_state_sequences = self.global_arguments_head(hidden_state_sequences)
        # [batch, context, hdim]
        global_embeddings = self.global_embeddings(scalar_proofstate_graph.context['global_context_ids'])
        # [batch, None(args), None(context)]
        global_arguments_logits = self.global_logits(queries=global_hidden_state_sequences, keys=global_embeddings)
        # [batch, max(args), max(context)]
        global_arguments_logits = convert_ragged_logits_to_dense(global_arguments_logits)

        normalized_local_arguments_logits, normalized_global_arguments_logits = self._normalize_logits(
            local_arguments_logits=local_arguments_logits,
            global_arguments_logits=global_arguments_logits)

        normalized_local_arguments_logits = self._reshape_inference_logits(logits=normalized_local_arguments_logits,
                                                                           tactic_expand_bound=tactic_expand_bound)
        normalized_global_arguments_logits = self._reshape_global_inference_logits(
            logits=normalized_global_arguments_logits,
            tactic_expand_bound=tactic_expand_bound,
            dyn_global_context=scalar_proofstate_graph.context['global_context_ids'])
                                                                            

        return tf.keras.Model(inputs={self.PROOFSTATE_GRAPH: proofstate_graph, self.TACTIC_MASK: tactic_mask},
                              outputs={self.TACTIC: tf.transpose(top_k_indices),
                                       self.TACTIC_LOGITS: tf.transpose(top_k_values),
                                       self.LOCAL_ARGUMENTS_LOGITS: normalized_local_arguments_logits,
                                       self.GLOBAL_ARGUMENTS_LOGITS: normalized_global_arguments_logits})

    @staticmethod
    def create_input_output(graph_tensor: tfgnn.GraphTensor) -> Tuple[
        tfgnn.GraphTensor, Dict[str, Union[tf.Tensor, tf.RaggedTensor]]]:
        outputs = {GlobalArgumentPrediction.TACTIC_LOGITS: graph_tensor.context['tactic'],
                   GlobalArgumentPrediction.LOCAL_ARGUMENTS_LOGITS: graph_tensor.context['local_arguments'],
                   GlobalArgumentPrediction.GLOBAL_ARGUMENTS_LOGITS: graph_tensor.context['global_arguments']}
        return graph_tensor, outputs

    def loss(self) -> Dict[str, tf.keras.losses.Loss]:
        return {GlobalArgumentPrediction.TACTIC_LOGITS: tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True),
                GlobalArgumentPrediction.LOCAL_ARGUMENTS_LOGITS: ArgumentSparseCategoricalCrossentropy(sum_loss_over_tactic=self._sum_loss_over_tactic),
                GlobalArgumentPrediction.GLOBAL_ARGUMENTS_LOGITS: ArgumentSparseCategoricalCrossentropy(sum_loss_over_tactic=self._sum_loss_over_tactic)}

    def loss_weights(self) -> Dict[str, float]:
        return {GlobalArgumentPrediction.TACTIC_LOGITS: 1.0,
                GlobalArgumentPrediction.LOCAL_ARGUMENTS_LOGITS: self._arguments_loss_coefficient,
                GlobalArgumentPrediction.GLOBAL_ARGUMENTS_LOGITS: self._arguments_loss_coefficient}

    def metrics(self) -> Dict[str, List[tf.keras.metrics.Metric]]:
        return {GlobalArgumentPrediction.TACTIC_LOGITS: [tf.keras.metrics.SparseCategoricalAccuracy(name='accuracy')],
                GlobalArgumentPrediction.LOCAL_ARGUMENTS_LOGITS: [ArgumentSparseCategoricalAccuracy(name='accuracy')],
                GlobalArgumentPrediction.GLOBAL_ARGUMENTS_LOGITS: [ArgumentSparseCategoricalAccuracy(name='accuracy')]}

    def callbacks(self) -> List[tf.keras.callbacks.Callback]:
        return [MixedMetricsCallback()]


class DefinitionTask(tf.keras.layers.Layer):
    """
    A layer to compute definition embeddings from definition cluster graphs. The input graphs should:
        - be scalar graphs
        - follow the schema for `vectorized_definition_graph_spec` in `graph2tac.tfgnn.graph_schema`
    """
    def __init__(self,
                 graph_embedding: tf.keras.layers.Layer,
                 gnn: tf.keras.layers.Layer,
                 definition_head_type: str,
                 definition_head_config: dict,
                 name: str = 'definition_layer',
                 **kwargs):
        """
        @param graph_embedding: the GraphEmbedding layer from the prediction task
        @param gnn: the GNN layer from the prediction task
        @param definition_head_type: the type of definition head to use
        @param definition_head_config: the hyperparameters for the definition head
        @param name: the name of this layer
        @param kwargs: passed on to parent constructor
        """
        super().__init__(name=name, **kwargs)
        self._definition_head_type = definition_head_type

        self._graph_embedding = graph_embedding
        self._gnn = gnn

        definition_head_constructor = get_definition_head_constructor(definition_head_type)

        self.definition_head = definition_head_constructor(
            hidden_size=graph_embedding._hidden_size,
            unit_normalize=graph_embedding._unit_normalize,
            **definition_head_config
        )

    def get_checkpoint(self) -> tf.train.Checkpoint:
        """
        @return: a checkpoint tracking any **new** variables created by this layer
        """
        return tf.train.Checkpoint(definition_head=self.definition_head)

    def get_config(self):
        config = super().get_config()

        definition_head_config = self.definition_head.get_config()
        definition_head_config.pop('hidden_size')  # use the setting from graph embedding
        definition_head_config.pop('unit_normalize')  # use the setting from graph embedding

        config.update({
            'definition_head_type': self._definition_head_type,
            'definition_head_config': definition_head_config
        })
        return config

    @classmethod
    def from_yaml_config(cls,
                         graph_embedding: tf.keras.layers.Layer,
                         gnn: tf.keras.layers.Layer,
                         yaml_filepath: Path
                         ) -> Optional["DefinitionTask"]:
        with yaml_filepath.open() as yaml_file:
            config = yaml.load(yaml_file, Loader=yaml.SafeLoader)
        if config.get('definition_head_type') is None:
            return None
        else:
            return cls(graph_embedding=graph_embedding, gnn=gnn, **config)

    @staticmethod
    def _mask_defined_embeddings(scalar_definition_graph: tfgnn.GraphTensor, embedded_graph: tfgnn.GraphTensor):
        num_definitions = tf.cast(scalar_definition_graph.context['num_definitions'], dtype=tf.int32)
        is_defined = tf.ragged.range(scalar_definition_graph.node_sets['node'].sizes) < tf.expand_dims(num_definitions, axis=-1)
        mask = tf.expand_dims(1 - tf.cast(is_defined.flat_values, dtype=tf.float32), axis=-1)
        masked_hidden_state = embedded_graph.node_sets['node']['hidden_state'] * mask
        return embedded_graph.replace_features(node_sets={'node': {'hidden_state': masked_hidden_state}})

    def call(self, scalar_definition_graph: tfgnn.GraphTensor, training: bool = False):
        bare_graph = strip_graph(scalar_definition_graph)
        embedded_graph = self._graph_embedding(bare_graph, training=training)
        masked_embedded_graph = self._mask_defined_embeddings(scalar_definition_graph, embedded_graph)
        hidden_graph = self._gnn(masked_embedded_graph, training=training)

        num_definitions = scalar_definition_graph.context['num_definitions']
        definition_name_vectors = scalar_definition_graph.context['definition_name_vectors']
        definition_body_embeddings = self.definition_head((hidden_graph, num_definitions, definition_name_vectors), training=training)
        return definition_body_embeddings


def get_prediction_task_constructor(prediction_task_type: str
                                    ) -> Callable[..., Union[TacticPrediction, LocalArgumentPrediction, GlobalArgumentPrediction]]:
    if prediction_task_type == BASE_TACTIC_PREDICTION:
        return TacticPrediction
    elif prediction_task_type == LOCAL_ARGUMENT_PREDICTION:
        return LocalArgumentPrediction
    elif prediction_task_type == GLOBAL_ARGUMENT_PREDICTION:
        return GlobalArgumentPrediction
    else:
        raise ValueError(f'{prediction_task_type} is not a valid prediction task type')
