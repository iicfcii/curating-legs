import argparse
import os
import shutil
import random
import time
import gymnasium as gym
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from rl.env import Env
from rl.agent import Agent
from rl.chunked_vec_env import ChunkedVectorEnv
import PIL
from scipy.cluster.vq import kmeans2


class HandleSetAttr(gym.Wrapper, gym.utils.RecordConstructorArgs):
    def __init__(self, env: gym.Env):
        gym.utils.RecordConstructorArgs.__init__(self)
        gym.Wrapper.__init__(self, env)

        self.rms = None
        self.curriculum_designs = None
        self.curriculum_designs_labels = None
        self.curriculum_cells = None

    def reset(self, **kwargs):
        # Set only once
        if self.rms is not None:
            self.get_wrapper_attr('return_rms').mean = self.rms['mean']
            self.get_wrapper_attr('return_rms').var = self.rms['var']
            self.get_wrapper_attr('return_rms').count = self.rms['count']
            self.rms = None
        if self.curriculum_designs is not None:
            self.env.unwrapped.curriculum_designs = self.curriculum_designs['value']
            self.curriculum_designs = None
        if self.curriculum_designs_labels is not None:
            self.env.unwrapped.curriculum_designs_labels = self.curriculum_designs_labels[
                'value'
            ]
            self.curriculum_designs_labels = None
        # Set every time
        self.env.unwrapped.curriculum_cells = self.curriculum_cells['value']
        return self.env.reset(**kwargs)


