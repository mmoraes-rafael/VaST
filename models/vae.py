"""
Variational State Tabulator
Author: Dane Corneil
"""

import tensorflow as tf
import numpy as np
import logging
import threading
import time
from ops import *
from base import BaseModel

cross_ent = tf.nn.sigmoid_cross_entropy_with_logits

eps = 1e-5

class FilteringVAE(BaseModel):

	def _final_init(self, params):
		self.channels = self.obs_channels*(self.hist_len+1)
		self.n_z = params["n_z"]
		self.n_dim = 2
		self.beta_prior = params["beta_prior"]
		self.beta_reward = params['beta_reward']
		self.tau_period = params["tau_period"]
		self.tau_min = params["tau_min"]
		self.tau_max = params["tau_max"]
		self.prior_tau = params.get("prior_tau", 0.5)
		self.straight_through = params.get("straight_through", False)
		if self.tau_min == self.tau_max:
			self._get_tau = self._get_constant_tau
		else:
			self._get_tau = self._get_annealed_tau
		self.weight_summaries = params.get("weight_summaries", False)

	def _create_network(self):
		super(FilteringVAE, self)._create_network()
		# tf Graph input
		self._create_input()
		# Use recognition network to determine probabilities in latent space
		self.logit_z = self.encoder_network(self.input_obs, 
		                               		self.n_z*self.n_dim)

		######
		# Invert order of computing z_prob to include NF and sampled variables
		#####
		self.z = self._sample_z(self.logit_z)
		self.z_after_flows = tf.reshape(self.z, [-1, self.n_z * self.n_dim])

		with tf.variable_scope('z_prob'):
			#self.z_prob = self._softmax(self.logit_z)
			self.z_prob = self.z_after_flows
		with tf.variable_scope('z_entropy'):
			#self.z_entropy = -tf.reduce_sum(self.z_prob*tf.log(self.z_prob+1e-15), 1)
			self.z_entropy = -tf.reduce_sum(self.z_after_flows * tf.log(self.z_after_flows + 1e-15), 1)
			variable_summaries(self.z_entropy)

		# Recover reconstruction of input from sampled z
		recon_zs = self.z[:self.minibatch_size]
		self.raw_obs_reconstr = self._decoder_network(recon_zs, self.net_arch)
		self.obs_reconstr = tf.nn.sigmoid(self.raw_obs_reconstr, 'obs_reconstr')
		# Predict next z from previous z
		pred_zs = self.z[self.minibatch_size:]
		pred_acts = self.acts[:self.minibatch_size]
		self.state_predictions = self._prediction_network(pred_zs, pred_acts, self.net_arch)
		if self.beta_reward>0:
			self.reward_predictions = self.state_predictions[:, 0]
			self.state_predictions = self.state_predictions[:, 1:]

	def _create_input(self):
		self.hist_obs = tf.placeholder(tf.uint8, [None, 
											 	  self.obs_size_x,
											 	  self.obs_size_y,
											 	  self.channels],
											 	  name="obs")
		self.acts = tf.placeholder(tf.uint16, [None], name="acts")
		self.starts = tf.placeholder(tf.bool, [None], name="starts")
		if self.beta_reward>0:
			self.batch_rewards = tf.placeholder(tf.float32, [None], name="rewards")
		pre_obs = self.hist_obs[:, :, :, self.obs_channels:]
		post_obs = self.hist_obs[:, :, :, :-self.obs_channels]
		self.obs = tf.concat([post_obs, pre_obs], 0)
		self.input_obs = tf.to_float(self.obs)/255.
		self.target_obs = (tf.to_float(post_obs[:self.minibatch_size, :, :, :self.obs_channels])/255.)

	def normFlow_1PF(self, z_0):
		log_abs_det_jacobian = 0

		self.u1 = tf.Variable(tf.random_normal(shape=[self.n_z], mean=0.0, stddev=1.0))
		self.w1 = tf.get_variable("w1", shape=[self.n_z, self.n_z], initializer=tf.contrib.layers.xavier_initializer())
		self.b1 = tf.get_variable("b1", shape=[self.n_z], initializer=tf.constant_initializer(0.1))

		wt_z0 = tf.matmul(z_0, self.w1)
		wt_z0 = tf.clip_by_value(wt_z0, -5, 5)

		self.uhat1 = tf.expand_dims(self.u1, 0) + tf.matmul( (-1 + tf.log(1 + tf.exp(wt_z0) ) - wt_z0) ,  self.w1 / (tf.reduce_sum(tf.square(self.w1))) )

		self.transf1 = wt_z0 + tf.expand_dims(self.b1, 0)
		#self.transf1 = tf.clip_by_value(self.transf1, -5, 5)
		self.z_1 = tf.add(z_0, tf.multiply(self.uhat1, tf.sigmoid(self.transf1)))

		h_prime1 = tf.exp(self.transf1) / tf.pow(1 + tf.exp(self.transf1), 2) 
		log_abs_det_jacobian += tf.log(tf.abs(1 + tf.matmul(tf.matmul(h_prime1, self.w1), tf.expand_dims(self.u1, 1) ) ) + eps)

		return self.z_1, log_abs_det_jacobian

	def normFlow_4PF(self, z_0):
		log_abs_det_jacobian = 0

		#### Flow 1
		self.u1 = tf.Variable(tf.random_normal(shape=[self.n_z], mean=0.0, stddev=1.0))
		self.w1 = tf.get_variable("w1", shape=[self.n_z, self.n_z], initializer=tf.contrib.layers.xavier_initializer())
		self.b1 = tf.get_variable("b1", shape=[self.n_z], initializer=tf.constant_initializer(0.1))

		wt_z0 = tf.matmul(z_0, self.w1)
		wt_z0 = tf.clip_by_value(wt_z0, -5, 5)

		self.uhat1 = tf.expand_dims(self.u1, 0) + tf.matmul( (-1 + tf.log(1 + tf.exp(wt_z0) ) - wt_z0) ,  self.w1 / (tf.reduce_sum(tf.square(self.w1))) )

		self.transf1 = wt_z0 + tf.expand_dims(self.b1, 0)
		#self.transf1 = tf.clip_by_value(self.transf1, -5, 5)
		self.z_1 = tf.add(z_0, tf.multiply(self.uhat1, tf.sigmoid(self.transf1)))

		h_prime1 = tf.exp(self.transf1) / tf.pow(1 + tf.exp(self.transf1), 2) 
		log_abs_det_jacobian += tf.log(tf.abs(1 + tf.matmul(tf.matmul(h_prime1, self.w1), tf.expand_dims(self.u1, 1) ) ) + eps)

		#### Flow 2
		self.u2 = tf.Variable(tf.random_normal(shape=[self.n_z], mean=0.0, stddev=1.0))
		self.w2 = tf.get_variable("w2", shape=[self.n_z, self.n_z], initializer=tf.contrib.layers.xavier_initializer())
		self.b2 = tf.get_variable("b2", shape=[self.n_z], initializer=tf.constant_initializer(0.1))

		wt_z1 = tf.matmul(self.z_1, self.w2)
		wt_z1 = tf.clip_by_value(wt_z1, -5, 5)

		self.uhat2 = tf.expand_dims(self.u2, 0) + tf.matmul( (-1 + tf.log(1 + tf.exp(wt_z1) ) - wt_z1) ,  self.w2 / (tf.reduce_sum(tf.square(self.w2))) )

		self.transf2 = wt_z1 + tf.expand_dims(self.b2, 0)
		#self.transf1 = tf.clip_by_value(self.transf1, -5, 5)
		self.z_2 = tf.add(self.z_1, tf.multiply(self.uhat2, tf.sigmoid(self.transf2)))

		h_prime2 = tf.exp(self.transf2) / tf.pow(1 + tf.exp(self.transf2), 2) 
		log_abs_det_jacobian += tf.log(tf.abs(1 + tf.matmul(tf.matmul(h_prime2, self.w2), tf.expand_dims(self.u2, 1) ) ) + eps)

		#### Flow 3
		self.u3 = tf.Variable(tf.random_normal(shape=[self.n_z], mean=0.0, stddev=1.0))
		self.w3 = tf.get_variable("w3", shape=[self.n_z, self.n_z], initializer=tf.contrib.layers.xavier_initializer())
		self.b3 = tf.get_variable("b3", shape=[self.n_z], initializer=tf.constant_initializer(0.1))

		wt_z2 = tf.matmul(self.z_2, self.w3)
		wt_z2 = tf.clip_by_value(wt_z2, -5, 5)

		self.uhat3 = tf.expand_dims(self.u3, 0) + tf.matmul( (-1 + tf.log(1 + tf.exp(wt_z2) ) - wt_z2) ,  self.w3 / (tf.reduce_sum(tf.square(self.w3))) )

		self.transf3 = wt_z2 + tf.expand_dims(self.b3, 0)
		#self.transf1 = tf.clip_by_value(self.transf1, -5, 5)
		self.z_3 = tf.add(self.z_2, tf.multiply(self.uhat3, tf.sigmoid(self.transf3)))

		h_prime3 = tf.exp(self.transf3) / tf.pow(1 + tf.exp(self.transf3), 2) 
		log_abs_det_jacobian += tf.log(tf.abs(1 + tf.matmul(tf.matmul(h_prime3, self.w3), tf.expand_dims(self.u3, 1) ) ) + eps)

		#### Flow 4
		self.u4 = tf.Variable(tf.random_normal(shape=[self.n_z], mean=0.0, stddev=1.0))
		self.w4 = tf.get_variable("w4", shape=[self.n_z, self.n_z], initializer=tf.contrib.layers.xavier_initializer())
		self.b4 = tf.get_variable("b4", shape=[self.n_z], initializer=tf.constant_initializer(0.1))

		wt_z3 = tf.matmul(self.z_3, self.w4)
		wt_z3 = tf.clip_by_value(wt_z3, -5, 5)

		self.uhat4 = tf.expand_dims(self.u4, 0) + tf.matmul( (-1 + tf.log(1 + tf.exp(wt_z3) ) - wt_z3) ,  self.w4 / (tf.reduce_sum(tf.square(self.w4))) )

		self.transf4 = wt_z3 + tf.expand_dims(self.b4, 0)
		#self.transf1 = tf.clip_by_value(self.transf1, -5, 5)
		self.z_4 = tf.add(self.z_3, tf.multiply(self.uhat4, tf.sigmoid(self.transf4)))

		h_prime4 = tf.exp(self.transf4) / tf.pow(1 + tf.exp(self.transf4), 2) 
		log_abs_det_jacobian += tf.log(tf.abs(1 + tf.matmul(tf.matmul(h_prime4, self.w4), tf.expand_dims(self.u4, 1) ) ) + eps)


		return self.z_4, log_abs_det_jacobian


	def RNVP_layer(self, z_in, D, d, layer_nr):
		# We need to alternate between which part is transformed
		# and which part is preserved
		if layer_nr % 2 == 0:
			z_pres = z_in[:, :d]
			z_trans_pre = z_in[:, d:]

			trans_dim = D - d
			pres_dim = d

		else:
			z_pres = z_in[:, d:]
			z_trans_pre = z_in[:, :d]

			trans_dim = d
			pres_dim = D - d

		# Weights for the s transform
		ws = tf.get_variable("ws{}".format(layer_nr), shape=[pres_dim, trans_dim], initializer=tf.contrib.layers.xavier_initializer())
		bs = tf.get_variable("bs{}".format(layer_nr), shape=[trans_dim], initializer=tf.constant_initializer(0.1))

		# Weights for the t transform
		wt = tf.get_variable("wt{}".format(layer_nr), shape=[pres_dim, trans_dim], initializer=tf.contrib.layers.xavier_initializer())
		bt = tf.get_variable("bt{}".format(layer_nr), shape=[trans_dim], initializer=tf.constant_initializer(0.1))

		# S uses tanh to avoid overflow in the exponential
		# see second to last paragraph page 7 RNVP paper
		s = tf.nn.tanh(tf.matmul(z_pres, ws) + bs)
		t = tf.nn.relu(tf.matmul(z_pres, wt) + bt)

		z_trans_post = tf.multiply(z_trans_pre, tf.exp(s)) + t

		# Concatenat the preserved and transformed values
		z_out = tf.concat([z_pres, z_trans_post], 1)

		log_abs_det_jacobian = tf.reduce_sum(s)

		return z_out, log_abs_det_jacobian


	def normFlow_RNVP(self, z_0):
		log_abs_det_jacobian = 0

		# Transform half of the variables at a time
		D = self.n_z
		d = int(D / 2)

		nr_layers = 4

		z_1 = z_0
		for i in range(nr_layers):
			z_1, cost = self.RNVP_layer(z_1, D, d, i)
			log_abs_det_jacobian += cost

		return z_1, log_abs_det_jacobian


	def _sample_z(self, logit_z):
		# Draw one sample z from Gumbel-Softmax distribution
		with tf.name_scope('z_sample'):
			self.tau = self._get_tau()
			all_vars = tf.reshape(self.logit_z, [-1, self.n_dim])
			self.log_alphas = tf.reshape(all_vars[:, 0] - all_vars[:, 1], [-1, self.n_z])
			self.y = (self.log_alphas + sample_logistic(tf.shape(self.log_alphas)))/self.tau

			#####
			## Apply Normalizing Flow on top of the logistic
			#####
			self.log_abs_det_jacobian = 0

			#self.z_1, log_det_jac  = self.normFlow_1PF(self.y)
			self.z_k, log_det_jac  = self.normFlow_4PF(self.y)
			#self.z_k, log_det_jac  = self.normFlow_RNVP(self.y)
			
			self.log_abs_det_jacobian += log_det_jac

			self.log_abs_det_jacobian = tf.clip_by_value(self.log_abs_det_jacobian, -100, 100)

			#zs = tf.nn.sigmoid(self.y)
			zs = tf.nn.sigmoid(self.z_k)

			z = tf.reshape(tf.stack([zs, 1-zs],2), [-1, self.n_z*self.n_dim])
		return z

	def _softmax(self, logit_z):
		all_vars = tf.reshape(logit_z, [-1, self.n_dim])
		return tf.reshape(tf.nn.softmax(all_vars), [-1, self.n_z*self.n_dim]) 

	def _get_annealed_tau(self):
		tau_rate = self.tau_period**-1
		step = tf.to_float(self.model_step)
		return tf.maximum(self.tau_max*tf.exp(-tau_rate*step), self.tau_min)

	def _get_constant_tau(self):
		return self.tau_max

	def _decoder_network(self, input_z, shapes, reuse=False):
		with tf.variable_scope('decoder'):
			batch_size = tf.shape(input_z)[0]
			strides = [layer_shape[-1] for layer_shape in shapes['decoder'][1:]]
			with tf.variable_scope('dec_h1'):
				scaling = float(np.prod(strides))
				x = int(np.ceil(self.obs_size_x/scaling))
				y = int(np.ceil(self.obs_size_y/scaling))
				last_layer = self.transfer_fct(linear(input_z, shapes['decoder'][0]*x*y, 
											   reuse, False), reuse)
				last_layer = tf.reshape(last_layer, (batch_size, x, y, shapes['decoder'][0]))
				if not reuse and self.weight_summaries:
					variable_summaries(last_layer)
			num_deconv_layers = len(shapes['decoder'][1:])
			for layer in range(1, num_deconv_layers):
				layer_name = 'dec_h%s' % str(layer+1)
				with tf.variable_scope(layer_name):
					scaling = float(np.prod(strides[layer:]))
					x = int(np.ceil(self.obs_size_x/scaling))
					y = int(np.ceil(self.obs_size_y/scaling))
					channels, filt_size, stride = shapes['decoder'][layer]
					shape = [batch_size, x, y, channels]
					last_layer = self.transfer_fct(deconv_lin(last_layer, shape, filt_size, 
					                                          stride, reuse, False), reuse)
					if not reuse and self.weight_summaries:
						variable_summaries(last_layer)
			with tf.variable_scope('dec_out'):
				filt_size, stride = shapes['decoder'][-1]
				shape_lout = [batch_size, self.obs_size_x, self.obs_size_y, self.obs_channels]
				raw_obs_reconstr = deconv_lin(last_layer, shape_lout, filt_size, stride, reuse)
		return raw_obs_reconstr

	def _prediction_network(self, input_z, act, shapes, reuse=False):
		with tf.variable_scope('prediction'):
			num_hidden_layers = len(shapes['prediction'][:-1])
			act_branches = []
			for act_br in range(self.n_act):
				last_layer = input_z
				for layer in range(num_hidden_layers):
					layer_name = 'act%i_pred_h%s' % (act_br, str(layer+1))
					with tf.variable_scope(layer_name):
						last_layer = self.transfer_fct(linear(last_layer, 
						                                      shapes['prediction'][layer], 
															  reuse, False), reuse)
						if not reuse and self.weight_summaries:
							variable_summaries(last_layer)
				with tf.variable_scope('act%i_pred_out' % act_br):
					if self.beta_reward>0:
						logit_act = linear(last_layer, self.n_z+1, reuse)
					else:
						logit_act = linear(last_layer, self.n_z, reuse)
				act_branches.append(logit_act)
			prediction_stack = tf.stack(act_branches, name='prediction_tree')
			prediction_stack = tf.transpose(prediction_stack, [1, 0, 2])
			act = tf.cast(act, tf.int32)
			batch_act_inds = tf.transpose(tf.stack([tf.range(tf.shape(act)[0]), act]))
		return tf.gather_nd(prediction_stack, batch_act_inds)

	def _create_losses(self):
		obs_cross_entropy = cross_ent(logits=self.raw_obs_reconstr, labels=self.target_obs)
		self.reconstr_cost = tf.reduce_mean(tf.reduce_sum(obs_cross_entropy, [1,2,3]))
		tf.summary.scalar('reconstr_cost', self.reconstr_cost)
		self.prior_cost, self.reward_cost = self._create_prior_cost()
		self.cost = self.reconstr_cost + self.beta_prior*self.prior_cost + self.beta_reward*self.reward_cost
		
	def _log_logistic_density(self, y, tau, log_alpha=0):
		return tf.log(tau) - tau*y + log_alpha - 2*tf.log(1 + tf.exp(-tau*y + log_alpha))

	def _create_prior_cost(self):
		output_y = self.y[:self.minibatch_size]
		log_y_density = tf.reduce_sum(self._log_logistic_density(output_y, 
		                                                         self.tau, 
		                                                         self.log_alphas[:self.minibatch_size]), 1)
		log_init_priors = tf.get_variable('log_init_priors', [self.n_z], 
		                                  initializer=tf.constant_initializer(0.0))
		starts = tf.cast(self.starts[:self.minibatch_size], tf.float32)
		init_priors = starts[:, None]*log_init_priors
		predict_priors = (1 - starts)[:, None]*self.state_predictions
		self.log_priors = init_priors + predict_priors
		log_prior_density = tf.reduce_sum(self._log_logistic_density(output_y, 
		                                                             self.prior_tau,
		                                                             self.log_priors), 1)
		prior_cost = tf.reduce_mean(log_y_density - log_prior_density) - tf.reduce_mean(self.log_abs_det_jacobian)
		tf.summary.scalar('prior_cost', prior_cost)
		reward_cost = 0
		if self.beta_reward>0:
			reward_cross_entropy = cross_ent(logits=self.reward_predictions, 
			                                 labels=self.rewards[self.minibatch_size])
			reward_cost = tf.reduce_mean(reward_cross_entropy)
			tf.summary.scalar('reward_cost', reward_cost)
		return prior_cost, reward_cost

	def finish_training(self, step, summary_writer):
		return None, None

	def train(self, step, summary_writer, batch=None):
		kwargs = {'run_metadata': None}
		replay_inds = None
		if batch is not None:
			feed_dict = {self.hist_obs: batch['obs'], 
						 self.acts: batch['acts'],
			             self.starts: batch['starts']}
			if self.beta_reward>0:
				feed_dict[self.batch_rewards] = batch['rewards']
			kwargs['feed_dict'] = feed_dict
			replay_inds = batch['inds']
		fetches = {'zs': self.z_prob, 'opt': self.optimizer}
		if step % (100*self.summary_step) == 0:
			trace = tf.RunOptions.FULL_TRACE
			kwargs['options'] = tf.RunOptions(trace_level=trace)
			kwargs['run_metadata'] = tf.RunMetadata()
		if step % self.summary_step == 0:
			fetches['summary'] = self.merged
		results = self.sess.run(fetches, **kwargs)
		if results.get('summary') is not None:
			logging.info("Train step: %i" % step)
			summary_writer.summarize_model(results.get('summary'), 
			                               step, 
			                               kwargs['run_metadata'])
		return self.process_state_assignments(results['zs']), replay_inds

	def process_state_assignments(self, zs):
		batch_size = len(zs)/2
		return_zs = np.empty_like(zs)
		return_zs[::2] = zs[:batch_size]
		return_zs[1::2] = zs[batch_size:]
		return return_zs
	
	def encode(self, obs):
		if len(obs.shape) == 3:
			zs = self.sess.run(self.z_prob, feed_dict={self.obs: obs[None]})[0]
		elif len(obs) <= 500:
			zs = self.sess.run(self.z_prob, feed_dict={self.obs: obs})
		else:
			all_chunk_obs = chunks(obs, 500)
			zs = []
			for chunk_obs in all_chunk_obs:
				zs.append(self.sess.run(self.z_prob, 
				                        feed_dict={self.obs: chunk_obs}))
			zs = np.concatenate(zs)
		return zs

	def encode_logits(self, obs):
		return self.sess.run(self.logit_z, feed_dict={self.obs: obs})

	def encode_and_sample(self, obs):
		return self.sess.run(self.z, feed_dict={self.obs: obs})

	def reconstruct(self, obs):
		return self.sess.run(self.obs_reconstr, feed_dict={self.obs: obs})
	
	def generate(self, z=None):
		if z is None:
			z = np.eye(self.n_dim)[np.random.choice(self.n_dim, self.n_z)].flatten()
		return self.sess.run(self.obs_reconstr, feed_dict={self.z: z})

	def _init_tensorboard(self):
		# Setup TensorBoard	
		tf.summary.image('compare_image', make_compare_plot(self.target_obs, 
		                                                    self.obs_reconstr, 
		                                                    1), 
						 max_outputs=1)	
		z_image = tf.reshape(self.z_prob, [-1, self.n_z, self.n_dim, 1])
		if self.n_dim < self.n_z:
			z_image = tf.transpose(z_image, [0,2,1,3])
		tf.summary.image('z', z_image, max_outputs=1)
		pred_image = tf.transpose(tf.stack([self.log_priors, self.y[:self.minibatch_size]]), [1, 0, 2])
		tf.summary.image('compare_prediction', pred_image[:, :, :, None], max_outputs=1)
		tf.summary.scalar('obs_mse', tf.reduce_mean(tf.square(self.obs_reconstr-self.target_obs)))
		super(FilteringVAE, self)._init_tensorboard()

