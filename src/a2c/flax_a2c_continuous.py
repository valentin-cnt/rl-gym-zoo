import argparse
import functools
import time
from datetime import datetime

import gymnasium as gym
import jax
import numpy as np
import optax
from flax import linen as nn
from flax.training.train_state import TrainState
from jax import numpy as jnp
from torch.utils.tensorboard.writer import SummaryWriter
from tqdm import tqdm


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env_id", type=str, default="HalfCheetah-v4")
    parser.add_argument("--total_timesteps", type=int, default=1_000_000)
    parser.add_argument("--num_envs", type=int, default=1)
    parser.add_argument("--num_steps", type=int, default=256)
    parser.add_argument("--learning_rate", type=float, default=1e-3)
    parser.add_argument("--list_layer", nargs="+", type=int, default=[64, 64])
    parser.add_argument("--gamma", type=float, default=0.99)
    parser.add_argument("--value_coef", type=float, default=0.5)
    parser.add_argument("--entropy_coef", type=float, default=0.01)
    parser.add_argument("--capture_video", action="store_true")
    parser.add_argument("--wandb", action="store_true")
    parser.add_argument("--seed", type=int, default=0)

    args = parser.parse_args()

    args.batch_size = int(args.num_envs * args.num_steps)
    args.num_updates = int(args.total_timesteps // args.batch_size)

    return args


def make_env(env_id, capture_video=False):
    def thunk():
        if capture_video:
            env = gym.make(env_id, render_mode="rgb_array")
            env = gym.wrappers.RecordVideo(
                env=env,
                video_folder="/videos/",
                episode_trigger=lambda x: x,
                disable_logger=True,
            )
        else:
            env = gym.make(env_id)
        env = gym.wrappers.RecordEpisodeStatistics(env)
        env = gym.wrappers.ClipAction(env)
        env = gym.wrappers.FlattenObservation(env)
        env = gym.wrappers.NormalizeObservation(env)
        env = gym.wrappers.TransformObservation(env, lambda obs: np.clip(obs, -10, 10))
        env = gym.wrappers.NormalizeReward(env)
        env = gym.wrappers.TransformReward(env, lambda reward: np.clip(reward, -10, 10))

        return env

    return thunk


class Normal:
    """Normal distribution."""

    def __init__(self, loc, scale):
        self.loc = loc
        self.scale = scale

    def sample(self):
        # Return a sample from the distribution in numpy
        return np.random.normal(self.loc, self.scale)

    def log_prob(self, x):
        log_unnormalized = -0.5 * jnp.square(x / self.scale - self.loc / self.scale)
        log_normalization = 0.5 * jnp.log(2.0 * jnp.pi) + jnp.log(self.scale)
        return log_unnormalized - log_normalization

    def entropy(self):
        log_normalization = 0.5 * jnp.log(2.0 * jnp.pi) + jnp.log(self.scale)
        entropy = 0.5 + log_normalization
        return entropy * jnp.ones_like(self.loc)


class ActorCriticNet(nn.Module):
    num_actions: int
    list_layer: list

    @nn.compact
    def __call__(self, x):
        for layer in self.list_layer:
            x = nn.Dense(features=layer)(x)
            x = nn.tanh(x)

        action_mean = nn.Dense(self.num_actions)(x)
        action_std = nn.Dense(features=self.num_actions)(x)
        action_std = nn.sigmoid(action_std) + 1e-7

        # action_std = self.param("action_std", nn.initializers.zeros, (self.num_actions,))
        # action_std = jnp.exp(action_std)

        values = nn.Dense(features=1)(x).squeeze()

        return action_mean, action_std, values


@functools.partial(jax.jit, static_argnums=(0,))
def get_policy(apply_fn, params, state):
    return apply_fn(params, state)


@jax.jit
@functools.partial(jax.vmap, in_axes=(1, 1, None), out_axes=1)
def compute_td_target(rewards, flags, gamma):
    td_target = []
    gain = 0.0
    for i in reversed(range(len(rewards))):
        terminal = 1.0 - flags[i]
        gain = rewards[i] + gain * gamma * terminal
        td_target.append(gain)

    td_target = td_target[::-1]
    return jnp.array(td_target)


def loss_fn(params, apply_fn, batch, value_coef, entropy_coef):
    states, actions, td_target = batch
    action_mean, action_std, td_predict = get_policy(apply_fn, params, states)

    dist = Normal(action_mean, action_std)
    log_probs_by_actions = dist.log_prob(actions).sum(axis=-1)

    advantages = td_target - td_predict

    actor_loss = (-log_probs_by_actions * advantages).mean()
    critic_loss = jnp.square(advantages).mean()
    entropy_loss = dist.entropy().mean()

    return actor_loss + critic_loss * value_coef - entropy_loss * entropy_coef


@functools.partial(jax.jit, static_argnums=(2, 3))
def train_step(train_state, batch, value_coef, entropy_coef):
    grad_fn = jax.value_and_grad(loss_fn)
    loss, grads = grad_fn(
        train_state.params,
        train_state.apply_fn,
        batch,
        value_coef,
        entropy_coef,
    )
    train_state = train_state.apply_gradients(grads=grads)
    return train_state, loss


def main():
    args = parse_args()

    # Create run directory
    run_time = str(datetime.now().strftime("%d-%m_%H:%M:%S"))
    run_name = "A2C_Flax"
    run_dir = f"runs/{args.env_id}__{run_name}__{run_time}"

    print(f"Training {run_name} on {args.env_id} for {args.total_timesteps} timesteps")
    print(f"Saving results to {run_dir}")

    # Initialize wandb if needed (https://wandb.ai/)
    if args.wandb:
        import wandb

        wandb.init(project=args.env_id, name=run_name, sync_tensorboard=True, config=vars(args))

    # Create tensorboard writer and save hyperparameters
    writer = SummaryWriter(run_dir)
    writer.add_text(
        "hyperparameters",
        "|param|value|\n|-|-|\n%s" % ("\n".join([f"|{key}|{value}|" for key, value in vars(args).items()])),
    )

    # Set seed for reproducibility
    if args.seed > 0:
        np.random.seed(args.seed)

    # Create vectorized environment(s)
    envs = gym.vector.AsyncVectorEnv([make_env(args.env_id) for _ in range(args.num_envs)])

    # Metadata about the environment
    obversation_shape = envs.single_observation_space.shape
    action_shape = envs.single_action_space.shape

    # Initialize environment
    state, _ = envs.reset(seed=args.seed) if args.seed > 0 else envs.reset()

    # Create policy network and optimizer
    policy_net = ActorCriticNet(num_actions=np.prod(action_shape), list_layer=args.list_layer)

    optimizer = optax.adam(learning_rate=args.learning_rate)

    key, subkey = jax.random.split(jax.random.PRNGKey(args.seed), 2)

    initial_params = policy_net.init(subkey, state)

    train_state = TrainState.create(
        params=initial_params,
        apply_fn=policy_net.apply,
        tx=optimizer,
    )

    del initial_params

    # Create buffers
    states = np.zeros((args.num_steps, args.num_envs) + obversation_shape, dtype=np.float32)
    actions = np.zeros((args.num_steps, args.num_envs) + action_shape, dtype=np.float32)
    rewards = np.zeros((args.num_steps, args.num_envs), dtype=np.float32)
    flags = np.zeros((args.num_steps, args.num_envs), dtype=np.float32)

    log_episodic_returns = []

    global_step = 0
    start_time = time.process_time()

    # Main loop
    for _ in tqdm(range(args.num_updates)):
        for i in range(args.num_steps):
            # Update global step
            global_step += 1 * args.num_envs

            # Get action
            action_mean, action_std, _ = get_policy(train_state.apply_fn, train_state.params, state)
            action = Normal(action_mean, action_std).sample()

            # Perform action
            next_state, reward, terminated, truncated, infos = envs.step(np.asarray(action))

            # Store transition
            states[i] = state
            actions[i] = action
            rewards[i] = reward
            flags[i] = np.logical_or(terminated, truncated)

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

        td_target = compute_td_target(rewards, flags, args.gamma)

        # Normalize td_target
        td_target = (td_target - td_target.mean()) / (td_target.std() + 1e-7)

        # Create batch
        batch = (
            states.reshape(-1, *obversation_shape),
            actions.reshape(-1, *action_shape),
            td_target.reshape(-1),
        )

        # Train
        train_state, loss = train_step(
            train_state,
            batch,
            args.value_coef,
            args.entropy_coef,
        )

        # Log training metrics
        writer.add_scalar("train/loss", np.asarray(loss), global_step)
        writer.add_scalar("rollout/SPS", int(global_step / (time.process_time() - start_time)), global_step)

    # Average of episodic returns (for the last 5% of the training)
    indexes = int(len(log_episodic_returns) * 0.05)
    avg_final_rewards = np.mean(log_episodic_returns[-indexes:])
    print(f"Average of the last {indexes} episodic returns: {round(avg_final_rewards, 2)}")
    writer.add_scalar("rollout/avg_final_rewards", avg_final_rewards, global_step)

    # Close the environment
    envs.close()
    writer.close()
    if args.wandb:
        wandb.finish()

    # Capture video of the policy
    if args.capture_video:
        print(f"Capturing videos and saving them to {run_dir}/videos ...")
        # env_test = gym.vector.SyncVectorEnv([make_env(args.env_id, capture_video=True)])
        state, _ = envs.reset()
        count_episodes = 0
        sum_rewards = 0

        while count_episodes < 100:
            log_probs, _ = get_policy(train_state.apply_fn, train_state.params, state)
            probs = np.exp(log_probs)
            action = np.array([np.random.choice(action_shape, p=probs[0])])
            state, reward, terminated, _, _ = envs.step(action)
            sum_rewards += reward

            if terminated:
                count_episodes += 1
                print(f"TEST - Episode {count_episodes+1} finished with reward {sum_rewards}")
                sum_rewards = 0
                state, _ = envs.reset()

        envs.close()
        print("Done!")


if __name__ == "__main__":
    main()