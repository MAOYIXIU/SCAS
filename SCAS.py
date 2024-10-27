import copy
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.optim.lr_scheduler import CosineAnnealingLR


device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class Actor(nn.Module):
	def __init__(self, state_dim, action_dim, max_action):
		super(Actor, self).__init__()

		self.l1 = nn.Linear(state_dim, 256)
		self.l2 = nn.Linear(256, 256)
		self.l3 = nn.Linear(256, action_dim)
		
		self.max_action = max_action
		

	def forward(self, state):
		a = F.relu(self.l1(state))
		a = F.relu(self.l2(a))
		return self.max_action * torch.tanh(self.l3(a))


class Critic(nn.Module):
	def __init__(self, state_dim, action_dim):
		super(Critic, self).__init__()

		self.l1 = nn.Linear(state_dim + action_dim, 256)
		self.l2 = nn.Linear(256, 256)
		self.l3 = nn.Linear(256, 1)

		self.l4 = nn.Linear(state_dim + action_dim, 256)
		self.l5 = nn.Linear(256, 256)
		self.l6 = nn.Linear(256, 1)
  
		self.l7 = nn.Linear(state_dim + action_dim, 256)
		self.l8 = nn.Linear(256, 256)
		self.l9 = nn.Linear(256, 1)

		self.l10 = nn.Linear(state_dim + action_dim, 256)
		self.l11 = nn.Linear(256, 256)
		self.l12 = nn.Linear(256, 1)

	def forward(self, state, action):
		sa = torch.cat([state, action], -1)

		q1 = F.relu(self.l1(sa))
		q1 = F.relu(self.l2(q1))
		q1 = self.l3(q1)

		q2 = F.relu(self.l4(sa))
		q2 = F.relu(self.l5(q2))
		q2 = self.l6(q2)
  
		q3 = F.relu(self.l7(sa))
		q3 = F.relu(self.l8(q3))
		q3 = self.l9(q3)

		q4 = F.relu(self.l10(sa))
		q4 = F.relu(self.l11(q4))
		q4 = self.l12(q4)
		return q1, q2, q3, q4


class SCAS(object):
	def __init__(
		self,
		state_dim,
		action_dim,
		max_action,
		replay_buffer,
		dynamics,
		antmaze,
		discount=0.99,
		tau=0.005,
		policy_freq=2,
		schedule=True,
		temp=5.0,
		lam=0.25,
		beta=3e-3,
	):
		
		self.actor = Actor(state_dim, action_dim, max_action).to(device)
		self.actor_target = copy.deepcopy(self.actor)
		self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=2e-4)
		self.critic = Critic(state_dim, action_dim).to(device)
		self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), lr=3e-4)
		self.critic_target = copy.deepcopy(self.critic)
  
		self.replay_buffer = replay_buffer
		self.max_action = max_action
		self.action_dim = action_dim
		self.discount = discount
		self.tau = tau
		self.policy_freq = policy_freq
		self.dynamics = dynamics
		self.actor_lr_schedule = CosineAnnealingLR(self.actor_optimizer, int(int(1e6)/self.policy_freq))
		self.schedule = schedule
		self.temp = temp
		self.beta = beta
		self.lam = lam
		self.antmaze = antmaze
		self.max_weight = 50.0
		self.total_it = 0


	def select_action(self, state):
		with torch.no_grad():
			self.actor.eval()
			state = torch.FloatTensor(state.reshape(1, -1)).to(device)
			action = self.actor(state).cpu().data.numpy().flatten()
			self.actor.train()
			return action

	def train(self, batch_size=256, writer=None):
		self.total_it += 1

		# Sample replay buffer 
		state, action, next_state, reward, not_done = self.replay_buffer.sample(batch_size)
		# Compute the target Q value
		with torch.no_grad():
			noise = (torch.randn_like(action) * 0.2).clamp(-0.5, 0.5)
			next_action = (self.actor_target(next_state) + noise).clamp(-self.max_action, self.max_action)
			target_Q1, target_Q2, target_Q3, target_Q4 = self.critic_target(next_state, next_action)
			target_Q = torch.cat([target_Q1, target_Q2, target_Q3, target_Q4],dim=1)
			if self.antmaze:
				target_Q = torch.mean(target_Q,dim=1,keepdim=True)
			else:
				target_Q,_ = torch.min(target_Q,dim=1,keepdim=True)
			target_Q = reward + not_done * self.discount * target_Q

		# Get current Q estimates
		current_Q1, current_Q2, current_Q3, current_Q4 = self.critic(state, action)
		critic_loss =  F.mse_loss(current_Q1, target_Q) + F.mse_loss(current_Q2, target_Q) + F.mse_loss(current_Q3, target_Q) + F.mse_loss(current_Q4, target_Q)

		if self.total_it % 10000 == 0:
			with torch.no_grad():
				writer.add_scalar('train/critic_loss', critic_loss.item(), self.total_it)
				curr_Q = torch.cat([current_Q1,current_Q2,current_Q3,current_Q4], dim=1)
				writer.add_scalar('train/Q', curr_Q.mean().item(), self.total_it)
		# Optimize the critic
		self.critic_optimizer.zero_grad()
		critic_loss.backward()
		self.critic_optimizer.step()

		# Delayed policy updates
		if self.total_it % self.policy_freq == 0:
			# Compute actor loss
			pi = self.actor(state)
			v1,v2,v3,v4 = self.critic(state, pi)
			v = torch.cat([v1,v2,v3,v4], dim=1)
			v_min,_ = torch.min(v, dim=1)
			lmbda = 1.0 / v_min.abs().mean().detach() # follow TD3BC
			maxq_loss = -lmbda * v_min.mean()

			with torch.no_grad():
				v_mean = (v1 + v2 + v3 + v4)/ 4
				next_pi = self.actor(next_state)
				next_v1,next_v2,next_v3,next_v4 = self.critic(next_state, next_pi)
				next_v_mean = (next_v1 + next_v2 + next_v3 + next_v4)/ 4
				weight =  (self.temp * (next_v_mean.detach() - v_mean.detach())).exp().clamp(max=self.max_weight)
				state_hat = state + torch.randn(state.shape).to(device) * self.beta
			pred_next_state = self.dynamics(state_hat, pi)
			state_recovery_loss = weight * (pred_next_state - next_state)**2
			state_recovery_loss = state_recovery_loss.mean()

			actor_loss = (1 - self.lam) * maxq_loss + self.lam * state_recovery_loss

			self.actor_optimizer.zero_grad()
			actor_loss.backward()
			self.actor_optimizer.step()
			if self.schedule:
				self.actor_lr_schedule.step()

			if self.total_it % 10000 == 0:
				writer.add_scalar('train/actor_loss', actor_loss.item(), self.total_it)
				writer.add_scalar('train/maxq_loss', maxq_loss.item(), self.total_it)
				writer.add_scalar('train/state_recovery_loss', (state_recovery_loss).item(), self.total_it)
			# Update the frozen target models
			for param, target_param in zip(self.critic.parameters(), self.critic_target.parameters()):
				target_param.data.copy_(self.tau * param.data + (1 - self.tau) * target_param.data)

			for param, target_param in zip(self.actor.parameters(), self.actor_target.parameters()):
				target_param.data.copy_(self.tau * param.data + (1 - self.tau) * target_param.data)