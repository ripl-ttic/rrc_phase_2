"""Gym environment for the Real Robot Challenge Phase 1 (Simulation)."""
import enum

import gym
import numpy as np

import robot_interfaces
import robot_fingers
import trifinger_simulation
import trifinger_simulation.visual_objects
from trifinger_simulation import trifingerpro_limits
# from trifinger_simulation.tasks import move_cube
from trifinger_env.simulation import TriFingerPlatform
from trifinger_env.simulation.tasks import move_cube
from trifinger_env.simulation import visual_objects
from trifinger_env.reward_fns import competition_reward
from trifinger_env.pinocchio_utils import PinocchioUtils
from trifinger_env.simulation.gym_wrapper.envs.cube_env import ActionType


# class ActionType(enum.Enum):
#     """Different action types that can be used to control the robot."""

#     #: Use pure torque commands.  The action is a list of torques (one per
#     #: joint) in this case.
#     TORQUE = enum.auto()
#     #: Use joint position commands.  The action is a list of angular joint
#     #: positions (one per joint) in this case.  Internally a PD controller is
#     #: executed for each action to determine the torques that are applied to
#     #: the robot.
#     POSITION = enum.auto()
#     #: Use both torque and position commands.  In this case the action is a
#     #: dictionary with keys "torque" and "position" which contain the
#     #: corresponding lists of values (see above).  The torques resulting from
#     #: the position controller are added to the torques in the action before
#     #: applying them to the robot.
#     TORQUE_AND_POSITION = enum.auto()


