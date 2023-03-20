import argparse
import time
from datetime import datetime

import gymnasium as gym
import numpy as np
import torch
from torch import nn, optim
from torch.distributions import Categorical
from torch.nn.functional import mse_loss
from torch.nn.utils.clip_grad import clip_grad_norm_
from torch.utils.tensorboard.writer import SummaryWriter
from tqdm import tqdm


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env_id", type=str, default="ALE/Pong-v5")
    parser.add_argument("--total_timesteps", type=int, default=10_000_000)
    parser.add_argument("--num_envs", type=int, default=8)
    parser.add_argument("--num_steps", type=int, default=128)
    parser.add_argument("--num_optims", type=int, default=4)
    parser.add_argument("--minibatch_size", type=int, default=256)
    parser.add_argument("--learning_rate", type=float, default=2.5e-4)
    parser.add_argument("--gamma", type=float, default=0.99)
    parser.add_argument("--gae", type=float, default=0.95)
    parser.add_argument("--eps_clip", type=float, default=0.1)
    parser.add_argument("--value_coef", type=float, default=0.5)
    parser.add_argument("--entropy_coef", type=float, default=0.01)
    parser.add_argument("--clip_grad_norm", type=float, default=0.5)
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--capture_video", action="store_true")
    parser.add_argument("--wandb", action="store_true")
    parser.add_argument("--seed", type=int, default=0)

    args = parser.parse_args()

    args.device = torch.device("cpu" if args.cpu or not torch.cuda.is_available() else "cuda")
    args.batch_size = int(args.num_envs * args.num_steps)
    args.num_minibatches = int(args.batch_size // args.minibatch_size)
    args.num_updates = int(args.total_timesteps // args.batch_size)

    return args


def make_env(env_id, capture_video=False, run_dir="."):
    def thunk():
        if capture_video:
            env = gym.make(env_id, frameskip=1, render_mode="rgb_array")
            env = gym.wrappers.RecordVideo(
                env=env,
                video_folder=f"{run_dir}/videos",
                episode_trigger=lambda x: x,
                disable_logger=True,
            )
        else:
            env = gym.make(env_id, frameskip=1)
        env = gym.wrappers.RecordEpisodeStatistics(env)
        env = gym.wrappers.AtariPreprocessing(env)
        env = gym.wrappers.FrameStack(env, 4)

        return env

    return thunk


def compute_advantages(rewards, flags, values, last_value, args):
    advantages = torch.zeros((args.num_steps, args.num_envs)).to(args.device)
    adv = torch.zeros(args.num_envs).to(args.device)

    for i in reversed(range(args.num_steps)):
        returns = rewards[i] + args.gamma * flags[i] * last_value
        delta = returns - values[i]

        adv = delta + args.gamma * args.gae * flags[i] * adv
        advantages[i] = adv

        last_value = values[i]

    return advantages


class RolloutBuffer:
    def __init__(self, num_steps, num_envs, observation_shape, device):
        self.states = np.zeros((num_steps, num_envs, *observation_shape), dtype=np.float32)
        self.actions = np.zeros((num_steps, num_envs), dtype=np.int64)
        self.rewards = np.zeros((num_steps, num_envs), dtype=np.float32)
        self.flags = np.zeros((num_steps, num_envs), dtype=np.float32)
        self.log_probs = np.zeros((num_steps, num_envs), dtype=np.float32)
        self.values = np.zeros((num_steps, num_envs), dtype=np.float32)

        self.step = 0
        self.num_steps = num_steps
        self.device = device

    def push(self, state, action, reward, flag, log_prob, value):
        self.states[self.step] = state
        self.actions[self.step] = action
        self.rewards[self.step] = reward
        self.flags[self.step] = flag
        self.log_probs[self.step] = log_prob
        self.values[self.step] = value

        self.step = (self.step + 1) % self.num_steps

    def get(self):
        return (
            torch.from_numpy(self.states).to(self.device),
            torch.from_numpy(self.actions).to(self.device),
            torch.from_numpy(self.rewards).to(self.device),
            torch.from_numpy(self.flags).to(self.device),
            torch.from_numpy(self.log_probs).to(self.device),
            torch.from_numpy(self.values).to(self.device),
        )


class ActorCriticNet(nn.Module):
    def __init__(self, action_dim, device):
        super().__init__()

        self.network = self._build_net()

        self.actor_net = self._build_linear(512, action_dim, std=0.01)
        self.critic_net = self._build_linear(512, 1, std=1.0)

        if device.type == "cuda":
            self.cuda()

    def _build_linear(self, in_size, out_size, apply_init=True, std=np.sqrt(2), bias_const=0.0):
        layer = nn.Linear(in_size, out_size)

        if apply_init:
            torch.nn.init.orthogonal_(layer.weight, std)
            torch.nn.init.constant_(layer.bias, bias_const)

        return layer

    def _build_conv2d(self, in_size, out_size, kernel_size, stride, apply_init=True, std=np.sqrt(2), bias_const=0.0):
        layer = nn.Conv2d(in_size, out_size, kernel_size, stride)

        if apply_init:
            torch.nn.init.orthogonal_(layer.weight, std)
            torch.nn.init.constant_(layer.bias, bias_const)

        return layer

    def _build_net(self):
        return nn.Sequential(
            self._build_conv2d(4, 32, 8, stride=4),
            nn.ReLU(),
            self._build_conv2d(32, 64, 4, stride=2),
            nn.ReLU(),
            self._build_conv2d(64, 64, 3, stride=1),
            nn.ReLU(),
            nn.Flatten(),
            self._build_linear(64 * 7 * 7, 512),
            nn.ReLU(),
        )

    def forward(self, state):
        output = self.network(state)
        actor_value = self.actor_net(output)
        distribution = Categorical(logits=actor_value)

        action = distribution.sample()
        log_prob = distribution.log_prob(action)

        critic_value = self.critic_net(output).squeeze(-1)

        return action, log_prob, critic_value

    def evaluate(self, states, actions):
        output = self.network(states)
        actor_values = self.actor_net(output)
        distribution = Categorical(logits=actor_values)

        log_probs = distribution.log_prob(actions)
        dist_entropy = distribution.entropy()

        critic_values = self.critic_net(output).squeeze(-1)

        return log_probs, critic_values, dist_entropy

    def critic(self, state):
        return self.critic_net(self.network(state)).squeeze(-1)


def train(args, run_name, run_dir):
    # Initialize wandb if needed (https://wandb.ai/)
    if args.wandb:
        import wandb

        wandb.init(project=args.env_id, name=run_name, sync_tensorboard=True, config=vars(args))

    # Create tensorboard writer and save hyperparameters
    writer = SummaryWriter(run_dir)
    hyperparameters = "\n".join([f"|{key}|{value}|" for key, value in vars(args).items()])
    table = f"|param|value|\n|-|-|\n{hyperparameters}"
    writer.add_text("hyperparameters", table)

    # Create vectorized environment(s)
    envs = gym.vector.AsyncVectorEnv([make_env(args.env_id) for _ in range(args.num_envs)])

    # Metadata about the environment
    observation_shape = envs.single_observation_space.shape
    action_dim = envs.single_action_space.n

    # Set seed for reproducibility
    if args.seed:
        numpy_rng = np.random.default_rng(args.seed)
        torch.manual_seed(args.seed)
        state, _ = envs.reset(seed=args.seed)
    else:
        numpy_rng = np.random.default_rng()
        state, _ = envs.reset()

    # Create policy network and optimizer
    policy = ActorCriticNet(action_dim, args.device)
    optimizer = optim.Adam(policy.parameters(), lr=args.learning_rate)
    scheduler = optim.lr_scheduler.LambdaLR(optimizer, lr_lambda=lambda epoch: 1.0 - (epoch - 1.0) / args.num_updates)

    # Create buffers
    rollout_buffer = RolloutBuffer(args.num_steps, args.num_envs, observation_shape, args.device)

    log_episodic_returns = []

    global_step = 0
    start_time = time.process_time()

    # Main loop
    for _ in tqdm(range(args.num_updates)):
        for _ in range(args.num_steps):
            # Update global step
            global_step += 1 * args.num_envs

            with torch.no_grad():
                # Get action
                action, log_prob, value = policy(torch.from_numpy(state).to(args.device).float())

            # Perform action
            action = action.cpu().numpy()
            next_state, reward, terminated, truncated, infos = envs.step(action)

            # Store transition
            flag = 1.0 - np.logical_or(terminated, truncated)
            log_prob = log_prob.cpu().numpy()
            value = value.cpu().numpy()
            rollout_buffer.push(state, action, reward, flag, log_prob, value)

            state = next_state

            if "final_info" not in infos:
                continue

            # Log episodic return and length
            for info in infos["final_info"]:
                if info is None:
                    continue

                log_episodic_returns.append(info["episode"]["r"])
                writer.add_scalar("rollout/episodic_return", info["episode"]["r"], global_step)
                writer.add_scalar("rollout/episodic_length", info["episode"]["l"], global_step)

                break

        # Get transition batch
        states, actions, rewards, flags, log_probs, values = rollout_buffer.get()

        with torch.no_grad():
            last_value = policy.critic(torch.from_numpy(next_state).to(args.device).float())

        # Calculate advantages and TD target
        advantages = compute_advantages(rewards, flags, values, last_value, args)
        td_target = advantages + values

        # Normalize advantages
        advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

        # Flatten batch
        states = states.reshape(-1, *observation_shape)
        actions = actions.reshape(-1)
        log_probs = log_probs.reshape(-1)
        td_target = td_target.reshape(-1)
        advantages = advantages.reshape(-1)
        values = values.reshape(-1)

        batch_indexes = np.arange(args.batch_size)

        clipfracs = []

        # Perform PPO update
        for _ in range(args.num_optims):
            # Shuffle batch
            numpy_rng.shuffle(batch_indexes)

            # Perform minibatch updates
            for start in range(0, args.batch_size, args.minibatch_size):
                end = start + args.minibatch_size
                index = batch_indexes[start:end]

                # Calculate new values from minibatch
                _log_probs, td_predict, dist_entropy = policy.evaluate(states[index], actions[index])

                # Calculate ratios
                logratio = _log_probs - log_probs[index]
                ratios = logratio.exp()

                # Calculate approx_kl (http://joschu.net/blog/kl-approx.html)
                with torch.no_grad():
                    old_approx_kl = (-logratio).mean()
                    approx_kl = ((ratios - 1) - logratio).mean()
                    clipfracs += [((ratios - 1.0).abs() > 0.2).float().mean().item()]

                # Calculate surrogates
                surr1 = advantages[index] * ratios
                surr2 = advantages[index] * torch.clamp(ratios, 1.0 - args.eps_clip, 1.0 + args.eps_clip)

                # Calculate losses
                actor_loss = -torch.min(surr1, surr2).mean()
                critic_loss = mse_loss(td_predict, td_target[index])
                entropy_bonus = dist_entropy.mean()

                loss = actor_loss + critic_loss * args.value_coef - entropy_bonus * args.entropy_coef

                # Update policy network
                optimizer.zero_grad()
                loss.backward()
                clip_grad_norm_(policy.parameters(), args.clip_grad_norm)
                optimizer.step()

        # Annealing learning rate
        scheduler.step()

        explained_var = (
            np.nan if torch.var(td_target) == 0 else 1 - torch.var(td_target - values) / torch.var(td_target)
        )

        # Log training metrics
        writer.add_scalar("train/actor_loss", actor_loss, global_step)
        writer.add_scalar("train/critic_loss", critic_loss, global_step)
        writer.add_scalar("train/old_approx_kl", old_approx_kl, global_step)
        writer.add_scalar("train/approx_kl", approx_kl, global_step)
        writer.add_scalar("train/clipfrac", np.mean(clipfracs), global_step)
        writer.add_scalar("train/explained_var", explained_var, global_step)
        writer.add_scalar("rollout/SPS", int(global_step / (time.process_time() - start_time)), global_step)

    # Save final policy
    torch.save(policy.state_dict(), f"{run_dir}/policy.pt")
    print(f"Saved policy to {run_dir}/policy.pt")

    # Close the environment
    envs.close()
    writer.close()

    # Average of episodic returns (for the last 5% of the training)
    indexes = int(len(log_episodic_returns) * 0.05)
    mean_train_return = np.mean(log_episodic_returns[-indexes:])
    writer.add_scalar("rollout/mean_train_return", mean_train_return, global_step)

    return mean_train_return


def eval_and_render(args, run_dir):
    # Create environment
    env = gym.vector.SyncVectorEnv([make_env(args.env_id, capture_video=True, run_dir=run_dir)])

    # Metadata about the environment
    action_dim = env.single_action_space.n

    # Load policy
    policy = ActorCriticNet(action_dim, args.device)
    policy.load_state_dict(torch.load(f"{run_dir}/policy.pt"))
    policy.eval()

    count_episodes = 0
    list_rewards = []

    state, _ = env.reset()

    # Run episodes
    while count_episodes < 30:
        with torch.no_grad():
            action, _, _ = policy(torch.from_numpy(state).to(args.device).float())

        action = action.cpu().numpy()
        state, _, _, _, infos = env.step(action)

        if "final_info" in infos:
            info = infos["final_info"][0]
            returns = info["episode"]["r"][0]
            count_episodes += 1
            list_rewards.append(returns)
            print(f"-> Episode {count_episodes}: {returns} returns")

    env.close()

    return np.mean(list_rewards)


if __name__ == "__main__":
    args_ = parse_args()

    # Create run directory
    run_time = str(datetime.now().strftime("%d-%m_%H:%M:%S"))
    run_name = "PPO_PyTorch"
    env_name = args_.env_id.split("/")[1]
    run_dir = f"runs/{env_name}__{run_name}__{run_time}"

    print(f"Commencing training of {run_name} on {args_.env_id} for {args_.total_timesteps} timesteps.")
    print(f"Results will be saved to: {run_dir}")
    mean_train_return = train(args=args_, run_name=run_name, run_dir=run_dir)
    print(f"Training - Mean returns achieved: {mean_train_return}.")

    if args_.capture_video:
        print(f"Evaluating and capturing videos of {run_name} on {args_.env_id}.")
        mean_eval_return = eval_and_render(args=args_, run_dir=run_dir)
        print(f"Evaluation - Mean returns achieved: {mean_eval_return}.")
