import torch
from torch.distributions import Categorical, Normal
from torch.nn.functional import softmax
from rlgym.algorithm.base import Base
from rlgym.neuralnet import LinearNet


class REINFORCE(Base):

    def update_policy(self, minibatch):
        rewards = minibatch["rewards"]
        log_probs = minibatch["logprobs"]

        discounted_rewards = self._discounted_rewards(rewards)

        loss = (-log_probs * discounted_rewards).mean()

        self._model.optimizer.zero_grad()
        loss.backward()
        self._model.optimizer.step()


class REINFORCEDiscrete(REINFORCE):

    def __init__(self, num_inputs, action_space, learning_rate, list_layer,
                 is_shared_network):
        super(REINFORCEDiscrete, self).__init__()

        num_actionss = action_space.n

        self._model = LinearNet(num_inputs,
                                num_actionss,
                                learning_rate,
                                list_layer,
                                is_continuous=False)
        self._model.cuda()

    def act(self, state):
        actor_value = self._model(state)

        probs = softmax(actor_value, dim=0)
        dist = Categorical(probs)

        action = dist.sample()
        logprob = dist.log_prob(action)

        return action.item(), logprob


class REINFORCEContinuous(REINFORCE):

    def __init__(self, num_inputs, action_space, learning_rate, list_layer,
                 is_shared_network):
        super(REINFORCEContinuous, self).__init__()

        self.bound_interval = torch.Tensor(action_space.high).cuda()

        self._model = LinearNet(num_inputs,
                                action_space,
                                learning_rate,
                                list_layer,
                                is_continuous=True)
        self._model.cuda()

    def act(self, state):
        actor_value = self._model(state)

        mean = torch.tanh(actor_value[0]) * self.bound_interval
        variance = torch.sigmoid(actor_value[1])
        dist = Normal(mean, variance)

        action = dist.sample()
        log_prob = dist.log_prob(action).sum()

        return action.cpu().numpy(), log_prob