class RealRobotCubeEnv(gym.GoalEnv):
    """Gym environment for moving cubes with simulated TriFingerPro."""

    def __init__(
        self,
        cube_goal_pose: dict,
        goal_difficulty: int,
        action_type: ActionType = ActionType.POSITION,
        frameskip: int = 1,
        sim: bool = False,
        visualization: bool = False,
        reward_fn: callable = competition_reward,
        termination_fn: callable = None,
        initializer: callable = None,
        episode_length: int = move_cube.episode_length,
    ):
        """Initialize.

        Args:
            cube_goal_pose (dict): Goal pose for the cube.  Dictionary with
                keys "position" and "orientation".
            goal_difficulty (int): Difficulty level of the goal (needed for
                reward computation).
            action_type (ActionType): Specify which type of actions to use.
                See :class:`ActionType` for details.
            frameskip (int):  Number of actual control steps to be performed in
                one call of step().
        """
        # Basic initialization
        # ====================

        self._compute_reward = reward_fn
        self._termination_fn = termination_fn if sim else None
        self.initializer = initializer if sim else None
        self.goal = {k: np.array(v) for k, v in cube_goal_pose.items()}
        self.info = {"difficulty": goal_difficulty}

        self.action_type = action_type

        # TODO: The name "frameskip" makes sense for an atari environment but
        # not really for our scenario.  The name is also misleading as
        # "frameskip = 1" suggests that one frame is skipped while it actually
        # means "do one step per step" (i.e. no skip).
        if frameskip < 1:
            raise ValueError("frameskip cannot be less than 1.")
        self.frameskip = frameskip

        # will be initialized in reset()
        self.real_platform = None
        self.platform = None
        self.simulation = sim
        self.visualization = visualization
        self.episode_length = episode_length if sim else move_cube.episode_length

        # Create the action and observation spaces
        # ========================================

        robot_torque_space = gym.spaces.Box(
            low=trifingerpro_limits.robot_torque.low,
            high=trifingerpro_limits.robot_torque.high,
        )
        robot_position_space = gym.spaces.Box(
            low=trifingerpro_limits.robot_position.low,
            high=trifingerpro_limits.robot_position.high,
        )
        robot_velocity_space = gym.spaces.Box(
            low=trifingerpro_limits.robot_velocity.low,
            high=trifingerpro_limits.robot_velocity.high,
        )

        object_state_space = gym.spaces.Dict(
            {
                "position": gym.spaces.Box(
                    low=trifingerpro_limits.object_position.low,
                    high=trifingerpro_limits.object_position.high,
                ),
                "orientation": gym.spaces.Box(
                    low=trifingerpro_limits.object_orientation.low,
                    high=trifingerpro_limits.object_orientation.high,
                ),
            }
        )

        # verify that the given goal pose is contained in the cube state space
        if not object_state_space.contains(self.goal):
            raise ValueError("Invalid goal pose.")

        if self.action_type == ActionType.TORQUE:
            self.action_space = robot_torque_space
            self._initial_action = trifingerpro_limits.robot_torque.default
        elif self.action_type == ActionType.POSITION:
            self.action_space = robot_position_space
            self._initial_action = trifingerpro_limits.robot_position.default
        elif self.action_type == ActionType.TORQUE_AND_POSITION:
            self.action_space = gym.spaces.Dict(
                {
                    "torque": robot_torque_space,
                    "position": robot_position_space,
                }
            )
            self._initial_action = {
                "torque": trifingerpro_limits.robot_torque.default,
                "position": trifingerpro_limits.robot_position.default,
            }
        else:
            raise ValueError("Invalid action_type")

        self.observation_names = [
            "robot_position",
            "robot_velocity",
            "robot_tip_positions",
            "object_position",
            "object_orientation",
            "goal_object_position",
            "goal_object_orientation",
            "tip_force",
        ]

        # self.observation_space = gym.spaces.Dict(
        #     {
        #         "robot": gym.spaces.Dict(
        #             {
        #                 "position": robot_position_space,
        #                 "velocity": robot_velocity_space,
        #                 "torque": robot_torque_space,
        #                 "tip_positions": gym.spaces.Box(
        #                     low=np.array([trifingerpro_limits.object_position.low] * 3),
        #                     high=np.array([trifingerpro_limits.object_position.high] * 3),
        #                 ),
        #                 "tip_force": gym.spaces.Box(low=np.zeros(3),
        #                                             high=np.ones(3))
        #             }
        #         ),
        #         "action": self.action_space,
        #         "desired_goal": object_state_space,
        #         "achieved_goal": object_state_space,
        #     }
        # )

        self.observation_space = gym.spaces.Dict(
            {
                "robot_position": robot_position_space,
                "robot_velocity": robot_velocity_space,
                "robot_tip_positions": gym.spaces.Box(
                    low=np.array([trifingerpro_limits.object_position.low] * 3),
                    high=np.array([trifingerpro_limits.object_position.high] * 3)
                ),
                "object_position": object_state_space["position"],
                "object_orientation": object_state_space["orientation"],
                "goal_object_position": object_state_space["position"],
                "goal_object_orientation": object_state_space["orientation"],
                "tip_force": gym.spaces.Box(
                    low=np.zeros(3),
                    high=np.ones(3),
                )
            }
        )

        self.pinocchio_utils = PinocchioUtils()
        self.prev_observation = None

    def compute_reward(self, achieved_goal, desired_goal, info):
        """Compute the reward for the given achieved and desired goal.

        Args:
            achieved_goal (dict): Current pose of the object.
            desired_goal (dict): Goal pose of the object.
            info (dict): An info dictionary containing a field "difficulty"
                which specifies the difficulty level.

        Returns:
            float: The reward that corresponds to the provided achieved goal
            w.r.t. to the desired goal. Note that the following should always
            hold true::

                ob, reward, done, info = env.step()
                assert reward == env.compute_reward(
                    ob['achieved_goal'],
                    ob['desired_goal'],
                    info,
                )
        """
        return -move_cube.evaluate_state(
            move_cube.Pose.from_dict(desired_goal),
            move_cube.Pose.from_dict(achieved_goal),
            info["difficulty"],
        )

    def step(self, action):
        """Run one timestep of the environment's dynamics.

        When end of episode is reached, you are responsible for calling
        ``reset()`` to reset this environment's state.

        Args:
            action: An action provided by the agent (depends on the selected
                :class:`ActionType`).

        Returns:
            tuple:

            - observation (dict): agent's observation of the current
              environment.
            - reward (float) : amount of reward returned after previous action.
            - done (bool): whether the episode has ended, in which case further
              step() calls will return undefined results.
            - info (dict): info dictionary containing the difficulty level of
              the goal.
        """
        if self.real_platform is None:
            raise RuntimeError("Call `reset()` before starting to step.")

        if self.platform is None:
            raise RuntimeError("platform is not instantiated.")

        if not self.action_space.contains(action):
            raise ValueError(
                "Given action is not contained in the action space."
            )

        num_steps = self.frameskip

        # ensure episode length is not exceeded due to frameskip
        step_count_after = self.step_count + num_steps
        if step_count_after > self.episode_length:
            excess = step_count_after - self.episode_length
            num_steps = max(1, num_steps - excess)

        reward = 0.0
        for _ in range(num_steps):
            # send action to robot
            robot_action = self._gym_action_to_robot_action(action)
            t = self.real_platform.append_desired_action(robot_action)
            # print("real t", t)
            # t_ = self.platform.append_desired_action(robot_action)
            # print("sim t", t)

            observation = self._create_observation(t, action)
            self._set_sim_state(observation)

            if self.prev_observation is None:
                self.prev_observation = observation
            reward += self._compute_reward(
                self.prev_observation,
                observation,
                self.info
            )
            self.prev_observation = observation

            self.step_count = t
            # make sure to not exceed the episode length
            if self.step_count >= self.episode_length - 1:
                break

        is_done = self.step_count == self.episode_length
        if self._termination_fn is not None:
            is_done = is_done or self._termination_fn(observation)

        return observation, reward, is_done, self.info

    def reset(self):
        # By changing the `_reset_*` method below you can switch between using
        # the platform frontend, which is needed for the submission system, and
        # the direct simulation, which may be more convenient if you want to
        # pre-train locally in simulation.
        if self.simulation:
            self._reset_direct_simulation()
        else:
            self._reset_platform_frontend()
            self._reset_simulation()

        self.step_count = 0

        # need to already do one step to get initial observation
        # TODO disable frameskip here?
        self.prev_observation, _, _, _ = self.step(self._initial_action)
        return self.prev_observation

    def _reset_platform_frontend(self):
        """Reset the platform frontend."""
        # reset is not really possible
        if self.real_platform is not None:
            raise RuntimeError(
                "Once started, this environment cannot be reset."
            )

        self.real_platform = robot_fingers.TriFingerPlatformFrontend()

    def _reset_simulation(self):
        del self.platform
        if hasattr(self, 'goal_marker'):
            del self.goal_marker

        # initialize simulation
        if self.initializer is None:
            # if no initializer is given (which will be the case during training),
            # we can initialize in any way desired. here, we initialize the cube always
            # in the center of the arena, instead of randomly, as this appears to help
            # training
            initial_robot_position = TriFingerPlatform.spaces.robot_position.default
            default_object_position = (
                TriFingerPlatform.spaces.object_position.default
            )
            default_object_orientation = (
                TriFingerPlatform.spaces.object_orientation.default
            )
            # initial_object_pose = move_cube.Pose(
            #     position=default_object_position,
            #     orientation=default_object_orientation,
            # )
            goal_object_pose = move_cube.sample_goal(difficulty=1)
        else:
            # if an initializer is given, i.e. during evaluation, we need to initialize
            # according to it, to make sure we remain coherent with the standard CubeEnv.
            # otherwise the trajectories produced during evaluation will be invalid.
            initial_robot_position = TriFingerPlatform.spaces.robot_position.default
            # initial_object_pose=self.initializer.get_initial_state()
            goal_object_pose = self.initializer.get_goal()

        dummy_initial_object_pose = move_cube.Pose(
            position=default_object_position,
            orientation=default_object_orientation,
        )
        self.platform = TriFingerPlatform(
            visualization=self.visualization,
            initial_robot_position=initial_robot_position,
            initial_object_pose=dummy_initial_object_pose,
        )

        self.goal = {
            "position": goal_object_pose.position,
            "orientation": goal_object_pose.orientation,
        }
        # visualize the goal
        is_level_4 = False
        if self.visualization:
            if is_level_4:  # TEMP
                self.goal_marker = visual_objects.CubeMarker(
                    width=0.065,
                    position=goal_object_pose.position,
                    orientation=goal_object_pose.orientation,
                )
                self.ori_goal_marker = VisualCubeOrientation(
                    goal_object_pose.position,
                    goal_object_pose.orientation
                )

            else:
                self.goal_marker = visual_objects.SphereMaker(
                    radius=0.065 / 2,
                    position=goal_object_pose.position,
                )

        self.step_count = 0
        # init_obs = self._create_observation(0)

    def _reset_direct_simulation(self):
        """Reset direct simulation.

        With this the env can be used without backend.
        """

        # reset simulation
        del self.platform

        # initialize simulation
        if self.initializer is None:
            initial_object_pose = move_cube.sample_goal(difficulty=-1)
        else:
            initial_object_pose = self.initializer.get_initial_state()
            self.goal = self.initializer.get_goal()
        self.platform = trifinger_simulation.TriFingerPlatform(
            visualization=self.visualization,
            initial_object_pose=initial_object_pose,
        )
        # visualize the goal
        if self.visualization:
            self.goal_marker = trifinger_simulation.visual_objects.CubeMarker(
                width=0.065,
                position=self.goal["position"],
                orientation=self.goal["orientation"],
                physicsClientId=self.platform.simfinger._pybullet_client_id,
            )

    def seed(self, seed=None):
        """Sets the seed for this env’s random number generator.

        .. note::

           Spaces need to be seeded separately.  E.g. if you want to sample
           actions directly from the action space using
           ``env.action_space.sample()`` you can set a seed there using
           ``env.action_space.seed()``.

        Returns:
            List of seeds used by this environment.  This environment only uses
            a single seed, so the list contains only one element.
        """
        self.np_random, seed = gym.utils.seeding.np_random(seed)
        move_cube.random = self.np_random
        return [seed]

    def _create_observation(self, t, action):
        robot_observation = self.real_platform.get_robot_observation(t)
        camera_observation = self.real_platform.get_camera_observation(t)

        observation = {
            "robot": {
                "position": robot_observation.position,
                "velocity": robot_observation.velocity,
                "torque": robot_observation.torque,
                "tip_positions": np.array(self.pinocchio_utils.forward_kinematics(robot_observation.position)),
                "tip_force": robot_observation.tip_force,
            },
            "action": action,
            "desired_goal": self.goal,
            "achieved_goal": {
                "position": camera_observation.object_pose.position,
                "orientation": camera_observation.object_pose.orientation,
            },
        }
        return self._newobs_to_oldobs(observation)

    def _newobs_to_oldobs(self, obs):
        old_obs = {
            "robot_position": obs['robot']['position'],
            "robot_velocity": obs['robot']['velocity'],
            "robot_tip_positions": obs['robot']['tip_positions'],
            "tip_force": obs['robot']['tip_force'],
            "object_position": obs['achieved_goal']['position'],
            "object_orientation": obs['achieved_goal']['orientation'],
            "goal_object_position": obs['desired_goal']['position'],
            "goal_object_orientation": obs['desired_goal']['orientation'],
            "desired_goal": obs['desired_goal'],
            'achieved_goal': obs['achieved_goal']
        }
        return old_obs

    def _set_sim_state(self, obs):
        # set cube position & orientation
        self.platform.cube.set_state(
            obs['object_position'],
            obs['object_orientation']
        )
        # set robot position & velocity
        self.platform.simfinger.reset_finger_positions_and_velocities(
            obs['robot_position'],
            obs['robot_velocity']
        )

    def _gym_action_to_robot_action(self, gym_action):
        # construct robot action depending on action type
        if self.action_type == ActionType.TORQUE:
            robot_action = robot_interfaces.trifinger.Action(torque=gym_action)
        elif self.action_type == ActionType.POSITION:
            robot_action = robot_interfaces.trifinger.Action(
                position=gym_action
            )
        elif self.action_type == ActionType.TORQUE_AND_POSITION:
            robot_action = robot_interfaces.trifinger.Action(
                torque=gym_action["torque"], position=gym_action["position"]
            )
        else:
            raise ValueError("Invalid action_type")

        return robot_action