class ConcurrentVAE(FilteringVAE):

	def _final_init(self, params):
		super(ConcurrentVAE, self)._final_init(params)
		self.train_step = params['train_step']
		self.training_thread = None
		self.is_not_training = threading.Event()
		self.is_not_training.set()
		self.exit_signal = threading.Event()
		self.state_assignments = None

	def _create_network(self):
		batch_data = self._create_batch_input()
		#Backwards compatible
		try:
			dataset = tf.data.Dataset.from_tensor_slices(batch_data)
		except AttributeError:
			dataset = tf.contrib.data.Dataset.from_tensor_slices(batch_data)

		self.dataset = dataset.batch(self.minibatch_size)
		self.iterator = self.dataset.make_initializable_iterator()
		super(ConcurrentVAE, self)._create_network()

	def _create_batch_input(self):
		self.batch_obs = tf.placeholder(tf.uint8, [None, 
											   	   self.obs_size_x,
											   	   self.obs_size_y,
											   	   self.channels],
											   	   name="batch_obs")
		self.batch_acts = tf.placeholder(tf.uint16, [None], name="batch_acts")
		self.batch_starts = tf.placeholder(tf.bool, [None], name="batch_starts")
		if self.beta_reward>0:
			self.batch_rewards = tf.placeholder(tf.float32, [None], name="batch_rewards")
			batch_data = (self.batch_obs, self.batch_acts, self.batch_starts, self.batch_rewards)
		else:
			batch_data = (self.batch_obs, self.batch_acts, self.batch_starts)
		return batch_data

	def _create_input(self):
		if self.beta_reward>0:
			obs, self.acts, self.starts, self.rewards = self.iterator.get_next()
			self.rewards = (tf.clip_by_value(self.rewards, -1, 1) + 1)/2
		else:
			obs, self.acts, self.starts = self.iterator.get_next()
		pre_obs = obs[:, :, :, self.obs_channels:]
		post_obs = obs[:, :, :, :-self.obs_channels]
		self.obs = tf.concat([post_obs, pre_obs], 0)
		self.input_obs = tf.to_float(self.obs)/255.
		self.target_obs = (tf.to_float(post_obs[:self.minibatch_size, :, :, :self.obs_channels])/255.)

	def _load_iterator(self, batch):
		feed_dict = {self.batch_obs: batch['obs'], 
					 self.batch_acts: batch['acts'],
		             self.batch_starts: batch['starts']}
		if self.beta_reward>0:
			feed_dict[self.batch_rewards] = batch['rewards']
		self.sess.run(self.iterator.initializer, feed_dict = feed_dict)

	def finish_training(self):
		logging.debug("Waiting for training thread to finish.")
		start_time = time.time()
		self.is_not_training.wait()
		wait_time = time.time() - start_time
		logging.debug("Finished waiting for training thread.")
		new_states = self.state_assignments
		self.state_assignments = None
		return new_states, wait_time

	def train(self, step, summary_writer, batch):
		new_states, wait_time = self.finish_training()
		self.is_not_training.clear()
		self.training_thread = threading.Thread(target=self.train_thread, 
		                                        name="TrainingThread",
		                                        args=(step, summary_writer, 
		                                              batch))
		logging.debug("Starting training thread.")
		self.training_thread.start()
		return new_states, wait_time

	def train_thread(self, step, summary_writer, batch):
		self._load_iterator(batch)
		del batch
		state_assignments = []
		while not self.exit_signal.is_set():
			try:
				zs, _ = super(ConcurrentVAE, self).train(step, summary_writer)
				state_assignments.append(zs)
				step += self.train_step
			except tf.errors.OutOfRangeError:
				logging.debug("Model training batch completed.")
				break
		self.state_assignments = np.concatenate(state_assignments)
		logging.debug("Notifying of thread completion.")
		self.is_not_training.set()

	def save(self, step):
		self.is_not_training.wait()
		super(ConcurrentVAE, self).save(step)
