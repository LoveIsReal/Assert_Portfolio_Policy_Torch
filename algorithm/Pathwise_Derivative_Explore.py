# coding=utf-8
import os
import sys
import random
import math
from collections import namedtuple
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from itertools import count
import torch
import torch.optim as optim
import torch.autograd
import torch.nn.functional as F

sys.path.append(os.getcwd())
from environment.MarketEnv import MarketEnv
from baselines.policy_network import A2C_ACTOR
from baselines.policy_network import A2C_QVALUE

os.environ['KMP_DUPLICATE_LIB_OK'] = 'True'
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def df_preprocess(path):
    df = pd.read_csv(path, index_col=0, header=0)
    df['trade_date'] = df['trade_date'].astype('datetime64')
    df = df[df['trade_date'] <= pd.datetime.strptime('20190809', '%Y%m%d')]
    df['trade_date'] = df['trade_date'].dt.date
    df = df.set_index('trade_date')
    colnames = df.columns.to_list()
    colnames = list(set(colnames) - set(['000001.SH_pe_y', '000300.SH_pe_y', '000905.SH_pe_y', '399006.SZ_pe_y']))
    colnames = [col for col in colnames if (col[:6] != '399016')]
    df = df[colnames].dropna(axis=0, how='all').fillna(method='ffill', axis=0).dropna(axis=0, how='any')
    for ind in [5, 10, 20, 30, 40, 60, 70, 125, 250, 500, 750]:
        df[[col + '_m' + str(ind) for col in colnames]] = df[colnames].rolling(window=ind, min_periods=1).mean()
        df[[col + '_q' + str(ind) for col in colnames]] = df[colnames].rolling(window=ind, min_periods=1).apply(
            lambda x: len(x[x <= x[-1]]) / len(x), raw=True)
    price_columns = [col for col in colnames if (col[-5:] == 'close')]
    return df, price_columns


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


BATCH_SIZE = 256
GAMMA = 0.999
EPS_START_LOW = 0.45
EPS_START_HIG = 0.55
EPS_END_LOW = 0.1
EPS_END_HIG = 0.9
EPS_DECAY = 100000
steps_done = 0


def select_action(state1, state2, hold_rate):
    global steps_done
    sample = random.random()
    eps_threshold_low = EPS_END_LOW + (EPS_START_LOW - EPS_END_LOW) * math.exp(-1. * steps_done / EPS_DECAY)
    eps_threshold_hig = EPS_END_HIG - (EPS_END_HIG - EPS_START_HIG) * math.exp(-1. * steps_done / EPS_DECAY)
    steps_done += 1
    if eps_threshold_low < sample < eps_threshold_hig:
        with torch.no_grad():
            return actor_policy(state1, state2)
    elif sample > eps_threshold_hig:
        return hold_rate.to(device)
    else:
        i = np.random.randint(low=0, high=n_actions)
        ratio = np.zeros(shape=(1, n_actions))
        ratio[0, i] = 1
        ratio = torch.from_numpy(ratio)
        return ratio.to(device)


# 优化模型参数
def optimize_model(memory):
    transitions = memory.sample(BATCH_SIZE)
    batch = Transition(*zip(*transitions))
    # 对next_state进行拼接
    env_next_state_batch = torch.tensor([env_next_state for env_next_state, _ in batch.next_state])
    act_next_state_batch = torch.tensor([act_next_state for _, act_next_state in batch.next_state])
    # 对state进行拼接
    env_state_batch = torch.cat([env_state for env_state, _ in batch.state])
    act_state_batch = torch.cat([act_state for _, act_state in batch.state])
    # 对action 和 reward 拼接
    action_batch = torch.cat(batch.action)
    reward_batch = torch.cat(batch.reward).unsqueeze(1)

    excepted_state_values = qvalue_target(env_next_state_batch, act_next_state_batch,
                                          actor_target(env_next_state_batch, act_next_state_batch)) + reward_batch
    estimate_state_values = qvalue_policy(env_state_batch, act_state_batch, action_batch)

    q_regression_loss = F.smooth_l1_loss(estimate_state_values, excepted_state_values * GAMMA)
    q_values_loss = - torch.mean(
        qvalue_policy(env_state_batch, act_state_batch, actor_policy(env_state_batch, act_state_batch)))
    # 优化模型
    qvalue_optimizer.zero_grad()
    q_regression_loss.backward()
    for param in qvalue_policy.parameters():
        param.grad.data.clamp_(-10, 10)
    qvalue_optimizer.step()

    actor_optimizer.zero_grad()
    q_values_loss.backward()
    for param in actor_policy.parameters():
        param.grad.data.clamp_(-10, 10)
    actor_optimizer.step()


