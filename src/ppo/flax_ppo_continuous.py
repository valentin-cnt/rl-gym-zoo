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
    parser.add_argument("--num_steps", type=int, default=2048)
    parser.add_argument("--num_minibatches", type=int, default=32)
    parser.add_argument("--num_optims", type=int, default=10)
    parser.add_argument("--learning_rate", type=float, default=3e-4)
    parser.add_argument("--list_layer", nargs="+", type=int, default=[256, 256])
    parser.add_argument("--gamma", type=float, default=0.99)
    parser.add_argument("--gae", type=float, default=0.95)
    parser.add_argument("--eps_clip", type=float, default=0.2)
    parser.add_argument("--value_coef", type=float, default=0.5)
    parser.add_argument("--entropy_coef", type=float, default=0.01)
    parser.add_argument("--capture_video", action="store_true")
    parser.add_argument("--wandb", action="store_true")
    parser.add_argument("--seed", type=int, default=0)

    args = parser.parse_args()

    args.batch_size = int(args.num_envs * args.num_steps)
    args.minibatch_size = int(args.batch_size // args.num_minibatches)
    args.num_updates = int(args.total_timesteps // args.batch_size)

    return args


def make_env(env_id, capture_video=False, run_dir=""):
    def thunk():
        if capture_video:
            env = gym.make(env_id, render_mode="rgb_array")
            env = gym.wrappers.RecordVideo(
                env=env,
                video_folder=f"{run_dir}/videos",
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


class ActorCriticNet(nn.Module):
    num_actions: int
    list_layer: list

    @nn.compact
    def __call__(self, x):
        for layer in self.list_layer:
            x = nn.Dense(features=layer)(x)
            x = nn.tanh(x)

        action_mean = nn.Dense(features=self.num_actions)(x)
        action_std = nn.Dense(features=self.num_actions)(x)
        action_std = nn.sigmoid(action_std) + 1e-7

        values = nn.Dense(features=1)(x).squeeze()

        return action_mean, action_std, values


@functools.partial(jax.jit, static_argnums=(0,))
def policy_output(apply_fn, params, state):
    return apply_fn(params, state)


@jax.jit
def sample_action(mean, std, key):
    key, subkey = jax.random.split(key)
    return jax.random.normal(subkey, shape=mean.shape) * std + mean, key


@jax.jit
def get_log_probs(mean, std, value):
    var = std**2
    log_scale = jnp.log(std)
    return -((value - mean) ** 2) / (2 * var) - log_scale - jnp.log(jnp.sqrt(2 * jnp.pi))


@jax.jit
def get_entropy(std):
    return 0.5 + 0.5 * jnp.log(2 * jnp.pi) + jnp.log(std)


@functools.partial(jax.jit, static_argnums=(4, 5))
def compute_gae(rewards, state_values, flags, next_state_value, gamma, gae):
    advantages = jnp.zeros_like(rewards)
    adv = jnp.zeros(rewards.shape[1])

    for i in reversed(range(rewards.shape[0])):
        terminal = 1.0 - flags[i]

        returns = rewards[i] + gamma * next_state_value * terminal
        delta = returns - state_values[i]

        adv = gamma * gae * adv * terminal + delta
        advantages = advantages.at[i].set(adv)

        next_state_value = state_values[i]

    return advantages


def loss_fn(params, apply_fn, batch, value_coef, entropy_coef, eps_clip):
    states, actions, old_log_probs, advantages, td_target = batch

    action_mean, action_std, td_predict = policy_output(apply_fn, params, states)

    log_probs = get_log_probs(action_mean, action_std, actions).sum(-1)

    ratios = jnp.exp(log_probs - old_log_probs)

    surr1 = advantages * ratios
    surr2 = advantages * jax.lax.clamp(1.0 - eps_clip, ratios, 1.0 + eps_clip)

    actor_loss = -jnp.minimum(surr1, surr2).mean()
    critic_loss = jnp.square(td_target - td_predict).mean()
    entropy_loss = get_entropy(action_std).sum(-1).mean()

    return actor_loss + critic_loss * value_coef - entropy_loss * entropy_coef


@functools.partial(jax.jit, static_argnums=(2, 3, 4, 5, 6))
def train_step(train_state, trajectories, num_minibatches, minibatch_size, value_coef, entropy_coef, eps_clip):
    trajectories = jax.tree_util.tree_map(
        lambda x: x.reshape((num_minibatches, minibatch_size) + x.shape[1:]), trajectories
    )

    for batch in zip(*trajectories):
        grad_fn = jax.value_and_grad(loss_fn)
        loss, grads = grad_fn(train_state.params, train_state.apply_fn, batch, value_coef, entropy_coef, eps_clip)
        train_state = train_state.apply_gradients(grads=grads)

    return train_state, loss


def train(args, run_name, run_dir):
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
    if args.seed:
        np.random.seed(args.seed)

    # Create vectorized environment(s)
    envs = gym.vector.AsyncVectorEnv([make_env(args.env_id) for _ in range(args.num_envs)])

    # Metadata about the environment
    obversation_shape = envs.single_observation_space.shape
    action_shape = envs.single_action_space.shape

    # Initialize environment
    state, _ = envs.reset(seed=args.seed) if args.seed else envs.reset()

    # Create policy network and optimizer
    policy = ActorCriticNet(num_actions=np.prod(action_shape), list_layer=args.list_layer)

    optimizer = optax.adam(learning_rate=args.learning_rate)

    key, subkey = jax.random.split(jax.random.PRNGKey(args.seed))

    initial_params = policy.init(subkey, state)

    train_state = TrainState.create(params=initial_params, apply_fn=policy.apply, tx=optimizer)

    del initial_params

    # Create buffers
    states = np.zeros((args.num_steps, args.num_envs) + obversation_shape, dtype=np.float32)
    actions = np.zeros((args.num_steps, args.num_envs) + action_shape, dtype=np.float32)
    rewards = np.zeros((args.num_steps, args.num_envs), dtype=np.float32)
    flags = np.zeros((args.num_steps, args.num_envs), dtype=np.float32)
    list_log_probs = np.zeros((args.num_steps, args.num_envs), dtype=np.float32)
    list_state_values = np.zeros((args.num_steps, args.num_envs), dtype=np.float32)

    log_episodic_returns = []

    global_step = 0
    start_time = time.process_time()

    # Main loop
    for _ in tqdm(range(args.num_updates)):
        for i in range(args.num_steps):
            # Update global step
            global_step += 1 * args.num_envs

            # Get action
            action_mean, action_std, state_values = policy_output(train_state.apply_fn, train_state.params, state)
            action, key = sample_action(action_mean, action_std, key)
            log_probs = get_log_probs(action_mean, action_std, action).sum(-1)

            action = np.array(action)

            # Perform action
            next_state, reward, terminated, truncated, infos = envs.step(action)

            # Store transition
            states[i] = state
            actions[i] = action
            rewards[i] = reward
            flags[i] = np.logical_or(terminated, truncated)
            list_log_probs[i] = log_probs
            list_state_values[i] = state_values

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

        _, _, next_state_value = policy_output(train_state.apply_fn, train_state.params, state)

        advantages = compute_gae(rewards, list_state_values, flags, next_state_value, args.gamma, args.gae)

        td_target = advantages + list_state_values
        advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-7)

        # Create batch
        batch = (
            states.reshape(-1, *obversation_shape),
            actions.reshape(-1, *action_shape),
            list_log_probs.reshape(-1),
            advantages.reshape(-1),
            td_target.reshape(-1),
        )

        # Update policy network
        for _ in range(args.num_optims):
            permutation = np.random.permutation(args.batch_size)
            batch = tuple(x[permutation] for x in batch)

            train_state, loss = train_step(
                train_state,
                batch,
                args.num_minibatches,
                args.minibatch_size,
                args.value_coef,
                args.entropy_coef,
                args.eps_clip,
            )

        # Log training metrics
        writer.add_scalar("train/loss", np.asarray(loss), global_step)
        writer.add_scalar("rollout/SPS", int(global_step / (time.process_time() - start_time)), global_step)

    # Close the environment
    envs.close()
    writer.close()
    if args.wandb:
        wandb.finish()

    # Average of episodic returns (for the last 5% of the training)
    indexes = int(len(log_episodic_returns) * 0.05)
    mean_train_return = np.mean(log_episodic_returns[-indexes:])
    writer.add_scalar("rollout/mean_train_return", mean_train_return, global_step)

    return mean_train_return


def eval_and_render(args, run_dir):
    # Create environment
    env = gym.vector.SyncVectorEnv([make_env(args.env_id, capture_video=True, run_dir=run_dir)])

    # Metadata about the environment
    # obversation_shape = env.single_observation_space.shape
    # action_shape = env.single_action_space.n

    # Load policy
    # policy = ActorCriticNet(obversation_shape, action_shape, args.list_layer)
    # policy.load_state_dict(torch.load(f"{run_dir}/policy.pt"))
    # policy.eval()

    # count_episodes = 0
    list_rewards = []

    # state, _ = env.reset(seed=args.seed) if args.seed else env.reset()

    # # Run episodes
    # while count_episodes < 30:
    #     log_probs, _ = policy_output(train_state.apply_fn, train_state.params, state)
    #     probs = np.exp(log_probs)
    #     action = np.array([np.random.choice(action_shape, p=probs[i]) for i in range(args.num_envs)])

    #     action = action.cpu().numpy()
    #     state, _, _, _, infos = env.step(action)

    #     if "final_info" in infos:
    #         info = infos["final_info"][0]
    #         returns = info["episode"]["r"][0]
    #         count_episodes += 1
    #         list_rewards.append(returns)
    #         print(f"-> Episode {count_episodes}: {returns} returns")

    env.close()

    return np.mean(list_rewards)


if __name__ == "__main__":
    args = parse_args()

    # Create run directory
    run_time = str(datetime.now().strftime("%d-%m_%H:%M:%S"))
    run_name = "PPO_Flax"
    run_dir = f"runs/{args.env_id}__{run_name}__{run_time}"

    print(f"Commencing training of {run_name} on {args.env_id} for {args.total_timesteps} timesteps.")
    print(f"Results will be saved to: {run_dir}")
    mean_train_return = train(args=args, run_name=run_name, run_dir=run_dir)
    print(f"Training - Mean returns achieved: {mean_train_return}.")

    if args.capture_video:
        print(f"Evaluating and capturing videos of {run_name} on {args.env_id}.")
        mean_eval_return = eval_and_render(args=args, run_dir=run_dir)
        print(f"Evaluation - Mean returns achieved: {mean_eval_return}.")
