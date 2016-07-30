#!/usr/bin/env python
# -*- tab-width:4;indent-tabs-mode:f;show-trailing-whitespace:t;rm-trailing-spaces:t -*-
# vi: set ts=4 et sw=4:


# adapt neural-fingeprint to tensorflow
# to fit fingerprints based on a single endpoint regression


from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from six.moves import xrange  # pylint: disable=redefined-builtin

import numpy as np
import tensorflow as tf
from neuralfingerprint import load_data
from neuralfingerprint.build_convnet import array_rep_from_smiles
from neuralfingerprint.features import num_atom_features, num_bond_features
from neuralfingerprint.mol_graph import degrees
from neuralfingerprint.build_convnet import array_rep_from_smiles



def initialize_variables(training_params, model_params):
	variables = {}
	with tf.name_scope("regularization") as scope:
		variables['l2_loss'] = tf.constant(0.0, name="l2_loss")
		variables['l1_loss'] = tf.constant(0.0, name="l1_loss")


	def add_weights(weight_key, shape, op=tf.random_normal):
		weights = tf.Variable(
			op(shape, stddev=np.exp(training_params['log_init_scale'])), name=weight_key)
		variables[weight_key] = weights
		with tf.name_scope("regularization/") as regularization_scope:
			variables['l2_loss'] += tf.nn.l2_loss(weights)
			variables['l1_loss'] += tf.reduce_sum(tf.abs(weights))

	with tf.name_scope("fingerprint") as scope:
		# neural fingerprint layer output weights and biases
		all_layer_sizes = [num_atom_features()] + [model_params['fp_width']] * model_params['fp_depth']
		for layer in range(len(all_layer_sizes)):
			with tf.name_scope("layer_{}".format(layer)):
				add_weights(
					'layer_output_weights_{}'.format(layer),
					[all_layer_sizes[layer], model_params['fp_length']])

				add_weights(
					'layer_output_bias_{}'.format(layer),
					[model_params['fp_length']])

		 # neural fingerprint graph integration layer weights and biases
		in_and_out_sizes = zip(all_layer_sizes[:-1], all_layer_sizes[1:])
		for layer, (N_prev, N_cur) in enumerate(in_and_out_sizes):
			with tf.name_scope("layer_{}/".format(layer)) as layer_scope:
				 add_weights(
					 "layer_{}_biases".format(layer),
					 [N_cur])

				 add_weights(
					 "layer_{}_self_filter".format(layer),
					 [N_prev, N_cur])

				 add_weights(
					 "layer_{}_neighbor_filter".format(layer),
					 [N_prev + num_bond_features(), N_cur])

				 for degree in degrees:
					 add_weights(
						 'layer_{}_neighbor_{}_filter'.format(layer, degree),
						[N_prev + num_bond_features(), N_cur])

	with tf.name_scope("prediction") as scope:
		# prediction network weights and biases
		layer_sizes = model_params['prediction_layer_sizes'] + [1]
		for i, shape in enumerate(zip(layer_sizes[:-1], layer_sizes[1:])):
			add_weights(
				"prediction_weights_{}".format(i),
				shape)

			add_weights(
				"prediction_biases_{}".format(i),
				[shape[1]])

	return variables


def build_summary_network(loss):
	loss_summary = tf.scalar_summary("loss", loss)
	summaries = tf.merge_all_summaries()
	return summaries


def build_morgan_fps_network(smiles, batch_size, model_params):
	def smiles_to_fps(smiles_tuple):
		fps = []
		for smiles in smiles_tuple:
			molecule = Chem.MolFromSmiles(smiles)
			fp = AllChem.GetMorganFingerprintAsBitVect(
				molecule, model_params['fp_radius'], nBits=model_params['fp_length'])
			fps.append(fp.ToBitString())
		fps = np.array(fps)
		fps = np.array([list(fp) for fp in fps], dtype=int)

	morgan_fps_list = tf.py_func(
		func=smiles_to_fps,
		inp=[smiles],
		Tout=[tf.int64])

	morgan_fps = morgan_fps_list[0]
	morgan_fps.set_shape([batch_size, model_params['fp_length']])
	return morgan_fps