def test_interact(env):
    net_list = []
    state1, state2 = env.reset()
    while True:
        state1 = torch.from_numpy(state1).unsqueeze(0)
        state2 = torch.from_numpy(state2).unsqueeze(0)
        with torch.no_grad():
            action = actor_policy(state1, state2)
        next_state, reward, done, _ = env.step(action.squeeze().detach().numpy())
        net_list.append(env.next_net)
        state1, state2 = next_state
        if done:
            print('test process: ', end=' ')
            env.render()
            break
    return np.array(net_list) / env.initial_account_balance - 1


if __name__ == '__main__':
    df, price_columns = df_preprocess('./data/create_feature.csv')
    windows = 250
    env = MarketEnv(df=df, price_cols=price_columns, windows=windows,
                    initial_account_balance=10000., buy_fee=0.015, sell_fee=0.)
    n_actions = env.action_space.shape[0]
    actor_policy = A2C_ACTOR(input_size=df.shape[1], hidden_size=128, output_size=n_actions).to(device)
    actor_target = A2C_ACTOR(input_size=df.shape[1], hidden_size=128, output_size=n_actions).to(device)
    actor_target.load_state_dict(actor_policy.state_dict())
    actor_optimizer = optim.RMSprop(actor_policy.parameters())
    qvalue_policy = A2C_QVALUE(input_size=df.shape[1], hidden_size=128, action_size=n_actions).to(device)
    qvalue_target = A2C_QVALUE(input_size=df.shape[1], hidden_size=128, action_size=n_actions).to(device)
    qvalue_target.load_state_dict(qvalue_policy.state_dict())
    qvalue_optimizer = optim.RMSprop(qvalue_policy.parameters())
    Transition = namedtuple('Transition', ('state', 'action', 'next_state', 'reward'))
    memory = ReplayMemory(30000)
    num_episodes = 50
    TARGET_UPDATE = 2

    ret_df = pd.DataFrame(index=df.index[250:], dtype=np.float64)
    for i_episode in range(num_episodes):
        # Initialize the environment and state
        state1, state2 = env.reset()
        for t in count():
            state1 = torch.from_numpy(state1).unsqueeze(0)
            state2 = torch.from_numpy(state2).unsqueeze(0)
            hold_rate = torch.from_numpy(env.next_rate.astype(np.float32)).unsqueeze(0)
            # Select and perform an action
            action = select_action(state1, state2, hold_rate)
            next_state, reward, done, _ = env.step(action.squeeze().detach().numpy())
            reward = torch.tensor([reward], device=device)
            memory.push((state1, state2), action, next_state, reward)
            # Move to the next state
            state1, state2 = next_state
            if len(memory) >= (BATCH_SIZE * 5) and (t % 3 == 0):
                optimize_model(memory)
            if len(memory) >= (BATCH_SIZE * 5) and (t % 100 == 0):
                qvalue_target.load_state_dict(qvalue_policy.state_dict())
                actor_target.load_state_dict(actor_policy.state_dict())
            if done:
                print('%s,  ' % i_episode, end=' ')
                env.render()
                break
        # Update the target network, copying all weights and biases in DQN
        if (i_episode + 1) % TARGET_UPDATE == 0:
            torch.save(actor_policy.state_dict(), "./model/pathwise_derivative_actor_%s epoch.pt" % (i_episode + 1))
            torch.save(qvalue_policy.state_dict(), "./model/pathwise_derivative_qvalue_%s epoch.pt" % (i_episode + 1))
            ret_df['%s epoch' % (i_episode + 1)] = test_interact(env)
            ret_df.plot(title='Returns Curve')
            plt.savefig('./image/ret/pathwise_derivative_update.jpg')
            plt.close()