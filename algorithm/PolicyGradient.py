# coding=utf-8
import os
import sys
import random
import math
from collections import namedtuple
import numpy as np
import pandas as pd
import torch
import torch.optim as optim
import torch.autograd
sys.path.append(os.getcwd())

from environment.MarketEnv import MarketEnv
from baselines.ReplayMemory import ReplayMemory
from baselines.policy_network import LSTM_NN



os.environ['KMP_DUPLICATE_LIB_OK'] = 'True'
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def df_preprocess(path):
    dataframe = pd.read_csv(path, index_col=0, header=0)
    dataframe['trade_date'] = dataframe['trade_date'].astype('datetime64')
    dataframe = dataframe[dataframe['trade_date'] <= pd.datetime.strptime('20190809', '%Y%m%d')]
    dataframe['trade_date'] = dataframe['trade_date'].dt.date
    dataframe = dataframe.set_index('trade_date').fillna(method='ffill', axis=0)
    # 剔除 399016
    colnames = dataframe.columns
    colnames = colnames[[col[:6] != '399016' for col in colnames]]
    dataframe = dataframe[colnames]
    dataframe = dataframe.dropna(axis=0, how='any')
    # 筛选出price列名及其对应的 dataframe
    price_columns = colnames[[col[-5:] == 'close' for col in colnames]]
    return dataframe, price_columns.to_list()


Transition = namedtuple('Transition', ('log_prob', 'reward'))


class ReplayMemory(object):
    def __init__(self, capacity):
        self.capacity = capacity
        self.memory = []
        self.position = 0

    def push(self, *args):
        """Saves a transition."""
        if len(self.memory) < self.capacity:
            self.memory.append(None)
        self.memory[self.position] = Transition(*args)
        self.position = (self.position + 1) % self.capacity

    def sample(self, batch_size):
        return random.sample(self.memory, batch_size)

    def __len__(self):
        return len(self.memory)


def select_action(state1, state2, hold_rate, train=True):
    mu, sigma_matrix, sigma_vector = policy_net(state1, state2)
    sigma = sigma_matrix.squeeze() * torch.diagflat(sigma_vector + 1e-2) * torch.transpose(sigma_matrix.squeeze(), 0, 1)
    dist = torch.distributions.multivariate_normal.MultivariateNormal(loc=mu, covariance_matrix=sigma)
    if not train:
        action = dist.sample()
        while torch.all(action < 0):
            action = dist.sample()
        action = torch.clamp(action, min=0, max=1)
        action = action / torch.sum(action)
    else:
        global steps_done
        sample = random.random()
        eps_threshold = EPS_END + (EPS_START - EPS_END) * math.exp(-1. * steps_done / EPS_DECAY)
        if sample > eps_threshold or train:
            action = dist.sample()
            while torch.all(action < 0):
                action = dist.sample()
            action = torch.clamp(action, min=0, max=1)
            action = action / torch.sum(action)
        else:
            action = hold_rate
    return action, dist


def interactivate(env):
    state1, state2 = env.reset()
    memory = ReplayMemory(3000)
    while True:
        state1 = torch.from_numpy(state1).unsqueeze(0)
        state2 = torch.from_numpy(state2).unsqueeze(0)
        hold_rate = torch.from_numpy(env.next_rate.astype(np.float32))
        action, dist = select_action(state1, state2, hold_rate)
        log_prob = dist.log_prob(action)
        state, reward, done, info = env.step(action.squeeze().detach().numpy())
        reward = torch.tensor([reward], device=device)
        state1, state2 = state
        memory.push(log_prob, reward)
        if done:
            break
    env.render()
    return memory, #np.array(net_list)/env.initial_account_balance-1


def discount_reward(rewards, gamma=0.04/250):
    rewards_list = rewards.detach().numpy().tolist()
    discounted_ep_rs = np.zeros_like(rewards_list)
    running_add = 0
    for t in reversed(range(0, len(rewards_list))):
        running_add = running_add * gamma + rewards_list[t]
        discounted_ep_rs[t] = running_add
    return torch.from_numpy(discounted_ep_rs).to(device)


def optimize_model(memory, batch_size):
    transitions = memory.sample(batch_size)
    batch = Transition(*zip(*transitions))
    reward_batch = torch.cat(batch.reward)
    log_prob_batch = torch.cat(batch.log_prob)

    discounted_rewards = discount_reward(reward_batch)
    loss = -torch.mean(log_prob_batch * discounted_rewards)
    optimizer.zero_grad()
    loss.backward(retain_graph=True)
    optimizer.step()


if __name__ == '__main__':

    df, price_columns = df_preprocess('./data/create_feature.csv')
    windows = 250
    env = MarketEnv(df=df, price_cols=price_columns, windows=windows,
                    initial_account_balance=10000., buy_fee=0.015, sell_fee=0.)
    policy_net = LSTM_NN(input_size=df.shape[1], action_size=8, hidden_size=128, output_size=8).to(device)
    optimizer = optim.RMSprop(policy_net.parameters())

    EPS_START = 0.99
    EPS_END = 0.05
    EPS_DECAY = 200
    steps_done = 0

    num_episodes = 100
    for i_episode in range(num_episodes):
        memory, = interactivate(env)
        steps_done += 1
        optimize_model(memory, batch_size=64)
        env.render()