def build_neural_fps_network(substances, variables, model_params):

	def matmult_neighbors(atom_features, layer, substances, variables):
		"""
		N_atoms = 14700
		  # with degree 0 = 0
		  # with degree 1 = 4310
		  # with degree 2 = 5283
		  # with degree 3 = 4290
		  # with degree 4 = 817
		  # with degree 5 = 0

		num_atom_features = 20
		num_bond_features = 6

		atom_features: [N_atoms, num_atom_features()]:float32

		atom_neighbor_list: [N_atoms with degree, degree]:int32
		bond_neighbor_list: [N_atoms with degree, degree]:int32

		neighbor_features: [
		    [N_atoms with degree, degree, num_atom_features],
		    [N_atoms with degree, degree, num_bond_features] ]

		stacked_neigbors: [N_atoms with degree, degree, num_atom_features + num_bond_features]
		summed_neighbors: [N_atoms with degree, num_atom_features + num_bond_features]
		activations: [N_atoms with degree, num_atom_features]
		return [N_atoms, num_atom_features]

		"""
		with tf.name_scope("neighbor_activations") as scope:
			activations_by_degree = []
			for degree in degrees:
				atom_neighbor_list = substances['atom_neighbors_{}'.format(degree)]
				bond_neighbor_list = substances['bond_neighbors_{}'.format(degree)]
				neighbor_features = [
					tf.gather(params=atom_features, indices=atom_neighbor_list),
					tf.gather(params=substances['bond_features'], indices=bond_neighbor_list)]
				stacked_neighbors = tf.concat(concat_dim=2, values=neighbor_features)
				summed_neighbors = tf.reduce_sum(stacked_neighbors, reduction_indices=1)
				neighbor_filter = variables['layer_{}_neighbor_{}_filter'.format(layer, degree)]
				activations = tf.matmul(summed_neighbors, neighbor_filter)
				activations_by_degree.append(activations)
				activations = tf.concat(concat_dim=0, values=activations_by_degree, name="activations")
			return activations

	def update_layer(atom_features, layer, substances, variables):
		with tf.name_scope("layer_{}/".format(layer)) as update_layer_scope:
			layer_bias		= variables["layer_{}_biases".format(layer)]
			layer_self_filter = variables["layer_{}_self_filter".format(layer)]
			self_activations = tf.matmul(atom_features, layer_self_filter)
			neighbor_activations = matmult_neighbors(
				atom_features, layer, substances, variables)
			activations = tf.nn.bias_add(tf.add(neighbor_activations, self_activations), layer_bias)
			activations_mean, activations_variance = tf.nn.moments(activations, [0], keep_dims=True)
			activations = tf.nn.batch_normalization(
				activations, activations_mean, activations_variance,
				offset=None, scale=None, variance_epsilon=1e-3)
			activations = tf.nn.relu(activations, name="activations")
			return activations

	def write_to_fingerprint(atom_features, layer, substances, variables):
		"""
		N_atoms = 14700 (for example)
		N_compounds = 800 (for example)
		num_atom_features = 20

		atom_features: [N_atoms, num_atom_features]
        substance_atoms: SparseTensor[N_substances, N_atoms]

		hidden: [N_atoms, fp_length]
		atom_outputs: [N_atoms, fp_length]
		layer_outputs: [N_compounds, fp_length]
		"""
		with tf.name_scope("layer_{}/".format(layer)) as scope:
			out_weights = variables['layer_output_weights_{}'.format(layer)]
			out_bias	= variables['layer_output_bias_{}'.format(layer)]
			hidden = tf.nn.bias_add(tf.matmul(atom_features, out_weights), out_bias)
			atom_outputs = tf.nn.softmax(hidden)
            layer_output = tf.sparse_tensor_dense_matmul(
				substances['substance_atoms'], atom_outputs, name=scope)
			return layer_output

	with tf.name_scope("fingerprint/") as fingerprint_scope:
		atom_features = substances['atom_features']
		fps = write_to_fingerprint(atom_features, 0, substances, variables)

		num_hidden_features = [model_params['fp_width']] * model_params['fp_depth']
		for layer in xrange(len(num_hidden_features)):
			atom_features = update_layer(atom_features, layer, substances, variables)
			fps += write_to_fingerprint(atom_features, layer+1, substances, variables)

		return fps

def build_normed_prediction_network(fps, variables, model_params):
	with tf.name_scope("prediction/") as scope:
		hidden = fps
		layer_sizes = model_params['prediction_layer_sizes'] + [1]
		for layer in range(len(layer_sizes) - 1):
			weights = variables['prediction_weights_{}'.format(layer)]
			biases = variables['prediction_biases_{}'.format(layer)]
			activations = tf.nn.bias_add(tf.matmul(hidden, weights), biases, name="activations")
			if layer < len(layer_sizes) - 2:
				activations_mean, activations_variance = tf.nn.moments(
					activations, [0], keep_dims=True)
				activations = tf.nn.batch_normalization(
					activations, activations_mean, activations_variance,
					offset=None, scale=None, variance_epsilon=1e-3)
			hidden = tf.nn.relu(activations)
		return tf.squeeze(hidden, name="predictions")


def build_loss_network(
	normed_predictions,
	labels,
	variables,
	model_params):

	with tf.name_scope("loss") as loss_scope:

		labels_mean, labels_variance = tf.nn.moments(labels, [0], keep_dims=False)
		labels_std = tf.sqrt(labels_variance)
		norm_labels = (labels - labels_mean) / labels_std
		predictions = normed_predictions * labels_std + labels_mean

		# http://stackoverflow.com/questions/33846069/how-to-set-rmse-cost-function-in-tensorflow
		return predictions, tf.sqrt(tf.reduce_mean((predictions - labels)**2)) \
			+ model_params['l2_penalty'] * variables['l2_loss'] \
			+ model_params['l1_penalty'] * variables['l1_loss']


def build_optimizer(loss, train_params):
	with tf.name_scope("optimizer") as optimizer_scope:
		batch = tf.Variable(0.0)
		learning_rate = tf.constant(np.exp(train_params['log_learning_rate']))
		beta1 = tf.constant(np.exp(train_params['log_b1']))
		beta2 = tf.constant(np.exp(train_params['log_b2']))
		adam = tf.train.AdamOptimizer(learning_rate, beta1, beta2)
		optimizer = adam.minimize(loss, global_step=batch)
		return optimizer