class Trainer():
    def __init__(
        self,
        name=None,
        track=False,
        checkpoint=None,
    ):
        if checkpoint is not None and name is None:
            self.name = checkpoint['name']
        elif name is None:
            self.name = f'{int(time.time())}'
        else:
            self.name = f'{int(time.time())}_{name}'
        self.folder_name = os.path.join('logs', 'rl', self.name)
        if not os.path.exists(self.folder_name):
            os.makedirs(self.folder_name)
        self.track = track
        self.checkpoint = checkpoint

        self.cuda = True
        self.torch_deterministic = True
        self.keypoint_frequency = 1000  # n updates per keypoint
        self.log_frequency = 50
        self.seed = 0
        self.num_updates = 100000
        if self.checkpoint is not None:
            self.num_updates = (
                self.num_updates - self.checkpoint['update_step']
            )
        self.learning_rate = 1e-3
        if self.checkpoint is not None:
            self.learning_rate = self.checkpoint['learning_rate']
        self.num_async_vec_envs = 64
        self.num_sync_vec_envs = 16
        self.num_steps = 50
        self.gamma = 0.99
        self.gae_lambda = 0.95
        self.num_minibatches = 5
        self.update_epochs = 5
        self.norm_adv = True
        self.clip_coef = 0.2
        self.clip_vloss = True
        self.ent_coef = 0.01
        self.vf_coef = 0.5
        self.max_grad_norm = 0.5
        self.target_kl = 0.01

        self.num_envs = int(self.num_async_vec_envs * self.num_sync_vec_envs)
        self.batch_size = int(self.num_envs * self.num_steps)
        self.minibatch_size = int(self.batch_size // self.num_minibatches)

        if self.track:
            import wandb
            if self.checkpoint is not None and name is None:
                wandb.init(
                    project='laminate-quadruped',
                    id=self.checkpoint['track_id'],
                    resume='must'
                )
            else:
                wandb.init(
                    project='laminate-quadruped',
                    name=self.name,
                    save_code=True,
                )
            wandb.define_metric('update/step')
            wandb.define_metric("update/*", step_metric="update/step")
            wandb.define_metric("episode/*", step_metric="update/step")
            wandb.run.log_code(os.getcwd())

        # Seeding
        random.seed(self.seed)
        np.random.seed(self.seed)
        torch.manual_seed(self.seed)
        # torch.backends.cudnn.deterministic = torch_deterministic
        eval(
            'setattr(torch.backends.cudnn, "deterministic", '
            f'{self.torch_deterministic})'
        )

        self.envs = ChunkedVectorEnv(
            self._make_env,
            self.num_async_vec_envs,
            self.num_sync_vec_envs
        )
        if self.checkpoint is not None:
            self.envs.set_attr(
                'rms',
                [
                    [
                        {
                            'mean': single_rms[0],
                            'var': single_rms[1],
                            'count': single_rms[2]
                        }
                        for single_rms in sync_rms
                    ]
                    for sync_rms in self.checkpoint['normalized_reward_rms']
                ]
            )

        self.device = torch.device(
            'cuda' if torch.cuda.is_available() and self.cuda else 'cpu'
        )
        self.agent = Agent(
            self.envs.single_observation_space.shape[-1],
            self.envs.single_action_space.shape[-1]
        ).to(self.device)
        if self.checkpoint is not None:
            self.agent.load_state_dict(self.checkpoint['agent'])
            self.agent.train()

        self.optimizer = optim.Adam(
            self.agent.parameters(), lr=self.learning_rate, eps=1e-5
        )
        if self.checkpoint is not None:
            self.optimizer.load_state_dict(self.checkpoint['optimizer'])

        _env = Env()
        self.curriculum_num_designs = 10
        self.curriculum_score_alpha = 0.2
        self.curriculum_score_th = _env.curriculum_score_th
        if self.checkpoint is not None:
            self.curriculum_designs = self.checkpoint['curriculum_designs']
            self.curriculum_designs_labels = self.checkpoint['curriculum_designs_labels']
            self.curriculum_cells = self.checkpoint['curriculum_cells']
            self.curriculum_scores = self.checkpoint['curriculum_scores']
            self.curriculum_counts = self.checkpoint['curriculum_counts']
        else:
            leg_params = (
                (_env.leg_params - _env.leg_param_min) /
                (_env.leg_param_max - _env.leg_param_min)
            )
            self.curriculum_designs, self.curriculum_designs_labels = kmeans2(
                leg_params, self.curriculum_num_designs,
                iter=10, minit='points', seed=self.seed
            )
            self.curriculum_cells = [
                [[0, 0]] for i in range(self.curriculum_num_designs)
            ]
            self.curriculum_scores = [
                [0] for i in range(self.curriculum_num_designs)
            ]
            self.curriculum_counts = [
                [0] for i in range(self.curriculum_num_designs)
            ]

        if self.checkpoint is not None:
            self.update_step = self.checkpoint['update_step']
            self.global_step = self.checkpoint['global_step']
        else:
            self.global_step = 0
            self.update_step = 0
        self.global_step_init = self.global_step
        self.start_time = time.time()

    def train(self):
        # Storage setup
        update_shape = (self.num_steps, self.num_envs)
        obs = torch.zeros(
            update_shape + self.envs.single_observation_space.shape[-1:]
        ).to(self.device)
        actions = torch.zeros(
            update_shape + self.envs.single_action_space.shape[-1:]
        ).to(self.device)
        logprobs = torch.zeros(update_shape).to(self.device)
        rewards = torch.zeros(update_shape).to(self.device)
        dones = torch.zeros(update_shape).to(self.device)
        values = torch.zeros(update_shape).to(self.device)

        self.envs.set_attr(
            'curriculum_designs',
            {'value': self.curriculum_designs}
        )
        self.envs.set_attr(
            'curriculum_designs_labels',
            {'value': self.curriculum_designs_labels}
        )
        self.envs.set_attr(
            'curriculum_cells',
            {'value': self.curriculum_cells}
        )
        next_obs, info = self.envs.reset(seed=self.seed)
        next_obs = torch.Tensor(next_obs).to(self.device)
        next_done = torch.zeros(self.num_envs).to(self.device)

        episode_rewards = {}

        for _ in range(self.num_updates):
            for step in range(0, self.num_steps):
                obs[step] = next_obs
                dones[step] = next_done

                # Action logic
                with torch.no_grad():
                    action, logprob, _ = self.agent.get_action(next_obs)
                    values[step] = self.agent.get_value(next_obs).squeeze()
                actions[step] = action
                logprobs[step] = logprob

                # Execute the game and log data.
                next_obs, reward, terminated, truncated, info = self.envs.step(
                    action.cpu().numpy()
                )
                done = np.logical_or(terminated, truncated)
                rewards[step] = torch.tensor(reward).to(self.device)
                next_obs = torch.Tensor(next_obs).to(self.device)
                next_done = torch.Tensor(done).to(self.device)

                # Handle timeout by bootstrapping with value function
                # https://github.com/DLR-RM/stable-baselines3/issues/633
                for i, single_truncated in enumerate(truncated):
                    if not single_truncated:
                        continue

                    terminated_obs = torch.Tensor(
                        info['final_observation'][i]
                    ).to(self.device)
                    with torch.no_grad():
                        terminated_value = self.agent.get_value(
                            terminated_obs
                        )[0]
                    rewards[step, i] += self.gamma * terminated_value

                self.global_step += 1 * self.num_envs

                # Log episode
                if not info or 'final_info' not in info:
                    continue

                for final_info in info['final_info']:
                    if final_info is None:
                        continue

                    # Record reward values
                    assert 'reward' in final_info
                    for k, v in final_info['reward'].items():
                        if k not in episode_rewards:
                            episode_rewards[k] = [v]
                        else:
                            episode_rewards[k].append(v)

                    # Update curriculum
                    assert 'curriculum' in final_info
                    design = final_info['curriculum']['design']

                    cell = final_info['curriculum']['cell']
                    cells = self.curriculum_cells[design]
                    cell_index = cells.index(cell)

                    scores = self.curriculum_scores[design]
                    prev_score = scores[cell_index]
                    score = final_info['curriculum']['score']
                    score = (
                        (1 - self.curriculum_score_alpha) * prev_score +
                        self.curriculum_score_alpha * score
                    )
                    scores[cell_index] = score

                    counts = self.curriculum_counts[design]
                    counts[cell_index] += 1

                    if score > self.curriculum_score_th:
                        for dx, dy in [(0, 1), (1, 0,), (0, -1), (-1, 0)]:
                            neighbour_cells = [  # with symmetry
                                [cell[0] + dx, cell[1] + dy],
                                [-(cell[0] + dx), cell[1] + dy],
                                [cell[0] + dx, -(cell[1] + dy)],
                                [-(cell[0] + dx), -(cell[1] + dy)],
                            ]
                            for neighbour_cell in neighbour_cells:
                                if neighbour_cell not in cells:
                                    cells.append(neighbour_cell)
                                    scores.append(0)
                                    counts.append(0)
                        # TODO: Try more immediate curriculum update

            # Bootstrap value if not done
            with torch.no_grad():
                next_value = self.agent.get_value(next_obs).squeeze()
                advantages = torch.zeros_like(rewards).to(self.device)
                lastgaelam = 0
                for t in reversed(range(self.num_steps)):
                    if t == self.num_steps - 1:
                        nextnonterminal = 1.0 - next_done
                        nextvalues = next_value
                    else:
                        nextnonterminal = 1.0 - dones[t + 1]
                        nextvalues = values[t + 1]
                    delta = (
                        rewards[t] +
                        self.gamma * nextvalues * nextnonterminal -
                        values[t]
                    )
                    advantages[t] = lastgaelam = (
                        delta + self.gamma * self.gae_lambda *
                        nextnonterminal * lastgaelam
                    )
                returns = advantages + values

            # Flatten the batch
            b_obs = obs.reshape(
                (-1,) + self.envs.single_observation_space.shape[-1:]
            )
            b_actions = actions.reshape(
                (-1,) + self.envs.single_action_space.shape[-1:]
            )
            b_logprobs = logprobs.reshape(-1)
            b_advantages = advantages.reshape(-1)
            b_returns = returns.reshape(-1)
            b_values = values.reshape(-1)

            optimize_infos = self._optimize(
                b_obs,
                b_actions,
                b_logprobs,
                b_advantages,
                b_returns,
                b_values
            )

            self.envs.set_attr(
                'curriculum_cells',
                {'value': self.curriculum_cells}
            )

            self.update_step += 1

            self._save()

            if self.update_step % self.log_frequency == 0:
                sps = int(
                    (self.global_step - self.global_step_init) /
                    (time.time() - self.start_time)
                )
                print(
                    f'global_step: {self.global_step}, '
                    f'update_step: {self.update_step}, '
                    f'learning_rate: {self.learning_rate:.5f}, '
                    f'SPS: {sps}'
                )

                episode_rewards_means = {}
                for k, v in episode_rewards.items():
                    episode_rewards_means[k] = np.mean(v)
                episode_rewards = {}
                print(', '.join([
                    f'{k}: {v:.4f}'
                    for k, v in episode_rewards_means.items()
                ]))

                area = np.sum([len(cells) for cells in self.curriculum_cells])
                _counts = np.concatenate(self.curriculum_counts)
                count_per_cell = np.sum(_counts) / area
                count_max = np.amax(_counts)
                count_min = np.amin(_counts)
                print(
                    f'curriculum_area: {area:.0f}, '
                    f'count_per_cell: {count_per_cell:.1f}, '
                    f'count_max: {count_max:.0f}, '
                    f'count_min: {count_min:.0f}'
                )

                if self.track:
                    wandb.log({
                        'update/step': self.update_step,
                        'update/global_step': self.global_step,
                        'update/SPS': sps
                    })
                    for k, v in episode_rewards_means.items():
                        wandb.log({f'episode/reward_{k}': v})
                    wandb.log({
                        'episode/curriculum_area': area,
                        'episode/curriculum_count_per_cell': count_per_cell,
                        'episode/curriculum_count_max': count_max,
                        'episode/curriculum_count_min': count_min
                    })
                    wandb.log(optimize_infos)

        self.envs.close()

    def _make_env(self):
        env = Env()
        # Normalizing reward is important!
        env = gym.wrappers.NormalizeReward(env, gamma=self.gamma)
        env = gym.wrappers.TransformReward(
            env, lambda reward: np.clip(reward, -10, 10)
        )
        env = HandleSetAttr(env)
        return env

    def _optimize(
        self,
        b_obs,
        b_actions,
        b_logprobs,
        b_advantages,
        b_returns,
        b_values
    ):
        # Optimizing the policy and value network
        b_inds = np.arange(self.batch_size)
        clipfracs = []
        for epoch in range(self.update_epochs):
            np.random.shuffle(b_inds)
            for start in range(0, self.batch_size, self.minibatch_size):
                end = start + self.minibatch_size
                mb_inds = b_inds[start:end]

                _, newlogprob, entropy = self.agent.get_action(
                    b_obs[mb_inds], action=b_actions[mb_inds]
                )
                newvalue = self.agent.get_value(b_obs[mb_inds])
                logratio = newlogprob - b_logprobs[mb_inds]
                ratio = logratio.exp()

                with torch.no_grad():
                    # calculate approx_kl http://joschu.net/blog/kl-approx.html
                    old_approx_kl = (-logratio).mean()
                    approx_kl = ((ratio - 1) - logratio).mean()
                    clipfracs += [(
                        (ratio - 1.0).abs() > self.clip_coef
                    ).float().mean().item()]

                mb_advantages = b_advantages[mb_inds]
                if self.norm_adv:
                    mb_advantages = (
                        (mb_advantages - mb_advantages.mean()) /
                        (mb_advantages.std() + 1e-8)
                    )

                # Policy loss
                pg_loss1 = -mb_advantages * ratio
                pg_loss2 = -mb_advantages * torch.clamp(
                    ratio, 1 - self.clip_coef, 1 + self.clip_coef
                )
                pg_loss = torch.max(pg_loss1, pg_loss2).mean()

                # Value loss
                newvalue = newvalue.view(-1)
                if self.clip_vloss:
                    v_loss_unclipped = (newvalue - b_returns[mb_inds]) ** 2
                    v_clipped = b_values[mb_inds] + torch.clamp(
                        newvalue - b_values[mb_inds],
                        -self.clip_coef,
                        self.clip_coef,
                    )
                    v_loss_clipped = (v_clipped - b_returns[mb_inds]) ** 2
                    v_loss_max = torch.max(v_loss_unclipped, v_loss_clipped)
                    v_loss = 0.5 * v_loss_max.mean()
                else:
                    v_loss = 0.5 * (
                        (newvalue - b_returns[mb_inds]) ** 2
                    ).mean()

                entropy_loss = entropy.mean()
                loss = (
                    pg_loss -
                    self.ent_coef * entropy_loss +
                    self.vf_coef * v_loss
                )

                self.optimizer.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(
                    self.agent.parameters(), self.max_grad_norm
                )
                self.optimizer.step()

                if self.target_kl is not None:
                    # Adaptive learning rate
                    if approx_kl > 2 * self.target_kl:
                        self.learning_rate = np.maximum(
                            1e-5, self.learning_rate / 1.5
                        )
                    elif approx_kl < 0.5 * self.target_kl:
                        self.learning_rate = np.minimum(
                            1e-2, self.learning_rate * 1.5
                        )
                    else:
                        pass
                    self.optimizer.param_groups[0]['lr'] = self.learning_rate

        y_pred = b_values.cpu().numpy()
        y_true = b_returns.cpu().numpy()
        explained_var = (
            1 - np.var(y_true - y_pred) / (np.var(y_true) + 1e-8)
        )

        return {
            'update/value_loss': v_loss.item(),
            'update/policy_loss': pg_loss.item(),
            'update/entropy': entropy_loss.item(),
            'update/old_approx_kl': old_approx_kl.item(),
            'update/approx_kl': approx_kl.item(),
            'update/clipfrac': np.mean(clipfracs),
            'update/learning_rate': self.learning_rate,
            'update/explained_variance': explained_var,
        }

    def _save(self):
        if self.update_step % self.keypoint_frequency != 0:
            return

        checkpoint = {
            'name': self.name,
            'track_id': wandb.run.id if self.track else None,
            'global_step': self.global_step,
            'update_step': self.update_step,
            'learning_rate': self.learning_rate,
            'optimizer': self.optimizer.state_dict(),
            'agent': self.agent.state_dict(),
            'curriculum_designs': self.curriculum_designs,
            'curriculum_designs_labels': self.curriculum_designs_labels,
            'curriculum_cells': self.curriculum_cells,
            'curriculum_scores': self.curriculum_scores,
            'curriculum_counts': self.curriculum_counts,
            'normalized_reward_rms': [
                [[rms.mean, rms.var, rms.count] for rms in sync_rms]
                for sync_rms in self.envs.call('call', 'get_wrapper_attr', 'return_rms')
            ]
        }

        # Specific checkpoint files
        checkpoint_name = f'checkpoint_{self.update_step}.pt'
        checkpoint_path = os.path.join(self.folder_name, checkpoint_name)
        torch.save(checkpoint, checkpoint_path)
        if self.track:
            shutil.copyfile(
                checkpoint_path,
                os.path.join(wandb.run.dir, checkpoint_name)
            )
            wandb.save(checkpoint_name, policy='now')

        # Curriculum counts visualization
        h_nrow = 7
        h_ncol = 7
        nrow = h_nrow * 2 + 1
        ncol = h_ncol * 2 + 1
        max_count = np.amax(np.concatenate(self.curriculum_counts))
        score_imgs = []
        counts_imgs = []
        for cells, scores, counts in zip(
            self.curriculum_cells, self.curriculum_scores, self.curriculum_counts
        ):
            score_img = np.zeros((nrow, ncol))
            count_img = np.zeros((nrow, ncol))
            for cell, score, count in zip(cells, scores, counts):
                score_img[cell[0] + h_nrow, cell[1] + h_ncol] = np.clip(
                    score / self.curriculum_score_th, 0, 1
                ) * 255
                count_img[cell[0] + h_nrow, cell[1] + h_ncol] = (
                    count / max_count * 255
                )
            score_imgs.append(score_img)
            counts_imgs.append(count_img)
        img_path = os.path.join(
            self.folder_name, f'curriculum_{self.update_step}.png'
        )
        PIL.Image.fromarray(
            np.concatenate([
                np.concatenate(score_imgs, axis=1),
                np.concatenate(counts_imgs, axis=1)
            ], axis=0)
        ).convert('L').save(img_path)
        if self.track:
            wandb.log({f'curriculum': wandb.Image(img_path)})

    def close(self):
        self.envs.close(terminate=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', type=str, default=None)
    parser.add_argument(
        '--track',
        default=False,
        action=argparse.BooleanOptionalAction
    )
    parser.add_argument('--checkpoint', type=str, default=None)
    args = vars(parser.parse_args())

    if args['checkpoint'] is not None:
        args['checkpoint'] = torch.load(args['checkpoint'])
    trainer = Trainer(**args)
    trainer.train()
