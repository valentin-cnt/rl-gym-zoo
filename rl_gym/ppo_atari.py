import argparse
import random
import time
from datetime import datetime
from pathlib import Path
from warnings import simplefilter

import gymnasium as gym
import numpy as np
import torch
from torch import nn, optim
from torch.distributions import Categorical
from torch.nn.functional import mse_loss
from torch.nn.utils.clip_grad import clip_grad_norm_
from torch.utils.tensorboard.writer import SummaryWriter
from tqdm import tqdm

simplefilter(action="ignore", category=DeprecationWarning)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=str, default="BreakoutNoFrameskip-v4")
    parser.add_argument("--total-timesteps", type=int, default=int(1e6))
    parser.add_argument("--num-envs", type=int, default=4)
    parser.add_argument("--num-steps", type=int, default=2048)
    parser.add_argument("--num-minibatches", type=int, default=32)
    parser.add_argument("--num-optims", type=int, default=10)
    parser.add_argument("--learning-rate", type=float, default=3e-4)
    parser.add_argument('--list-layer', nargs="+", type=int, default=[64, 64])
    parser.add_argument("--gamma", type=float, default=0.99)
    parser.add_argument("--gae", type=float, default=0.95)
    parser.add_argument("--eps-clip", type=float, default=0.2)
    parser.add_argument("--value-factor", type=float, default=0.5)
    parser.add_argument("--entropy-factor", type=float, default=0.01)
    parser.add_argument("--shared-network", action="store_true")
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--capture-video", action="store_true")
    parser.add_argument("--seed", type=int, default=0)

    _args = parser.parse_args()

    _args.device = torch.device(
        "cpu" if _args.cpu or not torch.cuda.is_available() else "cuda")
    _args.batch_size = int(_args.num_envs * _args.num_steps)
    _args.minibatch_size = int(_args.batch_size // _args.num_minibatches)
    _args.num_updates = int(_args.total_timesteps // _args.num_steps)

    return _args


def make_env(env_id, idx, run_dir, capture_video):

    def thunk():

        if capture_video:
            env = gym.make(env_id, render_mode="rgb_array")
        else:
            env = gym.make(env_id)
        env = gym.wrappers.RecordEpisodeStatistics(env)
        env = gym.wrappers.AtariPreprocessing(env, scale_obs=True)
        env = gym.wrappers.FrameStack(env, 4)
        if capture_video and idx == 0:
            env = gym.wrappers.RecordVideo(env=env,
                                           video_folder=f"{run_dir}/videos/",
                                           disable_logger=True)
        return env

    return thunk


def layer_init(layer, std=np.sqrt(2), bias_const=0.):

    torch.nn.init.orthogonal_(layer.weight, std)
    torch.nn.init.constant_(layer.bias, bias_const)
    return layer


class ActorCriticNet(nn.Module):

    def __init__(self, args, action_space):

        super().__init__()

        num_actions = action_space.n

        self.network = nn.Sequential(
            layer_init(nn.Conv2d(4, 32, 8, stride=4)),
            nn.ReLU(),
            layer_init(nn.Conv2d(32, 64, 4, stride=2)),
            nn.ReLU(),
            layer_init(nn.Conv2d(64, 64, 3, stride=1)),
            nn.ReLU(),
            nn.Flatten(),
            layer_init(nn.Linear(64 * 7 * 7, 512)),
            nn.ReLU(),
        )
        self.actor_net = layer_init(nn.Linear(512, num_actions), std=0.01)
        self.critic_net = layer_init(nn.Linear(512, 1), std=1)

        self.optimizer = optim.Adam(self.parameters(), lr=args.learning_rate)

        if args.device.type == "cuda":
            self.cuda()

    def forward(self):
        pass

    def get_action_value(self, state, action=None):

        output = self.network(state)
        actor_value = self.actor_net(output)
        distribution = Categorical(logits=actor_value)

        if action is None:
            action = distribution.sample()

        log_prob = distribution.log_prob(action)
        dist_entropy = distribution.entropy()

        critic_value = self.critic_net(output).squeeze()

        return action.cpu().numpy(), log_prob, critic_value, dist_entropy

    def get_value(self, state):
        output = self.network(state)
        return self.critic_net(output)


def main():
    args = parse_args()

    date = str(datetime.now().strftime("%d-%m_%H:%M:%S"))
    run_dir = Path(
        Path(__file__).parent.resolve().parent, "runs",
        f"{args.env}__ppo__{date}")
    writer = SummaryWriter(run_dir)
    writer.add_text(
        "hyperparameters",
        "|param|value|\n|-|-|\n%s" %
        ("\n".join([f"|{key}|{value}|" for key, value in vars(args).items()])),
    )

    if args.seed > 0:
        random.seed(args.seed)
        np.random.seed(args.seed)
        torch.manual_seed(args.seed)

    # Create vectorized environment(s)
    envs = gym.vector.SyncVectorEnv([
        make_env(args.env, i, run_dir, args.capture_video)
        for i in range(args.num_envs)
    ])

    action_space = envs.single_action_space

    policy_net = ActorCriticNet(args, action_space)

    obversation_shape = envs.single_observation_space.shape
    action_shape = envs.single_action_space.shape

    states = torch.zeros((args.num_steps, args.num_envs) +
                         obversation_shape).to(args.device)
    actions = torch.zeros((args.num_steps, args.num_envs) + action_shape).to(
        args.device)
    rewards = torch.zeros((args.num_steps, args.num_envs)).to(args.device)
    flags = torch.zeros((args.num_steps, args.num_envs)).to(args.device)
    log_probs = torch.zeros((args.num_steps, args.num_envs)).to(args.device)
    state_values = torch.zeros((args.num_steps, args.num_envs)).to(args.device)

    if args.seed > 0:
        state, _ = envs.reset(seed=args.seed)
    else:
        state, _ = envs.reset()

    global_step = 0

    for update in tqdm(range(args.num_updates)):
        start = time.perf_counter()

        # Annealing learning rate
        frac = 1. - (update - 1.) / args.num_updates
        new_lr = frac * args.learning_rate
        policy_net.optimizer.param_groups[0]["lr"] = new_lr

        # Generate transitions
        for i in range(args.num_steps):
            global_step += 1

            with torch.no_grad():
                state_torch = torch.from_numpy(state).to(args.device).float()
                action, log_prob, state_value, _ = policy_net.get_action_value(
                    state_torch)

            next_state, reward, terminated, truncated, infos = envs.step(
                action)

            states[i] = state_torch
            actions[i] = torch.from_numpy(action).to(args.device)
            rewards[i] = torch.from_numpy(reward).to(args.device)
            log_probs[i] = log_prob
            state_values[i] = state_value

            done = np.logical_or(terminated, truncated)
            flags[i] = torch.from_numpy(done).to(args.device)

            state = next_state

            if "final_info" not in infos:
                continue

            for info in infos["final_info"]:
                if info is None:
                    continue

                writer.add_scalar("rollout/episodic_return",
                                  info["episode"]["r"], global_step)
                writer.add_scalar("rollout/episodic_length",
                                  info["episode"]["l"], global_step)

        end = time.perf_counter()
        writer.add_scalar("rollout/time", end - start, global_step)

        # Compute values
        with torch.no_grad():
            state_torch = torch.from_numpy(state).to(args.device).float()
            next_state_value = policy_net.get_value(state_torch).squeeze(-1)

        advantages = torch.zeros(rewards.size()).to(args.device)
        adv = torch.zeros(rewards.size(1)).to(args.device)

        for i in reversed(range(rewards.size(0))):
            terminal = 1. - flags[i]

            returns = rewards[i] + args.gamma * next_state_value * terminal
            delta = returns - state_values[i]

            adv = args.gamma * args.gae * adv * terminal + delta
            advantages[i] = adv

            next_state_value = state_values[i]

        td_target = (advantages + state_values).squeeze()
        advantages = (advantages - advantages.mean()) / (advantages.std() +
                                                         1e-7)
        advantages = advantages.squeeze()

        # Flatten batch
        _states = states.flatten(0, 1)
        _actions = actions.flatten(0, 1)
        _log_probs = log_probs.reshape(-1)
        _td_target = td_target.reshape(-1)
        _advantages = advantages.reshape(-1)

        batch_indexes = np.arange(args.batch_size)

        clipfracs = []

        # Update policy
        for _ in range(args.num_optims):

            # Shuffle batch
            np.random.shuffle(batch_indexes)

            for start in range(0, args.batch_size, args.minibatch_size):
                end = start + args.minibatch_size
                index = batch_indexes[start:end]

                _, new_log_probs, td_predict, dist_entropy = policy_net.get_action_value(
                    _states[index], _actions[index])

                logratio = new_log_probs - _log_probs[index]
                ratios = logratio.exp()

                with torch.no_grad():
                    # Calculate approx_kl http://joschu.net/blog/kl-approx.html
                    old_approx_kl = (-logratio).mean()
                    approx_kl = ((ratios - 1) - logratio).mean()
                    clipfracs += [
                        ((ratios - 1.).abs() > 0.2).float().mean().item()
                    ]

                surr1 = _advantages[index] * ratios

                surr2 = _advantages[index] * torch.clamp(
                    ratios, 1. - args.eps_clip, 1. + args.eps_clip)

                policy_loss = -torch.min(surr1, surr2).mean()

                value_loss = args.value_factor * mse_loss(
                    td_predict, _td_target[index])

                entropy_bonus = args.entropy_factor * dist_entropy.mean()

                loss = policy_loss + value_loss - entropy_bonus

                policy_net.optimizer.zero_grad()
                loss.backward()
                clip_grad_norm_(policy_net.parameters(), 0.5)
                policy_net.optimizer.step()

        writer.add_scalar("update/policy_loss", policy_loss, global_step)
        writer.add_scalar("update/value_loss", value_loss, global_step)
        writer.add_scalar("debug/old_approx_kl", old_approx_kl, global_step)
        writer.add_scalar("debug/approx_kl", approx_kl, global_step)
        writer.add_scalar("debug/clipfrac", np.mean(clipfracs), global_step)

    envs.close()
    writer.close()


if __name__ == '__main__':
    main()